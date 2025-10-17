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
├── config.py                  # Configuration
├── build_config.py            # Build step configs
├── hardware_config.py         # Hardware test configs
│
├── models/                    # Data models
│   ├── test_step.py          # Input from test-pipeline.yaml
│   ├── buildkite_step.py     # Output for Buildkite
│   └── docker_config.py      # Docker/K8s configs
│
├── steps/                     # Step generators (organized by type)
│   ├── build_steps.py        # Docker image builds
│   ├── test_steps.py         # Regular test steps
│   ├── hardware_steps.py     # External hardware (Neuron, TPU, Intel, etc.)
│   └── group_steps.py        # Special groups (AMD, Torch Nightly)
│
├── transformers/              # Command transformation pipeline
│   ├── base.py               # Base transformer interface
│   ├── normalizer.py         # Flatten & normalize commands
│   ├── test_targeting.py     # Intelligent test targeting
│   └── coverage.py           # Coverage injection
│
├── selection/                 # Test selection logic
│   ├── filtering.py          # Should run/skip decisions
│   └── blocking.py           # Block step (manual trigger) logic
│
├── docker/                    # Docker plugin builders
│   └── plugin_builder.py     # Builds Docker/K8s plugins
│
├── utils/                     # Utilities
│   ├── constants.py          # Enums, constants
│   ├── agents.py             # Agent queue selection
│   └── commands.py           # Command helpers
│
└── tests/                     # Test suite
    ├── test_*.py             # 125 unit tests
    └── test_integration_comprehensive.py  # 56 integration scenarios
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

This structure keeps the high-level flow readable while organizing details into focused modules.

## Command Transformation Pipeline

One key improvement over Jinja is making command transformations explicit. When converting a test step to a Buildkite step, commands go through:

1. **Flatten** - Multi-node commands (list of lists) become single list
2. **Normalize** - Remove backslashes from YAML line continuations
3. **Test Targeting** - If only test files changed, run just those tests
4. **Coverage** - Inject coverage collection if enabled
5. **Join** - Combine into single command string

This happens in `docker/plugin_builder.py::build_docker_command()`. Adding a new transformation is straightforward - just create a new transformer in `transformers/`.

## Where to Find Things

Coming from the Jinja template? Here's where logic moved:

**Build steps** (lines 14-179 in Jinja)  
Now in: `steps/build_steps.py`

**Test step conversion** (lines 180-550 in Jinja)  
Now in: `steps/test_steps.py` and `docker/plugin_builder.py`

**Test selection/blocking** (lines 508-530, 600-621 in Jinja)  
Now in: `selection/blocking.py` and `selection/filtering.py`

**Coverage injection** (lines 33-158 in Jinja)  
Now in: `transformers/coverage.py`

**Intelligent test targeting** (lines 20-158 in Jinja)  
Now in: `transformers/test_targeting.py` and `selection/filtering.py`

**AMD tests** (lines 662-727 in Jinja)  
Now in: `steps/group_steps.py::generate_amd_group()`

**Torch Nightly tests** (lines 579-658 in Jinja)  
Now in: `steps/group_steps.py::generate_torch_nightly_group()`

**Hardware tests** (lines 729-863 in Jinja)  
Now in: `steps/hardware_steps.py`

## Common Tasks

### Adding a new build variant
Edit `steps/build_steps.py`. Follow the pattern of existing build steps.

### Adding command transformation logic
Create a new transformer in `transformers/`:

```python
from .base import CommandTransformer

class MyTransformer(CommandTransformer):
    def transform(self, commands, test_step, config):
        if should_apply():
            return modified_commands
        return None  # Falls through to next transformer
```

Then use it in `docker/plugin_builder.py::build_docker_command()`.

### Adding a new hardware platform
Add configuration to `hardware_config.py` and generation logic to `steps/hardware_steps.py`.

### Adjusting Docker plugin configuration
Look in `docker/plugin_builder.py` for the plugin builder logic, or `models/docker_config.py` for the config dataclasses.

### Changing test selection rules
- Run/skip decisions: `selection/filtering.py`
- Block (manual trigger) decisions: `selection/blocking.py`

## Testing

Run unit tests:
```bash
python -m pytest tests/ -k "not integration" -v
```

Run integration tests (verifies 100% compatibility with Jinja):
```bash
# CI mode (56 scenarios)
python tests/test_integration_comprehensive.py

# Fastcheck mode (8 scenarios)
python tests/test_integration_fastcheck.py

# AMD mode (not yet implemented)
python tests/test_integration_amd.py
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
