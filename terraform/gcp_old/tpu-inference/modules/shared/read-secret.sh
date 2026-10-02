# ==========================================
# Read secrets from Secret Manager
# ==========================================
# Injected verbatim into agent startup scripts with file(). The VM's service
# account reads each secret at boot, so the values never land in instance
# metadata or Terraform state, and rotating one is a new secret version plus a
# reboot, with no apply.
#
# Takes projects/<project>/secrets/<name> and prints the latest version.
# Retries for a minute in case the network is still coming up. The value goes
# to stdout only, for the caller's $( ), never to the serial console.
read_secret() {
  local value
  for _ in 1 2 3 4 5 6; do
    # gcloud comes from the google-cloud-cli snap on some images, and the
    # startup script's PATH lacks /snap/bin.
    if value=$(PATH="$PATH:/snap/bin" gcloud secrets versions access latest \
        --project="$(echo "$1" | cut -d/ -f2)" --secret="$(echo "$1" | cut -d/ -f4)" \
        --quiet 2>/dev/null) && [ -n "$value" ]; then
      printf '%s' "$value"
      return 0
    fi
    sleep 10
  done
  echo "read_secret: could not read $1" >&2
  return 1
}
