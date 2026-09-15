# Repository Agent Instructions

## Buildkite machine setup

When asked to prepare a Buildkite agent, execute the applicable steps in
`scripts/AGENT.md` and `scripts/buildkite-agent/RUNBOOK.md`; do not merely
repeat them. Stop only for required user input, credentials, or external
access.

- Create missing parent directories before installing files into root-owned
  paths. For the GPU CDI helper, run:

  ```bash
  sudo install -d -m 0755 /usr/local/libexec
  sudo install -m 0755 scripts/buildkite-agent/buildkite-gpu-cdi-env.sh /usr/local/libexec/buildkite-gpu-cdi-env.sh
  ```

- If a known root-owned write fails with `Permission denied`, retry that exact
  command with `sudo`. Do not use `sudo` to hide unrelated failures.
- Never put credentials in repository files, diffs, commits, or command
  output. Configure them only on the target machine and keep output redacted.
