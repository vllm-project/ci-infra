/* kernrec: record the set of GPU kernels a process launches.
 *
 * A CUPTI injection library: when CUDA_INJECTION64_PATH names it, the driver
 * loads it during cuInit and calls InitializeInjection(), so every process
 * that touches CUDA records itself.
 *
 * Output: $KERNREC_DIR/kern.<pid>.txt (default .kernrec/$BUILDKITE_JOB_ID).
 * One mangled kernel name per line, written the first time it is seen, each
 * line a single write(2). Lines starting with '#' are metadata, and
 * "# end ..." is the footer written at exit.
 *
 * Launch-time names (header sync=1): driver-API callbacks name each kernel
 * when it is launched or added to a CUDA graph, before it can run, so the
 * file is complete even if the process is killed or skips atexit. They are
 * enabled all or nothing; without them the header omits sync=1.
 *
 * Cross-check: the Activity API reports every kernel that ran. A name no
 * callback wrote is logged as "# error=callback-missed <name>", and a handle
 * the lookups cannot name as "# error=callback-unresolved".
 *
 * Flushing: CUPTI hands activity records over only when a buffer fills or is
 * flushed. A thread flushes every KERNREC_FLUSH_MS (default 1000, 0 turns it
 * off), and every KERNREC_FORCE_EVERY ticks (default 5) forces the flush so
 * the buffer with a kernel in flight is returned too.
 *
 * Idle processes: header activity=1 promises "# gpu_active", written when the
 * first CUDA context is created. A file without it launched nothing.
 *
 * CUPTI allows one callback subscriber per process. The marker and the launch
 * callbacks share it for the process's life; without launch callbacks the
 * recorder unsubscribes once the marker is written.
 *
 * CUPTI allows one activity client per process. kernrec_torch.py calls
 * kernrec_release() before a CUDA profiler prepares; the release drains this
 * recorder's buffers, frees CUPTI, and writes "# error=released-to-profiler".
 * KERNREC_RELEASED_PID keeps the recorder from arming in a process whose
 * profiler started before CUDA was initialised.
 */
#define _GNU_SOURCE
#include <cupti.h>
#include <dlfcn.h>
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
static char g_out_path[4096];
static int g_out_failed = 0;
static pid_t g_out_pid = 0;
static char **g_keys = NULL; /* open-addressing set of strdup'd names */
static size_t g_cap = 0, g_count = 0;
static unsigned long g_records = 0, g_dropped = 0;
static unsigned long g_errors = 0, g_unresolved = 0;

static FILE *output(void);
static void write_line(const char *prefix, const char *text);

/* Caller holds g_lock. Errors survive even when the workload passes. */
static void record_error(const char *operation) {
  g_errors++;
  if (output()) write_line("# error=", operation);
}

static void check_status(CUptiResult status, const char *operation) {
  if (status == CUPTI_SUCCESS) return;
  pthread_mutex_lock(&g_lock);
  record_error(operation);
  pthread_mutex_unlock(&g_lock);
}
static int g_initialized = 0;
static int g_armed = 0;
static int g_released = 0;
/* The context-creation marker; see the header comment. g_subscriber_pid is
 * the process whose header promised it (activity=1). */
static CUpti_SubscriberHandle g_subscriber;
static pid_t g_subscriber_pid = 0;
static int g_active = 0;
/* The process whose header says sync=1. */
static pid_t g_sync_pid = 0;

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
  if (!nk) { record_error("set-allocation"); return; }
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
  if (!g_keys || g_count + 1 >= g_cap) {
    record_error("set-capacity");
    return 0;
  }
  size_t i = fnv1a(name) & (g_cap - 1);
  while (g_keys[i]) {
    if (strcmp(g_keys[i], name) == 0) return 0;
    i = (i + 1) & (g_cap - 1);
  }
  g_keys[i] = strdup(name);
  if (!g_keys[i]) { record_error("name-allocation"); return 0; }
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

/* A line that cannot be written may be a name, so empty the file: with no
 * header it never reads as complete. Caller holds g_lock. */
