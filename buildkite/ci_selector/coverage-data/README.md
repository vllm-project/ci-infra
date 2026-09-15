Put the coverage table here as `table.json.gz`.

It is gitignored on purpose: a 15 MB artifact produced by an instrumented CI
sweep, not source. Without it the selector runs on the code map alone and says
so on stderr.

`ci_selector/coverage/source.py::fetch_table` is the only code that knows this
directory exists.
