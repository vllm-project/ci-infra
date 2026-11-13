import click
from pipeline_generator import PipelineGenerator


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
    pipeline_generator = PipelineGenerator(pipeline_config_path, output_file_path)
    if images_only:
        pipeline_generator.generate_images_only()
    else:
        pipeline_generator.generate()


if __name__ == "__main__":
    main()
