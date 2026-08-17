#!/usr/bin/env bash

_CI_INFRA_OTEL_DIR="${CI_INFRA_OTEL_DIR:?CI_INFRA_OTEL_DIR must point to the injected CI tracing helpers}"
export CI_INFRA_OTEL_SPOOL_DIR="${CI_INFRA_OTEL_SPOOL_DIR:-${_CI_INFRA_OTEL_DIR}/spans}"
CI_INFRA_OTEL_READY=0
export CI_INFRA_OTEL_READY

_ci_otel_disable() {
  CI_INFRA_OTEL_READY=0
  export CI_INFRA_OTEL_READY
  echo "vLLM CI OTel: tracing disabled after a helper failure" >&2 || :
  return 0
}

_ci_otel_python() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 2s python3 "$@"
  else
    python3 "$@"
  fi
}

_ci_otel_on_exit() {
  _CI_INFRA_OTEL_EXIT_STATUS=$?
  trap - 0
  if [ "${CI_INFRA_OTEL_READY:-0}" = "1" ]; then
    ci_otel_finish "${_CI_INFRA_OTEL_EXIT_STATUS}" || true
  fi
  if [ "${CI_INFRA_OTEL_READY:-0}" = "1" ]; then
    if command -v timeout >/dev/null 2>&1; then
      timeout 4s python3 "${_CI_INFRA_OTEL_DIR}/ci_otel.py" flush || true
    else
      python3 "${_CI_INFRA_OTEL_DIR}/ci_otel.py" flush || true
    fi
  fi
  exit "${_CI_INFRA_OTEL_EXIT_STATUS}"
}

ci_otel_start() {
  local command_index="$1"
  local encoded_label="$2"
  local command_label
  local context
  local trace_id
  local span_id
  local parent_span_id
  local start_ns

  command_label="$(printf '%s' "${encoded_label}" | base64 --decode 2>/dev/null)" ||
    command_label="command ${command_index}"

  if ! context="$(_ci_otel_python "${_CI_INFRA_OTEL_DIR}/ci_otel.py" new-context)"; then
    _ci_otel_disable
    return 0
  fi
  set -- ${context}
  if [ "$#" -ne 3 ]; then
    _ci_otel_disable
    return 0
  fi
  trace_id="$1"
  span_id="$2"
  parent_span_id="$3"
  [ "${parent_span_id}" = "-" ] && parent_span_id=""
  if ! start_ns="$(date +%s%N)"; then
    _ci_otel_disable
    return 0
  fi

  CI_INFRA_TRACE_ID="${trace_id}"
  CI_INFRA_COMMAND_SPAN_ID="${span_id}"
  export CI_INFRA_TRACE_ID CI_INFRA_COMMAND_SPAN_ID
  _CI_INFRA_OTEL_ACTIVE=1
  _CI_INFRA_OTEL_ACTIVE_INDEX="${command_index}"
  _CI_INFRA_OTEL_ACTIVE_LABEL="${command_label}"
  _CI_INFRA_OTEL_ACTIVE_TRACE_ID="${trace_id}"
  _CI_INFRA_OTEL_ACTIVE_SPAN_ID="${span_id}"
  _CI_INFRA_OTEL_ACTIVE_PARENT_SPAN_ID="${parent_span_id}"
  _CI_INFRA_OTEL_ACTIVE_START_NS="${start_ns}"
  return 0
}

ci_otel_finish() {
  local command_status="${1:-0}"
  local end_ns

  if [ "${_CI_INFRA_OTEL_ACTIVE:-0}" != "1" ]; then
    return 0
  fi
  _CI_INFRA_OTEL_ACTIVE=0
  CI_INFRA_TRACE_ID=""
  CI_INFRA_COMMAND_SPAN_ID=""
  export CI_INFRA_TRACE_ID CI_INFRA_COMMAND_SPAN_ID
  end_ns="$(date +%s%N 2>/dev/null)" ||
    end_ns="${_CI_INFRA_OTEL_ACTIVE_START_NS}"

  if ! _ci_otel_python "${_CI_INFRA_OTEL_DIR}/ci_otel.py" record-command \
    --trace-id "${_CI_INFRA_OTEL_ACTIVE_TRACE_ID}" \
    --span-id "${_CI_INFRA_OTEL_ACTIVE_SPAN_ID}" \
    --parent-span-id "${_CI_INFRA_OTEL_ACTIVE_PARENT_SPAN_ID}" \
    --start-ns "${_CI_INFRA_OTEL_ACTIVE_START_NS}" \
    --end-ns "${end_ns}" \
    --index "${_CI_INFRA_OTEL_ACTIVE_INDEX}" \
    --label "${_CI_INFRA_OTEL_ACTIVE_LABEL}" \
    --exit-code "${command_status}"; then
    _ci_otel_disable
  fi
  return 0
}

# Do not modify command lookup until the helpers, spool, and pytest shim are
# valid. No global Python or pytest options are changed.
if command -v python3 >/dev/null 2>&1 &&
  mkdir -p "${CI_INFRA_OTEL_SPOOL_DIR}" &&
  (
    export PYTHONPATH="${_CI_INFRA_OTEL_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
    python3 -c "import ci_otel, ci_pytest_otel" >/dev/null 2>&1
  ) &&
  sh -n "${_CI_INFRA_OTEL_DIR}/ci_pytest.sh"; then
  CI_INFRA_OTEL_REAL_PYTEST="$(command -v pytest 2>/dev/null || :)"
  export CI_INFRA_OTEL_REAL_PYTEST
  if [ -n "${CI_INFRA_OTEL_REAL_PYTEST}" ] &&
    [ -x "${CI_INFRA_OTEL_REAL_PYTEST}" ] &&
    mkdir -p "${_CI_INFRA_OTEL_DIR}/bin" &&
    ln -s "${_CI_INFRA_OTEL_DIR}/ci_pytest.sh" "${_CI_INFRA_OTEL_DIR}/bin/pytest"; then
    PATH="${_CI_INFRA_OTEL_DIR}/bin:${PATH}"
    export PATH
  fi
  CI_INFRA_OTEL_READY=1
  export CI_INFRA_OTEL_READY
  trap _ci_otel_on_exit 0
else
  echo "vLLM CI OTel: tracing disabled; test command will run normally" >&2 || :
fi

:
