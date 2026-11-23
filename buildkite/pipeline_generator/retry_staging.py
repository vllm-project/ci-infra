"""Staging only, never merged: proves in-place job retry on a throwaway
build. With TEST_RETRY_STAGING=1 the generator appends these steps to the
scoped build.

The three test jobs are copies of a real generated step (same image, docker
plugin, queue) with their commands swapped for bash that writes a result file
and then fails, times out or is SIGKILLed.
"""

import copy

TEMPLATE_KEY = "entrypoints-unit-tests"
TRUSTED = "ci-shadow"
RESULTS = "/workdir/.test-results"

# Each job writes .test-results/<step id>/<shard>/<retry count>.jsonl. The
# dirs are 0777 so the agent can delete what the container (root) created.
WRITE = (
    f"D={RESULTS}/$${{BUILDKITE_STEP_ID}}/$${{BUILDKITE_PARALLEL_JOB:-0}}"
    f' && mkdir -p "$$D" && chmod 0777 {RESULTS} "$$(dirname "$$D")" "$$D"'
    ' && echo "{\\"nodeid\\": \\"t.py::a\\", \\"outcome\\": \\"passed\\",'
    ' \\"job\\": \\"$${BUILDKITE_JOB_ID}\\"}" >> "$$D/$${BUILDKITE_RETRY_COUNT:-0}.jsonl"'
    ' && ls -la "$$D"'
)

FAIL = [
    WRITE,
    'echo "retry count $${BUILDKITE_RETRY_COUNT:-0}, job $${BUILDKITE_JOB_ID}"',
    'if [ "$${BUILDKITE_RETRY_COUNT:-0}" = 0 ]; then exit 1; fi',
    # A retry: the plugin's two reads, from inside the container.
    'echo "--- fact 9: earlier attempts\' files"',
    "mkdir -p /tmp/trs && cd /tmp/trs"
    " && (buildkite-agent artifact download --include-retried-jobs"
    ' ".test-results/$${BUILDKITE_STEP_ID}/0/*.jsonl" . || echo DOWNLOAD-FAILED)'
    ' && find . -type f -exec sh -c \'echo "== $$1"; cat "$$1"\' _ {} \\;',
    'buildkite-agent meta-data get "test-retry:$${BUILDKITE_JOB_ID}" --default NO-TAG'
    " || echo META-GET-FAILED",
    'echo "--- retry passes"',
]
PASS_ON_RETRY = (
    'if [ "$${BUILDKITE_RETRY_COUNT:-0}" != 0 ]; then echo "retry: pass"; exit 0; fi'
)
TIMEOUT = [WRITE, PASS_ON_RETRY, "sleep 600"]
KILL = [
    WRITE,
    PASS_ON_RETRY,
    "python3 -c 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'",
]


def _test_job(template: dict, key: str, commands: list, timeout: int) -> dict:
    step = copy.deepcopy(template)
    for field in ("parallelism", "retry"):
        step.pop(field, None)
    for name in ("RUNTIME_SHARD_PLAN", "RUNTIME_SHARD_PLUGIN", "PYTEST_ADDOPTS"):
        step.get("env", {}).pop(name, None)
    docker = next(
        (p for p in step["plugins"] if any(k.startswith("docker#") for k in p)), None
    )
    if docker is None:
        raise ValueError(f"TEST_RETRY_STAGING needs a docker# plugin on {TEMPLATE_KEY}")
    config = next(iter(docker.values()))
    config["mount-checkout"] = True
    config["mount-buildkite-agent"] = True
    step.update(
        key=key,
        label=f"test-retry staging: {key}",
        commands=commands,
        timeout_in_minutes=timeout,
        artifact_paths=[".test-results/*/*/*.jsonl"],
    )
    return step


def add(pipeline: dict) -> None:
    def walk(steps):
        for step in steps:
            yield from walk(step["steps"]) if "group" in step else [step]

    template = next(
        (s for s in walk(pipeline["steps"]) if s.get("key") == TEMPLATE_KEY), None
    )
    if template is None:
        raise ValueError(f"TEST_RETRY_STAGING needs {TEMPLATE_KEY} in the build")
    # Only the template; its real shards proved nothing new in 97339.
    for group in pipeline["steps"]:
        if "group" in group and template in group["steps"]:
            group["steps"].remove(template)
    pipeline["steps"] = [g for g in pipeline["steps"] if g.get("steps", [None])]
    pipeline["steps"].append(
        {
            "group": "test-retry staging",
            "steps": [
                _test_job(template, "trs-fail", FAIL, 20),
                _test_job(template, "trs-timeout", TIMEOUT, 2),
                _test_job(template, "trs-kill", KILL, 10),
                # Build 97339: a block that depends on failed steps goes
                # blocked_failed (not pressable) despite
                # allow_dependency_failure; "Run more jobs" uses [] and stays
                # pressable.
                {
                    "block": "Retry failed tests (1 of 3)",
                    "key": "trs-button-1",
                    "depends_on": [],
                },
                {
                    "key": "trs-trigger-1",
                    "trigger": TRUSTED,
                    "depends_on": ["trs-button-1"],
                    "allow_dependency_failure": True,
                    "soft_fail": True,
                    "async": False,
                    "build": {
                        "message": "test-retry staging: press 1",
                        "env": {
                            "STAGING_ROLE": "press",
                            # Single $: interpolated at pipeline-upload time to
                            # the upload job's ID. Any job ID in the source
                            # build grants access to that build's meta-data,
                            # which is all the follow-up read needs; the value
                            # is not used to pick the jobs to retry.
                            "SRC_JOB": "$BUILDKITE_JOB_ID",
                        },
                    },
                },
            ],
        }
    )