static void fail_closed(void) {
  g_errors++;
  fprintf(stderr, "kernrec: cannot write %s; recording dropped\n", g_out_path);
  if (truncate(g_out_path, 0) != 0)
    fprintf(stderr, "kernrec: cannot empty %s: %s\n", g_out_path, strerror(errno));
  fclose(g_out);
  g_out = NULL;
  g_out_failed = 1;
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
    snprintf(dirbuf, sizeof dirbuf, ".kernrec/%s", job && *job ? job : "local");
    dir = dirbuf;
  }
  int length = snprintf(g_out_path, sizeof g_out_path, "%s/kern.%ld.txt", dir, (long)pid);
  if (mkdir_p(dir) || length < 0 || (size_t)length >= sizeof g_out_path ||
      !(g_out = fopen(g_out_path, "a"))) {
    fprintf(stderr, "kernrec: cannot write under %s: %s\n", dir,
            strerror(errno));
    g_out_failed = 1;
    return NULL;
  }
  g_out_pid = pid;
  setvbuf(g_out, NULL, _IOLBF, 0);
  char exe[1024] = {0};
  ssize_t r = readlink("/proc/self/exe", exe, sizeof exe - 1);
  fprintf(g_out, "# kernrec v1 backend=cuda recorder=cupti%s%s pid=%ld ppid=%ld exe=%s\n",
          g_subscriber_pid == pid ? " activity=1" : "",
          g_sync_pid == pid ? " sync=1" : "", (long)pid,
          (long)getppid(), r > 0 ? exe : "?");
  if (ferror(g_out)) {
    fail_closed();
    return NULL;
  }
  return g_out;
}

/* Appends prefix + text + newline to g_out as one write(2), so a kill leaves
 * whole lines only; any failure fails closed. Caller holds g_lock and has
 * called output(). */
static void write_line(const char *prefix, const char *text) {
  if (!g_out) return; /* failed closed earlier in this call */
  size_t prefix_len = strlen(prefix), text_len = strlen(text);
  size_t n = prefix_len + text_len + 1;
  char stackbuf[4096];
  char *buf = n <= sizeof stackbuf ? stackbuf : malloc(n);
  int ok = 0;
  if (buf) {
    memcpy(buf, prefix, prefix_len);
    memcpy(buf + prefix_len, text, text_len);
    buf[n - 1] = '\n';
    int fd = fileno(g_out);
    if (fflush(g_out) == 0)
      ok = fd >= 0 ? write(fd, buf, n) == (ssize_t)n
                   : fwrite(buf, 1, n, g_out) == n && fflush(g_out) == 0;
    if (buf != stackbuf) free(buf);
  }
  if (!ok) fail_closed();
}

/* ---- launch-time names --------------------------------------------------- */

typedef CUresult (*name_lookup_t)(const char **, void *);
static name_lookup_t g_func_name = NULL, g_kernel_name = NULL;
static pid_t g_unresolved_pid = 0;

/* Driver entry points that launch a kernel or add one to a graph. Graph
 * replays run nodes these already named. */
static const CUpti_CallbackId LAUNCH_CALLBACKS[] = {
    CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchCooperativeKernel,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchCooperativeKernel_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchCooperativeKernelMultiDevice,
    CUPTI_DRIVER_TRACE_CBID_cuLaunch,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchGrid,
    CUPTI_DRIVER_TRACE_CBID_cuLaunchGridAsync,
    CUPTI_DRIVER_TRACE_CBID_cuGraphAddKernelNode,
    CUPTI_DRIVER_TRACE_CBID_cuGraphAddKernelNode_v2,
    CUPTI_DRIVER_TRACE_CBID_cuGraphKernelNodeSetParams,
    CUPTI_DRIVER_TRACE_CBID_cuGraphKernelNodeSetParams_v2,
    CUPTI_DRIVER_TRACE_CBID_cuGraphExecKernelNodeSetParams,
    CUPTI_DRIVER_TRACE_CBID_cuGraphExecKernelNodeSetParams_v2,
    CUPTI_DRIVER_TRACE_CBID_cuGraphAddNode,
    CUPTI_DRIVER_TRACE_CBID_cuGraphAddNode_v2,
    CUPTI_DRIVER_TRACE_CBID_cuGraphNodeSetParams,
    CUPTI_DRIVER_TRACE_CBID_cuGraphExecNodeSetParams,
};
#define N_LAUNCH_CALLBACKS (sizeof LAUNCH_CALLBACKS / sizeof LAUNCH_CALLBACKS[0])

