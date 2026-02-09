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
def main(pipeline_config_path, output_file_path, queue_routing_file_path):
    queue_routing_dict = None
    if queue_routing_file_path:
        with open(queue_routing_file_path, "r") as f:
            queue_routing_dict = json.load(f)
    pipeline_generator = PipelineGenerator(
        pipeline_config_path, output_file_path, queue_routing=queue_routing_dict
    )
    pipeline_generator.generate()


if __name__ == "__main__":
    main()
