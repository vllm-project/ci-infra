/* kernrec: record the set of GPU kernels a process launches.
 *
 * A CUPTI injection library. When CUDA_INJECTION64_PATH names this file, the
 * CUDA driver dlopens it during cuInit and calls InitializeInjection(), so no
 * command needs wrapping and every child process that touches CUDA records
 * itself. Nothing loads when the variable is unset, so PR jobs pay nothing.
 *
 * Output: $KERNREC_DIR/kern.<pid>.txt (default .fnrec/$BUILDKITE_JOB_ID, next
 * to the Python recorder's fn.*.txt). One mangled kernel name per line,
 * appended the first time the name is seen, so a SIGKILL loses nothing that
 * was written before it. Lines starting with '#' are metadata.
 *
 * Written, not just recorded: CUPTI hands records over only when a buffer
 * fills or someone flushes, and one 8 MiB buffer holds more kernel records
 * than most processes ever launch. Flushing only at exit meant every process
 * that never reaches atexit (engine cores and Ray workers SIGKILLed at
 * teardown, os._exit after fork) left nothing at all, about fifteen CUDA
 * steps per nightly. So a thread flushes on a timer (KERNREC_FLUSH_MS,
 * default 1000; 0 turns it off): a plain flush every tick, which returns
 * buffers whose records are all complete, and a forced one every
 * KERNREC_FORCE_EVERY ticks (default 5), which also returns the buffer a
 * busy process always has a kernel in flight in. A kill loses at most the
 * last few seconds of new names. The file is opened at cuInit, so a process
 * that initialised CUDA and was killed before any flush still says so.
 *
 * Why the Activity API and not the Callback API: kernels replayed from a
 * CUDA graph never pass through cuLaunchKernel, but CUPTI still reports one
 * kernel activity record per graph node. Only the set of names is kept, not
 * the timeline, which is what keeps the overhead and the output small.
 *
 * Known limit: CUPTI allows one activity client per process. If torch.profiler
 * (Kineto) starts in the same process it re-registers the buffer callbacks and
 * this recorder goes quiet. Steps that run profiler tests should not record.
 */
#define _GNU_SOURCE
#include <cupti.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* Kernel activity records are versioned structs (CUpti_ActivityKernel9 in
 * CUDA 12.x). build.sh probes the header and passes the newest one it finds.
 * Every version so far shares the prefix up to `name`, which is all we read. */
#ifndef KERNREC_KERNEL_T
#define KERNREC_KERNEL_T CUpti_ActivityKernel9
#endif
typedef KERNREC_KERNEL_T KernelRec;

#define HOST_BUF_SIZE (8u * 1024u * 1024u)
#define DEVICE_BUF_SIZE (32u * 1024u * 1024u)

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static FILE *g_out = NULL;
static int g_out_failed = 0;
static pid_t g_out_pid = 0;
static char **g_keys = NULL; /* open-addressing set of strdup'd names */
static size_t g_cap = 0, g_count = 0;
static unsigned long g_records = 0, g_dropped = 0;
static int g_initialized = 0;

/* Flush thread; see the header comment. g_flush_pid guards the join: a forked
 * child inherits the atexit handler but not the thread. */
static pthread_mutex_t g_flush_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_flush_cond = PTHREAD_COND_INITIALIZER;
static pthread_t g_flush_thread;
static pid_t g_flush_pid = 0;
static int g_flush_stop = 0;
static long g_flush_ms = 1000;
static long g_force_every = 5;

/* ---- string set -------------------------------------------------------- */

static uint64_t fnv1a(const char *s) {
  uint64_t h = 1469598103934665603ULL;
  for (; *s; ++s) {
    h ^= (unsigned char)*s;
    h *= 1099511628211ULL;
  }
  return h;
}

static void set_grow(void) {
  size_t ncap = g_cap ? g_cap * 2 : 4096;
  char **nk = calloc(ncap, sizeof(char *));
  if (!nk) return;
  for (size_t i = 0; i < g_cap; ++i) {
    if (!g_keys[i]) continue;
    size_t j = fnv1a(g_keys[i]) & (ncap - 1);
    while (nk[j]) j = (j + 1) & (ncap - 1);
    nk[j] = g_keys[i];
  }
  free(g_keys);
  g_keys = nk;
  g_cap = ncap;
}

/* Returns 1 when the name was not seen before. Caller holds g_lock. */
static int set_insert(const char *name) {
  if (g_count * 2 >= g_cap) set_grow();
  if (!g_keys) return 0;
  size_t i = fnv1a(name) & (g_cap - 1);
  while (g_keys[i]) {
    if (strcmp(g_keys[i], name) == 0) return 0;
    i = (i + 1) & (g_cap - 1);
  }
  g_keys[i] = strdup(name);
  if (!g_keys[i]) return 0;
  g_count++;
  return 1;
}

