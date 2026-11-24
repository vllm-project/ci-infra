// CPU-only callback contract checks. Build/run with test-rocm.sh.
#include "kernrec_rocm.cpp"

#include <cassert>
#include <fstream>
#include <iterator>
#include <sys/resource.h>
#include <sys/wait.h>

using Symbol = rocprofiler_callback_tracing_code_object_kernel_symbol_register_data_t;
using Dispatch = rocprofiler_buffer_tracing_kernel_dispatch_record_t;
using Enqueue = rocprofiler_callback_tracing_kernel_dispatch_data_t;

void symbol(uint64_t id, const char* name,
            rocprofiler_callback_phase_t phase = ROCPROFILER_CALLBACK_PHASE_LOAD) {
  Symbol data{};
  data.size = sizeof(data);
  data.kernel_id = id;
  data.kernel_name = name;
  rocprofiler_callback_tracing_record_t record{};
  record.kind = ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT;
  record.operation = ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER;
  record.phase = phase;
  record.payload = &data;
  symbols_callback(record, nullptr, nullptr);
}

void dispatch(uint64_t id, uint64_t dropped = 0) {
  Dispatch data{};
  data.size = sizeof(data);
  data.dispatch_info.kernel_id = id;
  rocprofiler_record_header_t record{};
  record.category = ROCPROFILER_BUFFER_CATEGORY_TRACING;
  record.kind = ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH;
  record.payload = &data;
  auto* ptr = &record;
  dispatch_callback({}, {}, &ptr, 1, nullptr, dropped);
}

void enqueue(uint64_t id,
             rocprofiler_callback_phase_t phase = ROCPROFILER_CALLBACK_PHASE_ENTER) {
  Enqueue data{};
  data.size = sizeof(data);
  data.dispatch_info.kernel_id = id;
  rocprofiler_callback_tracing_record_t record{};
  record.kind = ROCPROFILER_CALLBACK_TRACING_KERNEL_DISPATCH;
  record.operation = ROCPROFILER_KERNEL_DISPATCH_ENQUEUE;
  record.phase = phase;
  record.payload = &data;
  enqueue_callback(record, nullptr, nullptr);
}

std::string contents(pid_t pid) {
  std::ifstream input(std::string(state->dir) + "/kern." + std::to_string(pid) + ".txt");
  return std::string(std::istreambuf_iterator<char>(input), {});
}

size_t count(const std::string& text, const std::string& needle) {
  size_t n = 0;
  for (size_t at = text.find(needle); at != std::string::npos;
       at = text.find(needle, at + 1))
    ++n;
  return n;
}

// Launch-time names: the enqueue callback writes each name before its packet
// is queued, so a SIGKILLed process leaves a complete file. Buffered records
// cross-check it.
void check_sync(const std::string& root) {
  unsetenv("KERNREC_SYNC");
  rocprofiler_client_id_t client{};

  pid_t child = fork();
  assert(child >= 0);
  if (child == 0) {
    setenv("KERNREC_DIR", (root + "/sync-killed").c_str(), 1);
    if (!rocprofiler_configure(ROCPROFILER_SDK_VERSION, "", 0, &client)) _exit(2);
    symbol(1, "_Z4usedv.kd");
    symbol(2, "_Z6unusedv.kd");
    enqueue(1);
    enqueue(1);
    enqueue(1, ROCPROFILER_CALLBACK_PHASE_EXIT);
    raise(SIGKILL);  // no buffer flush, no footer
    _exit(3);
  }
  int status = 0;
  assert(waitpid(child, &status, 0) == child && WIFSIGNALED(status) &&
         WTERMSIG(status) == SIGKILL);
  std::ifstream input(root + "/sync-killed/kern." + std::to_string(child) + ".txt");
  std::string killed(std::istreambuf_iterator<char>(input), {});
  assert(killed.find(" activity=1 sync=1 pid=") != std::string::npos);
  assert(killed.find("# gpu_active\n_Z4usedv\n") != std::string::npos);
  assert(count(killed, "_Z4usedv") == 1);
  assert(killed.find("unused") == std::string::npos);
  assert(killed.find("# error") == std::string::npos);
  assert(killed.find("# end") == std::string::npos);
  assert(killed.back() == '\n');

  child = fork();
  assert(child >= 0);
  if (child == 0) {
    setenv("KERNREC_DIR", (root + "/sync").c_str(), 1);
    assert(rocprofiler_configure(ROCPROFILER_SDK_VERSION, "", 0, &client));
    symbol(1, "_Z4usedv.kd");
    symbol(2, "_Z6otherv.kd");
    symbol(4, "_Z5aliasv.kd");
    enqueue(1);
    dispatch(1);  // already named at enqueue: no error
    enqueue(4);
    symbol(5, "_Z5aliasv.kd");  // same name, new id (code object reloaded)
    enqueue(5);
    dispatch(2);  // ran but never enqueued through the callback
    enqueue(99);  // no symbol: cannot vouch for this launch
    enqueue(98);
    enqueue(99);
    enqueue(1);
    // A fork child launching a kernel its parent loaded never loads GPU code
    // itself; the enqueue callback still marks its trace incomplete.
    pid_t grandchild = fork();
    assert(grandchild >= 0);
    if (grandchild == 0) {
      enqueue(1);
      _exit(0);
    }
    int child_status = 0;
    assert(waitpid(grandchild, &child_status, 0) == grandchild && child_status == 0);
    auto forked = contents(grandchild);
    assert(forked.find("forked=1\n# error=gpu_use_after_fork\n") != std::string::npos);
    assert(forked.find("_Z4usedv") == std::string::npos);
    finish_output();
    auto text = contents(getpid());
    assert(count(text, "\n_Z4usedv\n") == 1);
    assert(count(text, "_Z5aliasv") == 1);
    assert(text.find("# error=callback-missed _Z6otherv\n_Z6otherv\n") != std::string::npos);
    assert(count(text, "# error=callback-unresolved\n") == 1);
    assert(text.find("# end records=2 unique=3 dropped=0 errors=2 unresolved=0\n") !=
           std::string::npos);
    _exit(0);
  }
  assert(waitpid(child, &status, 0) == child && WIFEXITED(status) &&
         WEXITSTATUS(status) == 0);
}

