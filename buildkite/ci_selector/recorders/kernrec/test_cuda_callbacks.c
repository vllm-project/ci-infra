/* CPU-only CUPTI failure-contract checks using real headers and mocked APIs.
 * The driver's name lookups come through dlopen/dlsym, renamed to mocks here
 * so the recorder needs no test hooks. */
#define dlopen mock_dlopen
#define dlsym mock_dlsym
#include "kernrec.c"
#undef dlopen
#undef dlsym
#include <assert.h>
#include <fcntl.h>
#include <sys/wait.h>

static CUptiResult registration_status = CUPTI_SUCCESS;
static CUptiResult enable_status = CUPTI_SUCCESS;
static CUptiResult read_status = CUPTI_ERROR_MAX_LIMIT_REACHED;
static CUptiResult dropped_status = CUPTI_SUCCESS;
static CUptiResult flush_status = CUPTI_SUCCESS;
static KernelRec mock_records[3];
static size_t mock_count = 0, mock_index = 0, mock_dropped = 0;

CUptiResult CUPTIAPI cuptiActivityRegisterCallbacks(
    CUpti_BuffersCallbackRequestFunc requested,
    CUpti_BuffersCallbackCompleteFunc completed) {
  (void)requested;
  (void)completed;
  return registration_status;
}

CUptiResult CUPTIAPI cuptiActivitySetAttribute(CUpti_ActivityAttribute attribute,
                                              size_t *size, void *value) {
  (void)attribute;
  (void)size;
  (void)value;
  return CUPTI_SUCCESS;
}

CUptiResult CUPTIAPI cuptiActivityEnable(CUpti_ActivityKind kind) {
  (void)kind;
  return enable_status;
}

static int disabled = 0, flushes = 0;

CUptiResult CUPTIAPI cuptiActivityDisable(CUpti_ActivityKind kind) {
  (void)kind;
  disabled++;
  return CUPTI_SUCCESS;
}

CUptiResult CUPTIAPI cuptiActivityGetNextRecord(uint8_t *buffer, size_t valid,
                                               CUpti_Activity **record) {
  (void)buffer;
  (void)valid;
  if (mock_index == mock_count) return read_status;
  *record = (CUpti_Activity *)&mock_records[mock_index++];
  return CUPTI_SUCCESS;
}

CUptiResult CUPTIAPI cuptiActivityGetNumDroppedRecords(CUcontext context,
                                                      uint32_t stream,
                                                      size_t *dropped) {
  (void)context;
  (void)stream;
  *dropped = mock_dropped;
  mock_dropped = 0;
  return dropped_status;
}

CUptiResult CUPTIAPI cuptiActivityFlushAll(uint32_t flags) {
  (void)flags;
  flushes++;
  return flush_status;
}

static CUptiResult subscribe_status = CUPTI_SUCCESS;
static int subscriber_token = 0, unsubscribes = 0;
static CUpti_CallbackFunc marker_callback = NULL;

CUptiResult CUPTIAPI cuptiSubscribe(CUpti_SubscriberHandle *subscriber,
                                    CUpti_CallbackFunc callback, void *userdata) {
  (void)userdata;
  if (subscribe_status != CUPTI_SUCCESS) return subscribe_status;
  *subscriber = (CUpti_SubscriberHandle)&subscriber_token;
  marker_callback = callback;
  return CUPTI_SUCCESS;
}

/* Net count of enabled driver-API callbacks; fail_cbid makes one enable fail. */
static int driver_enabled = 0;
static CUpti_CallbackId fail_cbid = (CUpti_CallbackId)-1;

CUptiResult CUPTIAPI cuptiEnableCallback(uint32_t enable,
                                         CUpti_SubscriberHandle subscriber,
                                         CUpti_CallbackDomain domain,
                                         CUpti_CallbackId cbid) {
  (void)subscriber;
  if (domain == CUPTI_CB_DOMAIN_DRIVER_API) {
    if (enable && cbid == fail_cbid) return CUPTI_ERROR_UNKNOWN;
    driver_enabled += enable ? 1 : -1;
    return CUPTI_SUCCESS;
  }
  assert(domain == CUPTI_CB_DOMAIN_RESOURCE);
  assert(cbid == CUPTI_CBID_RESOURCE_CONTEXT_CREATED ||
         cbid == CUPTI_CBID_RESOURCE_MODULE_UNLOAD_STARTING ||
         cbid == CUPTI_CBID_RESOURCE_CONTEXT_DESTROY_STARTING);
  return CUPTI_SUCCESS;
}

