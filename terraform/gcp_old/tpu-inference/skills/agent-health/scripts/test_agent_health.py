from __future__ import annotations

import datetime as dt

import agent_health as ah

NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)


def iso(hours_ago: float) -> str:
    moment = NOW - dt.timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_agent(
    name, queue="cpu_64_core", connected_hours=48.0, job=None, hostname=None
):
    return {
        "id": f"id-{name}",
        "name": name,
        "hostname": hostname or name,
        "connected_at": iso(connected_hours),
        "connection_state": "connected",
        "paused": False,
        "job": job,
        "meta_data": [f"queue={queue}"],
    }


def make_job(agent, state, finished_hours_ago=1.0, duration_seconds=600):
    finished = NOW - dt.timedelta(hours=finished_hours_ago)
    started = finished - dt.timedelta(seconds=duration_seconds)
    return {
        "_agent_name": agent,
        "state": state,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "log_url": f"{ah.API_ROOT}/jobs/{agent}-{finished_hours_ago}/log",
    }


def classify_names(agents, jobs, window=24.0):
    stats = ah.build_stats(agents, jobs, window, NOW)
    return {entry.name: entry.reason for entry in ah.classify(stats)}


def resolve_names(agents, jobs, logs, window=24.0):
    """Classify, then run log confirmation against a canned log per agent."""
    stats = ah.build_stats(agents, jobs, window, NOW)
    flagged = ah.classify(stats)
    for entry in flagged:
        ah.confirm_disk_full(
            "token",
            entry,
            fetch=lambda _token, job: logs.get(job["_agent_name"], ""),
        )
    return {entry.name: entry.reason for entry in flagged}, stats, flagged


# -- disk-full ------------------------------------------------------------

ENOSPC_DOCKER = (
    "failed to copy: httpReadSeeker: failed open: "
    "write /var/lib/docker/tmp/x: no space left on device\n"
)
ENOSPC_MODEL = (
    "Traceback (most recent call last):\n"
    "  File huggingface_hub/file_download.py, line 1, in http_get\n"
    "OSError: [Errno 28] No space left on device\n"
)
ENOSPC_HF_TRANSFER = "Error: Not enough free disk space to download the model\n"
FLAKY_LOG = "FAILED tests/test_attention.py::test_paged - AssertionError\n"


