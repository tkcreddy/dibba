"""
Custom exception classes for Dibba project.
Provides standardized error handling across the application.
"""
from typing import Optional, Dict, Any
from fastapi import HTTPException, status


class DibbaBaseException(Exception):
    """Base exception class for all Dibba exceptions."""
    
    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None
    ):
        """
        Initialize base exception.
        
        Args:
            message: Human-readable error message
            error_code: Machine-readable error code
            details: Additional error details
            cause: Original exception that caused this error
        """
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.details = details or {}
        self.cause = cause
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for API responses."""
        result = {
            "error": True,
            "error_code": self.error_code,
            "message": self.message
        }
        if self.details:
            result["details"] = self.details
        return result


class ConfigurationError(DibbaBaseException):
    """Raised when configuration errors occur."""
    pass


class AuthenticationError(DibbaBaseException):
    """Raised when authentication fails."""
    pass


class AuthorizationError(DibbaBaseException):
    """Raised when authorization fails."""
    pass


class ValidationError(DibbaBaseException):
    """Raised when input validation fails."""
    pass


class NotFoundError(DibbaBaseException):
    """Raised when a requested resource is not found."""
    pass


class RedisError(DibbaBaseException):
    """Raised when Redis operations fail."""
    pass


class CeleryTaskError(DibbaBaseException):
    """Raised when Celery task operations fail."""
    pass


class ContainerdError(DibbaBaseException):
    """Raised when containerd operations fail."""
    pass


class CNIError(DibbaBaseException):
    """Raised when CNI network operations fail."""
    pass


class AWSError(DibbaBaseException):
    """Raised when AWS operations fail."""
    pass


class ContainerError(DibbaBaseException):
    """Raised when container operations fail."""
    pass


class PodError(DibbaBaseException):
    """Raised when pod operations fail."""
    pass


class ImageError(DibbaBaseException):
    """Raised when image operations fail."""
    pass


class NetworkError(DibbaBaseException):
    """Raised when network operations fail."""
    pass


class TaskSubmissionError(DibbaBaseException):
    """Raised when task submission fails."""
    pass


# Mapping of custom exceptions to HTTP status codes
EXCEPTION_STATUS_MAP = {
    ConfigurationError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    AuthorizationError: status.HTTP_403_FORBIDDEN,
    ValidationError: status.HTTP_400_BAD_REQUEST,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    RedisError: status.HTTP_503_SERVICE_UNAVAILABLE,
    CeleryTaskError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ContainerdError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    CNIError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    AWSError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ContainerError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    PodError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ImageError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    NetworkError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    TaskSubmissionError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def exception_to_http_exception(exc: DibbaBaseException) -> HTTPException:
    """
    Convert DibbaBaseException to FastAPI HTTPException.
    
    Args:
        exc: DibbaBaseException instance
        
    Returns:
        HTTPException with appropriate status code and detail
    """
    status_code = EXCEPTION_STATUS_MAP.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
    detail = exc.to_dict()
    return HTTPException(status_code=status_code, detail=detail)

