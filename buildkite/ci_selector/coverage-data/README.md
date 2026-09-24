Where both coverage records live. Each is fetched with one command and
gitignored on purpose: artifacts produced by an instrumented CI sweep, not
source. Without one the selector says so on stderr and routes on the code map
alone.

| file | fetch it with | read by |
| --- | --- | --- |
| `table.json.gz` | `ci-fetch-function-record` | `source.py::fetch_table` |
| `kernel_table.json.gz` + `kernel_symbol_map.json.gz` | `ci-fetch-kernel-record` | `source.py::fetch_kernel_evidence` |

Both come from the public `vllm-ci-selector` bucket, each recorder under its
own prefix. `coverage/source.py` is the only code that knows this directory
exists or what the files are called.
