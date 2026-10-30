# Buildkite pipeline generator

## Automatic test sharding

Timing-based pytest sharding covers NVIDIA jobs, native AMD mirrors, and
standalone native AMD jobs. Docker-in-Docker and multi-node jobs, and jobs whose
commands aren't all plain pytest, run as one job.

A pipeline config with `automatic_shard_default: true` shards every eligible job.
`automatic_shard: false` opts a job out; a mirror follows its step unless its
`mirror.amd` block says otherwise; a fixed `parallelism` without `false` is an
error; and an annotation lists the build's jobs that ran as one job.

Without it (release branches, Intel, ROCm), `automatic_shard: true` enables a
job, an AMD mirror also needs the flag in its `mirror.amd` block, and explicit
parallelism takes precedence.

Each sharded build also runs a soft-failing job that checks every test a command
collects ran in some shard, and posts a red annotation listing any that didn't.

Commands retain their original pytest selectors. A command selecting a node ID
can split its parametrized cases when main has per-test timings for that exact
command. Without matching timings, the selected file stays together.

The planner's estimates are not a substitute for measuring CI: validate both
the slowest shard and the sum of all shard runtimes against the original job.
