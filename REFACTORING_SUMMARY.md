# Code Refactoring Summary - Duplicate Pattern Extraction

## Overview

This document summarizes the refactoring work done to extract repeated code patterns into reusable utility functions, addressing the "Duplicate Code Patterns" issue identified in the project review.

## Patterns Extracted

### 1. Queue Info Creation Pattern ✅

**Problem**: The pattern of creating queue info dictionaries was repeated 10+ times:
```python
queue_info = {
    'exchange': Exchange('secure_exchange', type='direct'),
    'queue': ue.encode_hostname_with_key(queue_name),
    'routing_key': ue.encode_hostname_with_key(queue_name),
    'delivery_mode': 2
}
```

**Solution**: Created utility functions in `utils/celery/queue_utils.py`:
- `create_queue_info()` - Generic queue info creation
- `create_host_queue_info()` - Convenience wrapper for host-based routing

**Impact**: 
- Reduced code duplication from 10+ instances to 1 utility function
- Centralized queue configuration logic
- Easier to maintain and modify queue settings

### 2. Task Submission Pattern ✅

**Problem**: Similar try/except blocks with task submission were repeated 12+ times:
```python
try:
    task = some_task.apply_async(args=..., kwargs=..., **queue_info)
    return create_success_response(
        message="Task submitted successfully",
        data={"task_id": task.id, ...}
    )
except Exception as e:
    raise TaskSubmissionError(
        message="Failed to submit task",
        error_code="TASK_ERROR",
        details={...},
        cause=e
    ) from e
```

**Solution**: Created `submit_celery_task()` function that:
- Handles task submission
- Standardizes error handling
- Creates consistent success responses
- Logs task submission events

**Impact**:
- Reduced code duplication from 12+ instances to 1 utility function
- Consistent error handling across all endpoints
- Better logging and debugging
- Reduced code by ~200+ lines

### 3. Extra Kwargs Extraction Pattern ✅

**Problem**: Pattern of extracting extra kwargs from requests was repeated:
```python
extra_kwargs = {k: v for k, v in request_data.items() if k not in defined_fields}
```

**Solution**: Created `extract_extra_kwargs()` utility function

**Impact**:
- Consistent extraction logic
- Reusable across endpoints

## Files Created

### `utils/celery/queue_utils.py`
New utility module containing:
- `create_queue_info()` - Create queue configuration
- `create_host_queue_info()` - Create host-specific queue configuration
- `submit_celery_task()` - Submit Celery tasks with standardized error handling
- `extract_extra_kwargs()` - Extract extra parameters from requests

## Files Modified

### `server/main_api.py`
Refactored endpoints to use utility functions:
1. ✅ `create_instances` - Uses `submit_celery_task()` and `extract_extra_kwargs()
2. ✅ `terminate_namespace` - Uses `submit_celery_task()`
3. ✅ `get_worker_node_data` - Uses `create_host_queue_info()` and `submit_celery_task()`
4. ✅ `get_worker_node_ip` - Uses `create_host_queue_info()` and `submit_celery_task()`
5. ✅ `get_worker_usage_data` - Uses `create_host_queue_info()` and `submit_celery_task()`
6. ✅ `create_pods` - Uses `create_host_queue_info()`, `submit_celery_task()`, and `extract_extra_kwargs()`
7. ✅ `list_namespaces_and_pods` - Uses `create_host_queue_info()` and `submit_celery_task()`
8. ✅ `list_pods_by_namespace` - Uses `create_host_queue_info()` and `submit_celery_task()`
9. ✅ `terminate_pod` - Uses `create_host_queue_info()` and `submit_celery_task()`
10. ✅ `terminate_pod_by_pause_cid` - Uses `create_host_queue_info()` and `submit_celery_task()`
11. ✅ `destroy_all_pods` - Uses `create_host_queue_info()` and `submit_celery_task()`
12. ✅ `destroy_container` - Uses `create_host_queue_info()` and `submit_celery_task()`
13. ✅ `get_container_info` - Uses `create_host_queue_info()` and `submit_celery_task()`
14. ✅ `purge_stopped` - Uses `create_host_queue_info()` and `submit_celery_task()`
15. ✅ `prune_namespace` - Uses `create_host_queue_info()` and `submit_celery_task()`
16. ✅ `cleanup_tasks_by_pod_prefix` - Uses `create_host_queue_info()` and `submit_celery_task()`

**Also Updated**:
- `aws_queue_info` - Now uses `create_queue_info()`
- `_host_queue()` - Deprecated, now uses `create_host_queue_info()` internally

## Code Reduction

### Before Refactoring
- ~12 instances of queue info creation (4-5 lines each) = ~50 lines
- ~12 instances of task submission pattern (15-20 lines each) = ~200 lines
- **Total duplicated code: ~250 lines**

### After Refactoring
- 1 utility module with 4 functions = ~150 lines (reusable)
- Endpoints now use 1-2 lines instead of 15-20 lines
- **Net reduction: ~200 lines of duplicated code**

## Benefits

1. **Maintainability**: Changes to queue configuration or task submission logic only need to be made in one place
2. **Consistency**: All endpoints now use the same error handling and response format
3. **Readability**: Endpoint code is cleaner and easier to understand
4. **Testability**: Utility functions can be tested independently
5. **Error Handling**: Standardized error handling across all endpoints
6. **Logging**: Consistent logging for all task submissions

## Example: Before vs After

### Before
```python
host_queue_info = {
    'exchange': Exchange('secure_exchange', type='direct'),
    'queue': ue.encode_hostname_with_key(request.host_name),
    'routing_key': ue.encode_hostname_with_key(request.host_name),
    'delivery_mode': 2
}
try:
    task = get_worker_node_info.apply_async(
        args=(),
        **host_queue_info
    )
    return create_success_response(
        message="Task submitted successfully",
        data={"task_id": task.id, "host_name": request.host_name}
    )
except Exception as e:
    raise TaskSubmissionError(
        message="Failed to submit get worker node data task",
        error_code="GET_WORKER_NODE_DATA_ERROR",
        details={"host_name": request.host_name},
        cause=e
    ) from e
```

### After
```python
host_queue_info = create_host_queue_info(request.host_name, ue)

return submit_celery_task(
    task=get_worker_node_info,
    queue_info=host_queue_info,
    operation_name="get_worker_node_data",
    error_code="GET_WORKER_NODE_DATA_ERROR",
    additional_data={"host_name": request.host_name}
)
```

**Reduction**: 20 lines → 8 lines (60% reduction)

## Testing

The utility functions should be tested to ensure:
1. Queue info creation works correctly
2. Task submission handles errors properly
3. Extra kwargs extraction works as expected

## Next Steps

1. ✅ Create utility module
2. ✅ Refactor endpoints to use utilities
3. ⚠️ Add unit tests for utility functions
4. ⚠️ Update documentation if needed

## Status

✅ **COMPLETED**: All repeated patterns have been extracted into utility functions and endpoints have been refactored.

---

*Last Updated: December 2024*