int main(int argc, char** argv) {
  assert(argc == 2);
  check_sync(argv[1]);
  // The rest checks the footer contract, which KERNREC_SYNC=0 restores.
  setenv("KERNREC_SYNC", "0", 1);
  setenv("KERNREC_DIR", argv[1], 1);
  rocprofiler_client_id_t client{};
  assert(rocprofiler_configure(ROCPROFILER_SDK_VERSION, "", 0, &client));
  assert(contents(getpid()).find("backend=rocm recorder=rocprofiler-sdk activity=1 ") !=
         std::string::npos);
  assert(contents(getpid()).find("sync=1") == std::string::npos);
  assert(contents(getpid()).find("# gpu_active") == std::string::npos);

  // Only dispatched symbols belong in the output; unload cannot invalidate names.
  symbol(1, "_Z4usedv.kd");
  symbol(2, "_Z6unusedv.kd");
  symbol(1, "_Z4usedv.kd", ROCPROFILER_CALLBACK_PHASE_UNLOAD);
  // The first loaded code object marks the process active, once.
  auto loaded = contents(getpid());
  assert(loaded.find("# gpu_active\n") != std::string::npos);
  assert(loaded.find("# gpu_active", loaded.find("# gpu_active") + 1) == std::string::npos);
  dispatch(1);
  dispatch(1);
  dispatch(3);
  symbol(3, "interior.kd.stays.kd");

  // The SDK delivers a cumulative drop counter, even on successive buffers.
  dispatch(1, 3);
  dispatch(1, 3);
  dispatch(1, 5);

  // A forked child must neither enter inherited locks nor write a recording
  // unless it loads GPU code; then its trace is marked incomplete.
  pid_t child = fork();
  assert(child >= 0);
  if (child == 0) {
    dispatch(1, 5);
    symbol(4, "_Z6parentv.kd", ROCPROFILER_CALLBACK_PHASE_UNLOAD);
    tool_fini(nullptr);
    _exit(0);
  }
  int status = 0;
  assert(waitpid(child, &status, 0) == child && status == 0);
  assert(access((std::string(state->dir) + "/kern." + std::to_string(child) + ".txt").c_str(),
                F_OK) != 0);

  child = fork();
  assert(child >= 0);
  if (child == 0) {
    symbol(5, "_Z5childv.kd");
    symbol(6, "_Z6child2v.kd");
    dispatch(5);
    tool_fini(nullptr);
    _exit(0);
  }
  assert(waitpid(child, &status, 0) == child && status == 0);
  auto child_text = contents(child);
  assert(child_text.find("forked=1\n# error=gpu_use_after_fork\n") != std::string::npos);
  assert(child_text.find("# error", child_text.find("# error") + 1) == std::string::npos);
  assert(child_text.find("# end") == std::string::npos);
  assert(child_text.find("_Z5childv") == std::string::npos);

  dispatch(999, 5);
  dispatch_callback({}, {}, nullptr, 1, nullptr, 5);
  finish_output();
  auto text = contents(getpid());
  assert(text.find("\n_Z4usedv\n") != std::string::npos);
  assert(text.find("_Z4usedv.kd") == std::string::npos);
  assert(text.find("unused") == std::string::npos);
  assert(text.find("\ninterior.kd.stays\n") != std::string::npos);
  assert(text.find("# dropped=3\n# dropped=2\n") != std::string::npos);
  assert(text.find("# error=missing_buffer_headers\n") != std::string::npos);
  assert(text.find("# end records=7 unique=2 dropped=5 errors=1 unresolved=1\n") !=
         std::string::npos);

  // A full disk after the header must not hide the process going active.
  child = fork();
  assert(child >= 0);
  if (child == 0) {
    assert(rocprofiler_configure(ROCPROFILER_SDK_VERSION, "", 0, &client));
    signal(SIGXFSZ, SIG_IGN);
    rlimit full{static_cast<rlim_t>(contents(getpid()).size()), RLIM_INFINITY};
    assert(setrlimit(RLIMIT_FSIZE, &full) == 0);
    symbol(1, "_Z4usedv.kd");
    _exit(contents(getpid()).empty() ? 0 : 1);
  }
  assert(waitpid(child, &status, 0) == child && status == 0);
  puts("ROCm SDK callback contract checks passed");
}
