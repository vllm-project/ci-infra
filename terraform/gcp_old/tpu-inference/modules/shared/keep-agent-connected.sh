# ==========================================
# Keep the agent connected
# ==========================================
# Injected verbatim into agent startup scripts with file(). Kept in one place
# because the startup scripts are already long and this logic has no reason to
# differ between fleets.
#
# The packaged unit is Restart=on-failure, so it only recovers when the process
# exits non-zero. A server-side deregistration makes the agent disconnect but
# leave its process alive: systemd keeps reporting the unit active while the
# queue quietly drains, and nothing alerts. Restart=always covers the
# clean-exit case; the watchdog covers the wedged-but-alive case.
#
# Safe to run before the agent is unmasked -- the watchdog no-ops until the
# unit is actually enabled.

sudo sed -i '/^health-check-addr=/d' /etc/buildkite-agent/buildkite-agent.cfg
echo 'health-check-addr="localhost:8123"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg > /dev/null

sudo mkdir -p /etc/systemd/system/buildkite-agent.service.d
cat <<'DROPIN' | sudo tee /etc/systemd/system/buildkite-agent.service.d/10-restart.conf > /dev/null
[Service]
Restart=always
RestartSec=10
DROPIN

cat <<'WATCHDOG' | sudo tee /usr/local/bin/buildkite-agent-watchdog > /dev/null
#!/bin/bash
set -u

# Multi-host TPU workers other than 0 leave the agent masked or disabled on
# purpose so they never join the queue. Nothing to watch on those hosts.
systemctl is-enabled --quiet buildkite-agent || exit 0

# The health endpoint stops returning 200 once the agent is no longer pinging
# Buildkite. It stays healthy while a job runs, so a miss means the agent is
# wedged, not busy. Three in a row rules out a network blip.
STATE=/run/buildkite-agent-watchdog.fails
fails=$(cat "$STATE" 2>/dev/null || echo 0)
if curl -fsS --max-time 10 http://localhost:8123/ >/dev/null 2>&1; then
  echo 0 > "$STATE"
  exit 0
fi
fails=$((fails + 1))
echo "$fails" > "$STATE"
if [ "$fails" -ge 3 ]; then
  logger -t buildkite-agent-watchdog "health check failed $fails times, restarting agent"
  echo 0 > "$STATE"
  systemctl restart buildkite-agent
fi
WATCHDOG
sudo chmod +x /usr/local/bin/buildkite-agent-watchdog

cat <<'UNIT' | sudo tee /etc/systemd/system/buildkite-agent-watchdog.service > /dev/null
[Unit]
Description=Restart the Buildkite agent when it stops reporting healthy
[Service]
Type=oneshot
ExecStart=/usr/local/bin/buildkite-agent-watchdog
UNIT

cat <<'TIMER' | sudo tee /etc/systemd/system/buildkite-agent-watchdog.timer > /dev/null
[Unit]
Description=Periodic Buildkite agent health check
[Timer]
OnBootSec=5min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable --now buildkite-agent-watchdog.timer
# ==========================================
