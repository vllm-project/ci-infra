# Buildkite pipeline generator

## Automatic test sharding

`automatic_shard: true` enables timing-based pytest sharding for NVIDIA jobs,
native AMD mirrors, and standalone native AMD jobs. AMD mirrors also need the
flag in their `mirror.amd` block. Explicit parallelism takes precedence;
Docker-in-Docker and multi-node jobs are not automatically sharded.

Commands retain their original pytest selectors. A command selecting a node ID
can split its parametrized cases when main has per-test timings for that exact
command. Without matching timings, the selected file stays together.

The planner's estimates are not a substitute for measuring CI: validate both
the slowest shard and the sum of all shard runtimes against the original job.