/* Driver name lookups. 0x1000 and 0x4000 are CUfunctions, 0x2000 a CUkernel
 * that cuFuncGetName rejects, 0x3000 resolves under neither. `generation`
 * counts unloads: each one gives 0x1000 to a different kernel. Lookups must
 * run without g_lock, which context creation and flushes also take. */
static int lookups_missing = 0, generation = 0, lookups = 0;
static const char *const generations[] = {"_Z4funcv", "_Z7reloadv", "_Z8destroyv"};

static void lookup_unlocked(void) {
  lookups++;
  assert(pthread_mutex_trylock(&g_lock) == 0);
  pthread_mutex_unlock(&g_lock);
}

static CUresult mock_func_name(const char **name, CUfunction handle) {
  lookup_unlocked();
  if (handle == (CUfunction)0x1000) { *name = generations[generation]; return CUDA_SUCCESS; }
  if (handle == (CUfunction)0x4000) { *name = "_Z5thirdv"; return CUDA_SUCCESS; }
  return CUDA_ERROR_INVALID_HANDLE;
}

static CUresult mock_kernel_name(const char **name, CUkernel handle) {
  lookup_unlocked();
  if (handle == (CUkernel)0x2000) { *name = "_Z6kernelv"; return CUDA_SUCCESS; }
  return CUDA_ERROR_INVALID_HANDLE;
}

void *mock_dlopen(const char *file, int mode) {
  (void)file;
  (void)mode;
  return NULL; /* the recorder then looks in the global scope */
}

void *mock_dlsym(void *handle, const char *symbol) {
  (void)handle;
  if (lookups_missing) return NULL;
  if (!strcmp(symbol, "cuFuncGetName")) return (void *)mock_func_name;
  if (!strcmp(symbol, "cuKernelGetName")) return (void *)mock_kernel_name;
  return NULL;
}

CUptiResult CUPTIAPI cuptiUnsubscribe(CUpti_SubscriberHandle subscriber) {
  (void)subscriber;
  unsubscribes++;
  return CUPTI_SUCCESS;
}

static void read_file(const char *path, char *text, size_t size) {
  FILE *input = fopen(path, "r");
  assert(input != NULL);
  memset(text, 0, size);
  assert(fread(text, 1, size - 1, input) > 0);
  fclose(input);
}

static int count(const char *text, const char *needle) {
  int n = 0;
  for (const char *at = text; (at = strstr(at, needle)); at += strlen(needle)) n++;
  return n;
}

/* The context-creation marker: the header promises it only when armed, the
 * first context writes it once, and the slot is freed afterwards. */
static void check_marker(const char *mode, const char *path) {
  char text[8192];
  if (!strcmp(mode, "marker-subscribe-fails")) {
    read_file(path, text, sizeof text);
    assert(!strstr(text, "activity=1") && marker_callback == NULL);
    return;
  }
  assert(marker_callback != NULL);
  read_file(path, text, sizeof text);
  assert(strstr(text, "backend=cuda recorder=cupti activity=1 sync=1 pid="));
  if (!strcmp(mode, "marker-idle")) {
    assert(!strstr(text, "# gpu_active"));
  } else if (!strcmp(mode, "marker-active")) {
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE,
                    CUPTI_CBID_RESOURCE_CONTEXT_CREATED, NULL);
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE,
                    CUPTI_CBID_RESOURCE_CONTEXT_CREATED, NULL);
    read_file(path, text, sizeof text);
    assert(count(text, "# gpu_active\n") == 1);
    release_marker_if_done(); /* the flush tick's step */
    assert(unsubscribes == 0); /* launch callbacks keep the slot */
    unsubscribe_activity_marker();
    assert(unsubscribes == 1);
    unsubscribe_activity_marker();
    assert(unsubscribes == 1);
  } else if (!strcmp(mode, "marker-release")) {
    assert(kernrec_release() == 1 && unsubscribes == 1);
  } else {
    assert(0 && "unknown marker mode");
  }
}

/* A full disk: every write fails. A recording that cannot be written must
 * fail closed, leaving an empty file, never a partial one. */
static ssize_t full_write(void *cookie, const char *data, size_t size) {
  (void)cookie;
  (void)data;
  (void)size;
  errno = ENOSPC;
  return -1;
}

static void fill_disk(void) {
  fclose(g_out);
  g_out = fopencookie(NULL, "w", (cookie_io_functions_t){.write = full_write});
  assert(g_out != NULL);
}

static void assert_empty(const char *path) {
  struct stat st;
  assert(stat(path, &st) == 0 && st.st_size == 0);
}

/* ---- launch-time names (sync=1) ---------------------------------------- */

