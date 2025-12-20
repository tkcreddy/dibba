from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Extra,ConfigDict, ValidationError as PydanticValidationError
from server.api_models import CreatePodsRequest
from utils.celery.tasks.worker_node_tasks import *
from utils.celery.tasks.containerd_tasks import *
from utils.celery.tasks.aws_tasks import get_ec2_instances, create_worker_nodes, terminate_worker_node
from utils.extensions.utilities_extention import UtilitiesExtension
from kombu import Exchange
from utils.redis.redis_interface import RedisInterface
from utils.ReadConfig import ReadConfig as rc
from utils.celery.celery_config import celery_app
from utils.exceptions import (
    DibbaBaseException,
    AuthenticationError,
    ValidationError,
    NotFoundError,
    TaskSubmissionError,
    exception_to_http_exception
)
from utils.error_handlers import handle_async_errors, create_error_response, create_success_response
from typing import Optional, Dict, Any, Union, List
import logging
import jwt
from datetime import datetime, timedelta,UTC
from logpkg.log_kcld import LogKCld, log_to_file
from dataclasses import is_dataclass, asdict


logger = LogKCld()
# Initialize FastAPI app with metadata
app = FastAPI(
    title="Dibba Container Orchestration API",
    description="""
    Dibba is a lightweight, Python-based container orchestration layer.
    
    ## Features
    
    * **Pod Management**: Create and manage pods/containers via containerd
    * **Worker Nodes**: Provision and manage AWS EC2 worker nodes
    * **Task Execution**: Distributed task execution via Celery
    * **Authentication**: OAuth2 password flow with JWT tokens
    
    ## Authentication
    
    Most endpoints require authentication. Use the `/token` endpoint to obtain a JWT token,
    then include it in the Authorization header: `Bearer <token>`
    """,
    version="1.0.0",
    contact={
        "name": "Dibba Contributors",
        "url": "https://github.com/tkcreddy/dibba",
    },
    license_info={
        "name": "Apache 2.0",
    },
)


# ==================== Error Handlers ====================

@app.exception_handler(DibbaBaseException)
async def dibba_exception_handler(request: Request, exc: DibbaBaseException):
    """Handle DibbaBaseException and convert to HTTP response."""
    http_exc = exception_to_http_exception(exc)
    return JSONResponse(
        status_code=http_exc.status_code,
        content=exc.to_dict()
    )


@app.exception_handler(PydanticValidationError)
async def validation_exception_handler(request: Request, exc: PydanticValidationError):
    """Handle Pydantic validation errors."""
    error_details = []
    for error in exc.errors():
        error_details.append({
            "field": ".".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
            "type": error["type"]
        })
    
    response = create_error_response(
        error_code="VALIDATION_ERROR",
        message="Request validation failed",
        details={"validation_errors": error_details},
        status_code=status.HTTP_400_BAD_REQUEST
    )
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=response)


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    logger.error(
        f"Unhandled exception: {str(exc)}",
        extra={
            "path": str(request.url),
            "method": request.method,
            "exception_type": type(exc).__name__
        },
        exc_info=True
    )
    
    response = create_error_response(
        error_code="INTERNAL_SERVER_ERROR",
        message="An unexpected error occurred",
        details={"exception_type": type(exc).__name__},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=response)

# OAuth2 for authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Logger setup
# logger = logging.getLogger(__name__)

# Read configuration
read_config = rc()
aws_config = read_config.aws_config
key_read = read_config.encryption_config
redis_db_config = read_config.redis_db_config
ue = UtilitiesExtension(key_read['key'])
rd = RedisInterface(
    redis_db_config['redis_host'],
    redis_db_config['redis_port'],
    redis_db_config['redis_db']
)

SECRET_KEY = key_read['key']
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
if not SECRET_KEY:
    raise ValueError("SECRET_KEY is required!")

# Queue Information
aws_queue_info = {
    'exchange': Exchange('secure_exchange', type='direct'),
    'queue': ue.encode_hostname_with_key('aws_interface'),
    'routing_key': ue.encode_hostname_with_key('aws_interface'),
    'delivery_mode': 2
}


# Models for request validation
class CreateInstanceRequest(BaseModel):
    instance_type: str
    ami_id: str
    key_name: str
    security_group_ids: list[str]
    subnet_id: str
    namespace: str
    min_count: int
    max_count: int
    model_config = ConfigDict(extra='allow')


class TerminateInstanceRequest(BaseModel):
    namespace: str


class TaskId(BaseModel):
    task_id: str


class HostName(BaseModel):
    host_name: str

