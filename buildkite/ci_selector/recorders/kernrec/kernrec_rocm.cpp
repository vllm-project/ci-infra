// Record dispatched ROCm kernels, including graph replay, through ROCProfiler SDK.
// Load the SDK with LD_PRELOAD and select this library with ROCP_TOOL_LIBRARIES.
// Output follows kernrec.c; errors/unresolved names make a recording incomplete.
#include <rocprofiler-sdk/rocprofiler.h>
#include <rocprofiler-sdk/registration.h>
#include <rocprofiler-sdk/version.h>

#include <cerrno>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <string>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <unordered_map>
#include <unordered_set>

#if ROCPROFILER_SDK_VERSION != 10302
#error "kernrec-rocm requires the validated ROCProfiler SDK 1.3.2 headers"
#endif

namespace {
struct State {
  pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
  pthread_mutex_t timer_lock = PTHREAD_MUTEX_INITIALIZER;
  pthread_cond_t timer_cond = PTHREAD_COND_INITIALIZER;
  pthread_t timer{};
  pid_t pid = getpid();
  int fd = -1;
  char dir[4096]{};
  bool stopping = false, timer_started = false, started = false;
  long flush_ms = 1000;
  uint64_t records = 0, dropped = 0, errors = 0;
  rocprofiler_context_id_t context{};
  rocprofiler_buffer_id_t buffer{};
  std::unordered_map<uint64_t, std::string> symbols;
  std::unordered_map<uint64_t, uint64_t> pending;
  std::unordered_set<std::string> names;
};

// Deliberately retained until process teardown, after SDK-owned threads stop.
State* state = nullptr;

bool owned() { return state && state->pid == getpid(); }

bool write_all(int fd, const char* data, size_t size) {
  while (size) {
    ssize_t count = write(fd, data, size);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return false;
    data += count;
    size -= static_cast<size_t>(count);
  }
  return true;
}

void output(const char* data, size_t size) {
  if (state->fd >= 0 && !write_all(state->fd, data, size)) {
    ++state->errors;
    close(state->fd);
    state->fd = -1;
    fprintf(stderr, "kernrec-rocm: cannot write recording: %s\n", strerror(errno));
  }
}

void error_locked(const char* operation) {
  ++state->errors;
  output("# error=", 8);
  output(operation, strlen(operation));
  output("\n", 1);
  fprintf(stderr, "kernrec-rocm: %s\n", operation);
}

bool check(rocprofiler_status_t status, const char* operation) {
  if (status == ROCPROFILER_STATUS_SUCCESS) return true;
  pthread_mutex_lock(&state->lock);
  error_locked(operation);
  pthread_mutex_unlock(&state->lock);
  fprintf(stderr, "kernrec-rocm: SDK status %d (%s)\n", static_cast<int>(status),
          rocprofiler_get_status_string(status));
  return false;
}

bool mkdir_open(const char* path) {
  if (mkdir(path, 0777) == 0) return chmod(path, 0777) == 0;
  return errno == EEXIST;
}

bool open_output() {
  const char* dir = getenv("KERNREC_DIR");
  int size;
  if (dir && *dir) {
    size = snprintf(state->dir, sizeof(state->dir), "%s", dir);
  } else {
    const char* job = getenv("BUILDKITE_JOB_ID");
    size = snprintf(state->dir, sizeof(state->dir), ".kernrec/%s",
                    job && *job ? job : "local");
  }
  if (size <= 0 || static_cast<size_t>(size) >= sizeof(state->dir) - 40)
    return false;
  char path[4096];
  memcpy(path, state->dir, static_cast<size_t>(size) + 1);
  for (char* p = path + 1; *p; ++p) {
    if (*p != '/') continue;
    *p = 0;
    if (!mkdir_open(path)) return false;
    *p = '/';
  }
  if (!mkdir_open(path)) return false;
  snprintf(path + size, sizeof(path) - static_cast<size_t>(size), "/kern.%ld.txt",
           static_cast<long>(state->pid));
  state->fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0666);
  if (state->fd < 0) return false;
  char exe[1024]{};
  ssize_t count = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
  char header[1400];
  int length = snprintf(header, sizeof(header),
                        "# kernrec v1 backend=rocm recorder=rocprofiler-sdk "
                        "pid=%ld ppid=%ld exe=%s\n",
                        static_cast<long>(state->pid), static_cast<long>(getppid()),
                        count > 0 ? exe : "?");
  output(header, static_cast<size_t>(length));
  return state->fd >= 0;
}

char* append(char* dest, const char* text) {
  while (*text) *dest++ = *text++;
  return dest;
}

char* append_pid(char* dest, pid_t pid) {
  char digits[32];
  size_t count = 0;
  do {
    digits[count++] = static_cast<char>('0' + pid % 10);
    pid /= 10;
  } while (pid);
  while (count) *dest++ = digits[--count];
  return dest;
}

