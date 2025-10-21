from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from kombu import Exchange
from datetime import datetime, timedelta
import jwt
import logging
from utils.ReadConfig import ReadConfig
from utils.celery.tasks.aws_tasks import create_worker_nodes, terminate_worker_node
from utils.redis.redis_interface import RedisInterface

# Initialize FastAPI app
app = FastAPI()

# Read configuration
read_config = ReadConfig()
aws_config = read_config.aws_config
key_config = read_config.encryption_config
redis_db_config = read_config.redis_db_config

# Configure Redis Interface
redis_client = RedisInterface(
    redis_db_config['redis_host'],
    redis_db_config['redis_port'],
    redis_db_config['redis_db']
)

# Security configuration
SECRET_KEY = key_config['key']
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# OAuth2 dependency
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Logging setup
logger = logging.getLogger("main")
logging.basicConfig(level=logging.INFO)

# Queue configuration (RabbitMQ or other backend)
queue_info = {
    'exchange': Exchange('secure_exchange', type='direct'),
    'queue': redis_client.encode_hostname_with_key("aws_interface"),
    'routing_key': redis_client.encode_hostname_with_key("aws_interface"),
    'delivery_mode': 2,  # Persistent messages
}


# Helper Functions


def authenticate_user(username: str, password: str) -> bool:
    """Authenticate user by checking credentials stored in Redis."""
    stored_password_hash = redis_client.get_user_pass(username)
    if not stored_password_hash:
        return False
    return redis_client.encode_phrase_with_key(password) == stored_password_hash


def create_access_token(data: dict, expires_delta: timedelta) -> str:
    """Generate JWT access token."""
    to_encode = data.copy()
    to_encode.update({"exp": datetime.utcnow() + expires_delta})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> str:
    """Decode JWT token and verify its validity."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username and redis_client.get_user_pass(username):
            return username
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def save_to_redis_instances(namespace: str, instances: list):
    """Save multiple instances to Redis."""
    for instance in instances:
        redis_client.save_node(instance["Name"], instance)


# Pydantic models


class CreateInstanceRequest(BaseModel):
    """Model for EC2 Instance Creation API"""
    instance_type: str
    ami_id: str
    key_name: str
    security_group_ids: list[str]
    namespace: str
    min_count: int
    max_count: int


class TerminateInstanceRequest(BaseModel):
    """Model for EC2 Termination API"""
    namespace: str


class TokenResponse(BaseModel):
    """Model for Token Response"""
    access_token: str
    token_type: str


@app.post("/token", response_model=TokenResponse)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Login endpoint to authenticate user and return JWT token.
    """
    if not authenticate_user(form_data.username, form_data.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Create JWT token
    access_token = create_access_token(
        data={"sub": form_data.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/create-instances/")
async def create_instances(
        request: CreateInstanceRequest, token: str = Depends(oauth2_scheme)
):
    """
    Create EC2 Instances via Celery Task.
    """
    # Validate token and extract user
    user = verify_token(token)

    try:
        task = create_worker_nodes.apply_async(
            args=(
                aws_config['aws_access_key_id'],
                aws_config['aws_secret_access_key'],
                aws_config['region'],
                request.instance_type,
                request.ami_id,
                request.key_name,
                request.security_group_ids,
                request.namespace,
            ),
            kwargs={
                'MinCount': request.min_count,
                'MaxCount': request.max_count
            },
            **queue_info
        )

        return {"message": "Task successfully submitted", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error in create-instances endpoint: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit task")


@app.post("/terminate-instances/")
async def terminate_instances(
        request: TerminateInstanceRequest, token: str = Depends(oauth2_scheme)
):
    """
    Terminate EC2 Instances linked to a namespace.
    """
    # Validate token and extract user
    user = verify_token(token)

    try:
        # Find associated instance IDs in Redis by namespace
        instances = redis_client.get_instance_ids_namespace(namespace=request.namespace)

        if not instances:
            raise HTTPException(status_code=404, detail="No instances found for this namespace")

        task = terminate_worker_node.apply_async(
            args=(
                aws_config['aws_access_key_id'],
                aws_config['aws_secret_access_key'],
                aws_config['region'],
                instances
            ),
            **queue_info
        )

        # Cleanup Redis entries for terminated namespace
        redis_client.delete_instance_ids_namespace(request.namespace)
        return {"message": "Termination task successfully submitted", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error in terminate-instances endpoint: {e}")
        raise HTTPException(status_code=500, detail="Failed to terminate instances")


@app.get("/task/{task_id}")
async def get_task_status(task_id: str, token: str = Depends(oauth2_scheme)):
    """
    Fetch status and result of a Celery Task.
    """
    # Validate token and extract user
    user = verify_token(token)

    task = create_worker_nodes.AsyncResult(task_id)

    if task.state == "SUCCESS":
        return {"task_id": task_id, "status": task.state, "result": task.result}
    elif task.state in ["PENDING", "FAILURE", "REVOKED"]:
        return {"task_id": task_id, "status": task.state, "error": str(task.result)}
    return {"task_id": task_id, "status": "In Progress"}


# Run the server
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