class ContainerdHostRequest(BaseModel):
    host_name: str
    model_config = ConfigDict(extra='allow')

class PodNamespaceHostRequest(BaseModel):
    host_name: str
    namespace: str
    model_config = ConfigDict(extra='allow')

class TerminatePodRequest(BaseModel):
    host_name: str
    namespace: str
    pod_name: str
    cni_network: Optional[str] = None
    ifname: Optional[str] = None
    model_config = ConfigDict(extra='allow')


class TerminatePodByCidRequest(BaseModel):
    host_name: str
    namespace: str
    pause_cid: str
    cni_network: Optional[str] = None
    ifname: Optional[str] = None
    model_config = ConfigDict(extra='allow')


class DestroyAllPodsRequest(BaseModel):
    host_name: str
    namespace: str
    cni_network: Optional[str] = None
    ifname: Optional[str] = None
    model_config = ConfigDict(extra='allow')


class DestroyContainerRequest(BaseModel):
    host_name: str
    namespace: str
    cid: str
    model_config = ConfigDict(extra='allow')


class PruneNamespaceRequest(BaseModel):
    host_name: str
    namespace: str
    aggressive: bool = True
    model_config = ConfigDict(extra='allow')


class PurgeStoppedRequest(BaseModel):
    host_name: str
    namespace: str
    model_config = ConfigDict(extra='allow')


class ContainerInfoRequest(BaseModel):
    host_name: str
    namespace: str
    cid: str
    model_config = ConfigDict(extra='allow')


class CleanupTasksByPodPrefixRequest(BaseModel):
    host_name: str
    namespace: str
    pod_id: str          # e.g. cd83c6a7ac0f47c6
    prefer_grpc: bool = True
    model_config = ConfigDict(extra='allow')

@log_to_file(logger)
def _host_queue(host_name: str) -> Dict[str, Any]:
    """Create queue information dictionary for a host.
    
    Args:
        host_name: Name of the host
        
    Returns:
        Dictionary with exchange, queue, routing_key, and delivery_mode
    """
    return {
        'exchange': Exchange('secure_exchange', type='direct'),
        'queue': ue.encode_hostname_with_key(host_name),
        'routing_key': ue.encode_hostname_with_key(host_name),
        'delivery_mode': 2
    }



@log_to_file(logger)
def authenticate_user(username: str, password: str) -> Union[str, bool]:
    """Authenticate a user with username and password.
    
    Args:
        username: Username to authenticate
        password: Password to verify
        
    Returns:
        Username if authentication successful, False otherwise
    """
    if not rd.get_user_pass(username):
        return False
    if ue.encode_phrase_with_key(password) == rd.get_user_pass(username):
        return username
    return False


