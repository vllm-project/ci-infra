#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "Usage: sudo $0 --env-file PATH [--user USER] [--state-dir PATH] [--no-start]"
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

render_unit() {
  local source=$1 destination=$2
  sed \
    -e "s|@@SERVICE_USER@@|$(escape_sed "$service_user")|g" \
    -e "s|@@SERVICE_GROUP@@|$(escape_sed "$service_group")|g" \
    -e "s|@@SKILL_DIR@@|$(escape_sed "$skill_dir")|g" \
    -e "s|@@ENV_FILE@@|$(escape_sed "$installed_env")|g" \
    -e "s|@@PYTHON@@|$(escape_sed "$venv_python")|g" \
    -e "s|@@STATE_DIR@@|$(escape_sed "$state_dir")|g" \
    -e "s|@@STATE_PATH@@|$(escape_sed "$state_path")|g" \
    "$source" >"$destination"
}

env_source=
service_user=${SUDO_USER:-}
state_dir=/var/lib/vllm-fast-ci-failure-alert
start_timer=true

while (($#)); do
  case "$1" in
    --env-file)
      (($# >= 2)) || die "--env-file requires a path"
      env_source=$2
      shift 2
      ;;
    --user)
      (($# >= 2)) || die "--user requires a value"
      service_user=$2
      shift 2
      ;;
    --state-dir)
      (($# >= 2)) || die "--state-dir requires a path"
      state_dir=$2
      shift 2
      ;;
    --no-start)
      start_timer=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run this installer with sudo"
[[ -n "$env_source" ]] || die "--env-file is required"
[[ -n "$service_user" ]] || die "--user is required when SUDO_USER is unavailable"
id "$service_user" >/dev/null 2>&1 || die "user does not exist: $service_user"
[[ -f "$env_source" ]] || die "environment file does not exist: $env_source"
for name in \
  DATABRICKS_HOST \
  DATABRICKS_TOKEN \
  DATABRICKS_WAREHOUSE_ID \
  SLACK_BOT_TOKEN \
  SLACK_CHANNEL_ID; do
  grep -Eq "^${name}=.+$" "$env_source" \
    || die "environment file must set $name"
done

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
skill_dir=$(cd -- "$script_dir/.." && pwd -P)
service_group=$(id -gn "$service_user")
user_home=$(getent passwd "$service_user" | cut -d: -f6)
uv_bin=$(command -v uv || true)
if [[ -z "$uv_bin" && -x "$user_home/.local/bin/uv" ]]; then
  uv_bin="$user_home/.local/bin/uv"
fi
[[ -n "$uv_bin" ]] || die "uv is required; install it before running this script"

runuser -u "$service_user" -- "$uv_bin" sync \
  --project "$skill_dir" \
  --frozen \
  --no-dev
venv_python="$skill_dir/.venv/bin/python"
[[ -x "$venv_python" ]] || die "uv did not create $venv_python"

installed_env=/etc/vllm-fast-ci-failure-alert.env
if [[ "$(realpath "$env_source")" != "$installed_env" ]]; then
  install -o root -g "$service_group" -m 0640 "$env_source" "$installed_env"
else
  chown root:"$service_group" "$installed_env"
  chmod 0640 "$installed_env"
fi
install -d -o "$service_user" -g "$service_group" -m 0750 "$state_dir"
state_path=$state_dir/state.sqlite3

rendered_units=$(mktemp -d)
trap 'rm -rf "$rendered_units"' EXIT
render_unit \
  "$skill_dir/assets/vllm-fast-ci-failure-alert.service.in" \
  "$rendered_units/vllm-fast-ci-failure-alert.service"
install -m 0644 \
  "$skill_dir/assets/vllm-fast-ci-failure-alert.timer" \
  "$rendered_units/vllm-fast-ci-failure-alert.timer"

systemd-analyze verify "$rendered_units"/*
install -m 0644 "$rendered_units"/* /etc/systemd/system/
systemctl daemon-reload

if [[ "$start_timer" == true ]]; then
  systemctl enable --now vllm-fast-ci-failure-alert.timer
  systemctl list-timers vllm-fast-ci-failure-alert.timer --no-pager
else
  echo "Installed without starting the timer."
  echo "Enable after cutover with:"
  echo "  sudo systemctl enable --now vllm-fast-ci-failure-alert.timer"
fi
