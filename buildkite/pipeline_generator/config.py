"""Configuration for pipeline generation."""
import re
from .utils.constants import PipelineMode


class PipelineGeneratorConfig:
    """Configuration for the pipeline generator."""
    
    def __init__(
        self,
        container_registry: str,
        container_registry_repo: str,
        commit: str,
        branch: str,
        list_file_diff: list,
        run_all: bool = False,
        nightly: bool = False,
        mirror_hw: str = "amdexperimental",
        fail_fast: bool = False,
        vllm_use_precompiled: str = "0",
        cov_enabled: bool = False,
        vllm_ci_branch: str = "main",
        pipeline_mode: PipelineMode = PipelineMode.CI,
    ):
        self.run_all = run_all
        self.nightly = nightly
        self.list_file_diff = list_file_diff
        self.container_registry = container_registry
        self.container_registry_repo = container_registry_repo
        self.commit = commit
        self.branch = branch
        self.mirror_hw = mirror_hw
        self.fail_fast = fail_fast
        self.vllm_use_precompiled = vllm_use_precompiled
        self.cov_enabled = cov_enabled
        self.vllm_ci_branch = vllm_ci_branch
        self.pipeline_mode = pipeline_mode

    @property
    def container_image(self):
        """Get the main CUDA container image."""
        # Fastcheck and AMD always use test-repo
        if self.pipeline_mode in [PipelineMode.FASTCHECK, PipelineMode.AMD]:
            return "public.ecr.aws/q9t5s3a7/vllm-ci-test-repo:$BUILDKITE_COMMIT"
        repo_suffix = "postmerge" if self.branch == "main" else "test"
        return "public.ecr.aws/q9t5s3a7/vllm-ci-{}-repo:$BUILDKITE_COMMIT".format(repo_suffix)
    
    @property
    def container_image_torch_nightly(self):
        """Get the torch nightly container image."""
        repo_suffix = "postmerge" if self.branch == "main" else "test"
        return f"public.ecr.aws/q9t5s3a7/vllm-ci-{repo_suffix}-repo:$BUILDKITE_COMMIT-torch-nightly"
    
    @property
    def container_image_cu118(self):
        """Get the CUDA 11.8 container image."""
        repo_suffix = "postmerge" if self.branch == "main" else "test"
        return f"public.ecr.aws/q9t5s3a7/vllm-ci-{repo_suffix}-repo:$BUILDKITE_COMMIT-cu118"
    
    @property
    def container_image_cpu(self):
        """Get the CPU container image."""
        repo_suffix = "postmerge" if self.branch == "main" else "test"
        return f"public.ecr.aws/q9t5s3a7/vllm-ci-{repo_suffix}-repo:$BUILDKITE_COMMIT-cpu"
    
    @property
    def container_image_amd(self):
        """Get the AMD container image."""
        return "rocm/vllm-ci:$BUILDKITE_COMMIT"
    
    def validate(self):
        """Validate the configuration."""
        # Check if commit is a valid Git commit hash
        pattern = r"^[0-9a-f]{40}$"
        if not re.match(pattern, self.commit):
            raise ValueError(f"Commit {self.commit} is not a valid Git commit hash")