@log_to_file(logger)
def create_access_token(data: Dict[str, Any], expires_delta: timedelta) -> str:
    """Create a JWT access token.
    
    Args:
        data: Dictionary containing token payload
        expires_delta: Time delta for token expiration
        
    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()
    expire = datetime.now(UTC) + expires_delta
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@log_to_file(logger)
def as_plain(obj: Any) -> Any:
    """Convert an object to plain Python types (dict, list, primitives).
    
    Args:
        obj: Object to convert (Pydantic model, dataclass, dict, list, etc.)
        
    Returns:
        Plain Python representation of the object
    """
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):     # Pydantic v2
        return obj.model_dump()
    if hasattr(obj, "dict"):           # Pydantic v1
        return obj.dict()
    if is_dataclass(obj):              # Python dataclass
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [as_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: as_plain(v) for k, v in obj.items()}
    # last resort: object's __dict__
    return getattr(obj, "__dict__", obj)



@log_to_file(logger)
@handle_async_errors("login", "AUTHENTICATION_ERROR")
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise AuthenticationError(
            message="Invalid username or password",
            error_code="INVALID_CREDENTIALS",
            details={"username": form_data.username}
        )
    access_token = create_access_token(
        data={"sub": form_data.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return create_success_response(
        message="Authentication successful",
        data={"access_token": access_token, "token_type": "bearer"}
    )


#Dependency to get the current user
@log_to_file(logger)
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        logger.debug(f"Authenticated user: {username}")
        if username is None or not rd.get_user_pass(username):
            raise AuthenticationError(
                message="Invalid authentication token",
                error_code="INVALID_TOKEN",
                details={"username": username}
            )
        return username
    except jwt.ExpiredSignatureError as e:
        raise AuthenticationError(
            message="Authentication token has expired",
            error_code="TOKEN_EXPIRED",
            cause=e
        ) from e
    except jwt.InvalidTokenError as e:
        raise AuthenticationError(
            message="Invalid authentication token",
            error_code="INVALID_TOKEN",
            cause=e
        ) from e


@log_to_file(logger)
@handle_async_errors("create_instances", "TASK_SUBMISSION_ERROR")
@app.post(
    "/create-instances/",
    tags=["AWS Management"],
    summary="Create AWS EC2 worker instances",
    description="""
    Create one or more AWS EC2 instances to be used as worker nodes.
    
    This endpoint submits a Celery task to provision EC2 instances with the specified
    configuration. The task runs asynchronously, and you can check its status using
    the returned task_id.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/create-instances/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "instance_type": "t2.micro",
           "ami_id": "ami-12345678",
           "key_name": "my-key-pair",
           "security_group_ids": ["sg-12345678"],
           "subnet_id": "subnet-12345678",
           "namespace": "production",
           "min_count": 1,
           "max_count": 3
         }'
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "abc123-def456-ghi789"
        }
    }
    ```
    
    **Note**: Use the task_id to check status via `/task/{task_id}` endpoint.
    """,
    response_description="Task submission response with task ID",
    responses={
        200: {
            "description": "Task submitted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "error": False,
                        "message": "Task submitted successfully",
                        "data": {"task_id": "abc123-def456-ghi789"}
                    }
                }
            }
        },
        401: {"description": "Unauthorized - Invalid or missing token"},
        500: {
            "description": "Task submission failed",
            "content": {
                "application/json": {
                    "example": {
                        "error": True,
                        "error_code": "CREATE_INSTANCES_TASK_ERROR",
                        "message": "Failed to submit create instances task"
                    }
                }
            }
        }
    }
)
async def create_instances(request: CreateInstanceRequest, user: str = Depends(get_current_user)):
    """
    Create AWS EC2 worker instances.
    
    Args:
        request: Instance creation request with AWS configuration
        user: Authenticated username (from JWT token)
        
    Returns:
        Success response with Celery task ID for tracking
        
    Raises:
        TaskSubmissionError: If task submission fails
        AuthenticationError: If authentication fails
    """
    request_data = request.dict()
    defined_fields = CreateInstanceRequest.__annotations__.keys()

    extra_kwargs = {k: v for k, v in request_data.items() if k not in defined_fields}

    try:
        # Submit the Celery create_worker_nodes task
        task = create_worker_nodes.apply_async(
            args=(
                aws_config['aws_access_key_id'],
                aws_config['aws_secret_access_key'],
                aws_config['region'],
                request.instance_type,
                request.ami_id,
                request.key_name,
                request.security_group_ids,
                request.subnet_id,
                request.namespace,
            ),
            kwargs={
                'MinCount': request.min_count,
                'MaxCount': request.max_count,
                **extra_kwargs
            },
            **aws_queue_info
        )

        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit create instances task",
            error_code="CREATE_INSTANCES_TASK_ERROR",
            details={"namespace": request.namespace, "instance_type": request.instance_type},
            cause=e
        ) from e


@log_to_file(logger)
@handle_async_errors("terminate_namespace", "TASK_SUBMISSION_ERROR")
@app.post(
    "/terminate-namespace/",
    tags=["AWS Management"],
    summary="Terminate all EC2 instances in a namespace",
    description="""
    Terminate all AWS EC2 instances associated with a specific namespace.
    
    This endpoint finds all instances registered under the given namespace in Redis
    and submits a Celery task to terminate them. All instances will be terminated
    regardless of their current state.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/terminate-namespace/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "namespace": "production"
         }'
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "xyz789-abc123-def456",
            "instances_count": 3
        }
    }
    ```
    
    **Example Response (No Instances):**
    ```json
    {
        "error": true,
        "error_code": "NO_INSTANCES_FOUND",
        "message": "No instances found for the given namespace"
    }
    ```
    """,
    response_description="Termination task submission response",
    responses={
        200: {
            "description": "Task submitted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "error": False,
                        "message": "Task submitted successfully",
                        "data": {
                            "task_id": "xyz789-abc123-def456",
                            "instances_count": 3
                        }
                    }
                }
            }
        },
        404: {
            "description": "No instances found for namespace",
            "content": {
                "application/json": {
                    "example": {
                        "error": True,
                        "error_code": "NO_INSTANCES_FOUND",
                        "message": "No instances found for the given namespace"
                    }
                }
            }
        }
    }
)
async def terminate_namespace(request: TerminateInstanceRequest, user: str = Depends(get_current_user)):
    """
    Terminate all EC2 instances in a namespace.
    
    Args:
        request: Termination request with namespace
        user: Authenticated username (from JWT token)
        
    Returns:
        Success response with task ID and instance count
        
    Raises:
        NotFoundError: If no instances found for namespace
        TaskSubmissionError: If task submission fails
    """
    try:
        # Fetch instance IDs to terminate from Redis
        instances_to_terminate = rd.get_instance_ids_namespace(request.namespace)

        if not instances_to_terminate:
            raise NotFoundError(
                message="No instances found for the given namespace",
                error_code="NO_INSTANCES_FOUND",
                details={"namespace": request.namespace}
            )

        # Submit the Celery terminate_worker_node task
        task = terminate_worker_node.apply_async(
            args=(
                aws_config['aws_access_key_id'],
                aws_config['aws_secret_access_key'],
                aws_config['region'],
                instances_to_terminate,
            ),
            **aws_queue_info
        )

        # Return task status
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "instances_count": len(instances_to_terminate)}
        )
    except NotFoundError:
        raise
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit terminate instances task",
            error_code="TERMINATE_INSTANCES_TASK_ERROR",
            details={"namespace": request.namespace},
            cause=e
        ) from e


@log_to_file(logger)
async def monitor_task(task_id: str, namespace: str, max_count: int) -> None:
    """
    Background task to monitor the Celery task result and save instances to Redis.
    
    Args:
        task_id: Celery task ID to monitor
        namespace: Namespace for the instances
        max_count: Maximum number of instances expected
    """
    try:
        logger.info(f"Starting the monitoring task with task_id: {task_id}")
        task_result = create_worker_nodes.AsyncResult(task_id).get(timeout=30)
        instances = {
            task_result['Instances'][i]['PrivateDnsName']: {
                'IpAddress': task_result['Instances'][i]['PrivateIpAddress'],
                'InstanceId': task_result['Instances'][i]['InstanceId'],
                'NameSpace': namespace,
                'InstanceType': task_result['Instances'][i]['InstanceType'],
            }
            for i in range(max_count)
        }

        logger.info(f"Instances successfully created and saved to Redis: {instances}")

    except Exception as e:
        logger.error(f"Error in monitoring task {task_id}: {e}")


@log_to_file(logger)
@app.get(
    "/task/{task_id}",
    tags=["Task Management"],
    summary="Get Celery task status",
    description="""
    Retrieve the current status and result of a Celery task.
    
    Use this endpoint to check the progress of long-running tasks such as
    instance creation, pod creation, or other asynchronous operations.
    
    **Task States:**
    - `PENDING`: Task is waiting to be executed
    - `PROGRESS`: Task is currently running (includes progress info)
    - `SUCCESS`: Task completed successfully (includes result)
    - `FAILURE`: Task failed (includes error information)
    - `REVOKED`: Task was cancelled
    
    **Example Request:**
    ```bash
    curl -X GET "http://localhost:8000/task/abc123-def456-ghi789" \\
         -H "Authorization: Bearer <token>"
    ```
    
    **Example Response (Pending):**
    ```json
    {
        "task_id": "abc123-def456-ghi789",
        "status": "PENDING",
        "result": null,
        "progress": null
    }
    ```
    
    **Example Response (Success):**
    ```json
    {
        "task_id": "abc123-def456-ghi789",
        "status": "SUCCESS",
        "result": {
            "instances": [...],
            "count": 3
        },
        "progress": null
    }
    ```
    
    **Example Response (Progress):**
    ```json
    {
        "task_id": "abc123-def456-ghi789",
        "status": "PROGRESS",
        "result": null,
        "progress": {
            "current": 2,
            "total": 3,
            "percent": 66.67
        }
    }
    ```
    """,
    response_description="Task status information",
    responses={
        200: {
            "description": "Task status retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "task_id": "abc123-def456-ghi789",
                        "status": "SUCCESS",
                        "result": {"instances": []},
                        "progress": None
                    }
                }
            }
        },
        401: {"description": "Unauthorized - Invalid or missing token"},
        404: {"description": "Task not found"}
    }
)
async def get_task_status(task_id: str, user: str = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Get the status of a Celery task.
    
    Args:
        task_id: ID of the task to check
        user: Authenticated username (from JWT token)
        
    Returns:
        Dictionary containing:
        - task_id: The task identifier
        - status: Current task status (PENDING, PROGRESS, SUCCESS, FAILURE, REVOKED)
        - result: Task result if completed, None otherwise
        - progress: Progress information if task is in PROGRESS state
    """
    task = celery_app.AsyncResult(task_id)

    return {
        "task_id": task.id,
        "status": task.status,
        "result": task.result if task.ready() else None,
        "progress": task.info if task.state == "PROGRESS" else None,
    }

