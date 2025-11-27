import click
import yaml
from pipeline_generator import PipelineGenerator

@click.command()
@click.option("--pipeline_config_path", type=click.Path(exists=True), help="Path to the pipeline config file")
@click.option("--output_file_path", type=click.Path(), help="Path to the output file")
def main(pipeline_config_path, output_file_path):
    pipeline_generator = PipelineGenerator(pipeline_config_path, output_file_path)
    pipeline = pipeline_generator.generate()
    with open(output_file_path, "w") as f:
        yaml.dump(pipeline, f)

if __name__ == "__main__":
    main()