static void driver_call(CUpti_CallbackId cbid, const void *params, CUpti_ApiCallbackSite site) {
  CUpti_CallbackData data;
  memset(&data, 0, sizeof data);
  data.callbackSite = site;
  data.functionParams = params;
  marker_callback(NULL, CUPTI_CB_DOMAIN_DRIVER_API, cbid, &data);
}

static void launch(uintptr_t handle) {
  cuLaunchKernel_params params;
  memset(&params, 0, sizeof params);
  params.f = (CUfunction)handle;
  driver_call(CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel, &params, CUPTI_API_ENTER);
}

/* The Activity API delivering one kernel record, as a flush would. */
static void delivered(const char *name) {
  mock_records[0].kind = CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL;
  mock_records[0].name = name;
  mock_count = 1;
  mock_index = 0;
  buffer_completed(NULL, 0, malloc(64), 64, 64);
}

static void footer_and_read(const char *path, char *text, size_t size) {
  at_exit();
  read_file(path, text, size);
  assert(strstr(text, "# end "));
}

static void check_sync(const char *mode, const char *path) {
  char text[8192];
  read_file(path, text, sizeof text);
  if (!strcmp(mode, "sync-off") || !strcmp(mode, "sync-enable-fails")) {
    /* No complete launch coverage: today's format, today's marker release. */
    assert(strstr(text, "activity=1 pid=") && !strstr(text, "sync=1"));
    assert(driver_enabled == 0);
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE, CUPTI_CBID_RESOURCE_CONTEXT_CREATED, NULL);
    release_marker_if_done();
    assert(unsubscribes == 1);
    footer_and_read(path, text, sizeof text);
    return;
  }
  assert(strstr(text, "backend=cuda recorder=cupti activity=1 sync=1 pid="));
  assert(driver_enabled > 0);
  marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE, CUPTI_CBID_RESOURCE_CONTEXT_CREATED, NULL);
  if (!strcmp(mode, "sync-launch")) {
    launch(0x1000);
    read_file(path, text, sizeof text); /* on disk before the launch proceeds */
    assert(count(text, "\n_Z4funcv\n") == 1);
    launch(0x1000);
    driver_call(CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel, &(cuLaunchKernel_params){.f = (CUfunction)0x4000},
                CUPTI_API_EXIT); /* only the entry site records */
    cuLaunchKernelEx_params ex;
    memset(&ex, 0, sizeof ex);
    ex.f = (CUfunction)0x2000; /* a CUkernel passed as CUfunction */
    driver_call(CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx, &ex, CUPTI_API_ENTER);
    launch(0x2000);
    delivered("_Z4funcv");
    delivered("_Z6kernelv");
    footer_and_read(path, text, sizeof text);
    assert(count(text, "_Z4funcv\n") == 1 && count(text, "_Z6kernelv\n") == 1);
    assert(!strstr(text, "_Z5thirdv") && !strstr(text, "# error"));
    assert(strstr(text, "# end records=2 unique=2 dropped=0 errors=0 unresolved=0\n"));
  } else if (!strcmp(mode, "sync-graph")) {
    CUDA_KERNEL_NODE_PARAMS node;
    memset(&node, 0, sizeof node);
    node.kern = (CUkernel)0x2000;
    cuGraphAddKernelNode_v2_params add;
    memset(&add, 0, sizeof add);
    add.nodeParams = &node;
    driver_call(CUPTI_DRIVER_TRACE_CBID_cuGraphAddKernelNode_v2, &add, CUPTI_API_ENTER);
    CUgraphNodeParams generic;
    memset(&generic, 0, sizeof generic);
    generic.type = CU_GRAPH_NODE_TYPE_KERNEL;
    generic.kernel.func = (CUfunction)0x1000;
    cuGraphAddNode_params add_node;
    memset(&add_node, 0, sizeof add_node);
    add_node.nodeParams = &generic;
    driver_call(CUPTI_DRIVER_TRACE_CBID_cuGraphAddNode, &add_node, CUPTI_API_ENTER);
    CUDA_KERNEL_NODE_PARAMS updated;
    memset(&updated, 0, sizeof updated);
    updated.func = (CUfunction)0x4000;
    cuGraphExecKernelNodeSetParams_v2_params set;
    memset(&set, 0, sizeof set);
    set.nodeParams = &updated;
    driver_call(CUPTI_DRIVER_TRACE_CBID_cuGraphExecKernelNodeSetParams_v2, &set, CUPTI_API_ENTER);
    generic.type = CU_GRAPH_NODE_TYPE_MEMSET; /* not a kernel: ignored */
    driver_call(CUPTI_DRIVER_TRACE_CBID_cuGraphAddNode, &add_node, CUPTI_API_ENTER);
    read_file(path, text, sizeof text);
    assert(strstr(text, "\n_Z6kernelv\n") && strstr(text, "\n_Z4funcv\n") &&
           strstr(text, "\n_Z5thirdv\n"));
    delivered("_Z6kernelv"); /* replays */
    delivered("_Z4funcv");
    delivered("_Z5thirdv");
    footer_and_read(path, text, sizeof text);
    assert(strstr(text, "unique=3 dropped=0 errors=0 unresolved=0\n"));
  } else if (!strcmp(mode, "sync-unresolved")) {
    launch(0x3000);
    launch(0x3000);
    assert(lookups == 2); /* both lookups once, then cached */
    footer_and_read(path, text, sizeof text);
    assert(count(text, "# error=callback-unresolved\n") == 1);
  } else if (!strcmp(mode, "sync-miss")) {
    launch(0x1000);
    delivered("_Z4funcv");
    delivered("_Z6missedv"); /* a launch no callback saw */
    delivered("_Z6missedv");
    footer_and_read(path, text, sizeof text);
    assert(count(text, "# error=callback-missed _Z6missedv\n_Z6missedv\n") == 1);
    assert(strstr(text, "unique=2 dropped=0 errors=1 unresolved=0\n"));
  } else if (!strcmp(mode, "sync-release")) {
    launch(0x1000);
    assert(kernrec_release() == 1 && unsubscribes == 1);
    footer_and_read(path, text, sizeof text);
    assert(strstr(text, "# error=released-to-profiler"));
  } else if (!strcmp(mode, "sync-cache-clear")) {
    launch(0x1000);
    generation = 1; /* the module is replaced and the handle reused */
    launch(0x1000);
    read_file(path, text, sizeof text);
    assert(!strstr(text, "_Z7reloadv")); /* cached */
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE, CUPTI_CBID_RESOURCE_MODULE_UNLOAD_STARTING, NULL);
    launch(0x1000);
    generation = 2;
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE, CUPTI_CBID_RESOURCE_CONTEXT_DESTROY_STARTING, NULL);
    launch(0x1000);
    read_file(path, text, sizeof text);
    assert(count(text, "\n_Z4funcv\n") == 1 && count(text, "\n_Z7reloadv\n") == 1 &&
           count(text, "\n_Z8destroyv\n") == 1);
  } else if (!strcmp(mode, "sync-write-fails")) {
    launch(0x1000);
    fill_disk();
    launch(0x4000);
    launch(0x2000);
    at_exit();
    assert_empty(path);
  } else {
    assert(0 && "unknown sync mode");
  }
}

