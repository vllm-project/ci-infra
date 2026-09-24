Put the coverage table here as `table.json.gz`.

It is gitignored on purpose: a 15 MB artifact produced by an instrumented CI
sweep, not source. Without it the selector runs on the code map alone and says
so on stderr.

`ci_selector/coverage/source.py::fetch_table` is the only code that knows this
directory exists.

The kernel record lives here too, as `kernel_table.json.gz` and
`kernel_symbol_map.json.gz`. `ci-fetch-kernel-record` downloads the latest
published pair from the public `vllm-ci-selector` bucket. Same rule: without
them the selector says so and csrc routes on the code map alone.
`ci_selector/coverage/source.py::fetch_kernel_evidence` is the only code that
knows their names.
