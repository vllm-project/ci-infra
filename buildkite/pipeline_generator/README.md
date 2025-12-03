# Pipeline Generator

Simple Python replacement for Jinja templates that generate Buildkite CI pipelines for vLLM.

## Quick Start

```bash
# CI mode (default)
python -m pipeline_generator --pipeline_mode ci

# Fastcheck mode
python -m pipeline_generator --pipeline_mode fastcheck

# AMD mode
python -m pipeline_generator --pipeline_mode amd
```

## Architecture

Simple, readable code matching Jinja template complexity:

```
pipeline_generator/
├── pipeline_generator.py     # Main entry point
├── config.py                  # All constants and configuration
├── models.py                  # TestStep input model only
│
├── modes/                     # One file per mode (simple dict generation)
│   ├── ci.py                 # CI pipeline (~630 lines)
│   ├── fastcheck.py          # Fastcheck pipeline (~520 lines)
│   └── amd.py                # AMD pipeline (~60 lines)
│
└── helpers/                   # Simple utilities
    ├── builds.py             # Build step dicts
    ├── commands.py           # Command normalization
    ├── coverage.py           # Coverage injection (complex)
    └── test_selection.py    # Intelligent test targeting (complex)
```

## Design Philosophy

- **Simple over clever**: Each mode file reads top-to-bottom like its Jinja template
- **Direct dict construction**: Use f-strings to build YAML dicts, no abstraction layers
- **Helper functions only where complex**: Coverage and test selection logic is genuinely complex (exists in Jinja too)
- **No Pydantic output models**: Only use Pydantic for input parsing (TestStep)

## Example Code

```python
def generate_test_step(test, config):
    """Generate a test step - simple dict construction."""
    return {
        "label": test.label,
        "agents": {"queue": get_queue(test)},
        "plugins": [{
            "docker#v5.2.0": {
                "image": config.container_image,
                "command": ["bash", "-xc", build_command(test, config)],
                "environment": ["VLLM_USAGE_SOURCE=ci-test", "HF_TOKEN"],
            }
        }],
        "depends_on": "image-build",
    }
```

## Testing

All integration tests verify byte-for-byte YAML compatibility with Jinja templates:

```bash
# All integration tests (64 scenarios)
pytest tests/test_integration_comprehensive.py tests/test_integration_fastcheck.py

# Unit tests
pytest tests/ -k "not integration"
```

**Status**: ✅ 100% YAML compatibility verified (64/64 scenarios pass)

## How It Works

1. Read `test-pipeline.yaml` → Parse into TestStep objects
2. Generate mode-specific pipeline → Simple dicts with f-strings
3. Write `pipeline.yaml` → Direct YAML dump

No plugin builders, no converters, no abstraction - just straightforward code.