@log_to_file(logger)
@handle_async_errors("get_worker_node_data", "TASK_SUBMISSION_ERROR")
@app.get(
    "/get_worker_node_data/",
    tags=["Worker Nodes"],
    summary="Get worker node system information",
    description="""
    Retrieve comprehensive system information from a worker node.
    
    This endpoint submits a Celery task to the specified worker node to gather
    system information including hostname, OS details, kernel version, and
    other system metrics.
    
    **Example Request:**
    ```bash
    curl -X GET "http://localhost:8000/get_worker_node_data/?host_name=worker-01" \\
         -H "Authorization: Bearer <token>"
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "sys-info-task-123",
            "host_name": "worker-01"
        }
    }
    ```
    
    **Note**: Use the task_id to retrieve the actual system information via `/task/{task_id}`.
    """,
    response_description="Task submission response",
    responses={
        200: {"description": "Task submitted successfully"},
        401: {"description": "Unauthorized"},
        500: {"description": "Task submission failed"}
    }
)
async def get_worker_node_data(request: HostName, user: str = Depends(get_current_user)):
    """
    Get worker node system information.
    
    Args:
        request: Request with host_name parameter
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
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


@log_to_file(logger)
@app.get(
    "/get_worker_node_ip/",
    tags=["Worker Nodes"],
    summary="Get worker node IP address",
    description="""
    Retrieve the IP address of a worker node.
    
    **Example Request:**
    ```bash
    curl -X GET "http://localhost:8000/get_worker_node_ip/?host_name=worker-01" \\
         -H "Authorization: Bearer <token>"
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "ip-task-456",
            "host_name": "worker-01"
        }
    }
    ```
    """,
    response_description="Task submission response"
)
async def get_worker_node_ip(request: HostName, user: str = Depends(get_current_user)):
    """
    Get worker node IP address.
    
    Args:
        request: Request with host_name parameter
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    host_queue_info = {
        'exchange': Exchange('secure_exchange', type='direct'),
        'queue': ue.encode_hostname_with_key(request.host_name),
        'routing_key': ue.encode_hostname_with_key(request.host_name),
        'delivery_mode': 2
    }
    try:
        task = get_host_ip.apply_async(
            args=(),
            **host_queue_info
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "host_name": request.host_name}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit get worker node IP task",
            error_code="GET_WORKER_NODE_IP_ERROR",
            details={"host_name": request.host_name},
            cause=e
        ) from e


