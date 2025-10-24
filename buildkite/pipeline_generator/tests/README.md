# Pipeline Generator Tests

Complete test suite for the refactored pipeline generator, including both unit tests and comprehensive integration tests.

## Test Suites

### 1. Unit Tests (125 tests)
Fast, focused tests for individual components

### 2. Integration Tests (56 scenarios)
Comprehensive backward compatibility tests verifying 100% YAML equivalence with Jinja template

## Running Tests

```bash
# All tests
pytest tests/

# Unit tests only
pytest tests/ -k "not integration"

# Integration tests
pytest tests/test_integration_comprehensive.py tests/test_integration_fastcheck.py

# Specific test
pytest tests/test_models.py::TestTestStep

# With coverage
pytest tests/ --cov=.. --cov-report=html
```

## Test Files

- `test_models.py` - Data models
- `test_transformers.py` - Command transformations  
- `test_selection.py` - Test filtering and blocking
- `test_docker.py` - Docker plugin construction
- `test_utils.py` - Utilities
- `test_pipeline_generator.py` - End-to-end
- `test_integration_comprehensive.py` - 56 CI scenarios
- `test_integration_fastcheck.py` - 8 fastcheck scenarios

All tests are pytest-based.

