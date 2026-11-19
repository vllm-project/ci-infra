from buildkite_step import BuildkiteCommandStep, BuildkiteGroupStep

FAILED_ONLY = 'step.outcome == "hard_failed"'


def _step(label, **fields):
    return BuildkiteCommandStep(label=label, key="k", commands=["true"], **fields)


def test_a_sharded_step_reports_one_status_named_without_its_shard_suffix():
    step = _step(":nvidia: (L4) PyTorch Compilation Passes Shard %N/%t", parallelism=2)

    assert step.dict(exclude_none=True)["notify"] == [
        {
            "github_commit_status": {
                "context": "ci/step: (L4) PyTorch Compilation Passes"
            },
            "if": FAILED_ONLY,
        }
    ]


def test_every_leading_emoji_is_dropped_and_the_rest_of_the_label_kept():
    step = _step(":amd: :docker: Build AMD image :rocm:")

    status = step.dict()["notify"][0]["github_commit_status"]

    assert status["context"] == "ci/step: Build AMD image :rocm:"


def test_steps_inside_a_group_carry_their_status_when_the_group_is_written():
    group = BuildkiteGroupStep(group="Kernels", steps=[_step("Kernels Shard %N/%t")])

    written = group.dict(exclude_none=True)["steps"][0]

    assert written["notify"][0]["github_commit_status"]["context"] == "ci/step: Kernels"
    assert written["notify"][0]["if"] == FAILED_ONLY