@log_to_file(logger)
@app.get(
    "/get_worker_usage_data/",
    tags=["Worker Nodes"],
    summary="Get worker node resource usage",
    description="""
    Retrieve resource usage metrics (CPU, memory, disk) from a worker node.
    
    **Example Request:**
    ```bash
    curl -X GET "http://localhost:8000/get_worker_usage_data/?host_name=worker-01" \\
         -H "Authorization: Bearer <token>"
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "usage-task-789",
            "host_name": "worker-01"
        }
    }
    ```
    
    **Note**: The task result will contain usage metrics like:
    ```json
    {
        "cpu": 45.5,
        "memory": 60.2,
        "disk": 75.0
    }
    ```
    """,
    response_description="Task submission response"
)
async def get_worker_usage_data(request: HostName, user: str = Depends(get_current_user)):
    """
    Get worker node resource usage metrics.
    
    Args:
        request: Request with host_name parameter
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    host_queue_info = {
        'exchange': Exchange('secure_exchange', type='direct'),
        'queue': ue.encode_hostname_with_key(request.host_name),
        'routing_key': ue.encode_hostname_with_key(request.host_name),
        'delivery_mode': 2
    }
    try:
        task = get_usage.apply_async(
            args=(),
            **host_queue_info
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "host_name": request.host_name}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit get worker usage data task",
            error_code="GET_WORKER_USAGE_DATA_ERROR",
            details={"host_name": request.host_name},
            cause=e
        ) from e


@log_to_file(logger)
@handle_async_errors("create_pods", "TASK_SUBMISSION_ERROR")
@app.post(
    "/containerd/create-pods",
    "/containerd/create-pods/",
    tags=["Containerd - Pods"],
    summary="Create a pod with containers",
    description="""
    Create a pod with one or more containers on a worker node using containerd.
    
    A pod consists of a pause container (sandbox) and one or more application
    containers. The pod will be attached to the CNI network (Calico by default).
    
    **Pod Structure:**
    - Pause container: Provides shared namespaces (network, IPC, PID)
    - Application containers: Run within the pod's namespaces
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/create-pods" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production",
           "containers": [
             {
               "name": "nginx",
               "image": "nginx:latest",
               "resources": {
                 "cpu_millicores": 500,
                 "memory": "256Mi"
               },
               "env": {
                 "NGINX_HOST": "example.com"
               },
               "args": ["nginx", "-g", "daemon off;"]
             }
           ]
         }'
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "pod-create-task-123",
            "host_name": "worker-01",
            "namespace": "production",
            "containers_count": 1
        }
    }
    ```
    
    **Resource Specifications:**
    - `cpu_millicores`: CPU allocation in millicores (1000 = 1 CPU)
    - `memory`: Memory limit (supports: "64Mi", "256M", "1Gi", etc.)
    - `cpuset_cpus`: Optional CPU set (e.g., "0-3" or "0,2,4")
    """,
    response_description="Pod creation task response",
    responses={
        200: {"description": "Task submitted successfully"},
        400: {"description": "Invalid request"},
        401: {"description": "Unauthorized"},
        500: {"description": "Task submission failed"}
    }
)
async def create_pods(request: CreatePodsRequest,user: str = Depends(get_current_user)):
    """
    Create a pod with containers on a worker node.
    
    Args:
        request: Pod creation request with container specifications
        user: Authenticated username
        
    Returns:
        Success response with task ID and pod details
        
    Raises:
        TaskSubmissionError: If task submission fails
        ValidationError: If request validation fails
    """
    host_queue_info = {
        'exchange': Exchange('secure_exchange', type='direct'),
        'queue': ue.encode_hostname_with_key(request.host_name),
        'routing_key': ue.encode_hostname_with_key(request.host_name),
        'delivery_mode': 2
    }
    logger.info(f"Inside create_pods")


    #containers_payload = [c.model_dump(mode="json") for c in request.containers]
    containers_payload = [c.model_dump() for c in request.containers]


    extra_kwargs = {
        k: v for k, v in request.model_dump().items()
        if k not in CreatePodsRequest.__annotations__.keys()
    } or {"host_name": request.host_name}
    #containers_payload  =  containers_payload.to_dict()
    namespace = request.namespace

    try:
        task = create_pod_task.apply_async(
            args=(containers_payload, namespace),
            kwargs={
            "host_name": request.host_name,
            # any extra kwargs you want to flow to the task (must be JSON-safe)
            # "cni_network": "calico",
            # "cni_ifname": "eth0",
        },
            **host_queue_info
        )
        return create_success_response(
            message="Task submitted successfully",
            data={
                "task_id": task.id,
                "host_name": request.host_name,
                "namespace": namespace,
                "containers_count": len(containers_payload)
            }
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit create pods task",
            error_code="CREATE_PODS_TASK_ERROR",
            details={
                "host_name": request.host_name,
                "namespace": namespace,
                "containers_count": len(containers_payload)
            },
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/list_namespaces_and_pods/",
    tags=["Containerd - Pods"],
    summary="List all namespaces and pods",
    description="""
    List all containerd namespaces and their associated pods on a worker node.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/list_namespaces_and_pods/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01"
         }'
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "list-pods-task-123"
        }
    }
    ```
    """,
    response_description="Task submission response"
)
async def list_namespaces_and_pods_api(request: ContainerdHostRequest, user: str = Depends(get_current_user)):
    """
    List all namespaces and pods on a worker node.
    
    Args:
        request: Request with host_name
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = list_namespaces_and_pods_task.apply_async(
            args=(),
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "host_name": request.host_name}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit list namespaces and pods task",
            error_code="LIST_NAMESPACES_PODS_ERROR",
            details={"host_name": request.host_name},
            cause=e
        ) from e

