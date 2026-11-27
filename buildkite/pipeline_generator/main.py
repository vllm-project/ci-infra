import sys

import click
from pipeline_generator import PipelineGenerator
import steps_cli


class _Main(click.Command):
    """`pipeline-generator [options]` generates the pipeline;
    `pipeline-generator explain|lint ...` runs the test_areas tools
    (steps_cli.py)."""

    def main(self, args=None, **kwargs):
        argv = sys.argv[1:] if args is None else list(args)
        if argv and argv[0] in steps_cli.COMMANDS:
            kwargs.setdefault("prog_name", "pipeline-generator")
            return steps_cli.cli.main(args=argv, **kwargs)
        return super().main(args=argv, **kwargs)


@click.command(cls=_Main)
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
    pipeline_generator = PipelineGenerator(pipeline_config_path, output_file_path)
    if images_only:
        pipeline_generator.generate_images_only()
    else:
        pipeline_generator.generate()


if __name__ == "__main__":
    main()