/* Handles already named, read without the lock. A full probe run only costs
 * a repeat lookup. */
#define HANDLE_BITS 16
#define HANDLE_SLOTS (1u << HANDLE_BITS)
#define HANDLE_PROBES 64
static uintptr_t g_handles[HANDLE_SLOTS];

static size_t handle_slot(uintptr_t handle) {
  return (size_t)(((uint64_t)handle * 0x9E3779B97F4A7C15ULL) >> (64 - HANDLE_BITS));
}

static int handle_seen(uintptr_t handle) {
  size_t i = handle_slot(handle);
  for (int k = 0; k < HANDLE_PROBES; ++k, i = (i + 1) & (HANDLE_SLOTS - 1)) {
    uintptr_t v = __atomic_load_n(&g_handles[i], __ATOMIC_ACQUIRE);
    if (v == handle) return 1;
    if (v == 0) return 0;
  }
  return 0;
}

static void handle_mark(uintptr_t handle) { /* caller holds g_lock */
  size_t i = handle_slot(handle);
  for (int k = 0; k < HANDLE_PROBES; ++k, i = (i + 1) & (HANDLE_SLOTS - 1)) {
    uintptr_t v = g_handles[i];
    if (v == handle) return;
    if (v == 0) {
      __atomic_store_n(&g_handles[i], handle, __ATOMIC_RELEASE);
      return;
    }
  }
}

/* An unloaded module's handles can be reused for other kernels. */
static void forget_handles(void) {
  for (size_t i = 0; i < HANDLE_SLOTS; ++i)
    __atomic_store_n(&g_handles[i], 0, __ATOMIC_RELEASE);
}

/* torch passes CUkernel handles as CUfunction; each lookup rejects the other
 * kind. */
static const char *handle_name(void *handle, int is_kernel) {
  name_lookup_t first = is_kernel ? g_kernel_name : g_func_name;
  name_lookup_t second = is_kernel ? g_func_name : g_kernel_name;
  const char *name = NULL;
  if (first(&name, handle) == CUDA_SUCCESS && name && *name) return name;
  name = NULL;
  if (second(&name, handle) == CUDA_SUCCESS && name && *name) return name;
  return NULL;
}

/* The lookups run before g_lock is taken: context creation and activity
 * flushes take g_lock from inside the driver. */
static void record_handle(void *handle, int is_kernel) {
  if (!handle) return; /* the launch fails */
  uintptr_t key = (uintptr_t)handle;
  if (handle_seen(key) || g_sync_pid != getpid()) return;
  const char *name = handle_name(handle, is_kernel);
  pthread_mutex_lock(&g_lock);
  if (!name) {
    if (g_unresolved_pid != g_sync_pid) {
      g_unresolved_pid = g_sync_pid;
      record_error("callback-unresolved");
    }
  } else if (set_insert(name) && output()) {
    write_line("", name);
  }
  handle_mark(key);
  pthread_mutex_unlock(&g_lock);
}

static void record_node(const CUDA_KERNEL_NODE_PARAMS *node) {
  if (!node) return;
  if (node->func) record_handle((void *)node->func, 0);
  else record_handle((void *)node->kern, 1);
}

/* v1 node params have no kern field. */
static void record_node_v1(const CUDA_KERNEL_NODE_PARAMS_v1 *node) {
  if (node) record_handle((void *)node->func, 0);
}

static void record_graph_node(const CUgraphNodeParams *node) {
  if (!node || node->type != CU_GRAPH_NODE_TYPE_KERNEL) return;
  if (node->kernel.func) record_handle((void *)node->kernel.func, 0);
  else record_handle((void *)node->kernel.kern, 1);
}

