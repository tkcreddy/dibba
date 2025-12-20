# Error Handling Guide

This document describes the standardized error handling system implemented in the Dibba project.

## Overview

The error handling system provides:
- **Custom exception classes** for different error types
- **Structured logging** for all errors
- **Consistent error response formats** across the API
- **Automatic error conversion** to HTTP responses

## Custom Exception Classes

All custom exceptions inherit from `DibbaBaseException`:

### Available Exception Types

- `ConfigurationError` - Configuration-related errors
- `AuthenticationError` - Authentication failures
- `AuthorizationError` - Authorization failures
- `ValidationError` - Input validation errors
- `NotFoundError` - Resource not found errors
- `RedisError` - Redis operation errors
- `CeleryTaskError` - Celery task errors
- `ContainerdError` - Containerd operation errors
- `CNIError` - CNI network errors
- `AWSError` - AWS operation errors
- `ContainerError` - Container operation errors
- `PodError` - Pod operation errors
- `ImageError` - Image operation errors
- `NetworkError` - Network operation errors
- `TaskSubmissionError` - Task submission errors

## Usage

### In API Endpoints

```python
from utils.exceptions import AuthenticationError, TaskSubmissionError
from utils.error_handlers import handle_async_errors, create_success_response

@handle_async_errors("operation_name", "ERROR_CODE")
@app.post("/endpoint")
async def my_endpoint(request: RequestModel, user: str = Depends(get_current_user)):
    try:
        # Your code here
        result = perform_operation()
        return create_success_response(
            message="Operation successful",
            data={"result": result}
        )
    except SpecificException as e:
        raise TaskSubmissionError(
            message="Failed to perform operation",
            error_code="OPERATION_ERROR",
            details={"operation": "operation_name"},
            cause=e
        ) from e
```

### In Utility Functions

```python
from utils.exceptions import RedisError
from utils.error_handlers import handle_errors

@handle_errors("operation_name", "ERROR_CODE")
def my_function():
    try:
        # Your code here
        result = redis_client.get("key")
        if result is None:
            raise RedisError(
                message="Key not found in Redis",
                error_code="REDIS_KEY_NOT_FOUND",
                details={"key": "key"}
            )
        return result
    except Exception as e:
        raise RedisError(
            message=f"Redis operation failed: {str(e)}",
            error_code="REDIS_OPERATION_ERROR",
            cause=e
        ) from e
```

### Error Response Format

All errors follow a consistent format:

```json
{
    "error": true,
    "error_code": "ERROR_CODE",
    "message": "Human-readable error message",
    "details": {
        "additional": "context"
    }
}
```

### Success Response Format

Success responses also follow a consistent format:

```json
{
    "error": false,
    "message": "Success message",
    "data": {
        "result": "data"
    }
}
```

## Error Handlers

### FastAPI Error Handlers

The application includes automatic error handlers:

1. **DibbaBaseException Handler** - Converts custom exceptions to HTTP responses
2. **Pydantic ValidationError Handler** - Handles request validation errors
3. **General Exception Handler** - Catches unexpected exceptions

### Decorators

#### `@handle_errors(operation_name, default_error_code, log_level)`

Decorator for synchronous functions:
- Automatically catches exceptions
- Logs errors with structured logging
- Converts generic exceptions to `DibbaBaseException`

#### `@handle_async_errors(operation_name, default_error_code, log_level)`

Decorator for async functions:
- Same functionality as `@handle_errors` but for async functions

## Structured Logging

All errors are logged with structured information:

```python
{
    "operation": "operation_name",
    "error_code": "ERROR_CODE",
    "message": "Error message",
    "details": {...},
    "function": "function_name",
    "original_error": "Original exception message"
}
```

## HTTP Status Code Mapping

Exceptions are automatically mapped to HTTP status codes:

- `ConfigurationError` → 500 Internal Server Error
- `AuthenticationError` → 401 Unauthorized
- `AuthorizationError` → 403 Forbidden
- `ValidationError` → 400 Bad Request
- `NotFoundError` → 404 Not Found
- `RedisError` → 503 Service Unavailable
- Other errors → 500 Internal Server Error

## Best Practices

1. **Use specific exception types** - Choose the most appropriate exception class
2. **Provide context** - Include relevant details in the `details` parameter
3. **Preserve original exceptions** - Use `cause` parameter to chain exceptions
4. **Use decorators** - Apply `@handle_errors` or `@handle_async_errors` to functions
5. **Log before raising** - The decorators handle logging automatically
6. **Return success responses** - Use `create_success_response()` for consistency

## Examples

### Example 1: API Endpoint with Error Handling

```python
@handle_async_errors("create_pod", "TASK_SUBMISSION_ERROR")
@app.post("/containerd/create-pods")
async def create_pods(request: CreatePodsRequest, user: str = Depends(get_current_user)):
    try:
        task = create_pod_task.apply_async(...)
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit create pods task",
            error_code="CREATE_PODS_TASK_ERROR",
            details={"namespace": request.namespace},
            cause=e
        ) from e
```

### Example 2: Utility Function with Error Handling

```python
@handle_errors("get_user", "REDIS_ERROR")
def get_user_password(username: str) -> str:
    try:
        password = redis_client.hget("authentication", username)
        if password is None:
            raise NotFoundError(
                message=f"User '{username}' not found",
                error_code="USER_NOT_FOUND",
                details={"username": username}
            )
        return password
    except NotFoundError:
        raise
    except Exception as e:
        raise RedisError(
            message=f"Failed to get user from Redis: {str(e)}",
            error_code="REDIS_GET_USER_ERROR",
            details={"username": username},
            cause=e
        ) from e
```

## Migration Guide

### Before (Old Error Handling)

```python
@app.post("/endpoint")
async def my_endpoint(request: RequestModel):
    try:
        result = perform_operation()
        return {"message": "Success", "data": result}
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail="Failed")
```

### After (New Error Handling)

```python
@handle_async_errors("my_endpoint", "OPERATION_ERROR")
@app.post("/endpoint")
async def my_endpoint(request: RequestModel):
    try:
        result = perform_operation()
        return create_success_response(
            message="Operation successful",
            data={"result": result}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to perform operation",
            error_code="OPERATION_ERROR",
            details={"operation": "my_endpoint"},
            cause=e
        ) from e
```

## Testing

Error handling can be tested using the test suite:

```python
def test_endpoint_error_handling(api_client):
    response = api_client.post("/endpoint", json={})
    assert response.status_code == 400
    assert response.json()["error"] == True
    assert "error_code" in response.json()
    assert "message" in response.json()
```

## Summary

The standardized error handling system provides:
- ✅ Consistent error responses
- ✅ Structured logging
- ✅ Type-safe exceptions
- ✅ Automatic HTTP conversion
- ✅ Better debugging information
- ✅ Improved error tracking