@log_to_file(logger)
@app.post(
    "/containerd/list_pods_by_namespace/",
    tags=["Containerd - Pods"],
    summary="List pods in a specific namespace",
    description="""
    List all pods in a specific containerd namespace on a worker node.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/list_pods_by_namespace/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production"
         }'
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "list-ns-pods-task-456"
        }
    }
    ```
    """,
    response_description="Task submission response"
)
async def list_namespaces_and_pods_api(request: PodNamespaceHostRequest, user: str = Depends(get_current_user)):
    """
    List pods in a specific namespace.
    
    Args:
        request: Request with host_name and namespace
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = list_pods_by_namespace_task.apply_async(
            args=([request.namespace]),
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "host_name": request.host_name, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit list pods by namespace task",
            error_code="LIST_PODS_BY_NAMESPACE_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace},
            cause=e
        ) from e



@log_to_file(logger)
@app.post(
    "/containerd/terminate_pod/",
    tags=["Containerd - Pods"],
    summary="Terminate a pod by name",
    description="""
    Terminate a pod and all its containers by pod name.
    
    This will stop all containers in the pod, remove the CNI network attachment,
    and clean up the pod resources.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/terminate_pod/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production",
           "pod_name": "my-pod",
           "cni_network": "calico",
           "ifname": "eth0"
         }'
    ```
    
    **Example Response:**
    ```json
    {
        "error": false,
        "message": "Task submitted successfully",
        "data": {
            "task_id": "terminate-pod-task-789"
        }
    }
    ```
    """,
    response_description="Task submission response"
)
async def terminate_pod_api(request: TerminatePodRequest, user: str = Depends(get_current_user)):
    """
    Terminate a pod by name.
    
    Args:
        request: Termination request with pod details
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = terminate_pod_task.apply_async(
            args=(request.namespace, request.pod_name),
            kwargs={
                "cni_network": request.cni_network,
                "ifname": request.ifname,
            },
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "pod_name": request.pod_name, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit terminate pod task",
            error_code="TERMINATE_POD_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace, "pod_name": request.pod_name},
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/terminate_pod_by_pause_cid/",
    tags=["Containerd - Pods"],
    summary="Terminate a pod by pause container ID",
    description="""
    Terminate a pod by its pause container ID.
    
    Useful when you have the pause container ID but not the pod name.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/terminate_pod_by_pause_cid/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production",
           "pause_cid": "abc123def456",
           "cni_network": "calico",
           "ifname": "eth0"
         }'
    ```
    """,
    response_description="Task submission response"
)
async def terminate_pod_by_pause_cid_api(request: TerminatePodByCidRequest, user: str = Depends(get_current_user)):
    """
    Terminate a pod by pause container ID.
    
    Args:
        request: Termination request with pause container ID
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = terminate_pod_by_pause_cid_task.apply_async(
            args=(request.namespace, request.pause_cid),
            kwargs={
                "cni_network": request.cni_network,
                "ifname": request.ifname,
            },
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "pause_cid": request.pause_cid, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit terminate pod by pause CID task",
            error_code="TERMINATE_POD_BY_CID_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace, "pause_cid": request.pause_cid},
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/destroy_all_pods/",
    tags=["Containerd - Pods"],
    summary="Destroy all pods in a namespace",
    description="""
    Destroy all pods in a specific namespace on a worker node.
    
    **Warning**: This operation is destructive and will remove all pods
    in the specified namespace. Use with caution.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/destroy_all_pods/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production"
         }'
    ```
    """,
    response_description="Task submission response"
)
async def destroy_all_pods_api(request: DestroyAllPodsRequest, user: str = Depends(get_current_user)):
    """
    Destroy all pods in a namespace.
    
    Args:
        request: Request with host_name and namespace
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = destroy_all_pods_task.apply_async(
            args=(request.namespace,),
            kwargs={
            },
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit destroy all pods task",
            error_code="DESTROY_ALL_PODS_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace},
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/destroy_container/",
    tags=["Containerd - Containers"],
    summary="Destroy a container by ID",
    description="""
    Destroy a specific container by its container ID.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/destroy_container/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production",
           "cid": "container-id-123"
         }'
    ```
    """,
    response_description="Task submission response"
)
async def destroy_container_api(request: DestroyContainerRequest, user: str = Depends(get_current_user)):
    """
    Destroy a container by ID.
    
    Args:
        request: Request with container ID
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = destroy_container_by_id_task.apply_async(
            args=(request.namespace, request.cid),
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "container_id": request.cid, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit destroy container task",
            error_code="DESTROY_CONTAINER_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace, "container_id": request.cid},
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/purge_stopped/",
    tags=["Containerd - Maintenance"],
    summary="Purge stopped containers and tasks",
    description="""
    Remove all stopped containers and tasks from a namespace.
    
    This is a cleanup operation that removes stopped/exited containers
    and their associated tasks to free up resources.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/purge_stopped/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production"
         }'
    ```
    """,
    response_description="Task submission response"
)
async def purge_stopped_api(request: PurgeStoppedRequest, user: str = Depends(get_current_user)):
    """
    Purge stopped containers and tasks.
    
    Args:
        request: Request with host_name and namespace
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = purge_stopped_tasks_and_containers_task.apply_async(
            args=(request.namespace,),
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit purge stopped task",
            error_code="PURGE_STOPPED_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace},
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/prune_namespace/",
    tags=["Containerd - Maintenance"],
    summary="Prune namespace resources",
    description="""
    Prune unused resources in a namespace (containers, snapshots, images).
    
    When `aggressive=True`, this will also remove stopped containers and
    unused snapshots. Use with caution in production.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/prune_namespace/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production",
           "aggressive": true
         }'
    ```
    """,
    response_description="Task submission response"
)
async def prune_namespace_api(request: PruneNamespaceRequest, user: str = Depends(get_current_user)):
    """
    Prune namespace resources.
    
    Args:
        request: Prune request with namespace and aggressive flag
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = prune_namespace_task.apply_async(
            args=(request.namespace,),
            kwargs={"aggressive": request.aggressive},
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "namespace": request.namespace, "aggressive": request.aggressive}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit prune namespace task",
            error_code="PRUNE_NAMESPACE_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace, "aggressive": request.aggressive},
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/get_container_info/",
    tags=["Containerd - Containers"],
    summary="Get container information",
    description="""
    Retrieve detailed information about a specific container.
    
    Returns container metadata including image, status, resources, and
    other configuration details.
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/get_container_info/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production",
           "cid": "container-id-123"
         }'
    ```
    """,
    response_description="Task submission response"
)
async def get_container_info_api(request: ContainerInfoRequest, user: str = Depends(get_current_user)):
    """
    Get container information.
    
    Args:
        request: Request with container ID
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = get_container_info_task.apply_async(
            args=(request.namespace, request.cid),
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "container_id": request.cid, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit get container info task",
            error_code="GET_CONTAINER_INFO_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace, "container_id": request.cid},
            cause=e
        ) from e


@log_to_file(logger)
@app.post(
    "/containerd/cleanup_tasks_by_pod_prefix/",
    tags=["Containerd - Maintenance"],
    summary="Cleanup tasks by pod prefix",
    description="""
    Clean up stopped tasks that match a pod ID prefix.
    
    This endpoint is useful for cleaning up leftover STOPPED tasks from
    `ctr -n <ns> task list`. It removes tasks matching the pattern:
    - `{pod_id}-*` (all tasks with the pod prefix)
    - `{pod_id}` (the pod task itself if it exists)
    
    **Example Request:**
    ```bash
    curl -X POST "http://localhost:8000/containerd/cleanup_tasks_by_pod_prefix/" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{
           "host_name": "worker-01",
           "namespace": "production",
           "pod_id": "cd83c6a7ac0f47c6",
           "prefer_grpc": true
         }'
    ```
    
    **Use Case:**
    When you see STOPPED tasks in `ctr -n <ns> task list` that should be
    cleaned up, use this endpoint with the pod ID prefix.
    """,
    response_description="Task submission response"
)
async def cleanup_tasks_by_pod_prefix_api(request: CleanupTasksByPodPrefixRequest, user: str = Depends(get_current_user)):
    """
    Cleanup tasks by pod prefix.
    
    Removes stopped tasks matching the pod ID prefix pattern.
    Example: pod_id="cd83c6a7ac0f47c6" removes:
    - cd83c6a7ac0f47c6-* (all tasks with prefix)
    - cd83c6a7ac0f47c6 (pod task itself if exists)
    
    Args:
        request: Cleanup request with pod_id
        user: Authenticated username
        
    Returns:
        Success response with task ID
    """
    try:
        task = cleanup_tasks_by_pod_prefix_task.apply_async(
            args=(request.namespace, request.pod_id),
            kwargs={"prefer_grpc": request.prefer_grpc},
            **_host_queue(request.host_name)
        )
        return create_success_response(
            message="Task submitted successfully",
            data={"task_id": task.id, "pod_id": request.pod_id, "namespace": request.namespace}
        )
    except Exception as e:
        raise TaskSubmissionError(
            message="Failed to submit cleanup tasks by pod prefix task",
            error_code="CLEANUP_TASKS_BY_POD_PREFIX_ERROR",
            details={"host_name": request.host_name, "namespace": request.namespace, "pod_id": request.pod_id},
            cause=e
        ) from e



#
# @log_to_file(logger)
# @app.get("/list_namespaces_and_pods/")
# async def list_namespaces_and_pods(request: HostName, user: str = Depends(get_current_user)):
#     host_queue_info = {
#         'exchange': Exchange('secure_exchange', type='direct'),
#         'queue': ue.encode_hostname_with_key(request.host_name),
#         'routing_key': ue.encode_hostname_with_key(request.host_name),
#         'delivery_mode': 2
#     }
#     try:
#         task = list_namespaces_and_pods.apply_async(
#             args=(),
#             **host_queue_info
#         )
#         return {"message": "Task submitted successfully", "task_id": task.id}
#     except Exception as e:
#         logger.error(f"Error submitting get_usage task: {e}")
#         raise HTTPException(status_code=500, detail="Failed to submit task") from e

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