// Only async-signal-safe operations: inherited SDK and mutex state are unusable.
// Even a child doing no GPU work is conservatively visible as incomplete.
void fork_child() {
  if (!state) return;
  if (state->fd >= 0) close(state->fd);
  state->fd = -1;
  char path[4096];
  char* end = append(path, state->dir);
  end = append(end, "/kern.");
  end = append_pid(end, getpid());
  end = append(end, ".txt");
  *end = 0;
  int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0666);
  if (fd < 0) return;
  char header[256];
  end = append(header, "# kernrec v1 backend=rocm recorder=rocprofiler-sdk pid=");
  end = append_pid(end, getpid());
  end = append(end, " ppid=");
  end = append_pid(end, getppid());
  end = append(end, " forked=1\n# error=fork_after_sdk_init\n");
  write_all(fd, header, static_cast<size_t>(end - header));
  close(fd);
}

void emit_name(const std::string& name) {
  if (state->names.insert(name).second) {
    output(name.data(), name.size());
    output("\n", 1);
  }
}

void symbols_callback(rocprofiler_callback_tracing_record_t record,
                      rocprofiler_user_data_t*, void*) {
  if (!owned() || record.kind != ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT ||
      record.operation != ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER ||
      record.phase != ROCPROFILER_CALLBACK_PHASE_LOAD)
    return;
  pthread_mutex_lock(&state->lock);
  try {
    using Symbol = rocprofiler_callback_tracing_code_object_kernel_symbol_register_data_t;
    const auto* symbol = static_cast<const Symbol*>(record.payload);
    if (!symbol || symbol->size < offsetof(Symbol, kernel_object) ||
        !symbol->kernel_name || !*symbol->kernel_name) {
      error_locked("invalid_kernel_symbol");
    } else {
      std::string name(symbol->kernel_name);
      if (name.size() > 3 && name.compare(name.size() - 3, 3, ".kd") == 0)
        name.resize(name.size() - 3);
      if (name.find_first_of("\r\n") != std::string::npos || name[0] == '#') {
        error_locked("invalid_kernel_name");
      } else {
        // Keep copied names after unload: outstanding dispatch records use them.
        auto inserted = state->symbols.emplace(symbol->kernel_id, name);
        if (!inserted.second && inserted.first->second != name)
          error_locked("conflicting_kernel_symbol");
        if (state->pending.erase(symbol->kernel_id)) emit_name(name);
      }
    }
  } catch (...) {
    error_locked("symbol_callback_exception");
  }
  pthread_mutex_unlock(&state->lock);
}

void dispatch_callback(rocprofiler_context_id_t, rocprofiler_buffer_id_t,
                       rocprofiler_record_header_t** headers, size_t count,
                       void*, uint64_t dropped) {
  if (!owned()) return;
  pthread_mutex_lock(&state->lock);
  if (dropped < state->dropped) error_locked("drop_counter_regressed");
  if (dropped > state->dropped) {
    uint64_t delta = dropped - state->dropped;
    state->dropped = dropped;
    char line[80];
    int size = snprintf(line, sizeof(line), "# dropped=%llu\n",
                        static_cast<unsigned long long>(delta));
    output(line, static_cast<size_t>(size));
  }
  try {
    if (!headers && count) {
      error_locked("missing_buffer_headers");
    } else {
      for (size_t i = 0; i < count; ++i) {
        const auto* header = headers[i];
        if (!header || !header->payload ||
            header->category != ROCPROFILER_BUFFER_CATEGORY_TRACING ||
            header->kind != ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH) {
          error_locked("invalid_dispatch_header");
          continue;
        }
        using Dispatch = rocprofiler_buffer_tracing_kernel_dispatch_record_t;
        const auto* record = static_cast<const Dispatch*>(header->payload);
        if (record->size < sizeof(Dispatch)) {
          error_locked("short_dispatch_record");
          continue;
        }
        ++state->records;
        uint64_t id = record->dispatch_info.kernel_id;
        auto symbol = state->symbols.find(id);
        if (symbol != state->symbols.end())
          emit_name(symbol->second);
        else
          ++state->pending[id];
      }
    }
  } catch (...) {
    error_locked("dispatch_callback_exception");
  }
  pthread_mutex_unlock(&state->lock);
}

void* flush_loop(void*) {
  pthread_mutex_lock(&state->timer_lock);
  while (!state->stopping) {
    timespec until{};
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_sec += state->flush_ms / 1000;
    until.tv_nsec += (state->flush_ms % 1000) * 1000000L;
    if (until.tv_nsec >= 1000000000L) {
      ++until.tv_sec;
      until.tv_nsec -= 1000000000L;
    }
    int status = 0;
    while (!state->stopping && status == 0)
      status = pthread_cond_timedwait(&state->timer_cond, &state->timer_lock, &until);
    if (state->stopping) break;
    pthread_mutex_unlock(&state->timer_lock);
    auto result = rocprofiler_flush_buffer(state->buffer);
    if (result != ROCPROFILER_STATUS_ERROR_BUFFER_BUSY)
      check(result, "periodic_flush_failed");
    pthread_mutex_lock(&state->timer_lock);
  }
  pthread_mutex_unlock(&state->timer_lock);
  return nullptr;
}

