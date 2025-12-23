from fastapi import FastAPI, Depends, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError as PydanticValidationError
from server.api_models import CreatePodsRequest

from utils.celery.tasks.worker_node_tasks import *  # noqa
from utils.celery.tasks.containerd_tasks import *   # noqa
from utils.celery.tasks.aws_tasks import create_worker_nodes, terminate_worker_node

from utils.extensions.utilities_extention import UtilitiesExtension
from utils.redis.redis_interface import RedisInterface
from utils.ReadConfig import ReadConfig as rc
from utils.celery.celery_config import celery_app

from utils.exceptions import (
    DibbaBaseException,
    AuthenticationError,
    NotFoundError,
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
from datetime import datetime, timedelta, UTC
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


# OAuth2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Read configuration
read_config = rc()
aws_config = read_config.aws_config
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
    expire = datetime.now(UTC) + expires_delta
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


@log_to_file(logger)
def get_current_user(token: str = Depends(oauth2_scheme)):
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


# ==================== Task Status ====================

@log_to_file(logger)
@app.get("/task/{task_id}", tags=["Task Management"])
async def get_task_status(task_id: str, user: str = Depends(get_current_user)) -> Dict[str, Any]:
    task = celery_app.AsyncResult(task_id)
    payload = {
        "task_id": task.id,
        "status": task.status,
        "result": task.result if task.ready() else None,
        "progress": task.info if task.state == "PROGRESS" else None,
    }
    return _envelope_success(message="Task status retrieved", data=payload)


# ==================== Worker Node Endpoints (query params) ====================

@log_to_file(logger)
@handle_async_errors("get_worker_node_data", "TASK_SUBMISSION_ERROR")
@app.get("/get_worker_node_data/", tags=["Worker Nodes"])
async def get_worker_node_data(host_name: str, user: str = Depends(get_current_user)):
    host_queue_info = create_host_queue_info(host_name, ue)
    result = submit_celery_task(
        task=get_worker_node_info,
        queue_info=host_queue_info,
        operation_name="get_worker_node_data",
        error_code="GET_WORKER_NODE_DATA_ERROR",
        additional_data={"host_name": host_name},
    )
    # submit_celery_task already returns your standard shape (if you wrote it that way),
    # but to guarantee the contract, we normalize here:
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.get("/get_worker_node_ip/", tags=["Worker Nodes"])
async def get_worker_node_ip(host_name: str, user: str = Depends(get_current_user)):
    host_queue_info = create_host_queue_info(host_name, ue)
    result = submit_celery_task(
        task=get_host_ip,
        queue_info=host_queue_info,
        operation_name="get_worker_node_ip",
        error_code="GET_WORKER_NODE_IP_ERROR",
        additional_data={"host_name": host_name},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.get("/get_worker_usage_data/", tags=["Worker Nodes"])
async def get_worker_usage_data(host_name: str, user: str = Depends(get_current_user)):
    host_queue_info = create_host_queue_info(host_name, ue)
    result = submit_celery_task(
        task=get_usage,
        queue_info=host_queue_info,
        operation_name="get_worker_usage_data",
        error_code="GET_WORKER_USAGE_DATA_ERROR",
        additional_data={"host_name": host_name},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


# ==================== AWS Management ====================

@log_to_file(logger)
@handle_async_errors("create_instances", "TASK_SUBMISSION_ERROR")
@app.post("/create-instances/", tags=["AWS Management"])
async def create_instances(request: CreateInstanceRequest, user: str = Depends(get_current_user)):
    request_data = request.model_dump()
    defined_fields = set(CreateInstanceRequest.__annotations__.keys())
    extra_kwargs = extract_extra_kwargs(request_data, defined_fields)

    result = submit_celery_task(
        task=create_worker_nodes,
        args=(
            aws_config["aws_access_key_id"],
            aws_config["aws_secret_access_key"],
            aws_config["region"],
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
async def terminate_namespace(request: TerminateInstanceRequest, user: str = Depends(get_current_user
)):
    instances_to_terminate = rd.get_instance_ids_namespace(request.namespace)
    if not instances_to_terminate:
        raise NotFoundError(
            message="No instances found for the given namespace",
            error_code="NO_INSTANCES_FOUND",
            details={"namespace": request.namespace},
        )

    result = submit_celery_task(
        task=terminate_worker_node,
        args=(
            aws_config["aws_access_key_id"],
            aws_config["aws_secret_access_key"],
            aws_config["region"],
            instances_to_terminate,
        ),
        queue_info=aws_queue_info,
        operation_name="terminate_namespace",
        error_code="TERMINATE_INSTANCES_TASK_ERROR",
        additional_data={"instances_count": len(instances_to_terminate)},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


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


@log_to_file(logger)
@app.post("/containerd/list_namespaces_and_pods/", tags=["Containerd - Pods"])
async def list_namespaces_and_pods_api(request: ContainerdHostRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=list_namespaces_and_pods_task,
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="list_namespaces_and_pods",
        error_code="LIST_NAMESPACES_PODS_ERROR",
        additional_data={"host_name": request.host_name},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/list_pods_by_namespace/", tags=["Containerd - Pods"])
async def list_pods_by_namespace_api(request: PodNamespaceHostRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=list_pods_by_namespace_task,
        args=([request.namespace]),
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="list_pods_by_namespace",
        error_code="LIST_PODS_BY_NAMESPACE_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace},
    )
    return _envelope_success("Task submitted successfully", result.get("data", result))


@log_to_file(logger)
@app.post("/containerd/terminate_pod/", tags=["Containerd - Pods"])
async def terminate_pod_api(request: TerminatePodRequest, user: str = Depends(get_current_user)):
    result = submit_celery_task(
        task=terminate_pod_task,
        args=(request.namespace, request.pod_name),
        kwargs={"cni_network": request.cni_network, "ifname": request.ifname},
        queue_info=create_host_queue_info(request.host_name, ue),
        operation_name="terminate_pod",
        error_code="TERMINATE_POD_ERROR",
        additional_data={"host_name": request.host_name, "namespace": request.namespace, "pod_name":request.pod_name},
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
async def get_container_info_api(request: ContainerInfoRequest, user: str = Depends(get_current_user)
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


def run_server():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run_server()