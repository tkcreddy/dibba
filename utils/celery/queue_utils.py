"""
Utility functions for Celery queue configuration and task submission.

This module provides reusable functions to eliminate code duplication
in queue info creation and task submission patterns.
"""
from typing import Dict, Any, Optional, Tuple
from kombu import Exchange
from celery import Task
from logpkg.log_kcld import LogKCld
from utils.exceptions import TaskSubmissionError
from utils.error_handlers import create_success_response
from utils.extensions.utilities_extention import UtilitiesExtension

import concurrent.futures
import time

logger = LogKCld()

# Shared executor to avoid spawning threads per request
_CELERY_SUBMIT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)


def create_queue_info(
    queue_name: str,
    exchange_name: str = "secure_exchange",
    exchange_type: str = "direct",
    delivery_mode: int = 2,
    utilities_extension: Optional[UtilitiesExtension] = None,
) -> Dict[str, Any]:
    """
    Create a standardized queue info dictionary for Celery task routing.

    Args:
        queue_name: Name or identifier for the queue (will be encoded if utilities_extension provided)
        exchange_name: Name of the exchange (default: 'secure_exchange')
        exchange_type: Type of exchange (default: 'direct')
        delivery_mode: Message delivery mode (default: 2 for persistent)
        utilities_extension: Optional UtilitiesExtension instance for encoding queue names

    Returns:
        Dictionary with exchange, queue, routing_key, and delivery_mode

    Example:
        >>> ue = UtilitiesExtension(key)
        >>> queue_info = create_queue_info('aws_interface', utilities_extension=ue)
        >>> # Returns: {
        >>> #     'exchange': Exchange('secure_exchange', type='direct'),
        >>> #     'queue': '<encoded_aws_interface>',
        >>> #     'routing_key': '<encoded_aws_interface>',
        >>> #     'delivery_mode': 2
        >>> # }
    """

    if utilities_extension:
        encoded_name = utilities_extension.encode_hostname_with_key(queue_name)
    else:
        encoded_name = queue_name

    return {
        "exchange": Exchange(exchange_name, type=exchange_type),
        "queue": encoded_name,
        "routing_key": encoded_name,
        "delivery_mode": delivery_mode,
    }


def create_host_queue_info(
    host_name: str,
    utilities_extension: UtilitiesExtension,
    exchange_name: str = "secure_exchange",
    exchange_type: str = "direct",
    delivery_mode: int = 2,
) -> Dict[str, Any]:
    """
    Create queue info for host-specific task routing.

    This is a convenience wrapper around create_queue_info specifically
    for host-based routing, which is a common pattern in the codebase.

    Args:
        host_name: Name of the target host/worker node
        utilities_extension: UtilitiesExtension instance for encoding
        exchange_name: Name of the exchange (default: 'secure_exchange')
        exchange_type: Type of exchange (default: 'direct')
        delivery_mode: Message delivery mode (default: 2)

    Returns:
        Dictionary with exchange, queue, routing_key, and delivery_mode

    Example:
        >>> ue = UtilitiesExtension(key)
        >>> host_queue = create_host_queue_info('worker-01', ue)
    """

    return create_queue_info(
        queue_name=host_name,
        exchange_name=exchange_name,
        exchange_type=exchange_type,
        delivery_mode=delivery_mode,
        utilities_extension=utilities_extension,
    )