int tool_init(rocprofiler_client_finalize_t, void*) {
  if (!owned()) return -1;
  rocprofiler_tracing_operation_t symbol_op =
      ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER;
  constexpr size_t buffer_size = 8 * 1024 * 1024;
  // SDK 1.3.2 rotates two buffers on flush; DISCARD allocates only one and can
  // silently lose dispatches in the other. LOSSLESS also avoids pressure loss.
  if (!check(rocprofiler_create_context(&state->context), "create_context_failed") ||
      !check(rocprofiler_configure_callback_tracing_service(
                 state->context, ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT,
                 &symbol_op, 1, symbols_callback, nullptr), "symbol_tracing_failed") ||
      !check(rocprofiler_create_buffer(state->context, buffer_size, buffer_size / 2,
                 ROCPROFILER_BUFFER_POLICY_LOSSLESS, dispatch_callback, nullptr,
                 &state->buffer), "create_buffer_failed") ||
      !check(rocprofiler_configure_buffer_tracing_service(state->context,
                 ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH, nullptr, 0,
                 state->buffer), "dispatch_tracing_failed") ||
      !check(rocprofiler_start_context(state->context), "start_context_failed"))
    return -1;
  state->started = true;
  const char* value = getenv("KERNREC_FLUSH_MS");
  if (value && *value) {
    char* end = nullptr;
    long ms = strtol(value, &end, 10);
    if (!*end && ms >= 0 && ms <= 3600000) state->flush_ms = ms;
  }
  if (state->flush_ms) {
    sigset_t all, previous;
    sigfillset(&all);
    pthread_sigmask(SIG_SETMASK, &all, &previous);
    int status = pthread_create(&state->timer, nullptr, flush_loop, nullptr);
    pthread_sigmask(SIG_SETMASK, &previous, nullptr);
    state->timer_started = status == 0;
    if (status) {
      pthread_mutex_lock(&state->lock);
      error_locked("periodic_flush_thread_failed");
      pthread_mutex_unlock(&state->lock);
    }
  }
  return 0;
}

void finish_output() {
  uint64_t unresolved = 0;
  for (const auto& entry : state->pending) unresolved += entry.second;
  char footer[256];
  int size = snprintf(footer, sizeof(footer),
      "# end records=%llu unique=%zu dropped=%llu errors=%llu unresolved=%llu\n",
      static_cast<unsigned long long>(state->records), state->names.size(),
      static_cast<unsigned long long>(state->dropped),
      static_cast<unsigned long long>(state->errors),
      static_cast<unsigned long long>(unresolved));
  output(footer, static_cast<size_t>(size));
  if (state->fd >= 0) close(state->fd);
  state->fd = -1;
  state->started = false;
}

void tool_fini(void*) {
  if (!owned() || !state->started) return;
  if (state->timer_started) {
    pthread_mutex_lock(&state->timer_lock);
    state->stopping = true;
    pthread_cond_signal(&state->timer_cond);
    pthread_mutex_unlock(&state->timer_lock);
    pthread_join(state->timer, nullptr);
  }
  // The SDK stops this client's contexts and synchronizes dispatches before fini.
  // Drain both internal buffers after all producers have stopped.
  check(rocprofiler_flush_buffer(state->buffer), "final_flush_failed");
  check(rocprofiler_flush_buffer(state->buffer), "final_flush_failed");
  pthread_mutex_lock(&state->lock);
  finish_output();
  pthread_mutex_unlock(&state->lock);
}
}  // namespace

extern "C" rocprofiler_tool_configure_result_t* rocprofiler_configure(
    uint32_t version, const char*, uint32_t priority, rocprofiler_client_id_t* id) {
  id->name = "kernrec-rocm";
  try {
    state = new State;
  } catch (...) {
    fprintf(stderr, "kernrec-rocm: cannot allocate recorder state\n");
    return nullptr;
  }
  if (!open_output()) {
    fprintf(stderr, "kernrec-rocm: cannot create recording: %s\n", strerror(errno));
    return nullptr;
  }
  char metadata[120];
  int size = snprintf(metadata, sizeof(metadata),
      "# sdk_version=%u build_sdk_version=%u\n", version,
      static_cast<unsigned>(ROCPROFILER_SDK_VERSION));
  output(metadata, static_cast<size_t>(size));
  if (version != ROCPROFILER_SDK_VERSION) {
    error_locked("unsupported_sdk_version");
    return nullptr;
  }
  if (priority != 0) {
    error_locked("another_profiler_registered_first");
    return nullptr;
  }
  if (pthread_atfork(nullptr, nullptr, fork_child)) {
    error_locked("register_atfork_failed");
    return nullptr;
  }
  static rocprofiler_tool_configure_result_t config{
      sizeof(rocprofiler_tool_configure_result_t), tool_init, tool_fini, nullptr};
  return &config;
}
