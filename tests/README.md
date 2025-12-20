# Test Suite Documentation

This directory contains the comprehensive test suite for the Dibba project using pytest with coverage reporting.

## Test Structure

```
tests/
├── conftest.py              # Shared fixtures and configuration
├── unit/                     # Unit tests for core utilities
│   ├── test_read_config.py
│   ├── test_utilities_extension.py
│   └── test_redis_interface.py
├── integration/              # Integration tests
│   └── test_api_endpoints.py
└── celery/                   # Celery task tests
    ├── test_worker_node_tasks.py
    └── test_containerd_tasks.py
```

## Running Tests

### Run all tests
```bash
pytest
```

### Run with coverage report
```bash
pytest --cov=. --cov-report=html --cov-report=term-missing
```

### Run specific test categories
```bash
# Unit tests only
pytest -m unit

# Integration tests only
pytest -m integration

# API tests only
pytest -m api

# Celery task tests only
pytest -m celery
```

### Run specific test file
```bash
pytest tests/unit/test_read_config.py
```

### Run with verbose output
```bash
pytest -v
```

### Run with coverage threshold (fails if below threshold)
```bash
pytest --cov-fail-under=70
```

## Test Markers

Tests are organized using pytest markers:

- `@pytest.mark.unit` - Unit tests for individual components
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.api` - API endpoint tests
- `@pytest.mark.celery` - Celery task tests
- `@pytest.mark.slow` - Slow running tests
- `@pytest.mark.requires_redis` - Tests requiring Redis
- `@pytest.mark.requires_containerd` - Tests requiring containerd

## Coverage Reports

After running tests with coverage, you can view the HTML report:

```bash
# Generate HTML report
pytest --cov=. --cov-report=html

# Open the report
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

Coverage reports are also generated in:
- `htmlcov/` - HTML coverage report
- `coverage.xml` - XML format for CI/CD integration

## Fixtures

Common fixtures are defined in `conftest.py`:

- `test_config_dir` - Temporary config directory with test config
- `mock_redis_client` - Mock Redis client
- `mock_redis_interface` - Mock RedisInterface
- `utilities_extension` - UtilitiesExtension instance with test key
- `api_client` - FastAPI test client
- `authenticated_client` - Authenticated test client
- `mock_celery_app` - Mock Celery app
- `mock_celery_task` - Mock Celery task result
- `sample_container_spec` - Sample container specification
- `sample_pod_request` - Sample pod creation request

## Writing New Tests

### Unit Test Example
```python
import pytest
from utils.some_module import SomeClass

@pytest.mark.unit
class TestSomeClass:
    def test_some_method(self):
        obj = SomeClass()
        result = obj.some_method()
        assert result == expected_value
```

### Integration Test Example
```python
import pytest

@pytest.mark.integration
@pytest.mark.api
def test_api_endpoint(authenticated_client):
    response = authenticated_client.get("/some-endpoint")
    assert response.status_code == 200
```

### Using Fixtures
```python
def test_with_fixture(mock_redis_interface):
    mock_redis_interface.get_user_pass.return_value = "hashed"
    # Test code here
```

## Mocking External Dependencies

Tests use mocks for external dependencies:
- Redis connections
- Celery tasks
- AWS services
- Containerd operations
- Network operations

## Continuous Integration

The test suite is designed to run in CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Run tests
  run: |
    pip install -r requirements.txt
    pytest --cov=. --cov-report=xml --cov-report=term
```

## Coverage Goals

- **Current target**: 70% overall coverage
- **Unit tests**: Aim for 90%+ coverage
- **Integration tests**: Cover all major API endpoints
- **Celery tasks**: Test all task functions

## Troubleshooting

### Tests failing due to missing config
Ensure test config is properly set up in `conftest.py` fixtures.

### Redis connection errors
Tests use mocked Redis clients. If you see connection errors, check that mocks are properly configured.

### Import errors
Make sure you're running tests from the project root directory.

### Coverage not showing
Ensure `--cov=.` flag is used and source files are not in omit list in `.coveragerc`.

## Best Practices

1. **Isolation**: Each test should be independent
2. **Mocking**: Mock external dependencies
3. **Naming**: Use descriptive test names
4. **Assertions**: Use specific assertions
5. **Fixtures**: Reuse fixtures for common setup
6. **Markers**: Use appropriate markers for test organization

## Adding New Tests

When adding new functionality:

1. Write unit tests for new utilities
2. Add integration tests for new API endpoints
3. Test Celery tasks if applicable
4. Ensure coverage doesn't drop below threshold
5. Update this README if adding new test categories

