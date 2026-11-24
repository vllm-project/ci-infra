from buildkite_step import BuildkiteCommandStep, BuildkiteGroupStep

FINAL_RESULT = (
    'step.outcome == "passed" || step.outcome == "hard_failed"'
    ' || step.outcome == "errored"'
)


def _step(label, **fields):
    return BuildkiteCommandStep(label=label, key="k", commands=["true"], **fields)


def _context(step):
    return step.dict()["notify"][0]["github_commit_status"]["context"]


def test_a_sharded_step_reports_one_status_named_without_its_shard_suffix():
    step = _step(":nvidia: (L4) PyTorch Compilation Passes Shard %N/%t", parallelism=2)

    assert step.dict(exclude_none=True)["notify"] == [
        {
            "github_commit_status": {
                "context": "ci/step: (L4) PyTorch Compilation Passes"
            },
            "if": FINAL_RESULT,
        }
    ]


def test_a_fixed_shard_count_label_loses_its_job_placeholder_too():
    assert _context(_step(":computer: (CPU) Multimodal Processor Shard %N")) == (
        "ci/step: (CPU) Multimodal Processor"
    )
    assert _context(_step("Distributed Tests %N")) == "ci/step: Distributed Tests"


def test_emoji_codes_are_dropped_because_github_shows_them_as_text():
    assert _context(_step(":amd: :docker: Build AMD image :rocm:")) == (
        "ci/step: Build AMD image"
    )


def test_steps_inside_a_group_carry_their_status_when_the_group_is_written():
    group = BuildkiteGroupStep(group="Kernels", steps=[_step("Kernels Shard %N/%t")])

    written = group.dict(exclude_none=True)["steps"][0]

    assert written["notify"][0]["github_commit_status"]["context"] == "ci/step: Kernels"
    assert written["notify"][0]["if"] == FINAL_RESULT
