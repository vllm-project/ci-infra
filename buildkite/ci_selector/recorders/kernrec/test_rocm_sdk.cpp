// CPU-only callback contract checks. Build/run with test-rocm.sh.
#include "kernrec_rocm.cpp"

#include <cassert>
#include <fstream>
#include <iterator>
#include <sys/wait.h>

using Symbol = rocprofiler_callback_tracing_code_object_kernel_symbol_register_data_t;
using Dispatch = rocprofiler_buffer_tracing_kernel_dispatch_record_t;

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

std::string contents(pid_t pid) {
  std::ifstream input(std::string(state->dir) + "/kern." + std::to_string(pid) + ".txt");
  return std::string(std::istreambuf_iterator<char>(input), {});
}

int main(int argc, char** argv) {
  assert(argc == 2);
  setenv("KERNREC_DIR", argv[1], 1);
  rocprofiler_client_id_t client{};
  assert(rocprofiler_configure(10302, "1.3.2", 0, &client));
  assert(contents(getpid()).find("backend=rocm recorder=rocprofiler-sdk") !=
         std::string::npos);

  // Only dispatched symbols belong in the output; unload cannot invalidate names.
  symbol(1, "_Z4usedv.kd");
  symbol(2, "_Z6unusedv.kd");
  symbol(1, "_Z4usedv.kd", ROCPROFILER_CALLBACK_PHASE_UNLOAD);
  dispatch(1);
  dispatch(1);
  dispatch(3);
  symbol(3, "interior.kd.stays.kd");

  // The SDK delivers a cumulative drop counter, even on successive buffers.
  dispatch(1, 3);
  dispatch(1, 3);
  dispatch(1, 5);

  // A forked child must neither enter inherited locks nor append a clean footer.
  pid_t child = fork();
  assert(child >= 0);
  if (child == 0) {
    dispatch(1, 5);
    tool_fini(nullptr);
    _exit(0);
  }
  int status = 0;
  assert(waitpid(child, &status, 0) == child && status == 0);
  auto child_text = contents(child);
  assert(child_text.find("# error=fork_after_sdk_init") != std::string::npos);
  assert(child_text.find("# end") == std::string::npos);
  assert(child_text.find("_Z4usedv") == std::string::npos);

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
  puts("ROCm SDK callback contract checks passed");
}