/* ---- output ------------------------------------------------------------ */

/* Directories are made world-writable on purpose. Under CI's docker plugin
 * this process is root and the output lands in a bind-mounted checkout owned
 * by the agent user; a root:root 0755 directory there is one the agent can
 * never delete, and every later job on that machine fails at checkout. The
 * umask would turn 0777 into 0755, so chmod after mkdir. */
static int mkdir_open(const char *dir) {
  if (mkdir(dir, 0777) == 0) return chmod(dir, 0777);
  return errno == EEXIST ? 0 : -1;
}

static int mkdir_p(const char *path) {
  char tmp[4096];
  size_t n = strlen(path);
  if (n == 0 || n >= sizeof tmp) return -1;
  memcpy(tmp, path, n + 1);
  for (char *p = tmp + 1; *p; ++p) {
    if (*p != '/') continue;
    *p = 0;
    if (mkdir_open(tmp)) return -1;
    *p = '/';
  }
  return mkdir_open(tmp);
}

/* Opened at cuInit and keyed by pid, so a process that forks after CUDA init
 * (unsupported by CUDA, but it happens) opens its own file on its first
 * record instead of writing into its parent's. Caller holds g_lock. */
static FILE *output(void) {
  pid_t pid = getpid();
  if (g_out && g_out_pid == pid) return g_out;
  if (g_out) { /* forked child: drop the inherited handle, start fresh */
    g_out = NULL;
    g_out_failed = 0;
    g_cap = g_count = 0;
    g_keys = NULL; /* leak the parent's set rather than double free */
  }
  if (g_out_failed) return NULL;

  char dirbuf[4096];
  const char *dir = getenv("KERNREC_DIR");
  if (!dir || !*dir) {
    const char *job = getenv("BUILDKITE_JOB_ID");
    snprintf(dirbuf, sizeof dirbuf, ".fnrec/%s", job && *job ? job : "local");
    dir = dirbuf;
  }
  char path[4096];
  if (mkdir_p(dir) ||
      snprintf(path, sizeof path, "%s/kern.%ld.txt", dir, (long)pid) < 0 ||
      !(g_out = fopen(path, "a"))) {
    fprintf(stderr, "kernrec: cannot write under %s: %s\n", dir,
            strerror(errno));
    g_out_failed = 1;
    return NULL;
  }
  g_out_pid = pid;
  setvbuf(g_out, NULL, _IOLBF, 0);
  char exe[1024] = {0};
  ssize_t r = readlink("/proc/self/exe", exe, sizeof exe - 1);
  fprintf(g_out, "# kernrec v1 pid=%ld ppid=%ld exe=%s\n", (long)pid,
          (long)getppid(), r > 0 ? exe : "?");
  return g_out;
}

/* ---- CUPTI callbacks --------------------------------------------------- */

static void CUPTIAPI buffer_requested(uint8_t **buffer, size_t *size,
                                      size_t *max_records) {
  /* glibc malloc is 16-byte aligned; CUPTI wants 8. */
  *buffer = malloc(HOST_BUF_SIZE);
  *size = *buffer ? HOST_BUF_SIZE : 0;
  *max_records = 0; /* as many as fit */
}

static void CUPTIAPI buffer_completed(CUcontext ctx, uint32_t stream_id,
                                      uint8_t *buffer, size_t size,
                                      size_t valid) {
  (void)size;
  CUpti_Activity *rec = NULL;
  pthread_mutex_lock(&g_lock);
  for (;;) {
    CUptiResult st = cuptiActivityGetNextRecord(buffer, valid, &rec);
    if (st != CUPTI_SUCCESS) break; /* MAX_LIMIT_REACHED ends the buffer */
    if (rec->kind != CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL &&
        rec->kind != CUPTI_ACTIVITY_KIND_KERNEL)
      continue;
    const KernelRec *k = (const KernelRec *)rec;
    g_records++;
    if (!k->name) continue;
    if (set_insert(k->name)) {
      FILE *f = output();
      if (f) {
        fputs(k->name, f);
        fputc('\n', f);
      }
    }
  }
  /* A dropped record is a launch we never saw. Leave a mark so build-table
   * can treat the row as suspect instead of trusting a hole. */
  size_t dropped = 0;
  if (cuptiActivityGetNumDroppedRecords(ctx, stream_id, &dropped) ==
          CUPTI_SUCCESS &&
      dropped) {
    g_dropped += dropped;
    FILE *f = output();
    if (f) fprintf(f, "# dropped=%zu\n", dropped);
    fprintf(stderr, "kernrec: %zu activity records dropped\n", dropped);
  }
  pthread_mutex_unlock(&g_lock);
  free(buffer);
}

