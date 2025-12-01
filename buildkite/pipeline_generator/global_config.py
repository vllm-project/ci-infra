from typing import TypedDict, List, Dict, Optional
import yaml
import os

class GlobalConfig(TypedDict):
    name: str
    job_dirs: List[str]
    registries: str
    repositories: Dict[str, str]
    branch: str
    commit: str
    pull_request: Optional[str] = None
    run_all_patterns: Optional[List[str]] = None
    run_all_exclude_patterns: Optional[List[str]] = None
    nightly: Optional[str] = "0"
    run_all: Optional[str] = "0"

config = None

def init_global_config(pipeline_config_path: str):
    global config
    if config:
        return
    pipeline_config = yaml.safe_load(open(pipeline_config_path, "r"))

    # validate pipeline config
    _validate_pipeline_config(pipeline_config)

    config = GlobalConfig(
        name=pipeline_config["name"],
        job_dirs=pipeline_config["job_dirs"],
        registries=pipeline_config["registries"],
        repositories=pipeline_config["repositories"],
        branch=os.getenv("BUILDKITE_BRANCH"),
        commit=os.getenv("BUILDKITE_COMMIT"),
        pull_request=os.getenv("BUILDKITE_PULL_REQUEST"),
        run_all_patterns=pipeline_config.get("run_all_patterns", None),
        run_all_exclude_patterns=pipeline_config.get("run_all_exclude_patterns", None),
        nightly=os.getenv("NIGHTLY", "0"),
        run_all=os.getenv("RUN_ALL", "0"),
    )

def get_global_config():
    global config
    if not config:
        raise ValueError("Global config not initialized")
    return config

def _validate_pipeline_config(pipeline_config: Dict):
    if not pipeline_config["name"]:
        raise ValueError("Pipeline name is required")
    if not pipeline_config["job_dirs"]:
        raise ValueError("Job directories are required")
    if not pipeline_config["registries"]:
        raise ValueError("Registries are required")
    if not pipeline_config["repositories"]:
        raise ValueError("Repositories are required")
    for job_dir in pipeline_config["job_dirs"]:
        if not os.path.exists(job_dir):
            raise ValueError(f"Job directory not found: {job_dir}")
