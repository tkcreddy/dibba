#!/bin/bash
# Test runner script for Dibba project

set -e

echo "🧪 Running Dibba Test Suite"
echo "============================"
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "❌ pytest is not installed. Installing dependencies..."
    pip install -r requirements.txt
fi

# Parse command line arguments
COVERAGE=true
VERBOSE=false
MARKER=""
TEST_PATH=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-coverage)
            COVERAGE=false
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -m|--marker)
            MARKER="$2"
            shift 2
            ;;
        --unit)
            MARKER="unit"
            shift
            ;;
        --integration)
            MARKER="integration"
            shift
            ;;
        --api)
            MARKER="api"
            shift
            ;;
        --celery)
            MARKER="celery"
            shift
            ;;
        --path)
            TEST_PATH="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-coverage      Run tests without coverage"
            echo "  -v, --verbose      Verbose output"
            echo "  -m, --marker       Run tests with specific marker"
            echo "  --unit             Run unit tests only"
            echo "  --integration      Run integration tests only"
            echo "  --api              Run API tests only"
            echo "  --celery           Run Celery tests only"
            echo "  --path PATH        Run specific test file or directory"
            echo "  -h, --help         Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Build pytest command
PYTEST_CMD="pytest"

if [ "$VERBOSE" = true ]; then
    PYTEST_CMD="$PYTEST_CMD -v"
fi

if [ "$COVERAGE" = true ]; then
    PYTEST_CMD="$PYTEST_CMD --cov=. --cov-report=html --cov-report=term-missing --cov-report=xml"
fi

if [ -n "$MARKER" ]; then
    PYTEST_CMD="$PYTEST_CMD -m $MARKER"
fi

if [ -n "$TEST_PATH" ]; then
    PYTEST_CMD="$PYTEST_CMD $TEST_PATH"
else
    PYTEST_CMD="$PYTEST_CMD tests/"
fi

echo "Running: $PYTEST_CMD"
echo ""
eval $PYTEST_CMD

if [ "$COVERAGE" = true ]; then
    echo ""
    echo "📊 Coverage report generated:"
    echo "   - HTML: htmlcov/index.html"
    echo "   - XML:  coverage.xml"
    echo ""
    echo "To view HTML report:"
    echo "   open htmlcov/index.html"
fi

echo ""
echo "✅ Tests completed!"

