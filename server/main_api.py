from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Extra,ConfigDict
from server.api_models import CreatePodsRequest
from utils.celery.tasks.worker_node_tasks import *
from utils.celery.tasks.containerd_tasks import *
from utils.celery.tasks.aws_tasks import get_ec2_instances, create_worker_nodes, terminate_worker_node
from utils.extensions.utilities_extention import UtilitiesExtension
from kombu import Exchange
from utils.redis.redis_interface import RedisInterface
import logging
import jwt
from datetime import datetime, timedelta,UTC
from logpkg.log_kcld import LogKCld, log_to_file
from dataclasses import is_dataclass, asdict


logger = LogKCld()
# Initialize FastAPI app
app = FastAPI()

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
def _host_queue(host_name: str) -> dict:
    return {
        'exchange': Exchange('secure_exchange', type='direct'),
        'queue': ue.encode_hostname_with_key(host_name),
        'routing_key': ue.encode_hostname_with_key(host_name),
        'delivery_mode': 2
    }



@log_to_file(logger)
def authenticate_user(username: str, password: str):
    if not rd.get_user_pass(username):
        return False
    if ue.encode_phrase_with_key(password) == rd.get_user_pass(username):
        return username


@log_to_file(logger)
def create_access_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.now(UTC) + expires_delta
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@log_to_file(logger)
def as_plain(obj):
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
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    access_token = create_access_token(
        data={"sub": form_data.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer"}


#Dependency to get the current user
@log_to_file(logger)
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        print(username)
        if username is None or not rd.get_user_pass(username):
            raise HTTPException(status_code=401, detail="Invalid authentication")
        return username
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="Token expired") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="Invalid token") from e


@log_to_file(logger)
@app.post("/create-instances/")
async def create_instances(request: CreateInstanceRequest, user: str = Depends(get_current_user)):
    """
    API endpoint to create AWS EC2 instances via a Celery task.
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

        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting create_instances task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/terminate-namespace/")
async def terminate_namespace(request: TerminateInstanceRequest, user: str = Depends(get_current_user)):
    """
    API endpoint to terminate AWS EC2 instances via a Celery task.
    """
    try:
        # Fetch instance IDs to terminate from Redis
        instances_to_terminate = rd.get_instance_ids_namespace(request.namespace)

        if not instances_to_terminate:
            return {"message": "No instances found for the given namespace"}

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
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting terminate_instances task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
async def monitor_task(task_id: str, namespace: str, max_count: int):
    """
    Background task to monitor the Celery task result and save instances to Redis.
    """
    try:
        print(f"starting the monitoring task with {task_id}")
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
@app.get("/task/{task_id}")
async def get_task_status(task_id: str, user: str = Depends(get_current_user)):
    task = celery_app.AsyncResult(task_id)

    return {
        "task_id": task.id,
        "status": task.status,
        "result": task.result if task.ready() else None,
        "progress": task.info if task.state == "PROGRESS" else None,
    }

@log_to_file(logger)
@app.get("/get_worker_node_data/")
async def get_worker_node_data(request: HostName, user: str = Depends(get_current_user)):
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
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting get_host_system_info task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.get("/get_worker_node_ip/")
async def get_worker_node_ip(request: HostName, user: str = Depends(get_current_user)):
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
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting host_ip task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.get("/get_worker_usage_data/")
async def get_worker_usage_data(request: HostName, user: str = Depends(get_current_user)):
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
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting get_usage task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/create-pods")
@app.post("/containerd/create-pods/")
async def create_pods(request: CreatePodsRequest,user: str = Depends(get_current_user)):
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
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting get_usage task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/list_namespaces_and_pods/")
async def list_namespaces_and_pods_api(request: ContainerdHostRequest, user: str = Depends(get_current_user)):
    try:
        task = list_namespaces_and_pods_task.apply_async(
            args=(),
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting list_namespaces_and_pods_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e

@log_to_file(logger)
@app.post("/containerd/list_pods_by_namespace/")
async def list_namespaces_and_pods_api(request: PodNamespaceHostRequest, user: str = Depends(get_current_user)):
    try:
        task = list_pods_by_namespace_task.apply_async(
            args=([request.namespace]),
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting list_namespaces_and_pods_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e



@log_to_file(logger)
@app.post("/containerd/terminate_pod/")
async def terminate_pod_api(request: TerminatePodRequest, user: str = Depends(get_current_user)):
    try:
        task = terminate_pod_task.apply_async(
            args=(request.namespace, request.pod_name),
            kwargs={
                "cni_network": request.cni_network,
                "ifname": request.ifname,
            },
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting terminate_pod_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/terminate_pod_by_pause_cid/")
async def terminate_pod_by_pause_cid_api(request: TerminatePodByCidRequest, user: str = Depends(get_current_user)):
    try:
        task = terminate_pod_by_pause_cid_task.apply_async(
            args=(request.namespace, request.pause_cid),
            kwargs={
                "cni_network": request.cni_network,
                "ifname": request.ifname,
            },
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting terminate_pod_by_pause_cid_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/destroy_all_pods/")
async def destroy_all_pods_api(request: DestroyAllPodsRequest, user: str = Depends(get_current_user)):
    try:
        task = destroy_all_pods_task.apply_async(
            args=(request.namespace,),
            kwargs={
                "cni_network": request.cni_network,
                "ifname": request.ifname,
            },
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting destroy_all_pods_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/destroy_container/")
async def destroy_container_api(request: DestroyContainerRequest, user: str = Depends(get_current_user)):
    try:
        task = destroy_container_by_id_task.apply_async(
            args=(request.namespace, request.cid),
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting destroy_container_by_id_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/purge_stopped/")
async def purge_stopped_api(request: PurgeStoppedRequest, user: str = Depends(get_current_user)):
    try:
        task = purge_stopped_tasks_and_containers_task.apply_async(
            args=(request.namespace,),
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting purge_stopped_tasks_and_containers_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/prune_namespace/")
async def prune_namespace_api(request: PruneNamespaceRequest, user: str = Depends(get_current_user)):
    try:
        task = prune_namespace_task.apply_async(
            args=(request.namespace,),
            kwargs={"aggressive": request.aggressive},
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting prune_namespace_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/get_container_info/")
async def get_container_info_api(request: ContainerInfoRequest, user: str = Depends(get_current_user)):
    try:
        task = get_container_info_task.apply_async(
            args=(request.namespace, request.cid),
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting get_container_info_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e


@log_to_file(logger)
@app.post("/containerd/cleanup_tasks_by_pod_prefix/")
async def cleanup_tasks_by_pod_prefix_api(request: CleanupTasksByPodPrefixRequest, user: str = Depends(get_current_user)):
    """
    This is the one you want for:
      ctr -n <ns> task list  -> STOPPED leftovers
    Example: pod_id="cd83c6a7ac0f47c6" removes:
      cd83c6a7ac0f47c6-*
      cd83c6a7ac0f47c6 (if exists)
    """
    try:
        task = cleanup_tasks_by_pod_prefix_task.apply_async(
            args=(request.namespace, request.pod_id),
            kwargs={"prefer_grpc": request.prefer_grpc},
            **_host_queue(request.host_name)
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting cleanup_tasks_by_pod_prefix_task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e



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

