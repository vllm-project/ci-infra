import os

import click
from pipeline_generator import PipelineGenerator


NOTIFY_TEST_PIPELINE = """# Throwaway test: does a step-level github_commit_status notify give one status
# per step (parallel jobs folded), only for hard-failed steps?
# Every step runs on the small CPU queue, no docker, writes no files.
steps:
  # A: 3 shards, shard 2 fails. Expect ONE red status "ci/step: A sharded, one shard fails".
  - label: "A sharded, one shard fails %n"
    key: notify-test-a
    parallelism: 3
    agents: {queue: small_cpu_queue_premerge}
    command: '[ "$$BUILDKITE_PARALLEL_JOB" != "1" ]'
    notify:
      - github_commit_status: {context: "ci/step: A sharded, one shard fails"}
        if: step.outcome == "hard_failed"

  # B: fails on the first attempt, passes on its automatic retry. Expect NO status.
  - label: "B fails then passes on retry"
    key: notify-test-b
    agents: {queue: small_cpu_queue_premerge}
    command: '[ "$${BUILDKITE_RETRY_COUNT:-0}" -ge 1 ]'
    retry: {automatic: [{exit_status: 1, limit: 1}]}
    notify:
      - github_commit_status: {context: "ci/step: B retried"}
        if: step.outcome == "hard_failed"

  # C: soft-fail. Expect NO status.
  - label: "C soft fail"
    key: notify-test-c
    agents: {queue: small_cpu_queue_premerge}
    command: "exit 1"
    soft_fail: true
    notify:
      - github_commit_status: {context: "ci/step: C soft fail"}
        if: step.outcome == "hard_failed"

  # D: passes. Expect NO status.
  - label: "D passes"
    key: notify-test-d
    agents: {queue: small_cpu_queue_premerge}
    command: "true"
    notify:
      - github_commit_status: {context: "ci/step: D passes"}
        if: step.outcome == "hard_failed"

  # E: control, one job that fails. Expect ONE red status (proves notify works at all).
  - label: "E plain failure"
    key: notify-test-e
    agents: {queue: small_cpu_queue_premerge}
    command: "exit 1"
    notify:
      - github_commit_status: {context: "ci/step: E plain failure"}
        if: step.outcome == "hard_failed"

  # F: 3 shards, all pass, NO filter. Counting the writes to its context shows
  # whether Buildkite posts per step or per job.
  - label: "F sharded, unfiltered %n"
    key: notify-test-f
    parallelism: 3
    agents: {queue: small_cpu_queue_premerge}
    command: "true"
    notify:
      - github_commit_status: {context: "ci/step: F sharded unfiltered"}
"""


@click.command()
@click.option(
    "--pipeline_config_path",
    type=click.Path(exists=True),
    help="Path to the pipeline config file",
)
@click.option(
    "--output_file_path",
    type=click.Path(),
    default=".buildkite/pipeline.yaml",
    show_default=True,
    help="Path to the output file",
)
@click.option(
    "--images_only",
    is_flag=True,
    help=(
        "Only upload the image builds that start before the test selector, "
        "for a step that runs beside bootstrap (VLLM_CI_IMAGES_STEP)."
    ),
)
def main(pipeline_config_path, output_file_path, images_only):
    # Throwaway staging branch, never merged: test step-level commit statuses.
    if os.getenv("VLLM_CI_NOTIFY_TEST") == "1":
        if not images_only:
            with open(output_file_path, "w") as f:
                f.write(NOTIFY_TEST_PIPELINE)
        return
    pipeline_generator = PipelineGenerator(pipeline_config_path, output_file_path)
    if images_only:
        pipeline_generator.generate_images_only()
    else:
        pipeline_generator.generate()


if __name__ == "__main__":
    main()