def test_docker_pull_enospc_is_disk_full():
    agents = [make_agent("bad"), make_agent("good")]
    jobs = [make_job("bad", "failed", i, duration_seconds=45) for i in range(1, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    reasons, _, _ = resolve_names(agents, jobs, {"bad": ENOSPC_DOCKER})
    assert reasons == {"bad": ah.REASON_DISK_FULL}


def test_slow_model_download_enospc_is_still_disk_full():
    """A model download churns for minutes before it runs out of room, so
    failure timing must not gate detection."""
    agents = [make_agent("bad"), make_agent("good")]
    jobs = [make_job("bad", "failed", i, duration_seconds=1800) for i in range(1, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    reasons, _, _ = resolve_names(agents, jobs, {"bad": ENOSPC_MODEL})
    assert reasons == {"bad": ah.REASON_DISK_FULL}


def test_hf_transfer_wording_is_recognised():
    agents = [make_agent("bad"), make_agent("good")]
    jobs = [make_job("bad", "failed", i, duration_seconds=1200) for i in range(1, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    reasons, _, _ = resolve_names(agents, jobs, {"bad": ENOSPC_HF_TRANSFER})
    assert reasons == {"bad": ah.REASON_DISK_FULL}


def test_failures_without_enospc_are_not_actionable():
    """Flaky tests look identical on the counters; only the log separates them."""
    agents = [make_agent("flaky"), make_agent("good")]
    jobs = [make_job("flaky", "failed", i, duration_seconds=900) for i in range(1, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    reasons, _, flagged = resolve_names(agents, jobs, {"flaky": FLAKY_LOG})
    assert reasons == {"flaky": ah.REASON_HIGH_FAILURE}
    assert not [e for e in flagged if e.reason in ah.ACTIONABLE_REASONS]


def test_unreachable_logs_do_not_convict():
    agents = [make_agent("bad"), make_agent("good")]
    jobs = [make_job("bad", "failed", i, duration_seconds=45) for i in range(1, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    reasons, _, _ = resolve_names(agents, jobs, {})
    assert reasons == {"bad": ah.REASON_HIGH_FAILURE}


def test_a_single_failure_is_noise():
    agents = [make_agent("bad"), make_agent("good")]
    jobs = [make_job("bad", "failed", 1, duration_seconds=20)]
    jobs += [make_job("bad", "passed", i) for i in range(2, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    assert classify_names(agents, jobs) == {}


def test_log_check_reads_the_newest_failures_first():
    """The oldest failure may predate the disk filling up."""
    agents = [make_agent("bad"), make_agent("good")]
    jobs = [make_job("bad", "failed", i, duration_seconds=45) for i in range(1, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    stats = ah.build_stats(agents, jobs, 24.0, NOW)
    entry = next(e for e in stats if e.name == "bad")
    assert len(entry.recent_failures) == ah.MAX_LOGS_PER_AGENT
    assert (
        entry.recent_failures[0]["finished_at"]
        > entry.recent_failures[-1]["finished_at"]
    )


def test_find_disk_full_marker_returns_context():
    excerpt = ah.find_disk_full_marker(ENOSPC_MODEL)
    assert "Errno 28" in excerpt
    assert "\n" not in excerpt


def test_find_disk_full_marker_ignores_ordinary_logs():
    assert ah.find_disk_full_marker(FLAKY_LOG) == ""


# -- silent wedge ---------------------------------------------------------


def test_idle_agent_in_busy_queue_is_wedged():
    agents = [make_agent("wedged"), make_agent("busy")]
    jobs = [make_job("busy", "passed", i) for i in range(1, 9)]
    assert classify_names(agents, jobs) == {"wedged": ah.REASON_SILENT_WEDGE}


def test_quiet_queue_flags_nobody():
    """An overnight lull must not flag the whole fleet."""
    agents = [make_agent("a"), make_agent("b"), make_agent("c")]
    assert classify_names(agents, []) == {}


def test_freshly_booted_agent_is_not_wedged():
    agents = [make_agent("new", connected_hours=0.5), make_agent("busy")]
    jobs = [make_job("busy", "passed", i) for i in range(1, 9)]
    assert classify_names(agents, jobs) == {}


def test_jobs_outside_the_window_do_not_count():
    agents = [make_agent("stale"), make_agent("busy")]
    jobs = [make_job("stale", "passed", 40)]
    jobs += [make_job("busy", "passed", i) for i in range(1, 9)]
    assert classify_names(agents, jobs) == {"stale": ah.REASON_SILENT_WEDGE}


def test_queues_are_scored_independently():
    """A busy cpu queue must not make a genuinely quiet tpu queue look wedged."""
    agents = [make_agent("cpu-0", "cpu"), make_agent("tpu-0", "tpu_v6e_1")]
    jobs = [make_job("cpu-0", "passed", i) for i in range(1, 9)]
    assert classify_names(agents, jobs) == {}


# -- guards ---------------------------------------------------------------


def test_queue_guard_blocks_a_broadly_broken_queue():
    agents = [make_agent(f"a{i}", "cpu") for i in range(4)]
    jobs = []
    for i in range(4):
        jobs += [
            make_job(f"a{i}", "failed", h, duration_seconds=30) for h in range(1, 9)
        ]
    logs = {f"a{i}": ENOSPC_DOCKER for i in range(4)}
    _, stats, flagged = resolve_names(agents, jobs, logs)
    degraded = [e for e in flagged if e.reason in ah.ACTIONABLE_REASONS]
    assert len(degraded) == 4
    assert ah.queue_guard(degraded, stats), "4/4 degraded should block"


def test_queue_guard_never_blocks_on_one_agent_however_small_the_queue():
    agents = [make_agent("a0", "cpu"), make_agent("a1", "cpu")]
    jobs = [make_job("a0", "failed", h, duration_seconds=30) for h in range(1, 9)]
    jobs += [make_job("a1", "passed", h) for h in range(1, 9)]
    _, stats, flagged = resolve_names(agents, jobs, {"a0": ENOSPC_DOCKER})
    degraded = [e for e in flagged if e.reason in ah.ACTIONABLE_REASONS]
    assert [e.name for e in degraded] == ["a0"]
    assert ah.queue_guard(degraded, stats) == []


def test_queue_guard_allows_a_single_bad_agent():
    agents = [make_agent(f"a{i}", "cpu") for i in range(8)]
    jobs = [make_job("a0", "failed", h, duration_seconds=30) for h in range(1, 9)]
    for i in range(1, 8):
        jobs += [make_job(f"a{i}", "passed", h) for h in range(1, 9)]
    _, stats, flagged = resolve_names(agents, jobs, {"a0": ENOSPC_DOCKER})
    degraded = [e for e in flagged if e.reason in ah.ACTIONABLE_REASONS]
    assert [e.name for e in degraded] == ["a0"]
    assert ah.queue_guard(degraded, stats) == []


# -- terraform mapping ----------------------------------------------------


STATE = {
    "resources": [
        {
            "mode": "managed",
            "type": "google_compute_instance",
            "name": "ci_cpu_64_core",
            "module": "module.ci_cpu_64_core_zone_c",
            "instances": [
                {"index_key": 0, "attributes": {"name": "vllm-ci-cpu-64-core-0"}},
                {"index_key": 6, "attributes": {"name": "vllm-ci-cpu-64-core-6"}},
            ],
        },
        {
            "mode": "data",
            "type": "google_compute_instance",
            "name": "decoy",
            "module": "module.other",
            "instances": [
                {"index_key": 0, "attributes": {"name": "vllm-ci-cpu-64-core-6"}}
            ],
        },
    ]
}


def test_find_in_state_maps_name_to_module_and_index():
    assert ah.find_in_state(STATE, ["vllm-ci-cpu-64-core-6"]) == (
        "module.ci_cpu_64_core_zone_c",
        6,
    )


def test_find_in_state_falls_back_to_hostname():
    assert ah.find_in_state(STATE, ["legacy-agent-name", "vllm-ci-cpu-64-core-0"]) == (
        "module.ci_cpu_64_core_zone_c",
        0,
    )


def test_find_in_state_returns_none_for_unknown_name():
    assert ah.find_in_state(STATE, ["nope"]) is None


# -- helpers and reporting ------------------------------------------------


def test_agent_queue_reads_meta_data():
    assert ah.agent_queue(make_agent("x", "tpu_v7x_8")) == "tpu_v7x_8"


def test_agent_queue_falls_back():
    assert ah.agent_queue({"queue": "cpu"}) == "cpu"
    assert ah.agent_queue({}) == "(none)"


def test_hours_since_handles_missing_and_bad_values():
    assert ah.hours_since(None, NOW) is None
    assert ah.hours_since("not a date", NOW) is None
    assert ah.hours_since(iso(3), NOW) == 3.0


def test_report_separates_actionable_from_review():
    agents = [make_agent("full"), make_agent("flaky"), make_agent("good")]
    jobs = [make_job("full", "failed", i, duration_seconds=45) for i in range(1, 9)]
    jobs += [make_job("flaky", "failed", i, duration_seconds=900) for i in range(1, 9)]
    jobs += [make_job("good", "passed", i) for i in range(1, 9)]
    _, stats, flagged = resolve_names(
        agents, jobs, {"full": ENOSPC_DOCKER, "flaky": FLAKY_LOG}
    )
    payload = ah.report(flagged, stats, NOW)
    assert payload["agents_scanned"] == 3
    assert [item["agent"] for item in payload["degraded"]] == ["full"]
    assert [item["agent"] for item in payload["needs_review"]] == ["flaky"]
    assert "8/8 jobs failed" in payload["degraded"][0]["evidence"]
    assert payload["blocked_queues"] == []


# -- disk replacement policy ----------------------------------------------


def test_disk_full_takes_the_data_disk_with_it():
    """The persist disk survives an instance-only replace, so it must go too."""
    assert ah.replace_disks_for(ah.REASON_DISK_FULL, None) is True


def test_wedge_keeps_the_data_disk():
    assert ah.replace_disks_for(ah.REASON_SILENT_WEDGE, None) is False


def test_explicit_override_wins_either_way():
    assert ah.replace_disks_for(ah.REASON_DISK_FULL, False) is False
    assert ah.replace_disks_for(ah.REASON_SILENT_WEDGE, True) is True