static void record_launch(CUpti_CallbackId cbid, const void *params) {
  switch (cbid) {
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel:
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel_ptsz:
    record_handle((void *)((const cuLaunchKernel_params *)params)->f, 0);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx:
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx_ptsz:
    record_handle((void *)((const cuLaunchKernelEx_params *)params)->f, 0);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchCooperativeKernel:
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchCooperativeKernel_ptsz:
    record_handle((void *)((const cuLaunchCooperativeKernel_params *)params)->f, 0);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchCooperativeKernelMultiDevice: {
    const cuLaunchCooperativeKernelMultiDevice_params *p = params;
    for (unsigned i = 0; p->launchParamsList && i < p->numDevices; ++i)
      record_handle((void *)p->launchParamsList[i].function, 0);
    break;
  }
  case CUPTI_DRIVER_TRACE_CBID_cuLaunch:
    record_handle((void *)((const cuLaunch_params *)params)->f, 0);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchGrid:
    record_handle((void *)((const cuLaunchGrid_params *)params)->f, 0);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuLaunchGridAsync:
    record_handle((void *)((const cuLaunchGridAsync_params *)params)->f, 0);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphAddKernelNode:
    record_node_v1(((const cuGraphAddKernelNode_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphAddKernelNode_v2:
    record_node(((const cuGraphAddKernelNode_v2_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphKernelNodeSetParams:
    record_node_v1(((const cuGraphKernelNodeSetParams_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphKernelNodeSetParams_v2:
    record_node(((const cuGraphKernelNodeSetParams_v2_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphExecKernelNodeSetParams:
    record_node_v1(((const cuGraphExecKernelNodeSetParams_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphExecKernelNodeSetParams_v2:
    record_node(((const cuGraphExecKernelNodeSetParams_v2_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphAddNode:
    record_graph_node(((const cuGraphAddNode_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphAddNode_v2:
    record_graph_node(((const cuGraphAddNode_v2_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphNodeSetParams:
    record_graph_node(((const cuGraphNodeSetParams_params *)params)->nodeParams);
    break;
  case CUPTI_DRIVER_TRACE_CBID_cuGraphExecNodeSetParams:
    record_graph_node(((const cuGraphExecNodeSetParams_params *)params)->nodeParams);
    break;
  default:
    break;
  }
}

/* A forked child may inherit a held lock. */
static void reset_after_fork(void) { pthread_mutex_init(&g_lock, NULL); }

/* Unloads and context teardown free handles the driver can reuse. */
static const CUpti_CallbackId FORGET_CALLBACKS[] = {
    CUPTI_CBID_RESOURCE_MODULE_UNLOAD_STARTING,
    CUPTI_CBID_RESOURCE_CONTEXT_DESTROY_STARTING,
};
#define N_FORGET_CALLBACKS (sizeof FORGET_CALLBACKS / sizeof FORGET_CALLBACKS[0])

static void disable(CUpti_CallbackDomain domain, const CUpti_CallbackId *ids, size_t n) {
  while (n--) cuptiEnableCallback(0, g_subscriber, domain, ids[n]);
}

static int enable_all(CUpti_CallbackDomain domain, const CUpti_CallbackId *ids, size_t n) {
  size_t enabled = 0;
  while (enabled < n &&
         cuptiEnableCallback(1, g_subscriber, domain, ids[enabled]) == CUPTI_SUCCESS)
    enabled++;
  if (enabled == n) return 1;
  disable(domain, ids, enabled);
  return 0;
}

/* All or nothing: a missing launch path would lose names under sync=1.
 * Caller is subscribed. */
static void enable_launch_callbacks(void) {
  void *cuda = dlopen("libcuda.so.1", RTLD_NOW | RTLD_NOLOAD);
  void *scope = cuda ? cuda : RTLD_DEFAULT;
  g_func_name = (name_lookup_t)dlsym(scope, "cuFuncGetName");
  g_kernel_name = (name_lookup_t)dlsym(scope, "cuKernelGetName");
  if (!g_func_name || !g_kernel_name) return; /* driver before CUDA 12.3 */
  if (!enable_all(CUPTI_CB_DOMAIN_DRIVER_API, LAUNCH_CALLBACKS, N_LAUNCH_CALLBACKS))
    return;
  if (!enable_all(CUPTI_CB_DOMAIN_RESOURCE, FORGET_CALLBACKS, N_FORGET_CALLBACKS)) {
    disable(CUPTI_CB_DOMAIN_DRIVER_API, LAUNCH_CALLBACKS, N_LAUNCH_CALLBACKS);
    return;
  }
  pthread_atfork(NULL, NULL, reset_after_fork);
  g_sync_pid = getpid();
}

/* ---- CUPTI callbacks --------------------------------------------------- */

/* First CUDA context in this process: from here on it can launch kernels, so
 * a later missing footer may hide some. Written before any launch, unbuffered,
 * so a SIGKILL right after still leaves it. */
static void mark_active(void) {
  pthread_mutex_lock(&g_lock);
  if (!g_active && g_subscriber_pid == getpid()) {
    g_active = 1;
    if (output()) write_line("# gpu_active", "");
  }
  pthread_mutex_unlock(&g_lock);
}

static void CUPTIAPI on_callback(void *userdata, CUpti_CallbackDomain domain,
                                 CUpti_CallbackId cbid, const void *data) {
  (void)userdata;
  if (domain == CUPTI_CB_DOMAIN_RESOURCE) {
    if (cbid == CUPTI_CBID_RESOURCE_CONTEXT_CREATED) mark_active();
    else if (cbid == CUPTI_CBID_RESOURCE_MODULE_UNLOAD_STARTING ||
             cbid == CUPTI_CBID_RESOURCE_CONTEXT_DESTROY_STARTING)
      forget_handles();
    return;
  }
  const CUpti_CallbackData *call = data;
  if (domain == CUPTI_CB_DOMAIN_DRIVER_API && call->callbackSite == CUPTI_API_ENTER)
    record_launch(cbid, call->functionParams);
}

/* Arms the marker, then the launch callbacks on the same subscriber. */
static void subscribe_activity_marker(void) {
  if (cuptiSubscribe(&g_subscriber, (CUpti_CallbackFunc)on_callback, NULL) !=
      CUPTI_SUCCESS)
    return;
  if (cuptiEnableCallback(1, g_subscriber, CUPTI_CB_DOMAIN_RESOURCE,
                          CUPTI_CBID_RESOURCE_CONTEXT_CREATED) != CUPTI_SUCCESS) {
    cuptiUnsubscribe(g_subscriber);
    return;
  }
  g_subscriber_pid = getpid();
  enable_launch_callbacks();
}

/* Frees the callback slot, launch callbacks included. Never call it from
 * inside a callback. */
static void unsubscribe_activity_marker(void) {
  if (g_subscriber_pid != getpid()) return;
  g_subscriber_pid = 0;
  g_sync_pid = 0;
  check_status(cuptiUnsubscribe(g_subscriber), "unsubscribe-marker");
}

static void release_marker_if_done(void) {
  if (g_active && g_sync_pid != getpid()) unsubscribe_activity_marker();
}

static void CUPTIAPI buffer_requested(uint8_t **buffer, size_t *size,
                                      size_t *max_records) {
  /* glibc malloc is 16-byte aligned; CUPTI wants 8. */
  *buffer = malloc(HOST_BUF_SIZE);
  *size = *buffer ? HOST_BUF_SIZE : 0;
  *max_records = 0; /* as many as fit */
  if (!*buffer) {
    pthread_mutex_lock(&g_lock);
    record_error("buffer-allocation");
    pthread_mutex_unlock(&g_lock);
  }
}

static void CUPTIAPI buffer_completed(CUcontext ctx, uint32_t stream_id,
                                      uint8_t *buffer, size_t size,
                                      size_t valid) {
  (void)size;
  CUpti_Activity *rec = NULL;
  pthread_mutex_lock(&g_lock);
  for (;;) {
    CUptiResult st = cuptiActivityGetNextRecord(buffer, valid, &rec);
    if (st != CUPTI_SUCCESS) {
      if (st != CUPTI_ERROR_MAX_LIMIT_REACHED) record_error("read-buffer");
      break;
    }
    if (rec->kind != CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL &&
        rec->kind != CUPTI_ACTIVITY_KIND_KERNEL)
      continue;
    const KernelRec *k = (const KernelRec *)rec;
    g_records++;
    if (!k->name || !*k->name) { g_unresolved++; continue; }
    if (set_insert(k->name)) {
      if (!output()) continue;
      /* Under sync=1 the callbacks already put every name in the set. The
       * error goes first, so a kill between the lines still flags it. */
      if (g_sync_pid == getpid()) {
        g_errors++;
        write_line("# error=callback-missed ", k->name);
      }
      write_line("", k->name);
    }
  }
  /* A dropped record is a launch we never saw. Leave a mark so build-table
   * can treat the row as suspect instead of trusting a hole. */
  size_t dropped = 0;
  CUptiResult status = cuptiActivityGetNumDroppedRecords(ctx, stream_id, &dropped);
  if (status != CUPTI_SUCCESS) record_error("read-drop-count");
  if (dropped) {
    g_dropped += dropped;
    char count[32];
    snprintf(count, sizeof count, "%zu", dropped);
    if (output()) write_line("# dropped=", count);
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
    check_status(cuptiActivityFlushAll(forced ? CUPTI_ACTIVITY_FLAG_FLUSH_FORCED : 0),
                 "periodic-flush");
    release_marker_if_done();
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

/* Hands CUPTI to another activity client; see the header comment. Returns 1
 * when this call released it. A forked child shares the parent's flag but
 * never had CUPTI armed, and its stop_flush_thread() is a no-op. */
int kernrec_release(void) {
  if (!g_armed || g_released) return 0;
  g_released = 1;
  /* Before the unsubscribe, after which launches go unnamed. */
  pthread_mutex_lock(&g_lock);
  record_error("released-to-profiler");
  pthread_mutex_unlock(&g_lock);
  stop_flush_thread();
  unsubscribe_activity_marker();
  check_status(cuptiActivityDisable(CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL),
               "release-disable");
  check_status(cuptiActivityFlushAll(CUPTI_ACTIVITY_FLAG_FLUSH_FORCED),
               "release-flush");
  return 1;
}

static void at_exit(void) {
  stop_flush_thread();
  /* Drain records still sitting in device buffers. After a release the
   * buffers are the profiler's, so leave them alone. */
  if (!g_released)
    check_status(cuptiActivityFlushAll(CUPTI_ACTIVITY_FLAG_FLUSH_FORCED),
                 "final-flush");
  pthread_mutex_lock(&g_lock);
  if (g_out && g_out_pid == getpid()) {
    if (fflush(g_out) != 0 || ferror(g_out)) g_errors++;
    fprintf(g_out, "# end records=%lu unique=%zu dropped=%lu errors=%lu unresolved=%lu\n",
            g_records, g_count, g_dropped, g_errors, g_unresolved);
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
  const char *released = getenv("KERNREC_RELEASED_PID");
  int released_here = released && strtol(released, NULL, 10) == (long)getpid();
  /* Before the header, which says activity=1 only if this armed. */
  if (!released_here) subscribe_activity_marker();
  pthread_mutex_lock(&g_lock);
  output();
  pthread_mutex_unlock(&g_lock);
  atexit(at_exit);

  /* Still writes a file, so the row shows this process ran unrecorded. */
  if (released_here) {
    g_released = 1;
    pthread_mutex_lock(&g_lock);
    record_error("released-to-profiler");
    pthread_mutex_unlock(&g_lock);
    return 1;
  }

  CUptiResult st = cuptiActivityRegisterCallbacks(buffer_requested,
                                                  buffer_completed);
  if (st != CUPTI_SUCCESS) {
    fprintf(stderr, "kernrec: cuptiActivityRegisterCallbacks: %s\n",
            cupti_err(st));
    check_status(st, "register-callbacks");
    return 1; /* never break the workload */
  }
  /* Bigger device buffers: fewer flushes, fewer dropped records. */
  size_t attr_size = sizeof(size_t), dev_buf = DEVICE_BUF_SIZE;
  check_status(cuptiActivitySetAttribute(CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_SIZE,
                                         &attr_size, &dev_buf),
               "set-buffer-size");
  st = cuptiActivityEnable(CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL);
  if (st != CUPTI_SUCCESS) {
    fprintf(stderr, "kernrec: cuptiActivityEnable(CONCURRENT_KERNEL): %s\n",
            cupti_err(st));
    check_status(st, "enable-kernel-activity");
    return 1;
  }
  g_armed = 1;
  start_flush_thread();
  return 1;
}
