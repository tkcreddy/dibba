# Testing Guide for Dibba

## Quick Start

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Run All Tests
```bash
# Using pytest directly
pytest

# Using the test runner script
./run_tests.sh
```

### Run Tests with Coverage
```bash
pytest --cov=. --cov-report=html --cov-report=term-missing
```

## Test Categories

### 1. Unit Tests (`tests/unit/`)

Test individual components in isolation:

- **test_read_config.py**: Tests for configuration management
- **test_utilities_extension.py**: Tests for utility functions (encoding, UUID generation)
- **test_redis_interface.py**: Tests for Redis operations

**Run unit tests:**
```bash
pytest -m unit
# or
./run_tests.sh --unit
```

### 2. Integration Tests (`tests/integration/`)

Test API endpoints and component interactions:

- **test_api_endpoints.py**: Tests for all FastAPI endpoints
  - Authentication endpoints
  - AWS instance management
  - Worker node information
  - Containerd operations

**Run integration tests:**
```bash
pytest -m integration
# or
./run_tests.sh --integration
```

### 3. Celery Task Tests (`tests/celery/`)

Test Celery task functions:

- **test_worker_node_tasks.py**: Tests for worker node tasks
- **test_containerd_tasks.py**: Tests for containerd task helpers

**Run Celery tests:**
```bash
pytest -m celery
# or
./run_tests.sh --celery
```

## Common Test Scenarios

### Testing with Mocks

All external dependencies are mocked:
- Redis connections
- Celery tasks
- AWS services
- Containerd operations

### Testing Authentication

Use the `authenticated_client` fixture for protected endpoints:

```python
def test_protected_endpoint(authenticated_client):
    response = authenticated_client.get("/protected-endpoint")
    assert response.status_code == 200
```

### Testing Error Cases

```python
def test_error_handling(mock_redis_interface):
    mock_redis_interface.get_user_pass.side_effect = Exception("Error")
    # Test error handling
```

## Coverage Goals

- **Overall**: 70% minimum
- **Core Utilities**: 90%+
- **API Endpoints**: All endpoints covered
- **Celery Tasks**: All task functions covered

## Viewing Coverage Reports

After running tests with coverage:

```bash
# HTML report
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux

# Terminal report shows in test output
```

## Continuous Integration

Tests are designed to run in CI/CD:

```yaml
# Example GitHub Actions
- name: Run Tests
  run: |
    pip install -r requirements.txt
    pytest --cov=. --cov-report=xml
```

## Troubleshooting

### Import Errors
- Ensure you're running from project root
- Check that all dependencies are installed

### Config Errors
- Tests use fixtures that create temporary configs
- Check `conftest.py` for config setup

### Redis Errors
- Tests use mocked Redis clients
- No actual Redis connection needed

### Coverage Not Showing
- Ensure `--cov=.` flag is used
- Check `.coveragerc` for omit patterns

## Best Practices

1. **Write tests first** (TDD approach)
2. **Test edge cases** and error conditions
3. **Use descriptive test names**
4. **Keep tests independent**
5. **Mock external dependencies**
6. **Maintain coverage above threshold**

## Adding New Tests

When adding new functionality:

1. **Unit tests**: Test individual functions/classes
2. **Integration tests**: Test API endpoints
3. **Update fixtures**: Add to `conftest.py` if needed
4. **Run tests**: Ensure all pass before committing
5. **Check coverage**: Don't let coverage drop

## Example: Adding a New API Endpoint Test

```python
@pytest.mark.integration
@pytest.mark.api
def test_new_endpoint(authenticated_client):
    response = authenticated_client.post(
        "/new-endpoint",
        json={"key": "value"}
    )
    assert response.status_code == 200
    assert "expected_field" in response.json()
```

## Test Markers Reference

- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.api` - API tests
- `@pytest.mark.celery` - Celery tests
- `@pytest.mark.slow` - Slow tests (skip in quick runs)
- `@pytest.mark.requires_redis` - Needs Redis (mocked in tests)
- `@pytest.mark.requires_containerd` - Needs containerd (mocked in tests)

