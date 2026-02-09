import json

import click
from pipeline_generator import PipelineGenerator


@click.command()
@click.option(
    "--pipeline_config_path",
    type=click.Path(exists=True),
    help="Path to the pipeline config file",
)
@click.option("--output_file_path", type=click.Path(), help="Path to the output file")
@click.option(
    "--queue_routing_file_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to a JSON file mapping original queue names to replacement queue names",
)
@click.option(
    "--filter_file_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to a JSON file specifying step filter criteria (e.g., queue, device)",
)
def main(pipeline_config_path, output_file_path, queue_routing_file_path, filter_file_path):
    queue_routing_dict = None
    if queue_routing_file_path:
        with open(queue_routing_file_path, "r") as f:
            queue_routing_dict = json.load(f)
    step_filter = None
    if filter_file_path:
        with open(filter_file_path, "r") as f:
            step_filter = json.load(f)
    pipeline_generator = PipelineGenerator(
        pipeline_config_path, output_file_path, queue_routing=queue_routing_dict, step_filter=step_filter
    )
    pipeline_generator.generate()


if __name__ == "__main__":
    main()
