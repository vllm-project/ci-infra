import os

import click
from pipeline_generator import PipelineGenerator


NOTIFY_TEST_PIPELINE = """# Throwaway test 2: step-level github_commit_status inside a group (as in
# production), timing of the red status, and a retried shard.
steps:
  - group: "notify test group"
    key: notify-test-group
    steps:
      # G: shard 1 fails at once, the others sleep 90 s. Red at ~0 s or ~90 s?
      - label: "G sharded, early failure %n"
        key: notify-test-g
        parallelism: 3
        agents: {queue: small_cpu_queue_premerge}
        command: 'if [ "$$BUILDKITE_PARALLEL_JOB" = "1" ]; then exit 1; fi; sleep 90'
        notify:
          - github_commit_status: {context: "ci/step: G sharded, early failure"}
            if: step.outcome == "hard_failed"
      # H: one failing job inside the group. Expect one red status.
      - label: "H failure in group"
        key: notify-test-h
        agents: {queue: small_cpu_queue_premerge}
        command: "exit 1"
        notify:
          - github_commit_status: {context: "ci/step: H failure in group"}
            if: step.outcome == "hard_failed"
      # I: 3 shards; shard 1 fails once then passes on its automatic retry. Expect no status.
      - label: "I sharded, retried shard %n"
        key: notify-test-i
        parallelism: 3
        agents: {queue: small_cpu_queue_premerge}
        command: '[ "$$BUILDKITE_PARALLEL_JOB" != "1" ] || [ "$${BUILDKITE_RETRY_COUNT:-0}" -ge 1 ]'
        retry: {automatic: [{exit_status: 1, limit: 1}]}
        notify:
          - github_commit_status: {context: "ci/step: I sharded, retried shard"}
            if: step.outcome == "hard_failed"
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