/* A process SIGKILLed right after a launch: the name is already on disk.
 * legacy-kill is today's recorder (no launch callbacks) losing the same way
 * it always did: a name from a flush, no footer. */
static void check_kill(const char *dir, const char *mode) {
  pid_t child = fork();
  assert(child >= 0);
  if (child == 0) {
    assert(InitializeInjection() == 1);
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE, CUPTI_CBID_RESOURCE_CONTEXT_CREATED, NULL);
    if (!strcmp(mode, "sync-kill")) launch(0x1000);
    else delivered("_Z4funcv");
    raise(SIGKILL);
    _exit(99);
  }
  int status = 0;
  assert(waitpid(child, &status, 0) == child);
  assert(WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL);
  char path[4096], text[8192];
  snprintf(path, sizeof path, "%s/kern.%ld.txt", dir, (long)child);
  read_file(path, text, sizeof text);
  assert(strstr(text, "\n_Z4funcv\n") && !strstr(text, "# end "));
  assert(!strstr(text, "sync=1") == !strcmp(mode, "legacy-kill"));
}

CUptiResult CUPTIAPI cuptiGetResultString(CUptiResult result, const char **text) {
  (void)result;
  *text = "injected test failure";
  return CUPTI_SUCCESS;
}

int main(int argc, char **argv) {
  assert(argc == 3);
  setenv("KERNREC_DIR", argv[1], 1);
  setenv("KERNREC_FLUSH_MS", "0", 1);
  const char *mode = argv[2];
  /* The Activity-path contract is checked as today's recorder runs it. */
  lookups_missing = strncmp(mode, "sync-", 5) && strncmp(mode, "marker-", 7);
  if (!strcmp(mode, "sync-off")) lookups_missing = 1;
  if (!strcmp(mode, "sync-enable-fails")) fail_cbid = CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx;
  if (!strcmp(mode, "sync-kill") || !strcmp(mode, "legacy-kill")) {
    check_kill(argv[1], mode);
    printf("CUDA callback check passed: %s\n", mode);
    return 0;
  }
  if (!strcmp(mode, "register")) registration_status = CUPTI_ERROR_UNKNOWN;
  if (!strcmp(mode, "enable")) enable_status = CUPTI_ERROR_UNKNOWN;
  if (!strcmp(mode, "marker-subscribe-fails")) subscribe_status = CUPTI_ERROR_UNKNOWN;
  if (!strcmp(mode, "released-pid")) {
    char pid[32];
    snprintf(pid, sizeof pid, "%ld", (long)getpid());
    setenv("KERNREC_RELEASED_PID", pid, 1);
  }
  assert(InitializeInjection() == 1);
  if (!strcmp(mode, "released-pid")) assert(!g_armed && kernrec_release() == 0);
  assert(g_out != NULL);
  char path[4096];
  assert(snprintf(path, sizeof(path), "%s/kern.%ld.txt", argv[1], (long)getpid()) > 0);
  if (!strncmp(mode, "marker-", 7) || !strncmp(mode, "sync-", 5)) {
    if (mode[0] == 'm') check_marker(mode, path);
    else check_sync(mode, path);
    printf("CUDA callback check passed: %s\n", mode);
    fflush(stdout);
    _exit(0);
  }
  if (!strcmp(mode, "write")) fill_disk();
  if (strcmp(mode, "register") && strcmp(mode, "enable")) {
    mock_records[0].kind = CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL;
    mock_records[0].name = "_Z4usedv";
    mock_records[1] = mock_records[0];
    mock_records[2] = mock_records[0];
    mock_records[2].name = NULL;
    mock_count = !strcmp(mode, "names") ? 3 : 1;
    if (!strcmp(mode, "read")) read_status = CUPTI_ERROR_UNKNOWN;
    if (!strcmp(mode, "dropped-query")) dropped_status = CUPTI_ERROR_UNKNOWN;
    if (!strcmp(mode, "drops")) mock_dropped = 3;
    buffer_completed(NULL, 0, malloc(64), 64, 64);
    if (!strcmp(mode, "drops")) {
      mock_index = 0;
      mock_dropped = 2;
      buffer_completed(NULL, 0, malloc(64), 64, 64);
    }
  }
  if (!strcmp(mode, "flush")) flush_status = CUPTI_ERROR_UNKNOWN;
  if (!strcmp(mode, "release")) {
    assert(kernrec_release() == 1 && disabled == 1 && flushes == 1);
    assert(kernrec_release() == 0 && disabled == 1);
  }
  at_exit();
  if (!strcmp(mode, "write")) {
    assert_empty(path);
    printf("CUDA callback check passed: %s\n", mode);
    return 0;
  }
  if (!strcmp(mode, "release") || !strcmp(mode, "released-pid"))
    assert(flushes == (strcmp(mode, "release") ? 0 : 1)); /* none at exit */
  FILE *input = fopen(path, "r");
  assert(input != NULL);
  char text[8192] = {0};
  assert(fread(text, 1, sizeof(text) - 1, input) > 0);
  fclose(input);
  assert(strstr(text, "backend=cuda recorder=cupti"));
  assert(strstr(text, "# end "));
  if (!strcmp(mode, "register")) assert(strstr(text, "# error=register-callbacks"));
  else if (!strcmp(mode, "enable")) assert(strstr(text, "# error=enable-kernel-activity"));
  else if (!strcmp(mode, "read")) assert(strstr(text, "# error=read-buffer"));
  else if (!strcmp(mode, "dropped-query")) assert(strstr(text, "# error=read-drop-count"));
  else if (!strcmp(mode, "flush")) assert(strstr(text, "# error=final-flush"));
  else if (!strcmp(mode, "release") || !strcmp(mode, "released-pid"))
    assert(strstr(text, "# error=released-to-profiler"));
  else if (!strcmp(mode, "names")) {
    assert(strstr(text, "# end records=3 unique=1 dropped=0 errors=0 unresolved=1\n"));
  } else if (!strcmp(mode, "drops")) {
    assert(strstr(text, "# dropped=3\n# dropped=2\n"));
    assert(strstr(text, "# end records=2 unique=1 dropped=5 errors=0 unresolved=0\n"));
  }
  if (strcmp(mode, "names") && strcmp(mode, "drops"))
    assert(strstr(text, "errors=1 unresolved=0\n"));
  printf("CUDA callback check passed: %s\n", mode);
  fflush(stdout);
  _exit(0);
}
