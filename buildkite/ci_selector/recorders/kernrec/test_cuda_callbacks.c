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
  return flush_status;
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
  assert(InitializeInjection() == 1);
  assert(g_out != NULL);
  char path[4096];
  assert(snprintf(path, sizeof(path), "%s/kern.%ld.txt", argv[1], (long)getpid()) > 0);
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
  at_exit();
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
