from typing import Any, Dict, Sequence

# signal_reason of a job lost to infrastructure, as REST reports it, e.g.
# failed/-1/stack_error. A job lost without one is failed/-1/None; exit 255
# counts only with one of these, since a test can exit 255 itself.
INFRA_SIGNAL_REASONS = (
    "stack_error",
    "agent_stop",
    "agent_refused",
    "agent_incompatible",
    "process_run_error",
)


def is_infra_failure(job: Dict[str, Any]) -> bool:
    """Timeouts (failed/-1/cancel or timed_out) and test failures are not."""
    if job.get("state") != "failed":
        return False
    reason = job.get("signal_reason")
    if job.get("exit_status") == -1:
        return reason is None or reason in INFRA_SIGNAL_REASONS
    return job.get("exit_status") == 255 and reason in INFRA_SIGNAL_REASONS


# Job states that say something about the queue; canceled jobs are mostly
# superseded builds, and broken or skipped ones never became runnable.
HISTORY_STATES = ("passed", "failed", "timed_out", "expired")


def infra_rate(jobs: Sequence[Dict[str, Any]]) -> float:
    if not jobs:
        return 0.0
    return sum(1 for job in jobs if is_infra_failure(job)) / len(jobs)


def infra_failure_rate(jobs: Sequence[Dict[str, Any]], above: float) -> bool:
    return infra_rate(jobs) > above


RULES = {
    "infra_failure_rate": infra_failure_rate,
}
