from fastapi import FastAPI, Depends, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError as PydanticValidationError
from server.api_models import CreatePodsRequest, ScheduleDeploymentRequest, CreatePVCRequest, CreatePVRequest
from server.sched.scheduler import schedule_deployment_from_yaml
from utils.redis.host_pod_store import HostPodStore  # adjust path to where you saved that class
from utils.celery.tasks.aws_tasks import create_worker_nodes, terminate_worker_node, get_ec2_instances

from utils.extensions.utilities_extention import UtilitiesExtension
from utils.redis.redis_interface import RedisInterface
from utils.ReadConfig import ReadConfig as rc
from utils.celery.celery_config import celery_app


from utils.celery.tasks.worker_node_tasks import (
    get_worker_node_info,
    get_host_ip,
    get_usage,
)
from utils.celery.tasks.containerd_tasks import (
    create_pod_task,
    list_namespaces_and_pods_task,
    list_pods_by_namespace_task,
    terminate_pod_task,
    terminate_pod_by_pause_cid_task,
    destroy_all_pods_task,
    destroy_container_by_id_task,
    purge_stopped_tasks_and_containers_task,
    prune_namespace_task,
    get_container_info_task,
    cleanup_tasks_by_pod_prefix_task,
)

from utils.exceptions import (
    DibbaBaseException,
    AuthenticationError,
    NotFoundError,
    ValidationError,
    exception_to_http_exception
)
from utils.error_handlers import (
    handle_async_errors,
    create_error_response,
    create_success_response
)
from utils.celery.queue_utils import (
    create_queue_info,
    create_host_queue_info,
    submit_celery_task,
    extract_extra_kwargs
)

from typing import Optional, Dict, Any, Union
import jwt
from datetime import datetime, timedelta, timezone
from logpkg.log_kcld import LogKCld, log_to_file
from dataclasses import is_dataclass, asdict


logger = LogKCld()

app = FastAPI(
    title="Dibba Container Orchestration API",
    description="Dibba is a lightweight, Python-based container orchestration layer.",
    version="1.0.0",
    contact={"name": "Dibba Contributors", "url": "https://github.com/tkcreddy/dibba"},
    license_info={"name": "Apache 2.0"},
)

