# Pipeline Generator

Python replacement for all three Jinja templates that generate Buildkite CI pipelines for vLLM:
- `test-template-ci.j2` (full CI pipeline)
- `test-template-fastcheck.j2` (fast pre-merge checks)
- `test-template-amd.j2` (AMD-only pipeline)

## Quick Start

```bash
# CI mode (default)
python pipeline_generator.py --pipeline_mode ci

# Fastcheck mode
python pipeline_generator.py --pipeline_mode fastcheck

# AMD mode
python pipeline_generator.py --pipeline_mode amd
```


## Directory Structure

```
pipeline_generator/
├── pipeline_generator.py      # Main entry point
├── pipeline_config.py         # Configuration
├── docker_build_configs.py    # Build step configs
├── hardware_test_configs.py   # Hardware test configs
├── pyproject.toml             # Ruff & mypy config
│
├── ci/                        # CI-specific logic
│   ├── ci_pipeline.py        # Main CI orchestration
│   ├── docker_builds.py      # CI Docker builds
│   ├── docker_plugins.py     # CI Docker plugin construction
│   ├── test_step_converter.py # Convert tests to Buildkite steps
│   ├── test_filtering.py     # Which tests to run
│   ├── manual_trigger_rules.py # Blocking logic
│   ├── amd_tests.py          # AMD test group
│   ├── torch_nightly_tests.py # Torch nightly group
│   └── hardware_tests.py     # External hardware tests
│
├── fastcheck/                 # Fastcheck-specific logic
│   ├── fastcheck_pipeline.py # Main fastcheck orchestration
│   ├── docker_builds.py      # Fastcheck Docker builds
│   ├── docker_plugins.py     # Fastcheck Docker plugin construction
│   ├── test_step_converter.py # Convert tests to Buildkite steps
│   ├── test_filtering.py     # Which tests to run
│   ├── manual_trigger_rules.py # Blocking logic
│   ├── amd_tests.py          # AMD test group (Basic Correctness only)
│   └── hardware_tests.py     # Hardware tests (TPU, GH200, Intel)
│
├── data_models/               # Pydantic data models
│   ├── test_step.py          # Input from test-pipeline.yaml
│   ├── buildkite_step.py     # Output for Buildkite
│   └── docker_config.py      # Docker/K8s configs
│
├── command_builders/          # Command transformations (CI only)
│   ├── normalizer.py         # Flatten & normalize commands
│   ├── intelligent_test_selection.py # Intelligent test targeting
│   └── coverage_injection.py # Coverage injection
│
├── utils/                     # Shared utilities & constants
│   ├── constants.py          # All constants (build keys, queues, labels, etc.)
│   ├── agent_queues.py       # Agent queue selection
│   ├── command_utils.py      # Command helpers
│   └── amd_command_builder.py # AMD command formatting
│
└── tests/                     # Test suite
    ├── test_*.py             # 125 unit tests
    ├── test_integration_comprehensive.py  # 56 CI scenarios
    └── test_integration_fastcheck.py      # 8 fastcheck scenarios
```

## Main Flow

The `pipeline_generator.py` orchestrates everything:

```python
def generate(self, test_steps):
    steps = []
    
    # Build Docker images
    steps.append(generate_main_build_step(self.config))
    steps.extend(generate_cu118_build_steps(self.config))
    steps.append(generate_cpu_build_step(self.config))
    
    # Generate test steps
    steps.extend(self.generate_test_steps(test_steps))
    
    # Add special groups
    steps.append(generate_torch_nightly_group(test_steps, self.config))
    steps.append(generate_amd_group(test_steps, self.config))
    
    # Add external hardware tests
    steps.extend(generate_all_hardware_tests(self.config.branch, self.config.nightly))
    
    return steps
```

CI and Fastcheck have completely separate implementations with zero shared logic that has mode checks. This makes it easy to modify one without worrying about breaking the other.

## Where to Find Things

**For CI pipeline:**
- Main logic: `ci/ci_pipeline.py`
- Build steps: `ci/docker_builds.py`
- Test filtering: `ci/test_filtering.py` and `ci/manual_trigger_rules.py`
- AMD/Torch nightly: `ci/amd_tests.py`, `ci/torch_nightly_tests.py`

**For Fastcheck pipeline:**
- Main logic: `fastcheck/fastcheck_pipeline.py`
- Everything else in `fastcheck/` directory

**Shared code:**
- Constants: `utils/constants.py` (build keys, queues, labels, etc.)
- Data models: `data_models/`
- Config files: `*_config.py` at root

## Testing

Run unit tests:
```bash
python -m pytest tests/ -k "not integration" -v
```

Run integration tests (verifies 100% compatibility with Jinja):
```bash
# All integration tests via pytest
pytest tests/test_integration_comprehensive.py tests/test_integration_fastcheck.py

# Or run specific scenario
pytest tests/test_integration_comprehensive.py -k "coverage"
```

**Status**: CI and Fastcheck modes achieve 100% YAML compatibility with their respective Jinja templates.

## How It Works

### Input: test-pipeline.yaml

```yaml
steps:
  - label: "Basic Tests"
    commands:
      - pytest tests/basic/
    source_file_dependencies:
      - vllm/engine/
```

### Processing

1. Parse YAML into `TestStep` objects (Pydantic models)
2. For each test, decide if it should run or be blocked
3. Convert to `BuildkiteStep` with appropriate Docker plugin
4. Apply command transformations
5. Add build steps, special groups, hardware tests
6. Write final pipeline YAML

### Output: pipeline.yaml

```yaml
steps:
  - label: "Build vLLM Image"
    key: "image-build"
    # ... build configuration
  
  - label: "Basic Tests"
    depends_on: "image-build"
    agents: {queue: "gpu_1_queue"}
    plugins:
      - docker#v5.2.0:
          image: "public.ecr.aws/..."
          command: ["bash", "-xc", "cd /vllm-workspace/tests && pytest tests/basic/"]
```

## Backward Compatibility

The Python generator produces identical YAML to the Jinja template. This is verified by `test_integration_comprehensive.py`, which runs 56 test scenarios covering:

- Different branches (main vs PR)
- Run all vs selective testing
- Nightly mode
- File change detection
- Coverage injection
- Intelligent test filtering
- Multi-node/GPU configurations
- Optional tests
- And more...

All 56 scenarios must produce byte-for-byte identical YAML.

## Contributing

When making changes:

1. Write tests first (in `tests/`)
2. Make your changes
3. Run unit tests: `python -m pytest tests/ -k "not integration"`
4. Run integration tests: `python tests/test_integration_comprehensive.py`
5. Both must pass before merging

The integration test is non-negotiable - it ensures we don't break existing pipelines.
