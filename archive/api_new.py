from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from utils.ReadConfig import ReadConfig as rc
from utils.celery.tasks.aws_tasks import get_ec2_instances, create_worker_nodes, terminate_worker_node
from utils.extensions.utilities_extention import UtilitiesExtension
from kombu import Exchange
from utils.redis.redis_interface import RedisInterface
import logging
import traceback

# Initialize FastAPI app
app = FastAPI()

# Logger setup
logger = logging.getLogger("main_api")
logging.basicConfig(level=logging.INFO)

# Configuration initialization
try:
    read_config = rc()  # Load configuration once globally
    aws_config = read_config.aws_config
    key_read = read_config.encryption_config
    redis_db_config = read_config.redis_db_config
    ue = UtilitiesExtension(key_read['key'])  # Encryption utilities
    rd = RedisInterface(
        redis_db_config['redis_host'],
        redis_db_config['redis_port'],
        redis_db_config['redis_db']
    )

    # Queue information
    queue_info = {
        'exchange': Exchange('secure_exchange', type='direct'),
        'queue': ue.encode_hostname_with_key('aws_interface'),
        'routing_key': ue.encode_hostname_with_key('aws_interface'),
        'delivery_mode': 2,
    }
except Exception as e:
    logger.error(f"Failed to load configuration: {e}")
    traceback.print_exc()
    raise


# Models for request validation
class CreateInstanceRequest(BaseModel):
    instance_type: str
    ami_id: str
    key_name: str
    security_group_ids: list[str]
    namespace: str
    min_count: int
    max_count: int


class TerminateInstanceRequest(BaseModel):
    namespace: str


# Helper Function: Submit Tasks
def submit_task(task_func, args: tuple, kwargs: dict, queue_params: dict):
    """
    A helper to provide consistent Celery task submission process.
    """
    try:
        task = task_func.apply_async(args=args, kwargs=kwargs, **queue_params)
        logger.info(f"Task {task_func.__name__} submitted with task ID: {task.id}")
        return task
    except Exception as e:
        logger.error(f"Error when submitting task: {e}")
        raise HTTPException(status_code=500, detail="Task submission failed")


# Endpoint: Create EC2 Instances
@app.post("/create-instances/")
async def create_instances(request: CreateInstanceRequest, background_tasks: BackgroundTasks):
    """
    API to create AWS EC2 instances as a Celery task, then handle results in the background.
    """
    try:
        # Submit the Celery task for creating EC2 instances
        task = submit_task(
            create_worker_nodes,
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
            kwargs={'MinCount': request.min_count, 'MaxCount': request.max_count},
            queue_params=queue_info,
        )

        # Add background monitoring for task results
        background_tasks.add_task(
            monitor_creation_task,
            task_id=task.id,
            namespace=request.namespace,
            max_count=request.max_count,
        )

        return {"message": "Task to create instances submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error processing create_instances API: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error creating instances")


# Background Task: Monitor EC2 Creation Result
async def monitor_creation_task(task_id: str, namespace: str, max_count: int):
    """
    Monitor the result of instance creation and save instances to Redis.
    """
    try:
        # Fetch result of Celery task execution
        result = create_worker_nodes.AsyncResult(task_id).get(timeout=30)
        instances = {}

        # Process the response and save to Redis
        for i in range(max_count):
            instances[result['Instances'][i]['PrivateDnsName']] = {
                'IpAddress': result['Instances'][i]['PrivateIpAddress'],
                'InstanceId': result['Instances'][i]['InstanceId'],
                'NameSpace': namespace,
                'InstanceType': result['Instances'][i]['InstanceType'],
            }

        for private_dns, instance_data in instances.items():
            # Save individual node details into Redis
            rd.save_node(private_dns, instance_data)

        logger.info(f"Instances created successfully and saved to Redis: {instances}")
    except Exception as e:
        logger.error(f"Error in monitoring creation task {task_id}: {e}")
        traceback.print_exc()


# Endpoint: Terminate EC2 Instances
@app.post("/terminate-instances/")
async def terminate_instances(request: TerminateInstanceRequest):
    """
    API to terminate AWS EC2 instances using a Celery task.
    """
    try:
        # Get saved instance IDs to terminate for the namespace
        instances_to_terminate = rd.get_instance_ids_namespace(request.namespace)

        if not instances_to_terminate:
            logger.info(f"No instances found in Redis for namespace: {request.namespace}")
            return {"message": "No instances found for the given namespace"}

        # Submit the Celery task for terminating EC2 instances
        task = submit_task(
            terminate_worker_node,
            args=(
                aws_config['aws_access_key_id'],
                aws_config['aws_secret_access_key'],
                aws_config['region'],
                instances_to_terminate,
            ),
            kwargs={},
            queue_params=queue_info,
        )

        # Clean up Redis after task submission
        rd.delete_instance_ids(instances_to_terminate)

        return {"message": "Task to terminate instances submitted successfully", "task_id": task.id}
    except Exception as e:
        logger.error(f"Error processing terminate_instances API: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error terminating instances")


if __name__ == "__main__":
    import uvicorn

    # Run FastAPI server
    uvicorn.run(app, host="0.0.0.0", port=8000)