# Mount static files for UI (use existing dibba-ui/dist)
import os
ui_dist_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dibba-ui", "dist")
if os.path.exists(ui_dist_dir):
    app.mount("/dibba", StaticFiles(directory=ui_dist_dir, html=True), name="dibba-ui")
    
    @app.get("/", tags=["UI"])
    async def root():
        """Redirect to UI."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/dibba/")

# OAuth2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Read configuration
read_config = rc()
# aws_config is now loaded dynamically via get_aws_node_config() helper
# which checks Redis first, then falls back to config file
key_read = read_config.encryption_config
redis_db_config = read_config.redis_db_config

ue = UtilitiesExtension(key_read["key"])
rd = RedisInterface(
    redis_db_config["redis_host"],
    redis_db_config["redis_port"],
    redis_db_config["redis_db"]
)

SECRET_KEY = key_read["key"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
if not SECRET_KEY:
    raise ValueError("SECRET_KEY is required!")

# Queue Information
aws_queue_info = create_queue_info("aws_interface", utilities_extension=ue)
store = HostPodStore(rd)

# ==================== Authentication ====================

@log_to_file(logger)
def get_current_user(token: str = Depends(oauth2_scheme)):
    """Get current authenticated user from JWT token.
    
    Args:
        token: JWT token from Authorization header
        
    Returns:
        Username string
        
    Raises:
        AuthenticationError: If token is invalid or expired
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None or not rd.get_user_pass(username):
            raise AuthenticationError(
                message="Invalid authentication token",
                error_code="INVALID_TOKEN",
                details={"username": username},
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

# ==================== User Management ====================

@log_to_file(logger)
@handle_async_errors("list_users", "REDIS_ERROR")
@app.get("/users/", tags=["Users"])
async def list_users(user: str = Depends(get_current_user)) -> Dict[str, Any]:
    """List all users.
    
    Args:
        user: Current authenticated user
        
    Returns:
        Dictionary with list of users
    """
    try:
        # Get all users from Redis authentication hash
        all_users = rd.redis_client.hgetall("authentication")
        users_list = [{"username": username} for username in all_users.keys()]
        
        return _envelope_success(
            message=f"Found {len(users_list)} user(s)",
            data={"users": users_list}
        )
    except Exception as e:
        logger.error(f"Failed to list users: {e}", exc_info=True)
        raise


@log_to_file(logger)
@handle_async_errors("create_user", "REDIS_ERROR")
@app.post("/users/", tags=["Users"])
async def create_user(
    username: str,
    password: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Create a new user.
    
    Args:
        username: Username for the new user
        password: Plain text password (will be hashed)
        user: Current authenticated user
        
    Returns:
        Success message
    """
    try:
        # Check if user already exists
        if rd.get_user_pass(username):
            raise ValidationError(
                message=f"User '{username}' already exists",
                error_code="USER_EXISTS",
                details={"username": username}
            )
        
        # Hash password and save
        hashed_password = ue.encode_phrase_with_key(password)
        rd.save_user_pass(username, hashed_password)
        
        logger.info(f"User '{username}' created by '{user}'")
        return _envelope_success(
            message=f"User '{username}' created successfully",
            data={"username": username}
        )
    except ValidationError:
        raise
    except Exception as e:
        logger.error(f"Failed to create user: {e}", exc_info=True)
        raise


@log_to_file(logger)
@handle_async_errors("update_user_password", "REDIS_ERROR")
@app.put("/users/{username}/password", tags=["Users"])
async def update_user_password(
    username: str,
    new_password: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update user password.
    
    Args:
        username: Username to update
        new_password: New plain text password (will be hashed)
        user: Current authenticated user
        
    Returns:
        Success message
    """
    try:
        # Check if user exists
        if not rd.get_user_pass(username):
            raise NotFoundError(
                message=f"User '{username}' not found",
                error_code="USER_NOT_FOUND",
                details={"username": username}
            )
        
        # Hash new password and save
        hashed_password = ue.encode_phrase_with_key(new_password)
        rd.save_user_pass(username, hashed_password)
        
        logger.info(f"Password updated for user '{username}' by '{user}'")
        return _envelope_success(
            message=f"Password updated for user '{username}'",
            data={"username": username}
        )
    except NotFoundError:
        raise
    except Exception as e:
        logger.error(f"Failed to update user password: {e}", exc_info=True)
        raise


@log_to_file(logger)
@handle_async_errors("delete_user", "REDIS_ERROR")
@app.delete("/users/{username}", tags=["Users"])
async def delete_user(
    username: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Delete a user.
    
    Args:
        username: Username to delete
        user: Current authenticated user
        
    Returns:
        Success message
    """
    try:
        # Prevent deleting yourself
        if username == user:
            raise ValidationError(
                message="Cannot delete your own user account",
                error_code="CANNOT_DELETE_SELF",
                details={"username": username}
            )
        
        # Check if user exists
        if not rd.get_user_pass(username):
            raise NotFoundError(
                message=f"User '{username}' not found",
                error_code="USER_NOT_FOUND",
                details={"username": username}
            )
        
        # Delete user from Redis
        rd.redis_client.hdel("authentication", username)
        
        logger.info(f"User '{username}' deleted by '{user}'")
        return _envelope_success(
            message=f"User '{username}' deleted successfully",
            data={"username": username}
        )
    except (ValidationError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"Failed to delete user: {e}", exc_info=True)
        raise

# ==================== Error Handlers ====================

@app.exception_handler(DibbaBaseException)
async def dibba_exception_handler(request: Request, exc: DibbaBaseException):
    http_exc = exception_to_http_exception(exc)
    return JSONResponse(status_code=http_exc.status_code, content=exc.to_dict())


@app.exception_handler(PydanticValidationError)
async def validation_exception_handler(request: Request, exc: PydanticValidationError):
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

# ==================== Models ====================

class CreateInstanceRequest(BaseModel):
    instance_type: str
    ami_id: str
    key_name: str
    security_group_ids: list[str]
    subnet_id: str
    namespace: str
    min_count: int
    max_count: int
    model_config = ConfigDict(extra="allow")


class TerminateInstanceRequest(BaseModel):
    namespace: str


class ContainerdHostRequest(BaseModel):
    host_name: str
    model_config = ConfigDict(extra="allow")


class PodNamespaceHostRequest(BaseModel):
    host_name: str
    namespace: str
    model_config = ConfigDict(extra="allow")


class TerminatePodRequest(BaseModel):
    host_name: str
    namespace: str
    pod_name: str
    cni_network: Optional[str] = None
    ifname: Optional[str] = None
    model_config = ConfigDict(extra="allow")


class TerminatePodByCidRequest(BaseModel):
    host_name: str
    namespace: str
    pause_cid: str
    cni_network: Optional[str] = None
    ifname: Optional[str] = None
    model_config = ConfigDict(extra="allow")


class DestroyAllPodsRequest(BaseModel):
    host_name: str
    namespace: str
    cni_network: Optional[str] = None
    ifname: Optional[str] = None
    model_config = ConfigDict(extra="allow")


class DestroyContainerRequest(BaseModel):
    host_name: str
    namespace: str
    cid: str
    model_config = ConfigDict(extra="allow")


class PruneNamespaceRequest(BaseModel):
    host_name: str
    namespace: str
    aggressive: bool = True
    model_config = ConfigDict(extra="allow")


class PurgeStoppedRequest(BaseModel):
    host_name: str
    namespace: str
    model_config = ConfigDict(extra="allow")


class ContainerInfoRequest(BaseModel):
    host_name: str
    namespace: str
    cid: str
    model_config = ConfigDict(extra="allow")


class CleanupTasksByPodPrefixRequest(BaseModel):
    host_name: str
    namespace: str
    pod_id: str
    prefer_grpc: bool = True
    model_config = ConfigDict(extra="allow")


# ==================== Helpers ====================

@log_to_file(logger)
def authenticate_user(username: str, password: str) -> Union[str, bool]:
    if not rd.get_user_pass(username):
        return False
    if ue.encode_phrase_with_key(password) == rd.get_user_pass(username):
        return username
    return False


@log_to_file(logger)
def create_access_token(data: Dict[str, Any], expires_delta: timedelta) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@log_to_file(logger)
def as_plain(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [as_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: as_plain(v) for k, v in obj.items()}
    return getattr(obj, "__dict__", obj)


def _envelope_success(message: str, data: Any = None) -> Dict[str, Any]:
    # Uses your existing helper for consistency across the codebase
    return create_success_response(message=message, data=as_plain(data))


def _envelope_error(error_code: str, message: str, details: Any = None, status_code: int = 500) -> Dict[str, Any]:
    # Uses your existing helper for consistency across the codebase
    return create_error_response(
        error_code=error_code,
        message=message,
        details=as_plain(details) if details is not None else None,
        status_code=status_code
    )


# ==================== Auth ====================

@log_to_file(logger)
@handle_async_errors("login", "AUTHENTICATION_ERROR")
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise AuthenticationError(
            message="Invalid username or password",
            error_code="INVALID_CREDENTIALS",
            details={"username": form_data.username},
        )
    access_token = create_access_token(
        data={"sub": form_data.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return _envelope_success(
        message="Authentication successful",
        data={"access_token": access_token, "token_type": "bearer"},
    )


# ==================== Task Status ====================

@app.get("/task/{task_id}", tags=["Task Management"])
async def get_task_status(task_id: str, user: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Get task status from Celery.
    
    This endpoint queries Celery for task status in a non-blocking way.
    For detailed task monitoring, use /flower/task/{task_id}.
    
    Note: This endpoint is frequently polled by the UI. Logging is minimal to reduce log verbosity.
    """
    try:
        task = celery_app.AsyncResult(task_id)
        
        # Get status without blocking
        task_status = task.state
        
        # Get result only if task is ready (SUCCESS or FAILURE)
        task_result = None
        if task.ready():
            try:
                task_result = task.result
            except Exception as e:
                logger.debug(f"Could not get task result for {task_id}: {e}")
                task_result = str(e) if task_status == "FAILURE" else None
        
        # Get progress info if available
        progress_info = None
        if task_status == "PROGRESS":
            try:
                progress_info = task.info
            except Exception:
                pass
        
        payload = {
            "task_id": task.id,
            "status": task_status,
            "result": task_result,
            "progress": progress_info,
        }
        return _envelope_success("Task status retrieved", payload)
    except Exception as e:
        logger.error(f"Failed to get task status for {task_id}: {e}", exc_info=True)
        return create_error_response(
            error_code="GET_TASK_STATUS_ERROR",
            message=f"Failed to get task status: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ==================== Worker Node Endpoints (query params) ====================

# @log_to_file(logger)
# @handle_async_errors("get_worker_node_data", "TASK_SUBMISSION_ERROR")
# @app.get("/get_worker_node_data/", tags=["Worker Nodes"])
# async def get_worker_node_data(host_name: str, user: str = Depends(get_current_user)):
#     host_queue_info = create_host_queue_info(host_name, ue)
#     result = submit_celery_task(
#         task=get_worker_node_info,
#         queue_info=host_queue_info,
#         operation_name="get_worker_node_data",
#         error_code="GET_WORKER_NODE_DATA_ERROR",
#         additional_data={"host_name": host_name},
#     )
#     # submit_celery_task already returns your standard shape (if you wrote it that way),
#     # but to guarantee the contract, we normalize here:
#     return _envelope_success("Task submitted successfully", result.get("data", result))

@log_to_file(logger)
@handle_async_errors("get_worker_node_data", "TASK_SUBMISSION_ERROR")
@app.get("/get_worker_node_data/", tags=["Worker Nodes"])
async def get_worker_node_data(host_name: str, user: str = Depends(get_current_user)):
    host = store.get_host(host_name) or {}
    return _envelope_success("Worker node data retrieved from Redis", host)


# @log_to_file(logger)
# @app.get("/get_worker_node_ip/", tags=["Worker Nodes"])
# async def get_worker_node_ip(host_name: str, user: str = Depends(get_current_user)):
#     host_queue_info = create_host_queue_info(host_name, ue)
#     result = submit_celery_task(
#         task=get_host_ip,
#         queue_info=host_queue_info,
#         operation_name="get_worker_node_ip",
#         error_code="GET_WORKER_NODE_IP_ERROR",
#         additional_data={"host_name": host_name},
#     )
#     return _envelope_success("Task submitted successfully", result.get("data", result))

@log_to_file(logger)
@app.get("/get_worker_node_ip/", tags=["Worker Nodes"])
async def get_worker_node_ip(host_name: str, user: str = Depends(get_current_user)):
    host = store.get_host(host_name) or {}
    return _envelope_success("Worker node IP retrieved from Redis", {"ip_address": host.get("ip_address")})

#
# @log_to_file(logger)
# @app.get("/get_worker_usage_data/", tags=["Worker Nodes"])
# async def get_worker_usage_data(host_name: str, user: str = Depends(get_current_user)):
#     host_queue_info = create_host_queue_info(host_name, ue)
#     result = submit_celery_task(
#         task=get_usage,
#         queue_info=host_queue_info,
#         operation_name="get_worker_usage_data",
#         error_code="GET_WORKER_USAGE_DATA_ERROR",
#         additional_data={"host_name": host_name},
#     )
#     return _envelope_success("Task submitted successfully", result.get("data", result))
#

@log_to_file(logger)
@app.get("/get_worker_usage_data/", tags=["Worker Nodes"])
async def get_worker_usage_data(host_name: str, user: str = Depends(get_current_user)):
    host = store.get_host(host_name) or {}
    return _envelope_success("Worker usage retrieved from Redis", host.get("usage_metrics") or {})

# ==================== AWS Management ====================

@log_to_file(logger)
@handle_async_errors("create_instances", "TASK_SUBMISSION_ERROR")
@app.post("/create-instances/", tags=["AWS Management"])
async def create_instances(request: CreateInstanceRequest, user: str = Depends(get_current_user)):
    request_data = request.model_dump()
    defined_fields = set(CreateInstanceRequest.__annotations__.keys())
    extra_kwargs = extract_extra_kwargs(request_data, defined_fields)

    # Get AWS config (from Redis with fallback to config file)
    from utils.aws.config_helper import get_aws_node_config
    aws_config = get_aws_node_config()
    
    result = submit_celery_task(
        task=create_worker_nodes,
        args=(
            None,  # aws_access_key - deprecated, read from config
            None,  # aws_secret_key - deprecated, read from config
            aws_config.get("region"),  # Optional region override
            request.instance_type,
            request.ami_id,
            request.key_name,
            request.security_group_ids,
            request.subnet_id,
            request.namespace,
        ),
        kwargs={
            "MinCount": request.min_count,
            "MaxCount": request.max_count,
            **extra_kwargs,
        },
        queue_info=aws_queue_info,
        operation_name="create_instances",
        error_code="CREATE_INSTANCES_TASK_ERROR",
        additional_data={"namespace": request.namespace, "instance_type": request.instance_type},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@handle_async_errors("terminate_namespace", "TASK_SUBMISSION_ERROR")
@app.post("/terminate-namespace/", tags=["AWS Management"])
async def terminate_namespace(
    request: TerminateInstanceRequest,
    user: str = Depends(get_current_user)
):
    instances_to_terminate = rd.get_instance_ids_namespace(request.namespace)
    if not instances_to_terminate:
        raise NotFoundError(
            message="No instances found for the given namespace",
            error_code="NO_INSTANCES_FOUND",
            details={"namespace": request.namespace},
        )

    # Get AWS config (from Redis with fallback to config file)
    from utils.aws.config_helper import get_aws_node_config
    aws_config = get_aws_node_config()

    result = submit_celery_task(
        task=terminate_worker_node,
        args=(
            None,  # aws_access_key - deprecated, read from config
            None,  # aws_secret_key - deprecated, read from config
            aws_config.get("region"),  # Optional region override
            instances_to_terminate,
        ),
        queue_info=aws_queue_info,
        operation_name="terminate_namespace",
        error_code="TERMINATE_INSTANCES_TASK_ERROR",
        additional_data={"instances_count": len(instances_to_terminate)},
        )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@handle_async_errors("list_ec2_instances", "TASK_SUBMISSION_ERROR")
@app.get("/aws/instances/", tags=["AWS Management"])
async def list_ec2_instances(user: str = Depends(get_current_user)):
    """List all EC2 instances."""
    # Get AWS config (from Redis with fallback to config file)
    from utils.aws.config_helper import get_aws_node_config
    aws_config = get_aws_node_config()
    
    result = submit_celery_task(
        task=get_ec2_instances,
        args=(
            None,  # aws_access_key - deprecated, read from config
            None,  # aws_secret_key - deprecated, read from config
            aws_config.get("region"),  # Optional region override
        ),
        kwargs={},
        queue_info=aws_queue_info,
        operation_name="list_ec2_instances",
        error_code="LIST_EC2_INSTANCES_ERROR",
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@handle_async_errors("terminate_instance_ids", "TASK_SUBMISSION_ERROR")
@app.post("/aws/terminate-instances/", tags=["AWS Management"])
async def terminate_instance_ids(
    request: Dict[str, Any],
    user: str = Depends(get_current_user)
):
    """Terminate specific EC2 instance IDs."""
    # Get AWS config (from Redis with fallback to config file)
    from utils.aws.config_helper import get_aws_node_config
    aws_config = get_aws_node_config()
    
    instance_ids = request.get("instance_ids", [])
    if not instance_ids or not isinstance(instance_ids, list):
        raise NotFoundError(
            message="instance_ids must be a non-empty list",
            error_code="INVALID_INSTANCE_IDS",
        )

    result = submit_celery_task(
        task=terminate_worker_node,
        args=(
            None,  # aws_access_key - deprecated, read from config
            None,  # aws_secret_key - deprecated, read from config
            aws_config.get("region"),  # Optional region override
            instance_ids,
        ),
        queue_info=aws_queue_info,
        operation_name="terminate_instance_ids",
        error_code="TERMINATE_INSTANCES_TASK_ERROR",
        additional_data={"instances_count": len(instance_ids)},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


# ==================== Hosts ====================

@log_to_file(logger)
@app.get("/hosts/", tags=["Hosts"])
async def get_all_hosts(user: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Get all hosts from Redis.
    
    Filters out hosts that haven't been updated in the last 180 seconds (3 minutes)
    and optionally deletes stale hosts from Redis.
    
    Returns:
        Dictionary with list of active hosts and their information
    """
    from datetime import datetime, timezone, timedelta
    
    try:
        hosts = store.get_all_hosts()
        
        # Calculate cutoff time (180 seconds ago)
        cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=180)
        stale_hosts = []
        active_hosts = []
        
        # Filter hosts by last_updated timestamp
        for host in hosts:
            hostname = host.get("hostname")
            last_updated_str = host.get("last_updated")
            
            if not last_updated_str:
                # If no last_updated, consider it stale
                logger.warning(f"Host {hostname} has no last_updated timestamp, marking as stale")
                stale_hosts.append(hostname)
                continue
            
            try:
                # Parse ISO format timestamp
                last_updated = datetime.fromisoformat(last_updated_str.replace('Z', '+00:00'))
                
                # Check if host is stale (not updated in last 180 seconds)
                if last_updated < cutoff_time:
                    logger.info(f"Host {hostname} is stale (last updated: {last_updated_str}, cutoff: {cutoff_time.isoformat()})")
                    stale_hosts.append(hostname)
                else:
                    active_hosts.append(host)
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse last_updated for host {hostname}: {last_updated_str}, error: {e}")
                # If we can't parse the timestamp, consider it stale
                stale_hosts.append(hostname)
        
        # Delete stale hosts from Redis
        if stale_hosts:
            logger.info(f"Deleting {len(stale_hosts)} stale hosts from Redis: {stale_hosts}")
            for hostname in stale_hosts:
                try:
                    store.delete_host(hostname)
                    logger.info(f"Deleted stale host {hostname} from Redis")
                except Exception as e:
                    logger.error(f"Failed to delete stale host {hostname}: {e}", exc_info=True)
        
        # Format active hosts for UI (extract key fields)
        host_list = []
        for host in active_hosts:
            host_list.append({
                "hostname": host.get("hostname"),
                "ip_address": host.get("ip_address"),
                "status": host.get("status", "unknown"),
                "last_updated": host.get("last_updated"),
            })
        
        # Sort by hostname for consistent ordering
        host_list.sort(key=lambda x: x.get("hostname", ""))
        
        payload = {
            "hosts": host_list,
            "host_count": len(host_list),
            "stale_hosts_deleted": len(stale_hosts),
        }
        return _envelope_success("Hosts retrieved from Redis", payload)
    except Exception as e:
        logger.error(f"Failed to get hosts: {e}", exc_info=True)
        raise


# ==================== Containerd - Pods ====================

@log_to_file(logger)
@handle_async_errors("create_pods", "TASK_SUBMISSION_ERROR")
@app.post("/containerd/create-pods/", tags=["Containerd - Pods"])
async def create_pods(request: CreatePodsRequest, user: str = Depends(get_current_user)):
    host_queue_info = create_host_queue_info(request.host_name, ue)

    containers_payload = [c.model_dump() for c in request.containers]
    namespace = request.namespace

    request_data = request.model_dump()
    defined_fields = set(CreatePodsRequest.__annotations__.keys())
    extra_kwargs = extract_extra_kwargs(request_data, defined_fields)
    extra_kwargs = extra_kwargs or {"host_name": request.host_name}

    result = submit_celery_task(
        task=create_pod_task,
        args=(containers_payload, namespace),
        kwargs={"host_name": request.host_name, **extra_kwargs},
        queue_info=host_queue_info,
        operation_name="create_pods",
        error_code="CREATE_PODS_TASK_ERROR",
        additional_data={
            "host_name": request.host_name,
            "namespace": namespace,
            "containers_count": len(containers_payload),
        },
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


# @log_to_file(logger)
# @app.post("/containerd/list_namespaces_and_pods/", tags=["Containerd - Pods"])
# async def list_namespaces_and_pods_api(request: ContainerdHostRequest, user: str = Depends(get_current_user)):
#     result = submit_celery_task(
#         task=list_namespaces_and_pods_task,
#         queue_info=create_host_queue_info(request.host_name, ue),
#         operation_name="list_namespaces_and_pods",
#         error_code="LIST_NAMESPACES_PODS_ERROR",
#         additional_data={"host_name": request.host_name},
#     )
#     return _envelope_success("Task submitted successfully", result.get("data", result))
#

@log_to_file(logger)
def _filter_pause_containers(containers: list, pause_container: dict = None) -> list:
    """Filter out pause containers from the containers list.
    
    Args:
        containers: List of container dictionaries
        pause_container: Optional pause container info dict
        
    Returns:
        Filtered list of containers (excluding pause containers)
    """
    if not containers:
        return []
    
    pause_cid = None
    if pause_container and isinstance(pause_container, dict):
        pause_cid = pause_container.get("cid") or pause_container.get("container_id")
    
    filtered = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        
        # Check if this is a pause container
        container_name = str(container.get("name", "")).lower()
        container_image = str(container.get("image", "")).lower()
        container_cid = container.get("cid") or container.get("container_id")
        container_labels = container.get("labels", {})
        if not isinstance(container_labels, dict):
            container_labels = {}
        
        # Skip if it's a pause container
        is_pause = (
            "pause" in container_name or
            "pause" in container_image or
            container_labels.get("role") == "pause" or
            (pause_cid and container_cid == pause_cid)
        )
        
        if not is_pause:
            filtered.append(container)
    
    return filtered


@app.post("/containerd/list_namespaces_and_pods/", tags=["Containerd - Pods"])
async def list_namespaces_and_pods_api(
    request: ContainerdHostRequest,
    user: str = Depends(get_current_user),
):
    # Host scoped view (what UI uses)
    pods = store.get_pods_by_host(request.host_name)

    # Build namespaces + inventory grouped by namespace
    inventory: Dict[str, Any] = {}
    namespaces = set()

    # Try to initialize etcd interface for IP lookup fallback
    etcd_interface = None
    try:
        from utils.etcd.etcd_interface import get_etcd_interface_from_config
        etcd_interface = get_etcd_interface_from_config()
    except Exception as e:
        logger.debug(f"ETCD interface not available for IP lookup: {e}")

    for p in pods:
        ns = p.get("namespace") or "default"
        namespaces.add(ns)

        # Get IP address - try Redis first, then etcd, then network namespace
        ip_address = p.get("ip_address")
        pod_id = p.get("pod_id")
        hostname = p.get("hostname") or request.host_name
        
        # Log IP retrieval for debugging
        if ip_address:
            logger.info(f"Retrieved IP {ip_address} from Redis for pod {pod_id}")
        else:
            logger.debug(f"No IP address in Redis for pod {pod_id}, will try fallback methods")
        
        if not ip_address:
            # Try etcd first
            if etcd_interface:
                try:
                    if pod_id and hostname:
                        # Try to get IP from etcd
                        etcd_pod_ips = etcd_interface.get_pods_by_node(hostname)
                        if etcd_pod_ips:
                            logger.debug(f"Found {len(etcd_pod_ips)} pod IPs in etcd for node {hostname}")
                            # Try exact match first
                            if pod_id in etcd_pod_ips:
                                ip_address = etcd_pod_ips[pod_id]
                                logger.info(f"Found IP {ip_address} for pod {pod_id} via exact match in etcd")
                            else:
                                # Try partial match
                                for etcd_pod_name, etcd_ip in etcd_pod_ips.items():
                                    if pod_id in etcd_pod_name or etcd_pod_name in pod_id:
                                        ip_address = etcd_ip
                                        logger.info(f"Found IP {ip_address} for pod {pod_id} via partial match with {etcd_pod_name} in etcd")
                                        break
                        else:
                            logger.debug(f"No pod IPs found in etcd for node {hostname}")
                except Exception as e:
                    logger.warning(f"Failed to get IP from etcd for pod {pod_id}: {e}", exc_info=True)
            
            # Fallback: Extract IP from CNI network info stored in Redis
            if not ip_address:
                try:
                    cni_network = p.get("cni_network", {})
                    if isinstance(cni_network, dict):
                        # Try to extract IP from CNI result (similar to containerd_tasks._extract_ipv4_from_cni_result)
                        ifname = cni_network.get("ifname", "eth0")
                        cni_result = cni_network.get("cni_result") or cni_network
                        
                        # Extract IP from CNI result structure
                        ips = cni_result.get("ips") or []
                        for ip in ips:
                            addr = ip.get("address")
                            version = ip.get("version")
                            if addr and (version == "4" or ":" not in addr):
                                ip_address = addr.split("/", 1)[0]
                                logger.info(f"Extracted IP {ip_address} from CNI network info for pod {pod_id}")
                                # Save it to Redis for future use
                                try:
                                    store.save_pod(
                                        pod_id=pod_id,
                                        namespace=ns,
                                        hostname=hostname,
                                        ip_address=ip_address,
                                        pause_container=p.get("pause_container"),
                                        containers=p.get("containers"),
                                        status=p.get("status") or None  # Let save_pod determine from containers if None
                                    )
                                except Exception as save_err:
                                    logger.debug(f"Failed to save IP to Redis: {save_err}")
                                break
                        
                        # If not found in ips, try interfaces
                        if not ip_address:
                            ifaces = cni_result.get("interfaces") or []
                            for itf in ifaces:
                                if itf.get("name") == ifname:
                                    for addr in (itf.get("addresses") or itf.get("address") or []):
                                        if isinstance(addr, str) and ":" not in addr:
                                            ip_address = addr.split("/", 1)[0]
                                            logger.info(f"Extracted IP {ip_address} from CNI interface info for pod {pod_id}")
                                            # Save it to Redis
                                            try:
                                                store.save_pod(
                                                    pod_id=pod_id,
                                                    namespace=ns,
                                                    hostname=hostname,
                                                    ip_address=ip_address,
                                                    pause_container=p.get("pause_container"),
                                                    containers=p.get("containers"),
                                                    status=p.get("status") or None  # Let save_pod determine from containers if None
                                                )
                                            except Exception as save_err:
                                                logger.debug(f"Failed to save IP to Redis: {save_err}")
                                            break
                                        elif isinstance(addr, dict) and "address" in addr and ":" not in addr["address"]:
                                            ip_address = addr["address"].split("/", 1)[0]
                                            logger.info(f"Extracted IP {ip_address} from CNI interface dict for pod {pod_id}")
                                            # Save it to Redis
                                            try:
                                                store.save_pod(
                                                    pod_id=pod_id,
                                                    namespace=ns,
                                                    hostname=hostname,
                                                    ip_address=ip_address,
                                                    pause_container=p.get("pause_container"),
                                                    containers=p.get("containers"),
                                                    status=p.get("status") or None  # Let save_pod determine from containers if None
                                                )
                                            except Exception as save_err:
                                                logger.debug(f"Failed to save IP to Redis: {save_err}")
                                            break
                                    if ip_address:
                                        break
                except Exception as e:
                    logger.debug(f"Failed to extract IP from CNI network info for pod {pod_id}: {e}")
        else:
            logger.debug(f"Pod {pod_id} already has IP {ip_address} in Redis")

        # Extract ports from containers
        containers = p.get("containers") or []
        pause_container = p.get("pause_container")
        # Filter out pause containers
        containers = _filter_pause_containers(containers, pause_container)
        ports = []
        for container in containers:
            if isinstance(container, dict):
                # Check for ports in container dict
                container_ports = container.get("ports") or []
                if isinstance(container_ports, list):
                    for port in container_ports:
                        if isinstance(port, dict):
                            port_num = port.get("containerPort") or port.get("port")
                            protocol = port.get("protocol", "TCP")
                            if port_num:
                                ports.append(f"{port_num}/{protocol}")
                        elif isinstance(port, (int, str)):
                            ports.append(str(port))
        
        # If no ports found in containers, try to get from deployment store
        if not ports:
            try:
                from utils.redis.deployment_store import DeploymentStore
                from utils.redis.host_pod_store import RedisKeyPatterns as PodRedisKeyPatterns
                deployment_store = DeploymentStore(store.redis_interface)
                
                # Try to get app_label from pod labels or pod data
                app_label = None
                pod_labels = p.get("labels")
                if isinstance(pod_labels, dict):
                    app_label = pod_labels.get("app")
                
                # If not found in labels, try to get from pod data directly
                if not app_label:
                    app_label = p.get("app_label")
                
                # If still not found, try reverse lookup from pod index
                if not app_label and pod_id:
                    try:
                        # Get all app indexes and check which one contains this pod
                        app_index_pattern = "pod:index:app:*"
                        for app_index_key in store.redis_interface.redis_client.scan_iter(match=app_index_pattern):
                            if store.redis_interface.redis_client.sismember(app_index_key, pod_id):
                                # Extract app_name from key pattern: pod:index:app:{app_name}
                                app_label = app_index_key.split(":")[-1]
                                logger.debug(f"Found app_label {app_label} for pod {pod_id} via reverse lookup")
                                break
                    except Exception as lookup_err:
                        logger.debug(f"Failed to reverse lookup app_label for pod {pod_id}: {lookup_err}")
                
                if app_label and ns:
                    # Try to find deployment by namespace and app_label (most efficient)
                    logger.debug(f"Looking for deployment with app_label={app_label}, namespace={ns} for pod {pod_id}")
                    deployments = deployment_store.get_deployments_by_namespace(ns)
                    logger.debug(f"Found {len(deployments)} deployments in namespace {ns}")
                    for deployment in deployments:
                        dep_app_label = deployment.get("app_label")
                        dep_namespace = deployment.get("namespace")
                        logger.debug(f"Checking deployment: app_label={dep_app_label}, namespace={dep_namespace}")
                        if dep_app_label == app_label and dep_namespace == ns:
                            deployment_spec = deployment.get("deployment_spec", {})
                            logger.debug(f"Deployment spec keys: {list(deployment_spec.keys())}")
                            containers_spec = deployment_spec.get("containers", [])
                            logger.debug(f"Found {len(containers_spec)} containers in deployment spec")
                            for container_spec in containers_spec:
                                if isinstance(container_spec, dict):
                                    logger.debug(f"Container spec keys: {list(container_spec.keys())}")
                                    container_ports = container_spec.get("ports", [])
                                    logger.debug(f"Container {container_spec.get('name')} has ports: {container_ports}")
                                    if isinstance(container_ports, list):
                                        for port in container_ports:
                                            if isinstance(port, dict):
                                                port_num = port.get("containerPort") or port.get("port")
                                                protocol = port.get("protocol", "TCP")
                                                if port_num:
                                                    ports.append(f"{port_num}/{protocol}")
                                                    logger.debug(f"Added port {port_num}/{protocol} for pod {pod_id}")
                                            elif isinstance(port, (int, str)):
                                                ports.append(str(port))
                                                logger.debug(f"Added port {port} for pod {pod_id}")
                            if ports:
                                logger.info(f"Found ports {ports} for pod {pod_id} from deployment store (app: {app_label}, namespace: {ns})")
                                break
                    
                    # Fallback: Try to find by app_label only if namespace search didn't work
                    if not ports:
                        deployments = deployment_store.get_deployments_by_app(app_label)
                        for deployment in deployments:
                            deployment_spec = deployment.get("deployment_spec", {})
                            containers_spec = deployment_spec.get("containers", [])
                            for container_spec in containers_spec:
                                if isinstance(container_spec, dict):
                                    container_ports = container_spec.get("ports", [])
                                    if isinstance(container_ports, list):
                                        for port in container_ports:
                                            if isinstance(port, dict):
                                                port_num = port.get("containerPort") or port.get("port")
                                                protocol = port.get("protocol", "TCP")
                                                if port_num:
                                                    ports.append(f"{port_num}/{protocol}")
                                            elif isinstance(port, (int, str)):
                                                ports.append(str(port))
                            if ports:
                                logger.info(f"Found ports {ports} for pod {pod_id} from deployment store (app: {app_label})")
                                break
                elif ns:
                    # Last resort: Try all deployments in namespace and match by container name/image
                    deployments = deployment_store.get_deployments_by_namespace(ns)
                    for deployment in deployments:
                        deployment_spec = deployment.get("deployment_spec", {})
                        containers_spec = deployment_spec.get("containers", [])
                        # Try to match by container name or image
                        for container in containers:
                            if isinstance(container, dict):
                                container_name = container.get("name")
                                container_image = container.get("image")
                                for container_spec in containers_spec:
                                    if isinstance(container_spec, dict):
                                        spec_name = container_spec.get("name")
                                        spec_image = container_spec.get("image")
                                        if (container_name and spec_name and container_name == spec_name) or \
                                           (container_image and spec_image and container_image == spec_image):
                                            container_ports = container_spec.get("ports", [])
                                            if isinstance(container_ports, list):
                                                for port in container_ports:
                                                    if isinstance(port, dict):
                                                        port_num = port.get("containerPort") or port.get("port")
                                                        protocol = port.get("protocol", "TCP")
                                                        if port_num:
                                                            ports.append(f"{port_num}/{protocol}")
                                                    elif isinstance(port, (int, str)):
                                                        ports.append(str(port))
                                            if ports:
                                                logger.info(f"Found ports {ports} for pod {pod_id} from deployment store by container match (namespace: {ns})")
                                                break
                                if ports:
                                    break
                        if ports:
                            break
            except Exception as e:
                logger.debug(f"Could not fetch ports from deployment store for pod {pod_id}: {e}")
        
        # Extract app_label for grouping
        app_label = None
        pod_labels = p.get("labels")
        if isinstance(pod_labels, dict):
            app_label = pod_labels.get("app")
        
        # If not in labels, try pod data directly
        if not app_label:
            app_label = p.get("app_label")
        
        # If still not found, try reverse lookup from pod index
        if not app_label and pod_id:
            try:
                app_index_pattern = "pod:index:app:*"
                for app_index_key in store.redis_interface.redis_client.scan_iter(match=app_index_pattern):
                    if store.redis_interface.redis_client.sismember(app_index_key, pod_id):
                        app_label = app_index_key.split(":")[-1]
                        logger.debug(f"Found app_label {app_label} for pod {pod_id} via reverse lookup in list_namespaces_and_pods_api")
                        break
            except Exception as lookup_err:
                logger.debug(f"Failed to reverse lookup app_label for pod {pod_id}: {lookup_err}")
        
        # Get deployment name if available (use metadata.name from deployment YAML)
        deployment_name = None
        if ns:
            try:
                from utils.redis.deployment_store import DeploymentStore
                deployment_store = DeploymentStore(store.redis_interface)
                
                # First try to match by app_label if we have it
                if app_label:
                    deployments = deployment_store.get_deployments_by_namespace(ns)
                    for deployment in deployments:
                        if deployment.get("app_label") == app_label:
                            deployment_name = deployment.get("name")
                            logger.debug(f"Matched pod {pod_id} to deployment {deployment_name} by app_label {app_label} in list_namespaces_and_pods_api")
                            break
                    if not deployment_name:
                        # Fallback: try by app_label only
                        deployments_by_app = deployment_store.get_deployments_by_app(app_label)
                        for deployment in deployments_by_app:
                            if deployment.get("namespace") == ns:
                                deployment_name = deployment.get("name")
                                logger.debug(f"Matched pod {pod_id} to deployment {deployment_name} by app_label {app_label} (fallback) in list_namespaces_and_pods_api")
                                break
                
                # If still no match and we have containers, try to match by container name/image
                # Also try matching even if app_label exists but is empty/None (might be wrong)
                if (not deployment_name and not app_label) or (app_label and app_label.strip() == ""):
                    deployments = deployment_store.get_deployments_by_namespace(ns)
                    logger.info(f"Trying container matching for pod {pod_id} in list_namespaces_and_pods_api: found {len(deployments)} deployments in namespace {ns}")
                    logger.info(f"Pod containers data: {containers}")
                    
                    if not deployments:
                        # Try all namespaces as fallback (in case namespace mismatch)
                        logger.debug(f"No deployments in namespace {ns}, trying all deployments")
                        all_deployments = deployment_store.get_all_deployments()
                        logger.info(f"Found {len(all_deployments)} total deployments across all namespaces")
                        deployments = all_deployments
                    
                    for deployment in deployments:
                        deployment_spec = deployment.get("deployment_spec", {})
                        containers_spec = deployment_spec.get("containers", [])
                        dep_name = deployment.get("name")
                        dep_app_label = deployment.get("app_label")
                        dep_namespace = deployment.get("namespace")
                        logger.info(f"Checking deployment {dep_name} (namespace: {dep_namespace}, app_label: {dep_app_label}) with {len(containers_spec)} container specs")
                        
                        # Try to match by container name or image
                        for container in containers:
                            if isinstance(container, dict):
                                # Try multiple possible keys for container name
                                container_name = container.get("name") or container.get("id") or ""
                                # Container ID might be like "pod-id-container-name", extract just the container name part
                                if container_name and "-" in container_name:
                                    # Try to extract container name from ID (format: pod-id-container-name)
                                    parts = container_name.split("-")
                                    if len(parts) > 2:
                                        # Assume last part or last two parts are the container name
                                        container_name_alt = "-".join(parts[-2:])  # Try last 2 parts
                                        container_name_single = parts[-1]  # Try last part
                                    else:
                                        container_name_alt = container_name
                                        container_name_single = container_name
                                else:
                                    container_name_alt = container_name
                                    container_name_single = container_name
                                
                                container_image = container.get("image") or ""
                                logger.info(f"Pod container: name={container_name} (alt: {container_name_alt}, single: {container_name_single}), image={container_image}")
                                
                                for container_spec in containers_spec:
                                    if isinstance(container_spec, dict):
                                        spec_name = container_spec.get("name") or ""
                                        spec_image = container_spec.get("image") or ""
                                        logger.info(f"Deployment container spec: name={spec_name}, image={spec_image}")
                                        
                                        # Try exact match first
                                        name_match = (container_name and spec_name and container_name == spec_name) or \
                                                    (container_name_alt and spec_name and container_name_alt == spec_name) or \
                                                    (container_name_single and spec_name and container_name_single == spec_name)
                                        image_match = container_image and spec_image and container_image == spec_image
                                        
                                        # If no exact match, try partial image match (in case of tags/digests)
                                        if not name_match and not image_match and container_image and spec_image:
                                            # Extract base image name (before tag/digest)
                                            container_base = container_image.split("@")[0].split(":")[0]
                                            spec_base = spec_image.split("@")[0].split(":")[0]
                                            image_match = container_base == spec_base
                                            logger.info(f"Trying base image match: {container_base} == {spec_base} -> {image_match}")
                                        
                                        if name_match or image_match:
                                            deployment_name = dep_name
                                            # Also set app_label from the deployment's app_label
                                            if dep_app_label:
                                                app_label = dep_app_label
                                                logger.info(f"Matched pod {pod_id} to deployment {deployment_name} (app_label: {app_label}) by container match (name_match={name_match}, image_match={image_match}, container={container_name}/{container_image}) in list_namespaces_and_pods_api")
                                                
                                                # Persist the matched labels to Redis so future lookups work
                                                try:
                                                    existing_labels = p.get("labels", {})
                                                    if not isinstance(existing_labels, dict):
                                                        existing_labels = {}
                                                    
                                                    # Update labels with the matched app_label
                                                    updated_labels = {
                                                        **existing_labels,
                                                        "app": dep_app_label,
                                                        "app_label": dep_app_label,
                                                    }
                                                    
                                                    # Save updated pod with labels to Redis
                                                    store.save_pod(
                                                        pod_id=pod_id,
                                                        namespace=ns,
                                                        hostname=p.get("hostname") or request.host_name,
                                                        ip_address=ip_address,
                                                        pause_container=p.get("pause_container"),
                                                        containers=containers,
                                                        labels=updated_labels,
                                                        status=p.get("status", "unknown"),
                                                        creation_time=p.get("creation_time") or p.get("created_at"),
                                                        startup_time=p.get("startup_time")
                                                    )
                                                    logger.info(f"Updated pod {pod_id} in Redis with app_label {dep_app_label} in list_namespaces_and_pods_api")
                                                except Exception as save_err:
                                                    logger.warning(f"Failed to persist labels to Redis for pod {pod_id} in list_namespaces_and_pods_api: {save_err}", exc_info=True)
                                            else:
                                                logger.info(f"Matched pod {pod_id} to deployment {deployment_name} by container match (name_match={name_match}, image_match={image_match}, container={container_name}/{container_image}) in list_namespaces_and_pods_api")
                                            break
                                if deployment_name:
                                    break
                        if deployment_name:
                            break
            except Exception as e:
                logger.debug(f"Failed to get deployment_name for pod {pod_id} in list_namespaces_and_pods_api: {e}", exc_info=True)
        
        # Normalize pod record for UI (you can add/remove fields here)
        pod_view = {
            "pod_id": p.get("pod_id"),
            "pod_name": p.get("pod_name") or p.get("pod_id"),
            "namespace": ns,
            "hostname": p.get("hostname") or request.host_name,
            "ip_address": ip_address,  # Use the IP we fetched (from Redis or etcd)
            "ports": sorted(list(set(ports))),  # Ensure unique and sorted ports
            "status": p.get("status") or "unknown",
            "pause_container": p.get("pause_container"),
            "containers": containers,
            "creation_time": p.get("creation_time") or p.get("created_at"),
            "startup_time": p.get("startup_time"),
            "app_label": app_label,  # Include app_label for UI grouping
            "deployment_name": deployment_name,  # Use metadata.name from deployment YAML
        }

        inventory.setdefault(ns, []).append(pod_view)

    payload = {
        "host_name": request.host_name,
        "namespaces": sorted(list(namespaces)),
        "inventory": inventory,
        "pod_count": sum(len(v) for v in inventory.values()),
    }
    return _envelope_success("Namespaces and pods retrieved from Redis", payload)


@log_to_file(logger)
@app.get("/containerd/list_pods_by_filter/", tags=["Containerd - Pods"])
async def list_pods_by_filter_api(
    namespace: Optional[str] = None,
    app_name: Optional[str] = None,
    user: str = Depends(get_current_user),
):
    """List pods filtered by namespace and/or app_name across all hosts.
    
    Args:
        namespace: Optional namespace filter
        app_name: Optional app name filter
        user: Authenticated user
        
    Returns:
        List of pods matching the filters with IP address, ports, creation time, and startup time
    """
    try:
        from utils.redis.deployment_store import DeploymentStore
        
        deployment_store = DeploymentStore(rd)
        
        # Get all hosts
        all_hosts = store.get_all_hosts()
        all_pods = []
        seen_pod_ids = set()  # Track seen pod IDs to avoid duplicates
        
        # Collect pods from all hosts, deduplicating by pod_id
        for host in all_hosts:
            hostname = host.get("hostname")
            if hostname:
                host_pods = store.get_pods_by_host(hostname)
                for p in host_pods:
                    pod_id = p.get("pod_id")
                    if pod_id and pod_id not in seen_pod_ids:
                        seen_pod_ids.add(pod_id)
                        all_pods.append(p)
        
        # Apply filters
        filtered_pods = []
        for p in all_pods:
            pod_ns = p.get("namespace") or "default"
            pod_labels = p.get("labels", {})
            pod_app_label = None
            
            if isinstance(pod_labels, dict):
                pod_app_label = pod_labels.get("app")
            
            # If not in labels, try reverse lookup from pod index
            if not pod_app_label:
                pod_id = p.get("pod_id")
                if pod_id:
                    try:
                        app_index_pattern = "pod:index:app:*"
                        for app_index_key in rd.redis_client.scan_iter(match=app_index_pattern):
                            if rd.redis_client.sismember(app_index_key, pod_id):
                                pod_app_label = app_index_key.split(":")[-1]
                                break
                    except Exception:
                        pass
            
            # Apply namespace filter
            if namespace and pod_ns != namespace:
                continue
            
            # Apply app_name filter - match by app_label or deployment_name
            if app_name:
                # First try to match by app_label
                if pod_app_label == app_name:
                    pass  # Match found
                else:
                    # Try to match by deployment_name
                    pod_deployment_name = None
                    if pod_ns:
                        try:
                            # Get deployment name from deployment store
                            deployments = deployment_store.get_deployments_by_namespace(pod_ns)
                            for deployment in deployments:
                                dep_app_label = deployment.get("app_label")
                                if dep_app_label == pod_app_label:
                                    pod_deployment_name = deployment.get("name")
                                    break
                            # If not found, try by app_label only
                            if not pod_deployment_name and pod_app_label:
                                deployments_by_app = deployment_store.get_deployments_by_app(pod_app_label)
                                for deployment in deployments_by_app:
                                    if deployment.get("namespace") == pod_ns:
                                        pod_deployment_name = deployment.get("name")
                                        break
                        except Exception:
                            pass
                    
                    # If still no match, skip this pod
                    if pod_deployment_name != app_name:
                        continue
            
            # Extract IP address (same logic as list_namespaces_and_pods_api)
            ip_address = p.get("ip_address")
            pod_id = p.get("pod_id")
            hostname = p.get("hostname")
            
            # Try etcd if IP not found
            if not ip_address:
                try:
                    from utils.etcd.etcd_interface import get_etcd_interface_from_config
                    etcd_interface = get_etcd_interface_from_config()
                    if etcd_interface and pod_id and hostname:
                        etcd_pod_ips = etcd_interface.get_pods_by_node(hostname)
                        if etcd_pod_ips:
                            if pod_id in etcd_pod_ips:
                                ip_address = etcd_pod_ips[pod_id]
                            else:
                                for etcd_pod_name, etcd_ip in etcd_pod_ips.items():
                                    if pod_id in etcd_pod_name or etcd_pod_name in pod_id:
                                        ip_address = etcd_ip
                                        break
                except Exception:
                    pass
            
            # Extract ports (same logic as list_namespaces_and_pods_api)
            containers = p.get("containers") or []
            pause_container = p.get("pause_container")
            # Filter out pause containers
            containers = _filter_pause_containers(containers, pause_container)
            ports = []
            for container in containers:
                if isinstance(container, dict):
                    container_ports = container.get("ports") or []
                    if isinstance(container_ports, list):
                        for port in container_ports:
                            if isinstance(port, dict):
                                port_num = port.get("containerPort") or port.get("port")
                                protocol = port.get("protocol", "TCP")
                                if port_num:
                                    ports.append(f"{port_num}/{protocol}")
                            elif isinstance(port, (int, str)):
                                ports.append(str(port))
            
            # Try to get ports from deployment store if not found
            if not ports and pod_app_label and pod_ns:
                try:
                    logger.debug(f"Looking for ports in deployment store for pod {pod_id}: app_label={pod_app_label}, namespace={pod_ns}")
                    deployments = deployment_store.get_deployments_by_namespace(pod_ns)
                    logger.debug(f"Found {len(deployments)} deployments in namespace {pod_ns}")
                    for deployment in deployments:
                        dep_app_label = deployment.get("app_label")
                        dep_namespace = deployment.get("namespace")
                        logger.debug(f"Checking deployment: app_label={dep_app_label}, namespace={dep_namespace}")
                        if dep_app_label == pod_app_label and dep_namespace == pod_ns:
                            deployment_spec = deployment.get("deployment_spec", {})
                            logger.debug(f"Deployment spec keys: {list(deployment_spec.keys())}")
                            containers_spec = deployment_spec.get("containers", [])
                            logger.debug(f"Found {len(containers_spec)} containers in deployment spec")
                            for container_spec in containers_spec:
                                if isinstance(container_spec, dict):
                                    container_name = container_spec.get("name", "unknown")
                                    container_ports = container_spec.get("ports", [])
                                    logger.debug(f"Container {container_name} has ports: {container_ports}")
                                    if isinstance(container_ports, list):
                                        for port in container_ports:
                                            if isinstance(port, dict):
                                                port_num = port.get("containerPort") or port.get("port")
                                                protocol = port.get("protocol", "TCP")
                                                if port_num:
                                                    ports.append(f"{port_num}/{protocol}")
                                                    logger.debug(f"Added port {port_num}/{protocol} for pod {pod_id}")
                                            elif isinstance(port, (int, str)):
                                                ports.append(str(port))
                                                logger.debug(f"Added port {port} for pod {pod_id}")
                            if ports:
                                logger.info(f"Found ports {ports} for pod {pod_id} from deployment store (app: {pod_app_label}, namespace: {pod_ns})")
                                break
                    
                    # Fallback: Try to find by app_label only if namespace search didn't work
                    if not ports:
                        logger.debug(f"Trying fallback search by app_label only: {pod_app_label}")
                        deployments = deployment_store.get_deployments_by_app(pod_app_label)
                        for deployment in deployments:
                            deployment_spec = deployment.get("deployment_spec", {})
                            containers_spec = deployment_spec.get("containers", [])
                            for container_spec in containers_spec:
                                if isinstance(container_spec, dict):
                                    container_ports = container_spec.get("ports", [])
                                    if isinstance(container_ports, list):
                                        for port in container_ports:
                                            if isinstance(port, dict):
                                                port_num = port.get("containerPort") or port.get("port")
                                                protocol = port.get("protocol", "TCP")
                                                if port_num:
                                                    ports.append(f"{port_num}/{protocol}")
                                            elif isinstance(port, (int, str)):
                                                ports.append(str(port))
                            if ports:
                                logger.info(f"Found ports {ports} for pod {pod_id} from deployment store (app: {pod_app_label}, fallback)")
                                break
                except Exception as e:
                    logger.debug(f"Could not fetch ports from deployment store for pod {pod_id}: {e}", exc_info=True)
            
            # Get deployment name if available (use metadata.name from deployment YAML)
            deployment_name = None
            if pod_ns:
                try:
                    # First try to match by app_label if we have it
                    if pod_app_label:
                        deployments = deployment_store.get_deployments_by_namespace(pod_ns)
                        for deployment in deployments:
                            if deployment.get("app_label") == pod_app_label:
                                deployment_name = deployment.get("name")
                                logger.debug(f"Matched pod {pod_id} to deployment {deployment_name} by app_label {pod_app_label}")
                                break
                        if not deployment_name:
                            # Fallback: try by app_label only
                            deployments_by_app = deployment_store.get_deployments_by_app(pod_app_label)
                            for deployment in deployments_by_app:
                                if deployment.get("namespace") == pod_ns:
                                    deployment_name = deployment.get("name")
                                    logger.debug(f"Matched pod {pod_id} to deployment {deployment_name} by app_label {pod_app_label} (fallback)")
                                    break
                    
                    # If still no match, try to match by container name/image
                    # Also try matching even if pod_app_label exists but is empty/None (might be wrong)
                    if (not deployment_name and not pod_app_label) or (pod_app_label and pod_app_label.strip() == ""):
                        deployments = deployment_store.get_deployments_by_namespace(pod_ns)
                        logger.info(f"Trying container matching for pod {pod_id} in namespace {pod_ns}: found {len(deployments)} deployments")
                        logger.info(f"Pod containers data: {containers} (count: {len(containers) if containers else 0})")
                        
                        # If containers are empty, try to get them from the pod data directly
                        if not containers or len(containers) == 0:
                            # Try to get containers from the full pod data
                            containers = p.get("containers") or []
                            logger.warning(f"Pod {pod_id} has empty containers list in Redis. Attempting to match by other means.")
                        
                        if not deployments:
                            # Try all namespaces as fallback (in case namespace mismatch)
                            logger.debug(f"No deployments in namespace {pod_ns}, trying all deployments")
                            all_deployments = deployment_store.get_all_deployments()
                            logger.info(f"Found {len(all_deployments)} total deployments across all namespaces")
                            deployments = all_deployments
                        
                    # If still no containers, try matching by pod creation time proximity to deployment creation time
                    # or by checking if this is the only deployment in the namespace
                    if (not containers or len(containers) == 0):
                        if len(deployments) == 1:
                            # Only one deployment in namespace - likely match
                            deployment = deployments[0]
                            dep_name = deployment.get("name")
                            dep_app_label = deployment.get("app_label")
                            logger.info(f"Pod {pod_id} has no containers but only one deployment in namespace {pod_ns}: {dep_name} (app_label: {dep_app_label}). Assuming match.")
                            deployment_name = dep_name
                            if dep_app_label:
                                pod_app_label = dep_app_label
                                # Persist the matched labels
                                try:
                                    existing_labels = p.get("labels", {})
                                    if not isinstance(existing_labels, dict):
                                        existing_labels = {}
                                    updated_labels = {
                                        **existing_labels,
                                        "app": dep_app_label,
                                        "app_label": dep_app_label,
                                    }
                                    store.save_pod(
                                        pod_id=pod_id,
                                        namespace=pod_ns,
                                        hostname=hostname,
                                        ip_address=p.get("ip_address"),
                                        pause_container=p.get("pause_container"),
                                        containers=containers,
                                        labels=updated_labels,
                                        status=p.get("status", "unknown"),
                                        creation_time=p.get("creation_time") or p.get("created_at"),
                                        startup_time=p.get("startup_time")
                                    )
                                    logger.info(f"Updated pod {pod_id} in Redis with app_label {dep_app_label} (single deployment match)")
                                except Exception as save_err:
                                    logger.warning(f"Failed to persist labels to Redis for pod {pod_id}: {save_err}", exc_info=True)
                        elif len(deployments) > 1:
                            # Multiple deployments - try to match by creation time proximity
                            pod_creation_time = p.get("creation_time") or p.get("created_at")
                            if pod_creation_time:
                                try:
                                    from datetime import datetime
                                    pod_creation_dt = datetime.fromisoformat(pod_creation_time.replace('Z', '+00:00'))
                                    best_match = None
                                    best_time_diff = None
                                    
                                    for deployment in deployments:
                                        dep_created_at = deployment.get("created_at")
                                        if dep_created_at:
                                            try:
                                                dep_creation_dt = datetime.fromisoformat(dep_created_at.replace('Z', '+00:00'))
                                                time_diff = abs((pod_creation_dt - dep_creation_dt).total_seconds())
                                                if best_time_diff is None or time_diff < best_time_diff:
                                                    best_time_diff = time_diff
                                                    best_match = deployment
                                            except Exception:
                                                continue
                                    
                                    # If we found a match within 5 minutes, use it
                                    if best_match and best_time_diff is not None and best_time_diff < 300:  # 5 minutes
                                        dep_name = best_match.get("name")
                                        dep_app_label = best_match.get("app_label")
                                        logger.info(f"Pod {pod_id} matched to deployment {dep_name} (app_label: {dep_app_label}) by creation time proximity ({best_time_diff:.1f}s difference)")
                                        deployment_name = dep_name
                                        if dep_app_label:
                                            pod_app_label = dep_app_label
                                            # Persist the matched labels
                                            try:
                                                existing_labels = p.get("labels", {})
                                                if not isinstance(existing_labels, dict):
                                                    existing_labels = {}
                                                updated_labels = {
                                                    **existing_labels,
                                                    "app": dep_app_label,
                                                    "app_label": dep_app_label,
                                                }
                                                store.save_pod(
                                                    pod_id=pod_id,
                                                    namespace=pod_ns,
                                                    hostname=hostname,
                                                    ip_address=p.get("ip_address"),
                                                    pause_container=p.get("pause_container"),
                                                    containers=containers,
                                                    labels=updated_labels,
                                                    status=p.get("status", "unknown"),
                                                    creation_time=p.get("creation_time") or p.get("created_at"),
                                                    startup_time=p.get("startup_time")
                                                )
                                                logger.info(f"Updated pod {pod_id} in Redis with app_label {dep_app_label} (time-based match)")
                                            except Exception as save_err:
                                                logger.warning(f"Failed to persist labels to Redis for pod {pod_id}: {save_err}", exc_info=True)
                                except Exception as time_err:
                                    logger.debug(f"Failed to match pod {pod_id} by creation time: {time_err}", exc_info=True)
                        
                        # Try container matching if we have containers
                        if containers and len(containers) > 0:
                            for deployment in deployments:
                                deployment_spec = deployment.get("deployment_spec", {})
                                containers_spec = deployment_spec.get("containers", [])
                                dep_name = deployment.get("name")
                                dep_app_label = deployment.get("app_label")
                                dep_namespace = deployment.get("namespace")
                                logger.info(f"Checking deployment {dep_name} (namespace: {dep_namespace}, app_label: {dep_app_label}) with {len(containers_spec)} container specs")
                            
                            # Try to match by container name or image
                            for container in containers:
                                if isinstance(container, dict):
                                    # Try multiple possible keys for container name
                                    container_name = container.get("name") or container.get("id") or ""
                                    # Container ID might be like "pod-id-container-name", extract just the container name part
                                    if container_name and "-" in container_name:
                                        # Try to extract container name from ID (format: pod-id-container-name)
                                        parts = container_name.split("-")
                                        if len(parts) > 2:
                                            # Assume last part or last two parts are the container name
                                            container_name_alt = "-".join(parts[-2:])  # Try last 2 parts
                                            container_name_single = parts[-1]  # Try last part
                                        else:
                                            container_name_alt = container_name
                                            container_name_single = container_name
                                    else:
                                        container_name_alt = container_name
                                        container_name_single = container_name
                                    
                                    container_image = container.get("image") or ""
                                    logger.info(f"Pod container: name={container_name} (alt: {container_name_alt}, single: {container_name_single}), image={container_image}")
                                    
                                    for container_spec in containers_spec:
                                        if isinstance(container_spec, dict):
                                            spec_name = container_spec.get("name") or ""
                                            spec_image = container_spec.get("image") or ""
                                            logger.info(f"Deployment container spec: name={spec_name}, image={spec_image}")
                                            
                                            # Try exact match first
                                            name_match = (container_name and spec_name and container_name == spec_name) or \
                                                        (container_name_alt and spec_name and container_name_alt == spec_name) or \
                                                        (container_name_single and spec_name and container_name_single == spec_name)
                                            image_match = container_image and spec_image and container_image == spec_image
                                            
                                            # If no exact match, try partial image match (in case of tags/digests)
                                            if not name_match and not image_match and container_image and spec_image:
                                                # Extract base image name (before tag/digest)
                                                container_base = container_image.split("@")[0].split(":")[0]
                                                spec_base = spec_image.split("@")[0].split(":")[0]
                                                image_match = container_base == spec_base
                                                logger.info(f"Trying base image match: {container_base} == {spec_base} -> {image_match}")
                                            
                                            if name_match or image_match:
                                                deployment_name = dep_name
                                                # Also set pod_app_label from the deployment's app_label
                                                if dep_app_label:
                                                    pod_app_label = dep_app_label
                                                    logger.info(f"Matched pod {pod_id} to deployment {deployment_name} (app_label: {pod_app_label}) by container match (name_match={name_match}, image_match={image_match}, container={container_name}/{container_image})")
                                                    
                                                    # Persist the matched labels to Redis so future lookups work
                                                    try:
                                                        existing_pod = p  # Use the pod data from Redis
                                                        existing_labels = existing_pod.get("labels", {})
                                                        if not isinstance(existing_labels, dict):
                                                            existing_labels = {}
                                                        
                                                        # Update labels with the matched app_label
                                                        updated_labels = {
                                                            **existing_labels,
                                                            "app": dep_app_label,
                                                            "app_label": dep_app_label,
                                                        }
                                                        
                                                        # Save updated pod with labels to Redis
                                                        store.save_pod(
                                                            pod_id=pod_id,
                                                            namespace=pod_ns,
                                                            hostname=hostname,
                                                            ip_address=p.get("ip_address"),
                                                            pause_container=p.get("pause_container"),
                                                            containers=containers,
                                                            labels=updated_labels,
                                                            status=p.get("status", "unknown"),
                                                            creation_time=p.get("creation_time") or p.get("created_at"),
                                                            startup_time=p.get("startup_time")
                                                        )
                                                        logger.info(f"Updated pod {pod_id} in Redis with app_label {dep_app_label}")
                                                    except Exception as save_err:
                                                        logger.warning(f"Failed to persist labels to Redis for pod {pod_id}: {save_err}", exc_info=True)
                                                else:
                                                    logger.info(f"Matched pod {pod_id} to deployment {deployment_name} by container match (name_match={name_match}, image_match={image_match}, container={container_name}/{container_image})")
                                                break
                                    if deployment_name:
                                        break
                            if deployment_name:
                                break
                except Exception as e:
                    logger.debug(f"Failed to get deployment_name for pod {pod_id}: {e}", exc_info=True)
            
            # Build pod view
            pod_view = {
                "pod_id": pod_id,
                "pod_name": p.get("pod_name") or pod_id,
                "namespace": pod_ns,
                "hostname": hostname,
                "ip_address": ip_address,
                "ports": sorted(list(set(ports))),
                "status": p.get("status") or "unknown",
                "containers": [c.get("name") for c in containers if isinstance(c, dict)],
                "creation_time": p.get("creation_time") or p.get("created_at"),
                "startup_time": p.get("startup_time"),
                "app_label": pod_app_label,
                "deployment_name": deployment_name,  # Use metadata.name from deployment YAML
            }
            
            # Add health check success rate for last 180 seconds
            try:
                from utils.healthcheck import get_health_check_success_rate
                health_data = {
                    'liveness': None,
                    'readiness': None,
                    'overall_success_rate': None,
                    'overall_total_checks': 0,
                    'overall_successful_checks': 0,
                    'overall_failed_checks': 0
                }
                
                # Get fresh pod data from Redis to ensure we have latest health_checks field
                fresh_pod = None
                try:
                    fresh_pod = store.get_pod(pod_id)
                except Exception as e:
                    logger.debug(f"Could not get fresh pod data for {pod_id}: {e}")
                
                # Get health check data for each container
                # Note: containers might be a list of strings (container names) or list of dicts
                if containers:
                    liveness_rates = []
                    readiness_rates = []
                    total_checks = 0
                    
                    logger.debug(f"Getting health check data for pod {pod_id} with {len(containers)} containers (type: {type(containers[0]) if containers else 'empty'})")
                    
                    # Get container names - handle both string list and dict list
                    container_names = []
                    for container in containers:
                        if isinstance(container, dict):
                            container_name = container.get('name') or 'unknown'
                            container_names.append(container_name)
                        elif isinstance(container, str):
                            container_names.append(container)
                    
                    # If no container names found, try to get from fresh_pod or original pod data
                    if not container_names and fresh_pod:
                        pod_containers = fresh_pod.get('containers', [])
                        for c in pod_containers:
                            if isinstance(c, dict):
                                container_names.append(c.get('name', 'unknown'))
                            elif isinstance(c, str):
                                container_names.append(c)
                    
                    logger.debug(f"Container names for pod {pod_id}: {container_names}")
                    
                    # Check if readiness has already succeeded (if so, don't count readiness checks)
                    readiness_succeeded = False
                    if fresh_pod:
                        health_checks = fresh_pod.get('health_checks', {})
                        readiness_data = health_checks.get('readiness', {})
                        if readiness_data.get('status') == 'success':
                            readiness_succeeded = True
                            logger.debug(f"Readiness has already succeeded for pod {pod_id}, excluding readiness checks from count")
                    
                    for container_name in container_names:
                        if not container_name or container_name == 'unknown':
                            continue
                        logger.debug(f"Checking health history for pod {pod_id}, container {container_name}")
                        
                        # Get liveness probe success rate
                        try:
                            liveness_rate = get_health_check_success_rate(
                                rd, pod_id, 'liveness', container_name, seconds=180
                            )
                            total_liveness_checks = liveness_rate.get('total_checks', 0)
                            logger.debug(f"Liveness rate for pod {pod_id} container {container_name}: {liveness_rate}")
                            if total_liveness_checks > 0:
                                liveness_rates.append(liveness_rate)
                                total_checks += total_liveness_checks
                            else:
                                logger.debug(f"No liveness history found for pod {pod_id} container {container_name} (total_checks=0)")
                        except Exception as e:
                            logger.warning(f"Could not get liveness rate for pod {pod_id} container {container_name}: {e}", exc_info=True)
                        
                        # Get readiness probe success rate - ALWAYS include readiness history from last 180 seconds
                        # Even if readiness has succeeded, we should count ALL historical readiness checks in the 180-second window
                        # The 180-second window should include both readiness and liveness checks
                        try:
                            readiness_rate = get_health_check_success_rate(
                                rd, pod_id, 'readiness', container_name, seconds=180
                            )
                            total_readiness_checks = readiness_rate.get('total_checks', 0)
                            logger.debug(f"Readiness rate for pod {pod_id} container {container_name}: {readiness_rate} (readiness_succeeded={readiness_succeeded})")
                            if total_readiness_checks > 0:
                                readiness_rates.append(readiness_rate)
                                # ALWAYS add readiness checks to total count - they happened in the last 180 seconds
                                # Even if readiness succeeded and we stopped checking, the historical checks count
                                total_checks += total_readiness_checks
                                if readiness_succeeded:
                                    logger.debug(f"Readiness succeeded for pod {pod_id}, but including historical readiness checks ({total_readiness_checks}) in 180-second window")
                            else:
                                logger.debug(f"No readiness history found for pod {pod_id} container {container_name} (total_checks=0)")
                        except Exception as e:
                            logger.warning(f"Could not get readiness rate for pod {pod_id} container {container_name}: {e}", exc_info=True)
                    
                    # Calculate overall success rates
                    if liveness_rates:
                        total_liveness = sum(r.get('total_checks', 0) for r in liveness_rates)
                        successful_liveness = sum(r.get('successful_checks', 0) for r in liveness_rates)
                        health_data['liveness'] = {
                            'success_rate': (successful_liveness / total_liveness * 100) if total_liveness > 0 else 0.0,
                            'total_checks': total_liveness,
                            'successful_checks': successful_liveness
                        }
                    
                    if readiness_rates:
                        total_readiness = sum(r.get('total_checks', 0) for r in readiness_rates)
                        successful_readiness = sum(r.get('successful_checks', 0) for r in readiness_rates)
                        health_data['readiness'] = {
                            'success_rate': (successful_readiness / total_readiness * 100) if total_readiness > 0 else 0.0,
                            'total_checks': total_readiness,
                            'successful_checks': successful_readiness
                        }
                    
                    # Calculate overall success rate (combined liveness + readiness)
                    if total_checks > 0:
                        total_successful = sum(r.get('successful_checks', 0) for r in liveness_rates + readiness_rates)
                        total_failed = total_checks - total_successful
                        health_data['overall_success_rate'] = (total_successful / total_checks * 100) if total_checks > 0 else 0.0
                        health_data['overall_total_checks'] = total_checks
                        health_data['overall_successful_checks'] = total_successful
                        health_data['overall_failed_checks'] = total_failed
                        logger.info(f"Health check data for pod {pod_id}: {total_successful}/{total_failed}/{total_checks} (rate: {health_data['overall_success_rate']:.1f}%)")
                    else:
                        logger.debug(f"No health check history found for pod {pod_id} (total_checks=0), trying fallback from pod data")
                        # Fallback: Try to get health check data from pod's stored health_checks field
                        logger.debug(f"No health check history found for pod {pod_id} (total_checks=0), trying fallback from pod data")
                        # Use fresh_pod if we already fetched it, otherwise get it now
                        if not fresh_pod:
                            try:
                                fresh_pod = store.get_pod(pod_id)
                            except Exception as e:
                                logger.debug(f"Could not get fresh pod data for {pod_id}: {e}")
                        
                        pod_health_checks = {}
                        if fresh_pod:
                            pod_health_checks = fresh_pod.get('health_checks', {})
                            logger.debug(f"Retrieved fresh pod data for {pod_id}, health_checks keys: {list(pod_health_checks.keys()) if pod_health_checks else 'empty'}")
                        else:
                            logger.debug(f"Pod {pod_id} not found in Redis, trying original pod data")
                            pod_health_checks = p.get('health_checks', {})
                        
                        if pod_health_checks:
                            # Extract health check data from stored health_checks field
                            liveness_data = pod_health_checks.get('liveness', {})
                            readiness_data = pod_health_checks.get('readiness', {})
                            
                            # Use consecutive successes/failures to estimate health check stats
                            liveness_successes = liveness_data.get('consecutive_successes', 0)
                            liveness_failures = liveness_data.get('consecutive_failures', 0)
                            readiness_successes = readiness_data.get('consecutive_successes', 0)
                            readiness_failures = readiness_data.get('consecutive_failures', 0)
                            
                            logger.debug(f"Pod {pod_id} health check stats: liveness={liveness_successes}/{liveness_failures}, readiness={readiness_successes}/{readiness_failures}")
                            
                            # Calculate totals
                            liveness_total = liveness_successes + liveness_failures
                            readiness_total = readiness_successes + readiness_failures
                            estimated_total = liveness_total + readiness_total
                            
                            if estimated_total > 0:
                                estimated_successful = liveness_successes + readiness_successes
                                estimated_failed = liveness_failures + readiness_failures
                                
                                health_data['overall_success_rate'] = (estimated_successful / estimated_total * 100) if estimated_total > 0 else 0.0
                                health_data['overall_total_checks'] = estimated_total
                                health_data['overall_successful_checks'] = estimated_successful
                                health_data['overall_failed_checks'] = estimated_failed
                                
                                if liveness_total > 0:
                                    health_data['liveness'] = {
                                        'success_rate': (liveness_successes / liveness_total * 100) if liveness_total > 0 else 0.0,
                                        'total_checks': liveness_total,
                                        'successful_checks': liveness_successes
                                    }
                                
                                if readiness_total > 0:
                                    health_data['readiness'] = {
                                        'success_rate': (readiness_successes / readiness_total * 100) if readiness_total > 0 else 0.0,
                                        'total_checks': readiness_total,
                                        'successful_checks': readiness_successes
                                    }
                                
                                logger.info(f"Using fallback health data for pod {pod_id}: {estimated_successful}/{estimated_failed}/{estimated_total} (rate: {health_data['overall_success_rate']:.1f}%)")
                            else:
                                logger.debug(f"Pod {pod_id} has health_checks field but no consecutive_successes/failures data")
                        else:
                            logger.debug(f"Pod {pod_id} has no health_checks field in stored data")
                
                pod_view['health_check'] = health_data
            except Exception as e:
                logger.warning(f"Could not get health check data for pod {pod_id}: {e}", exc_info=True)
                pod_view['health_check'] = {
                    'liveness': None,
                    'readiness': None,
                    'overall_success_rate': None,
                    'overall_total_checks': 0,
                    'overall_successful_checks': 0,
                    'overall_failed_checks': 0
                }
            
            filtered_pods.append(pod_view)
        
        return _envelope_success("Pods filtered successfully", {
            "namespace": namespace,
            "app_name": app_name,
            "pods": filtered_pods,
            "pod_count": len(filtered_pods)
        })
        
    except Exception as e:
        logger.error(f"Failed to filter pods: {e}", exc_info=True)
        return create_error_response(
            error_code="FILTER_PODS_ERROR",
            message=f"Failed to filter pods: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/deployments/", tags=["Deployment"])
async def get_all_deployments_api(
    user: str = Depends(get_current_user),
):
    """Get all deployments from Redis DeploymentStore.
    
    Returns:
        List of all deployment configurations
    """
    try:
        from utils.redis.deployment_store import DeploymentStore
        
        deployment_store = DeploymentStore(rd)
        deployments = deployment_store.get_all_deployments()
        
        # Format deployments for UI display
        deployment_list = []
        for deployment in deployments:
            # Extract ports from deployment spec
            ports = []
            deployment_spec = deployment.get("deployment_spec", {})
            containers_spec = deployment_spec.get("containers", [])
            for container_spec in containers_spec:
                if isinstance(container_spec, dict):
                    container_ports = container_spec.get("ports", [])
                    if isinstance(container_ports, list):
                        for port in container_ports:
                            if isinstance(port, dict):
                                port_num = port.get("containerPort") or port.get("port")
                                protocol = port.get("protocol", "TCP")
                                if port_num:
                                    ports.append(f"{port_num}/{protocol}")
                            elif isinstance(port, (int, str)):
                                ports.append(str(port))
            
            deployment_list.append({
                "name": deployment.get("name"),
                "namespace": deployment.get("namespace"),
                "app_label": deployment.get("app_label"),
                "replicas": deployment.get("replicas", 0),
                "min_replicas": deployment.get("min_replicas"),
                "max_replicas": deployment.get("max_replicas"),
                "ports": sorted(list(set(ports))),  # Unique and sorted ports
                "yaml_content": deployment.get("yaml_content"),  # Include full YAML
                "created_at": deployment.get("created_at"),
                "last_updated": deployment.get("last_updated"),
            })
        
        # Sort by namespace, then by name
        deployment_list.sort(key=lambda x: (x.get("namespace", ""), x.get("name", "")))
        
        return _envelope_success(
            f"Retrieved {len(deployment_list)} deployments",
            {
                "deployments": deployment_list,
                "count": len(deployment_list),
            }
        )
        
    except Exception as e:
        logger.error(f"Failed to get all deployments: {e}", exc_info=True)
        return create_error_response(
            error_code="GET_ALL_DEPLOYMENTS_ERROR",
            message=f"Failed to get deployments: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.put("/deployment/replicas/", tags=["Deployment"])
async def update_deployment_replicas_api(
    name: str,
    namespace: str,
    min_replicas: Optional[int] = None,
    max_replicas: Optional[int] = None,
    user: str = Depends(get_current_user),
):
    """Update min/max replicas for a deployment and trigger scaling.
    
    Args:
        name: Deployment name
        namespace: Namespace
        min_replicas: New minimum replicas (optional)
        max_replicas: New maximum replicas (optional)
        user: Authenticated user
        
    Returns:
        Updated deployment data
    """
    try:
        from utils.redis.deployment_store import DeploymentStore
        from utils.celery.tasks.deployment_recovery_tasks import scale_deployment_task
        
        deployment_store = DeploymentStore(rd)
        deployment = deployment_store.get_deployment(name, namespace)
        
        if not deployment:
            return create_error_response(
                error_code="DEPLOYMENT_NOT_FOUND",
                message=f"Deployment {namespace}/{name} not found",
                status_code=status.HTTP_404_NOT_FOUND
            )
        
        # Update replicas if provided
        updated = False
        if min_replicas is not None:
            deployment["min_replicas"] = min_replicas
            updated = True
        if max_replicas is not None:
            deployment["max_replicas"] = max_replicas
            updated = True
        
        if updated:
            deployment["last_updated"] = datetime.now(timezone.utc).isoformat()
            # Save updated deployment
            deployment_store.save_deployment(
                name=deployment["name"],
                namespace=deployment["namespace"],
                app_label=deployment["app_label"],
                yaml_content=deployment.get("yaml_content", ""),
                deployment_spec=deployment.get("deployment_spec", {}),
                replicas=deployment.get("replicas", 0),
                min_replicas=deployment.get("min_replicas"),
                max_replicas=deployment.get("max_replicas"),
            )
            
            # Trigger scaling task on scheduler queue
            try:
                from utils.extensions.utilities_extention import UtilitiesExtension
                from utils.ReadConfig import ReadConfig as rc
                
                read_config = rc()
                key = read_config.encryption_config['key']
                utilities = UtilitiesExtension(key)
                scheduler_queue_info = create_queue_info('scheduler', utilities_extension=utilities)
                
                # Use apply_async to route to scheduler queue
                result = scale_deployment_task.apply_async(
                    args=(name, namespace),
                    queue=scheduler_queue_info['queue'],
                    routing_key=scheduler_queue_info['routing_key'],
                    exchange=scheduler_queue_info['exchange']
                )
                logger.info(f"Triggered scaling task for deployment {namespace}/{name} (task_id: {result.id}, queue: {scheduler_queue_info['queue']})")
            except Exception as e:
                logger.error(f"Failed to trigger scaling task: {e}", exc_info=True)
        
        return _envelope_success(
            f"Deployment {namespace}/{name} updated",
            {
                "name": deployment["name"],
                "namespace": deployment["namespace"],
                "min_replicas": deployment.get("min_replicas"),
                "max_replicas": deployment.get("max_replicas"),
                "replicas": deployment.get("replicas"),
            }
        )
        
    except Exception as e:
        logger.error(f"Failed to update deployment replicas: {e}", exc_info=True)
        return create_error_response(
            error_code="UPDATE_REPLICAS_ERROR",
            message=f"Failed to update replicas: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.delete("/deployment/", tags=["Deployment"])
async def delete_deployment_api(
    name: str,
    namespace: str,
    user: str = Depends(get_current_user),
):
    """Delete a deployment from Redis.
    
    Args:
        name: Deployment name
        namespace: Namespace
        user: Authenticated user
        
    Returns:
        Success message
    """
    try:
        from utils.redis.deployment_store import DeploymentStore
        
        deployment_store = DeploymentStore(rd)
        deployment = deployment_store.get_deployment(name, namespace)
        
        if not deployment:
            return create_error_response(
                error_code="DEPLOYMENT_NOT_FOUND",
                message=f"Deployment {namespace}/{name} not found",
                status_code=status.HTTP_404_NOT_FOUND
            )
        
        deployment_store.delete_deployment(name, namespace)
        logger.info(f"Deleted deployment {namespace}/{name} from Redis")
        
        return _envelope_success(f"Deployment {namespace}/{name} deleted", {})
        
    except Exception as e:
        logger.error(f"Failed to delete deployment: {e}", exc_info=True)
        return create_error_response(
            error_code="DELETE_DEPLOYMENT_ERROR",
            message=f"Failed to delete deployment: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.post("/deployment/terminate-pods/", tags=["Deployment"])
async def terminate_deployment_pods_api(
    name: str,
    namespace: str,
    user: str = Depends(get_current_user),
):
    """Terminate all pods for a deployment.
    
    Args:
        name: Deployment name
        namespace: Namespace
        user: Authenticated user
        
    Returns:
        Task ID for termination
    """
    try:
        from utils.redis.deployment_store import DeploymentStore
        from utils.redis.host_pod_store import HostPodStore
        
        deployment_store = DeploymentStore(rd)
        deployment = deployment_store.get_deployment(name, namespace)
        
        if not deployment:
            return create_error_response(
                error_code="DEPLOYMENT_NOT_FOUND",
                message=f"Deployment {namespace}/{name} not found",
                status_code=status.HTTP_404_NOT_FOUND
            )
        
        app_label = deployment.get("app_label")
        deployment_name = deployment.get("name", name)  # Use name from deployment or parameter
        deployment_spec = deployment.get("deployment_spec", {})
        containers_spec = deployment_spec.get("containers", [])
        
        logger.info(f"Terminating pods for deployment: name={deployment_name}, app_label={app_label}, namespace={namespace}")
        
        # Use EXACT same approach as list_pods_by_filter - get all pods from all hosts first
        host_pod_store = HostPodStore(rd)
        all_hosts = host_pod_store.get_all_hosts()
        all_pods = []
        seen_pod_ids = set()  # Track seen pod IDs to avoid duplicates
        
        # Collect pods from all hosts, deduplicating by pod_id (same as list_pods_by_filter)
        logger.info(f"Collecting pods from {len(all_hosts)} hosts")
        for host in all_hosts:
            hostname = host.get("hostname")
            if hostname:
                host_pods = host_pod_store.get_pods_by_host(hostname)
                for p in host_pods:
                    pod_id = p.get("pod_id")
                    if pod_id and pod_id not in seen_pod_ids:
                        seen_pod_ids.add(pod_id)
                        all_pods.append(p)
        
        logger.info(f"Collected {len(all_pods)} total pods from all hosts")
        
        # Filter pods that match this deployment (same logic as list_pods_by_filter)
        matching_pods = []
        seen_pod_ids.clear()  # Reset for matching
        
        for p in all_pods:
            pod_ns = p.get("namespace") or "default"
            
            # Apply namespace filter
            if pod_ns != namespace:
                continue
            
            # Extract app_label from pod (same logic as list_pods_by_filter)
            pod_labels = p.get("labels", {})
            pod_app_label = None
            
            if isinstance(pod_labels, dict):
                pod_app_label = pod_labels.get("app")
            
            # If not in labels, try reverse lookup from pod index (same as list_pods_by_filter)
            if not pod_app_label:
                pod_id = p.get("pod_id")
                if pod_id:
                    try:
                        app_index_pattern = "pod:index:app:*"
                        for app_index_key in rd.redis_client.scan_iter(match=app_index_pattern):
                            if rd.redis_client.sismember(app_index_key, pod_id):
                                pod_app_label = app_index_key.split(":")[-1]
                                logger.debug(f"Found app_label {pod_app_label} for pod {pod_id} via index lookup")
                                break
                    except Exception as e:
                        logger.debug(f"Error during index lookup for pod {pod_id}: {e}")
            
            # Also try pod data directly
            if not pod_app_label:
                pod_app_label = p.get("app_label")
            
            # Match by app_label (primary match)
            matched = False
            if app_label and pod_app_label == app_label:
                matched = True
                logger.info(f"Matched pod {p.get('pod_id')} by app_label: {pod_app_label} == {app_label}")
            else:
                # Try to match by deployment_name (same logic as list_pods_by_filter)
                pod_deployment_name = None
                if pod_ns:
                    try:
                        # Get deployment name from deployment store
                        deployments = deployment_store.get_deployments_by_namespace(pod_ns)
                        for dep in deployments:
                            dep_app_label = dep.get("app_label")
                            if dep_app_label == pod_app_label:
                                pod_deployment_name = dep.get("name")
                                break
                        # If not found, try by app_label only
                        if not pod_deployment_name and pod_app_label:
                            deployments_by_app = deployment_store.get_deployments_by_app(pod_app_label)
                            for dep in deployments_by_app:
                                if dep.get("namespace") == pod_ns:
                                    pod_deployment_name = dep.get("name")
                                    break
                    except Exception as e:
                        logger.debug(f"Error looking up deployment for pod {p.get('pod_id')}: {e}")
                
                # Match by deployment_name
                if deployment_name and pod_deployment_name == deployment_name:
                    matched = True
                    logger.info(f"Matched pod {p.get('pod_id')} by deployment_name: {pod_deployment_name} == {deployment_name}")
            
            # If still not matched, try container matching
            if not matched and containers_spec:
                pod_containers = p.get("containers", [])
                for pod_container in pod_containers:
                    if not isinstance(pod_container, dict):
                        continue
                    pod_container_name = pod_container.get("name")
                    pod_container_image = pod_container.get("image")
                    
                    for container_spec in containers_spec:
                        if not isinstance(container_spec, dict):
                            continue
                        spec_name = container_spec.get("name")
                        spec_image = container_spec.get("image")
                        
                        if (pod_container_name and spec_name and pod_container_name == spec_name) or \
                           (pod_container_image and spec_image and pod_container_image == spec_image):
                            matched = True
                            logger.info(f"Matched pod {p.get('pod_id')} by container (name={pod_container_name}, image={pod_container_image})")
                            break
                    if matched:
                        break
            
            if matched:
                pod_id = p.get("pod_id")
                if pod_id not in seen_pod_ids:
                    matching_pods.append(p)
                    seen_pod_ids.add(pod_id)
        
        namespace_pods = matching_pods
        logger.info(f"Final matching pods count: {len(namespace_pods)}")
        
        # Log pod IDs for debugging
        if namespace_pods:
            pod_ids = [p.get("pod_id") for p in namespace_pods]
            logger.info(f"Pods to terminate: {pod_ids}")
        else:
            logger.warning(f"No pods matched for deployment {namespace}/{name} (app_label={app_label}, deployment_name={deployment_name})")
            logger.warning(f"Total pods in namespace: {len(namespace_pods)}")
            # Log all pods in namespace for debugging
            if namespace_pods:
                logger.warning("Pods in namespace (for debugging):")
                for pod in namespace_pods:
                    pod_id = pod.get("pod_id")
                    pod_app_label = pod.get("app_label")
                    pod_labels = pod.get("labels", {})
                    if isinstance(pod_labels, dict):
                        pod_app_label = pod_app_label or pod_labels.get("app") or pod_labels.get("app_label")
                    logger.warning(f"  - Pod {pod_id}: app_label={pod_app_label}, labels={pod_labels}")
        
        if not namespace_pods:
            return _envelope_success("No pods found for deployment", {"pods_terminated": 0})
        
        # Submit termination tasks for each pod
        terminated_count = 0
        task_ids = []
        
        for pod in namespace_pods:
            pod_id = pod.get("pod_id")
            hostname = pod.get("hostname")
            
            if not pod_id or not hostname:
                continue
            
            try:
                # Create host queue info (same pattern as other endpoints)
                host_queue_info = create_host_queue_info(hostname, ue)
                
                result = submit_celery_task(
                    task=terminate_pod_task,
                    args=(namespace, pod_id),
                    kwargs={},
                    queue_info=host_queue_info,
                    operation_name="terminate_pod",
                    error_code="TERMINATE_POD_TASK_ERROR",
                    additional_data={
                        "namespace": namespace,
                        "pod_name": pod_id,
                        "host_name": hostname,
                        "deployment_name": name,
                    }
                )
                
                task_id = result.get("data", {}).get("task_id")
                if task_id:
                    task_ids.append(task_id)
                    terminated_count += 1
            except Exception as e:
                logger.warning(f"Failed to submit termination task for pod {pod_id}: {e}")
        
        return _envelope_success(
            f"Terminated {terminated_count} pods for deployment {namespace}/{name}",
            {
                "pods_terminated": terminated_count,
                "task_ids": task_ids,
            }
        )
        
    except Exception as e:
        logger.error(f"Failed to terminate deployment pods: {e}", exc_info=True)
        return create_error_response(
            error_code="TERMINATE_PODS_ERROR",
            message=f"Failed to terminate pods: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.post("/deployment/reassociate-pods/", tags=["Deployment"])
async def reassociate_deployment_pods_api(
    name: str,
    namespace: str,
    user: str = Depends(get_current_user),
):
    """Re-associate pods with a deployment by matching container info.
    
    This is useful when pods exist but aren't properly indexed by app_label.
    
    Args:
        name: Deployment name
        namespace: Namespace
        user: Authenticated user
        
    Returns:
        Number of pods re-associated
    """
    try:
        from utils.redis.deployment_store import DeploymentStore
        from utils.redis.host_pod_store import HostPodStore
        
        deployment_store = DeploymentStore(rd)
        host_pod_store = HostPodStore(rd)
        
        deployment = deployment_store.get_deployment(name, namespace)
        if not deployment:
            return create_error_response(
                error_code="DEPLOYMENT_NOT_FOUND",
                message=f"Deployment {namespace}/{name} not found",
                status_code=status.HTTP_404_NOT_FOUND
            )
        
        app_label = deployment.get("app_label")
        deployment_spec = deployment.get("deployment_spec", {})
        containers_spec = deployment_spec.get("containers", [])
        
        # Get all pods in the namespace
        namespace_pods = host_pod_store.get_pods_by_namespace(namespace)
        
        # Find pods that match this deployment by container info
        matched_pods = []
        for pod in namespace_pods:
            pod_containers = pod.get("containers", [])
            if not pod_containers:
                continue
            
            # Check if any pod container matches any deployment container
            for pod_container in pod_containers:
                if not isinstance(pod_container, dict):
                    continue
                
                pod_container_name = pod_container.get("name")
                pod_container_image = pod_container.get("image")
                
                for container_spec in containers_spec:
                    if not isinstance(container_spec, dict):
                        continue
                    
                    spec_name = container_spec.get("name")
                    spec_image = container_spec.get("image")
                    
                    # Match by name or image
                    if (pod_container_name and spec_name and pod_container_name == spec_name) or \
                       (pod_container_image and spec_image and pod_container_image == spec_image):
                        pod_id = pod.get("pod_id")
                        if pod_id and not any(p.get("pod_id") == pod_id for p in matched_pods):
                            matched_pods.append(pod)
                            break
                if any(p.get("pod_id") == pod.get("pod_id") for p in matched_pods):
                    break
        
        # Re-associate matched pods by updating their labels and re-indexing
        reassociated_count = 0
        for pod in matched_pods:
            pod_id = pod.get("pod_id")
            hostname = pod.get("hostname")
            existing_labels = pod.get("labels", {})
            
            # Update labels with app_label if not already set
            if not existing_labels.get("app") and app_label:
                updated_labels = {
                    **existing_labels,
                    "app": app_label,
                    "app_label": app_label,
                }
                
                # Re-save pod with updated labels to trigger re-indexing
                host_pod_store.save_pod(
                    pod_id=pod_id,
                    pod_name=pod.get("pod_name"),
                    namespace=namespace,
                    hostname=hostname,
                    ip_address=pod.get("ip_address"),
                    pause_container=pod.get("pause_container"),
                    containers=pod.get("containers"),
                    cni_network=pod.get("cni_network"),
                    resources=pod.get("resources"),
                    labels=updated_labels,
                    status=pod.get("status") or None,  # Let save_pod determine from containers if None
                    creation_time=pod.get("creation_time"),
                    startup_time=pod.get("startup_time"),
                )
                reassociated_count += 1
                logger.info(f"Re-associated pod {pod_id} with deployment {namespace}/{name} (app_label: {app_label})")
        
        return _envelope_success(
            f"Re-associated {reassociated_count} pods with deployment {namespace}/{name}",
            {
                "pods_reassociated": reassociated_count,
                "total_matched": len(matched_pods),
            }
        )
        
    except Exception as e:
        logger.error(f"Failed to re-associate pods: {e}", exc_info=True)
        return create_error_response(
            error_code="REASSOCIATE_PODS_ERROR",
            message=f"Failed to re-associate pods: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/deployment/yaml/", tags=["Deployment"])
async def get_deployment_yaml_api(
    namespace: str,
    app_label: str,
    deployment_name: Optional[str] = None,
    user: str = Depends(get_current_user),
):
    """Get deployment YAML for a specific application.
    
    Args:
        namespace: Namespace name
        app_label: Application label (primary lookup method)
        deployment_name: Optional deployment name (metadata.name) for fallback lookup
        user: Authenticated user
        
    Returns:
        Deployment YAML content if found
    """
    try:
        from utils.redis.deployment_store import DeploymentStore
        
        deployment_store = DeploymentStore(rd)
        
        # Get deployments by namespace
        deployments = deployment_store.get_deployments_by_namespace(namespace)
        
        # Find deployment matching app_label first
        matching_deployment = None
        for deployment in deployments:
            if deployment.get("app_label") == app_label:
                matching_deployment = deployment
                break
        
        # If not found by app_label and deployment_name is provided, try by deployment name
        if not matching_deployment and deployment_name:
            for deployment in deployments:
                if deployment.get("name") == deployment_name:
                    matching_deployment = deployment
                    logger.debug(f"Found deployment {deployment_name} by name (app_label: {deployment.get('app_label')})")
                    break
        
        if not matching_deployment:
            # Try by app_label only as fallback
            deployments_by_app = deployment_store.get_deployments_by_app(app_label)
            for deployment in deployments_by_app:
                if deployment.get("namespace") == namespace:
                    matching_deployment = deployment
                    break
        
        if not matching_deployment:
            return create_error_response(
                error_code="DEPLOYMENT_NOT_FOUND",
                message=f"Deployment not found for app_label={app_label} in namespace={namespace}",
                status_code=status.HTTP_404_NOT_FOUND
            )
        
        yaml_content = matching_deployment.get("yaml_content", "")
        
        return _envelope_success(
            f"Deployment YAML retrieved for {app_label} in {namespace}",
            {
                "namespace": namespace,
                "app_label": app_label,
                "deployment_name": matching_deployment.get("name"),
                "yaml_content": yaml_content,
            }
        )
        
    except Exception as e:
        logger.error(f"Failed to get deployment YAML: {e}", exc_info=True)
        return create_error_response(
            error_code="GET_DEPLOYMENT_YAML_ERROR",
            message=f"Failed to get deployment YAML: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# @log_to_file(logger)
# @app.post("/containerd/list_pods_by_namespace/", tags=["Containerd - Pods"])
# async def list_pods_by_namespace_api(request: PodNamespaceHostRequest, user: str = Depends(get_current_user)):
#     result = submit_celery_task(
#         task=list_pods_by_namespace_task,
#         args=([request.namespace]),
#         queue_info=create_host_queue_info(request.host_name, ue),
#         operation_name="list_pods_by_namespace",
#         error_code="LIST_PODS_BY_NAMESPACE_ERROR",
#         additional_data={"host_name": request.host_name, "namespace": request.namespace},
#     )
#     return _envelope_success("Task submitted successfully", result.get("data", result))
@log_to_file(logger)
@app.post("/containerd/list_pods_by_namespace/", tags=["Containerd - Pods"])
async def list_pods_by_namespace_api(
    request: PodNamespaceHostRequest,
    user: str = Depends(get_current_user),
):
    # If you want host+namespace scoped list:
    pods = store.get_pods_by_host_and_namespace(request.host_name, request.namespace)

    # If you want namespace-wide across all hosts, use:
    # pods = store.get_pods_by_namespace(request.namespace)

    payload = {
        "host_name": request.host_name,
        "namespace": request.namespace,
        "pods": pods,
        "pod_count": len(pods),
    }
    return _envelope_success("Pods retrieved from Redis", payload)



@app.post("/containerd/terminate_pod/")
async def terminate_pod_api(request: TerminatePodRequest, user: str = Depends(get_current_user)):
    # DO NOT call worker or redis or grpc here beyond enqueueing.
    result = submit_celery_task(
        task=terminate_pod_task,
        args=(request.namespace, request.pod_name),
        kwargs={"cni_network": request.cni_network, "ifname": request.ifname},
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="terminate_pod",
        error_code="TERMINATE_POD_ERROR",
        additional_data={
            "namespace": request.namespace,
            "pod_name": request.pod_name,
            "host_name": request.host_name,
        },
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/terminate_pod_by_pause_cid/", tags=["Containerd - Pods"])
async def terminate_pod_by_pause_cid_api(request: TerminatePodByCidRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=terminate_pod_by_pause_cid_task,
        args=(request.namespace, request.pause_cid),
        kwargs={"cni_network": request.cni_network, "ifname": request.ifname},
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="terminate_pod_by_pause_cid",
        error_code="TERMINATE_POD_BY_CID_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace, "pause_cid":request.pause_cid},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/destroy_all_pods/", tags=["Containerd - Pods"])
async def destroy_all_pods_api(request: DestroyAllPodsRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=destroy_all_pods_task,
        args=(request.namespace,),
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="destroy_all_pods",
        error_code="DESTROY_ALL_PODS_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


# ==================== Deployment Scheduler ====================

@log_to_file(logger)
@handle_async_errors("schedule_deployment", "SCHEDULER_ERROR")
@app.post("/scheduler/deploy/", tags=["Deployment Scheduler"])
async def schedule_deployment(
    request: ScheduleDeploymentRequest,
    use_chain: bool = True,
    user: str = Depends(get_current_user)
):
    """Schedule a deployment from Kubernetes-like YAML using Celery chain.
    
    This endpoint uses a Celery chain of tasks:
    1. evaluate_deployment_requirements_task - Parses YAML, evaluates resources
    2. create_aws_nodes_if_needed_task - Creates AWS nodes if needed
    3. place_and_create_pods_task - Places and creates pods on hosts
    
    Args:
        request: ScheduleDeploymentRequest with YAML content
        use_chain: Use Celery chain tasks (default: True)
        user: Authenticated user
        
    Returns:
        Task submission result with task_id for monitoring
    """
    try:
        logger.info("=" * 80)
        logger.info("DEPLOYMENT SCHEDULING REQUEST RECEIVED")
        logger.info(f"YAML length: {len(request.yaml_content)} characters")
        logger.info("=" * 80)
        
        result = schedule_deployment_from_yaml(request.yaml_content, use_chain=use_chain)
        
        if result.get('status') == 'submitted':
            # Chain task was submitted
            task_id = result.get('task_id')
            logger.info(f"Deployment scheduling chain submitted with task_id: {task_id}")
            logger.info("Chain tasks: 1) evaluate_deployment_requirements, 2) create_aws_nodes_if_needed, 3) place_and_create_pods")
            logger.info("NOTE: This chain does NOT terminate any pods or nodes")
            
            return _envelope_success(
                "Deployment scheduling chain submitted",
                {
                    'task_id': task_id,
                    'message': 'Use /task/{task_id} to check status',
                    'chain_tasks': [
                        'evaluate_deployment_requirements',
                        'create_aws_nodes_if_needed',
                        'place_and_create_pods'
                    ]
                }
            )
        else:
            # Synchronous result
            return _envelope_success("Deployment scheduled successfully", result)
    
    except Exception as e:
        logger.error(f"Failed to schedule deployment: {e}", exc_info=True)
        return create_error_response(
            error_code="SCHEDULER_ERROR",
            message=f"Failed to schedule deployment: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ==================== Containerd - Containers ====================

@log_to_file(logger)
@app.post("/containerd/destroy_container/", tags=["Containerd - Containers"])
async def destroy_container_api(request: DestroyContainerRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=destroy_container_by_id_task,
        args=(request.namespace, request.cid),
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="destroy_container",
        error_code="DESTROY_CONTAINER_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace, "container_id": request.cid},
        )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/purge_stopped/", tags=["Containerd - Maintenance"])
async def purge_stopped_api(request: PurgeStoppedRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=purge_stopped_tasks_and_containers_task,
        args=(request.namespace,),
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="purge_stopped",
        error_code="PURGE_STOPPED_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace},
        )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/prune_namespace/", tags=["Containerd - Maintenance"])
async def prune_namespace_api(request: PruneNamespaceRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=prune_namespace_task,
        args=(request.namespace,),
        kwargs={"aggressive": request.aggressive},
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="prune_namespace",
        error_code="PRUNE_NAMESPACE_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace, "aggressive": request.aggressive},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/get_container_info/", tags=["Containerd - Containers"])
async def get_container_info_api(
    request: ContainerInfoRequest,
    user: str = Depends(get_current_user)
):
    result = submit_celery_task(
        task=get_container_info_task,
        args=(request.namespace, request.cid),
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="get_container_info",
        error_code="GET_CONTAINER_INFO_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace, "container_id": request.cid},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/cleanup_tasks_by_pod_prefix/", tags=["Containerd - Maintenance"])
async def cleanup_tasks_by_pod_prefix_api(request: CleanupTasksByPodPrefixRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=cleanup_tasks_by_pod_prefix_task,
        args=(request.namespace, request.pod_id),
        kwargs={"prefer_grpc": request.prefer_grpc},
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="cleanup_tasks_by_pod_prefix",
        error_code="CLEANUP_TASKS_BY_POD_PREFIX_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace, "pod_id": request.pod_id},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


# ==================== Volume Management ====================

@log_to_file(logger)
@app.post("/storage/pvc/", tags=["Storage"])
async def create_pvc_api(
    request: CreatePVCRequest,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Create a PersistentVolumeClaim.
    
    This will automatically provision a PersistentVolume if needed.
    """
    try:
        from utils.storage.volume_manager import VolumeManager
        from utils.storage.volume import PersistentVolumeClaim, VolumeAccessMode
        
        volume_manager = VolumeManager()
        
        # Convert access modes
        access_modes = [VolumeAccessMode(am) for am in request.access_modes]
        
        # Create PVC
        pvc = PersistentVolumeClaim(
            name=request.name,
            namespace=request.namespace,
            storage_class=request.storage_class,
            access_modes=access_modes,
            resources=request.resources
        )
        
        # Create and bind to PV
        pvc = volume_manager.create_pvc(pvc)
        
        return _envelope_success(
            f"PVC {request.namespace}/{request.name} created and bound",
            pvc.to_dict()
        )
    except Exception as e:
        logger.error(f"Failed to create PVC: {e}", exc_info=True)
        return create_error_response(
            error_code="PVC_CREATION_ERROR",
            message=f"Failed to create PVC: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/storage/pvc/{namespace}/{name}", tags=["Storage"])
async def get_pvc_api(
    namespace: str,
    name: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get a PersistentVolumeClaim by namespace and name."""
    try:
        from utils.storage.volume_store import VolumeStore
        
        volume_store = VolumeStore()
        pvc = volume_store.get_pvc(namespace, name)
        
        if not pvc:
            return create_error_response(
                error_code="PVC_NOT_FOUND",
                message=f"PVC {namespace}/{name} not found",
                status_code=status.HTTP_404_NOT_FOUND
            )
        
        return _envelope_success("PVC retrieved", pvc.to_dict())
    except Exception as e:
        logger.error(f"Failed to get PVC: {e}", exc_info=True)
        return create_error_response(
            error_code="GET_PVC_ERROR",
            message=f"Failed to get PVC: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/storage/pvc/", tags=["Storage"])
async def list_pvcs_api(
    namespace: Optional[str] = None,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """List all PersistentVolumeClaims, optionally filtered by namespace."""
    try:
        from utils.storage.volume_store import VolumeStore
        
        volume_store = VolumeStore()
        pvcs = volume_store.list_pvcs(namespace=namespace)
        
        return _envelope_success(
            f"Retrieved {len(pvcs)} PVC(s)",
            [pvc.to_dict() for pvc in pvcs]
        )
    except Exception as e:
        logger.error(f"Failed to list PVCs: {e}", exc_info=True)
        return create_error_response(
            error_code="LIST_PVCS_ERROR",
            message=f"Failed to list PVCs: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.delete("/storage/pvc/{namespace}/{name}", tags=["Storage"])
async def delete_pvc_api(
    namespace: str,
    name: str,
    delete_volume: bool = False,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Delete a PersistentVolumeClaim.
    
    Args:
        namespace: PVC namespace
        name: PVC name
        delete_volume: If True, also delete the bound PersistentVolume
    """
    try:
        from utils.storage.volume_manager import VolumeManager
        
        volume_manager = VolumeManager()
        success = volume_manager.delete_pvc(namespace, name, delete_volume=delete_volume)
        
        if not success:
            return create_error_response(
                error_code="PVC_DELETION_ERROR",
                message=f"Failed to delete PVC {namespace}/{name}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        return _envelope_success(f"PVC {namespace}/{name} deleted")
    except Exception as e:
        logger.error(f"Failed to delete PVC: {e}", exc_info=True)
        return create_error_response(
            error_code="PVC_DELETION_ERROR",
            message=f"Failed to delete PVC: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/storage/pv/", tags=["Storage"])
async def list_pvs_api(
    storage_class: Optional[str] = None,
    status_filter: Optional[str] = None,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """List all PersistentVolumes, optionally filtered by storage class or status."""
    try:
        from utils.storage.volume_store import VolumeStore
        from utils.storage.volume import VolumeStatus
        
        volume_store = VolumeStore()
        status = VolumeStatus(status_filter) if status_filter else None
        pvs = volume_store.list_pvs(storage_class=storage_class, status=status)
        
        return _envelope_success(
            f"Retrieved {len(pvs)} PV(s)",
            [pv.to_dict() for pv in pvs]
        )
    except Exception as e:
        logger.error(f"Failed to list PVs: {e}", exc_info=True)
        return create_error_response(
            error_code="LIST_PVS_ERROR",
            message=f"Failed to list PVs: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/storage/pv/{name}", tags=["Storage"])
async def get_pv_api(
    name: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get a PersistentVolume by name."""
    try:
        from utils.storage.volume_store import VolumeStore
        
        volume_store = VolumeStore()
        pv = volume_store.get_pv(name)
        
        if not pv:
            return create_error_response(
                error_code="PV_NOT_FOUND",
                message=f"PV {name} not found",
                status_code=status.HTTP_404_NOT_FOUND
            )
        
        return _envelope_success("PV retrieved", pv.to_dict())
    except Exception as e:
        logger.error(f"Failed to get PV: {e}", exc_info=True)
        return create_error_response(
            error_code="GET_PV_ERROR",
            message=f"Failed to get PV: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.delete("/storage/pv/{name}", tags=["Storage"])
async def delete_pv_api(
    name: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Delete a PersistentVolume."""
    try:
        from utils.storage.volume_manager import VolumeManager
        
        volume_manager = VolumeManager()
        success = volume_manager.delete_pv(name)
        
        if not success:
            return create_error_response(
                error_code="PV_DELETION_ERROR",
                message=f"Failed to delete PV {name}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        return _envelope_success(f"PV {name} deleted")
    except Exception as e:
        logger.error(f"Failed to delete PV: {e}", exc_info=True)
        return create_error_response(
            error_code="PV_DELETION_ERROR",
            message=f"Failed to delete PV: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/storage/storageclasses/", tags=["Storage"])
async def list_storage_classes_api(
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """List all available storage classes."""
    try:
        from utils.storage.storage_classes import DEFAULT_STORAGE_CLASSES
        
        storage_classes = [
            sc.to_dict() for sc in DEFAULT_STORAGE_CLASSES.values()
        ]
        
        return _envelope_success(
            f"Retrieved {len(storage_classes)} storage class(es)",
            storage_classes
        )
    except Exception as e:
        logger.error(f"Failed to list storage classes: {e}", exc_info=True)
        return create_error_response(
            error_code="LIST_STORAGE_CLASSES_ERROR",
            message=f"Failed to list storage classes: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.post("/storage/snapshot/", tags=["Storage"])
async def create_snapshot_api(
    namespace: str,
    pvc_name: str,
    snapshot_name: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Create a snapshot of a PersistentVolumeClaim."""
    try:
        from utils.storage.volume_manager import VolumeManager
        
        volume_manager = VolumeManager()
        snapshot = volume_manager.create_snapshot(namespace, pvc_name, snapshot_name)
        
        if not snapshot:
            return create_error_response(
                error_code="SNAPSHOT_CREATION_ERROR",
                message=f"Failed to create snapshot {snapshot_name}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        return _envelope_success(
            f"Snapshot {snapshot_name} created",
            snapshot.to_dict()
        )
    except Exception as e:
        logger.error(f"Failed to create snapshot: {e}", exc_info=True)
        return create_error_response(
            error_code="SNAPSHOT_CREATION_ERROR",
            message=f"Failed to create snapshot: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.get("/storage/snapshot/", tags=["Storage"])
async def list_snapshots_api(
    namespace: Optional[str] = None,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """List all snapshots, optionally filtered by namespace."""
    try:
        from utils.storage.volume_store import VolumeStore
        
        volume_store = VolumeStore()
        snapshots = volume_store.list_snapshots(namespace=namespace)
        
        return _envelope_success(
            f"Retrieved {len(snapshots)} snapshot(s)",
            [snapshot.to_dict() for snapshot in snapshots]
        )
    except Exception as e:
        logger.error(f"Failed to list snapshots: {e}", exc_info=True)
        return create_error_response(
            error_code="LIST_SNAPSHOTS_ERROR",
            message=f"Failed to list snapshots: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.delete("/storage/snapshot/{namespace}/{name}", tags=["Storage"])
async def delete_snapshot_api(
    namespace: str,
    name: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Delete a volume snapshot."""
    try:
        from utils.storage.volume_manager import VolumeManager
        
        volume_manager = VolumeManager()
        success = volume_manager.delete_snapshot(namespace, name)
        
        if not success:
            return create_error_response(
                error_code="SNAPSHOT_DELETION_ERROR",
                message=f"Failed to delete snapshot {namespace}/{name}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        return _envelope_success(f"Snapshot {namespace}/{name} deleted")
    except Exception as e:
        logger.error(f"Failed to delete snapshot: {e}", exc_info=True)
        return create_error_response(
            error_code="SNAPSHOT_DELETION_ERROR",
            message=f"Failed to delete snapshot: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@log_to_file(logger)
@app.post("/storage/snapshot/restore/", tags=["Storage"])
async def restore_from_snapshot_api(
    snapshot_name: str,
    namespace: str,
    new_pvc_name: str,
    user: str = Depends(get_current_user)
) -> Dict[str, Any]:
    """Restore a PVC from a snapshot."""
    try:
        from utils.storage.volume_manager import VolumeManager
        
        volume_manager = VolumeManager()
        pvc = volume_manager.restore_from_snapshot(snapshot_name, namespace, new_pvc_name)
        
        if not pvc:
            return create_error_response(
                error_code="SNAPSHOT_RESTORE_ERROR",
                message=f"Failed to restore PVC from snapshot {snapshot_name}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        return _envelope_success(
            f"PVC {namespace}/{new_pvc_name} restored from snapshot",
            pvc.to_dict()
        )
    except Exception as e:
        logger.error(f"Failed to restore from snapshot: {e}", exc_info=True)
        return create_error_response(
            error_code="SNAPSHOT_RESTORE_ERROR",
            message=f"Failed to restore from snapshot: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


def run_server():
    import uvicorn
    uvicorn.run( "server.main_api:app", host="0.0.0.0", port=8000,workers=1)


if __name__ == "__main__":
    run_server()