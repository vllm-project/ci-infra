#!/bin/bash
# Run all pipeline generator unit tests

set -e

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "=================================="
echo "Pipeline Generator Unit Tests"
echo "=================================="
echo ""

# Check if pytest is installed
if ! python -m pytest --version > /dev/null 2>&1; then
    echo "ERROR: pytest not installed"
    echo "Install with: pip install pytest pytest-cov"
    exit 1
fi

# Run tests with coverage
echo "Running unit tests with coverage..."
python -m pytest tests/ \
    -v \
    --cov=. \
    --cov-report=term \
    --cov-report=html:htmlcov \
    --cov-config=.coveragerc \
    "$@"

echo ""
echo "=================================="
echo "Test Results"
echo "=================================="
echo "HTML coverage report: htmlcov/index.html"
echo ""