def submit_celery_task(
    task: Task,
    args: Optional[Tuple] = None,
    kwargs: Optional[Dict[str, Any]] = None,
    queue_info: Optional[Dict[str, Any]] = None,
    operation_name: str = "task",
    error_code: str = "TASK_SUBMISSION_ERROR",
    success_message: str = "Task submitted successfully",
    additional_data: Optional[Dict[str, Any]] = None,
    # 🔥 NEW: hard timeout so FastAPI never hangs
    submit_timeout_s: float = 2.5,
) -> Dict[str, Any]:
    """
    Submit a Celery task with standardized error handling and response.

    This function encapsulates the common pattern of:
    1. Submitting a Celery task with apply_async
    2. Handling exceptions and converting to TaskSubmissionError
    3. Creating a standardized success response

    Args:
        task: Celery task to submit
        args: Positional arguments for the task
        kwargs: Keyword arguments for the task
        queue_info: Queue configuration dictionary (from create_queue_info)
        operation_name: Name of the operation for error messages
        error_code: Error code to use if submission fails
        success_message: Success message for the response
        additional_data: Additional data to include in success response

    Returns:
        Standardized success response dictionary with task_id

    Raises:
        TaskSubmissionError: If task submission fails

    Example:
        >>> queue_info = create_host_queue_info('worker-01', ue)
        >>> response = submit_celery_task(
        ...     task=get_worker_node_info,
        ...     queue_info=queue_info,
        ...     operation_name="get_worker_node_data",
        ...     additional_data={"host_name": "worker-01"}
        ... )
    """


    args = args or ()
    kwargs = kwargs or {}
    additional_data = additional_data or {}

    def _do_submit():
        # Important: disable publish retries to avoid long hangs
        if queue_info:
            return task.apply_async(
                args=args,
                kwargs=kwargs,
                **queue_info,
                retry=False,   # ✅ stop broker retry loops
            )
        return task.apply_async(
            args=args,
            kwargs=kwargs,
            retry=False,
        )

    t0 = time.time()
    try:
        fut = _CELERY_SUBMIT_EXECUTOR.submit(_do_submit)
        celery_task = fut.result(timeout=submit_timeout_s)

        logger.info(
            f"Task {task.name} submitted successfully",
            extra={
                "task_id": celery_task.id,
                "operation": operation_name,
                # Avoid logging huge payloads; can slow you down under load
                "args_len": len(args) if isinstance(args, tuple) else None,
                "kwargs_keys": list(kwargs.keys())[:20],
                "took_ms": int((time.time() - t0) * 1000),
            },
        )

        response_data = {"task_id": celery_task.id, **additional_data}
        return create_success_response(message=success_message, data=response_data)

    except concurrent.futures.TimeoutError as e:
        error_details = {
            "operation": operation_name,
            "task_name": getattr(task, "name", "unknown"),
            "submit_timeout_s": submit_timeout_s,
            **additional_data,
        }
        logger.error(
            f"Celery publish timed out submitting {operation_name}",
            extra=error_details,
            exc_info=True,
        )
        raise TaskSubmissionError(
            message=f"Timed out submitting {operation_name} task (broker publish timeout)",
            error_code=error_code,
            details=error_details,
            cause=e,
        ) from e

    except Exception as e:
        error_details = {
            "operation": operation_name,
            "task_name": getattr(task, "name", "unknown"),
            **additional_data,
        }
        logger.error(
            f"Failed to submit {operation_name} task: {str(e)}",
            extra=error_details,
            exc_info=True,
        )
        raise TaskSubmissionError(
            message=f"Failed to submit {operation_name} task",
            error_code=error_code,
            details=error_details,
            cause=e,
        ) from e


def extract_extra_kwargs(
    request_dict: Dict[str, Any],
    defined_fields: set
) -> Dict[str, Any]:
    """
    Extract extra keyword arguments from a request that aren't in defined fields.
    
    This is useful for passing through additional parameters that aren't
    explicitly defined in Pydantic models but should be forwarded to tasks.
    
    Args:
        request_dict: Dictionary representation of the request
        defined_fields: Set of field names that are explicitly defined
    
    Returns:
        Dictionary of extra keyword arguments
    
    Example:
        >>> request_data = request.model_dump()
        >>> defined = set(CreateInstanceRequest.__annotations__.keys())
        >>> extra = extract_extra_kwargs(request_data, defined)
    """
    return {k: v for k, v in request_dict.items() if k not in defined_fields}


