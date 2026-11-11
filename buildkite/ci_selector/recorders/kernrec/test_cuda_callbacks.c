/* CPU-only CUPTI failure-contract checks using real headers and mocked APIs. */
#include "kernrec.c"
#include <assert.h>
#include <fcntl.h>

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

CUptiResult CUPTIAPI cuptiEnableCallback(uint32_t enable,
                                         CUpti_SubscriberHandle subscriber,
                                         CUpti_CallbackDomain domain,
                                         CUpti_CallbackId cbid) {
  (void)enable;
  (void)subscriber;
  assert(domain == CUPTI_CB_DOMAIN_RESOURCE);
  assert(cbid == CUPTI_CBID_RESOURCE_CONTEXT_CREATED);
  return CUPTI_SUCCESS;
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
  assert(strstr(text, "backend=cuda recorder=cupti activity=1 pid="));
  if (!strcmp(mode, "marker-idle")) {
    assert(!strstr(text, "# gpu_active"));
  } else if (!strcmp(mode, "marker-active")) {
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE,
                    CUPTI_CBID_RESOURCE_CONTEXT_CREATED, NULL);
    marker_callback(NULL, CUPTI_CB_DOMAIN_RESOURCE,
                    CUPTI_CBID_RESOURCE_CONTEXT_CREATED, NULL);
    read_file(path, text, sizeof text);
    assert(count(text, "# gpu_active\n") == 1);
    if (g_active) unsubscribe_activity_marker(); /* the flush tick's step */
    assert(unsubscribes == 1);
    unsubscribe_activity_marker();
    assert(unsubscribes == 1);
  } else if (!strcmp(mode, "marker-release")) {
    assert(kernrec_release() == 1 && unsubscribes == 1);
  } else {
    assert(0 && "unknown marker mode");
  }
}

CUptiResult CUPTIAPI cuptiGetResultString(CUptiResult result, const char **text) {
  (void)result;
  *text = "injected test failure";
  return CUPTI_SUCCESS;
}

/* The first line flush fails, later writes recover. A footer must retain the
 * failure even if the number of output lines happens to match unique names. */
struct failing_stream { int fd, failed; };

static ssize_t transient_write(void *cookie, const char *data, size_t size) {
  struct failing_stream *stream = cookie;
  if (!stream->failed++) { errno = ENOSPC; return -1; }
  return write(stream->fd, data, size);
}

static int close_stream(void *cookie) {
  return close(((struct failing_stream *)cookie)->fd);
}

int main(int argc, char **argv) {
  assert(argc == 3);
  setenv("KERNREC_DIR", argv[1], 1);
  setenv("KERNREC_FLUSH_MS", "0", 1);
  const char *mode = argv[2];
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
  if (!strncmp(mode, "marker-", 7)) {
    check_marker(mode, path);
    printf("CUDA callback check passed: %s\n", mode);
    fflush(stdout);
    _exit(0);
  }
  struct failing_stream stream = {-1, 0};
  if (!strcmp(mode, "write")) {
    fclose(g_out);
    stream.fd = open(path, O_WRONLY | O_APPEND);
    assert(stream.fd >= 0);
    cookie_io_functions_t io = {.write = transient_write, .close = close_stream};
    g_out = fopencookie(&stream, "w", io);
    assert(g_out != NULL);
    setvbuf(g_out, NULL, _IOLBF, 0);
  }
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
  } else assert(!strcmp(mode, "write"));
  if (strcmp(mode, "names") && strcmp(mode, "drops"))
    assert(strstr(text, "errors=1 unresolved=0\n"));
  printf("CUDA callback check passed: %s\n", mode);
  fflush(stdout);
  _exit(0);
}
