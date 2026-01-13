"""
Error handlers and utilities for standardized error handling.
"""
from typing import Dict, Any, Optional, Callable
from functools import wraps
from logpkg.log_kcld import LogKCld
from utils.exceptions import DibbaBaseException, exception_to_http_exception

logger = LogKCld()


def handle_errors(
    operation_name: str,
    default_error_code: Optional[str] = None,
    log_level: str = "error"
) -> Callable:
    """
    Decorator for standardized error handling.
    
    Args:
        operation_name: Name of the operation for logging
        default_error_code: Default error code if exception is not DibbaBaseException
        log_level: Logging level (error, warning, info)
    
    Returns:
        Decorated function with error handling
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except DibbaBaseException as e:
                # Log structured error
                log_message = {
                    "operation": operation_name,
                    "error_code": e.error_code,
                    "message": e.message,
                    "details": e.details,
                    "function": func.__name__
                }
                
                if log_level == "error":
                    logger.error(f"Error in {operation_name}: {e.message}", extra=log_message, exc_info=True)
                elif log_level == "warning":
                    logger.warning(f"Warning in {operation_name}: {e.message}", extra=log_message)
                else:
                    logger.info(f"Info in {operation_name}: {e.message}", extra=log_message)
                
                # Re-raise to be handled by caller
                raise
            except Exception as e:
                # Convert generic exceptions to DibbaBaseException
                error_code = default_error_code or f"{func.__name__}_error"
                dibba_exc = DibbaBaseException(
                    message=f"Unexpected error in {operation_name}: {str(e)}",
                    error_code=error_code,
                    details={"function": func.__name__},
                    cause=e
                )
                
                log_message = {
                    "operation": operation_name,
                    "error_code": error_code,
                    "error_message": dibba_exc.message,  # Changed from "message" to avoid LogRecord conflict
                    "function": func.__name__,
                    "original_error": str(e)
                }
                logger.error(f"Unexpected error in {operation_name}: {str(e)}", extra=log_message, exc_info=True)
                raise dibba_exc
        
        return wrapper
    return decorator


def handle_async_errors(
    operation_name: str,
    default_error_code: Optional[str] = None,
    log_level: str = "error"
) -> Callable:
    """
    Decorator for standardized async error handling.
    
    Args:
        operation_name: Name of the operation for logging
        default_error_code: Default error code if exception is not DibbaBaseException
        log_level: Logging level (error, warning, info)
    
    Returns:
        Decorated async function with error handling
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except DibbaBaseException as e:
                # Log structured error
                log_message = {
                    "operation": operation_name,
                    "error_code": e.error_code,
                    "message": e.message,
                    "details": e.details,
                    "function": func.__name__
                }
                
                if log_level == "error":
                    logger.error(f"Error in {operation_name}: {e.message}", extra=log_message, exc_info=True)
                elif log_level == "warning":
                    logger.warning(f"Warning in {operation_name}: {e.message}", extra=log_message)
                else:
                    logger.info(f"Info in {operation_name}: {e.message}", extra=log_message)
                
                # Re-raise to be handled by caller
                raise
            except Exception as e:
                # Convert generic exceptions to DibbaBaseException
                error_code = default_error_code or f"{func.__name__}_error"
                dibba_exc = DibbaBaseException(
                    message=f"Unexpected error in {operation_name}: {str(e)}",
                    error_code=error_code,
                    details={"function": func.__name__},
                    cause=e
                )
                
                log_message = {
                    "operation": operation_name,
                    "error_code": error_code,
                    "error_message": dibba_exc.message,  # Changed from "message" to avoid LogRecord conflict
                    "function": func.__name__,
                    "original_error": str(e)
                }
                logger.error(f"Unexpected error in {operation_name}: {str(e)}", extra=log_message, exc_info=True)
                raise dibba_exc
        
        return wrapper
    return decorator


def create_error_response(
    error_code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    status_code: int = 500
) -> Dict[str, Any]:
    """
    Create a standardized error response dictionary.
    
    Args:
        error_code: Machine-readable error code
        message: Human-readable error message
        details: Additional error details
        status_code: HTTP status code
    
    Returns:
        Standardized error response dictionary
    """
    response = {
        "error": True,
        "error_code": error_code,
        "message": message,
        "status_code": status_code
    }
    if details:
        response["details"] = details
    return response


def create_success_response(
    message: str,
    data: Optional[Dict[str, Any]] = None,
    status_code: int = 200
) -> Dict[str, Any]:
    """
    Create a standardized success response dictionary.
    
    Args:
        message: Success message
        data: Response data
        status_code: HTTP status code
    
    Returns:
        Standardized success response dictionary
    """
    response = {
        "error": False,
        "message": message,
        "status_code": status_code
    }
    if data:
        response["data"] = data
    return response

