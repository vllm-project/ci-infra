# Buildkite GPU CDI host rollout

These files move Buildkite Docker-plugin jobs from the legacy NVIDIA `--gpus`
hook to native CDI device requests. The agent pre-start regenerates the CDI spec
so a current MIG topology is available before the agent accepts work.

The drop-in sets `TimeoutStartSec=120`. On the 56-slice H200 layout,
`nvidia-ctk cdi generate` has taken about 20 seconds; the base unit's 10-second
timeout can otherwise trap the service in a restart loop.

## Read-only audit

`audit.sh` checks the effective timeout, pre-start command, hook, service state,
and CDI inventory. It does not modify the host and can be run while jobs are
active:

```bash
sudo scripts/buildkite-gpu-cdi/audit.sh
```

Do not regenerate CDI, reload systemd, or restart Docker or the agent merely to
make an audit pass. Those operations require the drain below.

## Drain and deploy

Treat `systemctl daemon-reload`, `systemctl daemon-reexec`, Docker or agent
restarts, NVIDIA toolkit/driver changes, and MIG topology changes as
workload-disruptive.

For one host at a time:

1. Pause every Buildkite agent record on the host with a maintenance note and a
   long enough `timeout_in_minutes` value.
2. Wait for the Buildkite API to report zero assigned jobs.
3. Verify both local gates are empty:

   ```bash
   docker ps -q
   nvidia-smi --query-compute-apps=pid --format=csv,noheader
   ```

4. Run the guarded installer from this directory:

   ```bash
   sudo env \
     CONFIRM_BUILDKITE_AGENTS_PAUSED=1 \
     CONFIRM_BUILDKITE_API_JOBS_ZERO=1 \
     ./deploy.sh
   ```

The installer checks the two explicit confirmations and both local zero-state
gates before it writes the hook/drop-in, regenerates CDI, or calls
`systemctl daemon-reload`. It fails before the reload if the checked-in timeout
is below 120 seconds. It does not restart the agent.

5. Keep the agents paused. Run `audit.sh`, then run the reload canary below.
6. Remove the canary, prove the local gates are empty again, and only then
   resume the agents. Preserve any pause that predates this maintenance.

## Reload canary

Use a cached CI image and the exact CDI selector assigned to the agent. On a
MIG host, use a current MIG UUID present in both `nvidia-smi -L` and
`nvidia-ctk cdi list`.

```bash
docker run -d --rm \
  --name gpu-cdi-reload-canary \
  --device "nvidia.com/gpu=${GPU_OR_MIG_SELECTOR}" \
  --entrypoint bash "${CACHED_IMAGE}" -lc 'sleep infinity'

docker exec gpu-cdi-reload-canary nvidia-smi -L >before.txt
agent_pid_before="$(systemctl show buildkite-agent.service -p MainPID --value)"
sudo systemctl daemon-reload
docker exec gpu-cdi-reload-canary nvidia-smi -L >after.txt
agent_pid_after="$(systemctl show buildkite-agent.service -p MainPID --value)"

cmp before.txt after.txt
test "${agent_pid_before}" = "${agent_pid_after}"
docker rm -f gpu-cdi-reload-canary
```

Pass only when both `nvidia-smi` calls succeed, the visible GPU/MIG UUIDs are
byte-identical, the agent PID is unchanged, and the host returns to zero test
containers and GPU processes. Do not resume a failed canary host.

## Rollback

Rollback has the same pause, API-zero, container-zero, and GPU-process-zero
requirements. Restore `/etc/buildkite-agent/hooks/environment.pre-cdi` (or
remove only the exact CDI source line), remove the `gpu-cdi.conf` drop-in and
selector helper, then run `systemctl daemon-reload` while the host remains
drained. Resume only after documenting that legacy GPU jobs are again exposed
to the daemon-reload device-loss behavior.
