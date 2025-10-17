# Pipeline Generator Tests

Complete test suite for the refactored pipeline generator, including both unit tests and comprehensive integration tests.

## Test Suites

### 1. Unit Tests (125 tests)
Fast, focused tests for individual components

### 2. Integration Tests (56 scenarios)
Comprehensive backward compatibility tests verifying 100% YAML equivalence with Jinja template

## Running Tests

### Run all unit tests
```bash
cd /Users/rezabarazesh/Documents/test/ci-infra/buildkite/pipeline_generator
python -m pytest tests/ -v -k "not integration"
```

### Run integration tests (comprehensive)
```bash
cd /Users/rezabarazesh/Documents/test/ci-infra/buildkite/pipeline_generator/tests
python test_integration_comprehensive.py
# or
./test_integration_comprehensive.py
```

### Run ALL tests (unit + integration)
```bash
cd /Users/rezabarazesh/Documents/test/ci-infra/buildkite/pipeline_generator
python -m pytest tests/ -v
python tests/test_integration_comprehensive.py
```

### Run specific test files
```bash
# Test models
python -m pytest tests/test_models.py -v

# Test transformers
python -m pytest tests/test_transformers.py -v

# Test selection logic
python -m pytest tests/test_selection.py -v

# Test Docker plugin builder
python -m pytest tests/test_docker.py -v

# Test utilities
python -m pytest tests/test_utils.py -v

# Integration tests
python -m pytest tests/test_pipeline_generator.py -v
```

### Run with coverage
```bash
python -m pytest tests/ --cov=. --cov-report=html --cov-report=term
```

### Run specific test class or method
```bash
# Run specific class
python -m pytest tests/test_models.py::TestTestStep -v

# Run specific test method
python -m pytest tests/test_models.py::TestTestStep::test_create_test_step_with_command -v
```

## Test Structure

```
tests/
├── __init__.py
├── conftest.py                       # Shared fixtures
├── README.md                         # This file
├── TEST_SUMMARY.md                   # Complete test summary
├── run_tests.sh                      # Unit test runner
│
├── test_integration_comprehensive.py # 56 integration scenarios (backward compatibility)
│
├── test_models.py                    # Tests for data models
│   ├── TestStepKey                # Step key generation
│   ├── TestBlockStep              # Block step creation
│   ├── TestTestStep               # TestStep model
│   ├── TestBuildkiteStep          # BuildkiteStep model
│   └── TestBuildkiteBlockStep     # BuildkiteBlockStep model
│
├── test_transformers.py           # Tests for command transformers
│   ├── TestNormalizer             # Command normalization
│   ├── TestTestTargetingTransformer  # Intelligent test targeting
│   └── TestCoverageTransformer    # Coverage injection
│
├── test_selection.py              # Tests for test selection
│   ├── TestShouldRunStep          # Run/skip decisions
│   ├── TestChangedTests           # Changed test detection
│   ├── TestIntelligentTestTargeting  # Intelligent targeting
│   ├── TestExtractCoveredTestPaths   # Test path extraction
│   └── TestShouldBlockStep        # Block decisions
│
├── test_docker.py                 # Tests for Docker plugins
│   ├── TestDockerEnvironment      # Environment configuration
│   ├── TestDockerVolumes          # Volume configuration
│   ├── TestStandardDockerConfig   # Standard Docker config
│   ├── TestSpecialGPUDockerConfig # Special GPU config
│   ├── TestDockerCommandBuilder   # Command builder
│   └── TestPluginBuilder          # Plugin builder
│
├── test_utils.py                  # Tests for utilities
│   ├── TestAgentQueue             # Agent queue selection
│   ├── TestFullTestCommand        # Full command generation
│   ├── TestMultiNodeTestCommand   # Multi-node commands
│   └── TestConstants              # Constants validation
│
├── test_pipeline_generator.py    # Integration tests
│   ├── TestPipelineGeneratorConfig  # Config tests
│   ├── TestReadTestSteps          # YAML reading
│   ├── TestWriteBuildkitePipeline # YAML writing
│   ├── TestPipelineGenerator      # Pipeline generation
│   └── TestEndToEnd               # Full integration
│
└── test_files/                    # Test data files
    ├── test-pipeline.yaml         # Sample test configuration
    └── expected_pipeline.yaml     # Expected output
```

## Jinja Compatibility

The integration test (`test_integration_comprehensive.py`) verifies 100% backward 
compatibility with the Jinja template by comparing generated YAMLs.
This ensures that the refactored Python code produces identical output to the original 
Jinja template in all cases.