static long env_long(const char *name, long dflt, long lo, long hi) {
  const char *v = getenv(name);
  if (!v || !*v) return dflt;
  char *end = NULL;
  long n = strtol(v, &end, 10);
  if (*end || n < lo || n > hi) return dflt;
  return n;
}

static void *flush_loop(void *arg) {
  (void)arg;
  long tick = 0;
  pthread_mutex_lock(&g_flush_lock);
  while (!g_flush_stop) {
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_sec += g_flush_ms / 1000;
    until.tv_nsec += (g_flush_ms % 1000) * 1000000L;
    if (until.tv_nsec >= 1000000000L) {
      until.tv_sec += 1;
      until.tv_nsec -= 1000000000L;
    }
    int rc = 0;
    while (!g_flush_stop && rc != ETIMEDOUT)
      rc = pthread_cond_timedwait(&g_flush_cond, &g_flush_lock, &until);
    if (g_flush_stop) break;
    pthread_mutex_unlock(&g_flush_lock);
    /* Outside g_flush_lock: the flush calls buffer_completed, which takes
     * g_lock, and at_exit takes g_flush_lock to stop us. */
    tick++;
    int forced = g_force_every > 0 && tick % g_force_every == 0;
    cuptiActivityFlushAll(forced ? CUPTI_ACTIVITY_FLAG_FLUSH_FORCED : 0);
    pthread_mutex_lock(&g_flush_lock);
  }
  pthread_mutex_unlock(&g_flush_lock);
  return NULL;
}

static void start_flush_thread(void) {
  g_flush_ms = env_long("KERNREC_FLUSH_MS", 1000, 0, 3600000);
  g_force_every = env_long("KERNREC_FORCE_EVERY", 5, 0, 1000000);
  if (g_flush_ms == 0) return;
  /* Keep this thread out of the application's signal handling. */
  sigset_t all, old;
  sigfillset(&all);
  pthread_sigmask(SIG_SETMASK, &all, &old);
  if (pthread_create(&g_flush_thread, NULL, flush_loop, NULL) == 0)
    g_flush_pid = getpid();
  else
    fprintf(stderr, "kernrec: no flush thread; recording until exit only\n");
  pthread_sigmask(SIG_SETMASK, &old, NULL);
}

static void stop_flush_thread(void) {
  if (g_flush_pid != getpid()) return;
  pthread_mutex_lock(&g_flush_lock);
  g_flush_stop = 1;
  pthread_cond_signal(&g_flush_cond);
  pthread_mutex_unlock(&g_flush_lock);
  pthread_join(g_flush_thread, NULL);
  g_flush_pid = 0;
}

static void at_exit(void) {
  stop_flush_thread();
  /* Drain records still sitting in device buffers. */
  cuptiActivityFlushAll(CUPTI_ACTIVITY_FLAG_FLUSH_FORCED);
  pthread_mutex_lock(&g_lock);
  if (g_out && g_out_pid == getpid()) {
    fprintf(g_out, "# end records=%lu unique=%zu dropped=%lu\n", g_records,
            g_count, g_dropped);
    fclose(g_out);
    g_out = NULL;
  }
  pthread_mutex_unlock(&g_lock);
}

static const char *cupti_err(CUptiResult st) {
  const char *s = "?";
  cuptiGetResultString(st, &s);
  return s;
}

/* The driver calls this once per process when CUDA_INJECTION64_PATH points
 * here. Return 1 on success (the value the CUPTI injection samples use). */
int InitializeInjection(void) {
  if (g_initialized) return 1;
  g_initialized = 1;

  CUptiResult st = cuptiActivityRegisterCallbacks(buffer_requested,
                                                  buffer_completed);
  if (st != CUPTI_SUCCESS) {
    fprintf(stderr, "kernrec: cuptiActivityRegisterCallbacks: %s\n",
            cupti_err(st));
    return 1; /* never break the workload */
  }
  /* Bigger device buffers: fewer flushes, fewer dropped records. */
  size_t attr_size = sizeof(size_t), dev_buf = DEVICE_BUF_SIZE;
  cuptiActivitySetAttribute(CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_SIZE, &attr_size,
                            &dev_buf);
  st = cuptiActivityEnable(CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL);
  if (st != CUPTI_SUCCESS) {
    fprintf(stderr, "kernrec: cuptiActivityEnable(CONCURRENT_KERNEL): %s\n",
            cupti_err(st));
    return 1;
  }
  /* Open the file now, not at the first record, so a process killed before
   * its first flush still leaves a header saying it initialised CUDA. */
  pthread_mutex_lock(&g_lock);
  output();
  pthread_mutex_unlock(&g_lock);
  atexit(at_exit);
  start_flush_thread();
  return 1;
}
