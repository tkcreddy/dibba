from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Extra,ConfigDict
from utils.ReadConfig import ReadConfig as rc
from utils.celery.celery_config import celery_app
from utils.celery.tasks.worker_node_tasks import *
from utils.celery.tasks.containerd_tasks import *
from utils.celery.tasks.aws_tasks import get_ec2_instances, create_worker_nodes, terminate_worker_node
from utils.extensions.utilities_extention import UtilitiesExtension
from kombu import Exchange
from utils.redis.redis_interface import RedisInterface
from dataclasses import dataclass,field
import logging
import jwt
from datetime import datetime, timedelta,UTC
from logpkg.log_kcld import LogKCld, log_to_file
from typing import Optional, Dict, List, Tuple
from utils.containerd.containerd_interface import ContainerdClient, PodManager, ResourceSpec,ContainerSpec



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


@dataclass
class ContainerSpec:
    name: str
    image: str
    args: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    resources: Optional[ResourceSpec] = None


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


class CreatePodsRequest(BaseModel):
    containers: List[ContainerSpec]
    namespace: str


class TerminateInstanceRequest(BaseModel):
    namespace: str


class TaskId(BaseModel):
    task_id: str


class HostName(BaseModel):
    host_name: str


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
@app.get("/create_pods/")
async def create_pods(request: CreatePodsRequest,user: str = Depends(get_current_user)):
    host_queue_info = {
        'exchange': Exchange('secure_exchange', type='direct'),
        'queue': ue.encode_hostname_with_key(request.host_name),
        'routing_key': ue.encode_hostname_with_key(request.host_name),
        'delivery_mode': 2
    }
    request_data = request.dict()
    defined_fields = CreatePodsRequest.__annotations__.keys()
    extra_kwargs = {k: v for k, v in request_data.items() if k not in defined_fields}

    try:
        task = create_pod_task.apply_async(
            args=(
                request.containers,
                request.namespace
            ),
            kwargs={
                **extra_kwargs
                   },
            **host_queue_info
        )
        return {"message": "Task submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error submitting get_usage task: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task") from e

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
