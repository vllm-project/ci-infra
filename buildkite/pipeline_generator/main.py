import os
import subprocess

import click
from pipeline_generator import PipelineGenerator

GENERATOR_ERROR_ANNOTATION_CONTEXT = "pipeline-generator-error"


def annotate_generation_failure(message: str) -> None:
    """Record a generation failure as a build annotation for CI tooling to read."""
    if os.getenv("BUILDKITE") != "true":
        return
    subprocess.run(
        [
            "buildkite-agent",
            "annotate",
            message,
            "--style",
            "error",
            "--context",
            GENERATOR_ERROR_ANNOTATION_CONTEXT,
        ],
        check=False,
    )


@click.command()
@click.option(
    "--pipeline_config_path",
    type=click.Path(exists=True),
    help="Path to the pipeline config file",
)
@click.option("--output_file_path", type=click.Path(), help="Path to the output file")
def main(pipeline_config_path, output_file_path):
    pipeline_generator = PipelineGenerator(pipeline_config_path, output_file_path)
    try:
        pipeline_generator.generate()
    except Exception as error:
        annotate_generation_failure(f"Pipeline generation failed: {error}")
        raise


if __name__ == "__main__":
    main()
