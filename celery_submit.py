import socket
from utils.celery.tasks  import AWSTasks
from utils.celery.celery_config import celery_app
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld

logger = LogKCld()

class TaskSubmitter:
    def __init__(self, aws_access_key=None, aws_secret_key=None, region=None):
        """
        Initializes the TaskSubmitter with AWS credentials.
        """
        read_config = rc("/")
        instance_type = "t2.micro"
        ami_id = "ami-02d3fd86e6a2f5122"
        key_name = "NEW_KCR"
        security_group_ids = ["sg-09ac434d5bead2ab1"]
        aws_config = read_config.aws_config
        logger.debug(f"AWS config loaded: region={aws_config.get('region')}")
        self.aws_tasks = AWSTasks(aws_config['aws_access_key_id'], aws_config['aws_secret_access_key'], aws_config['region'])
        self.hostname = socket.gethostname()

    def submit_task(self, instance_type, key_name, security_group, ami_id):
        """
        Submits the EC2 creation task to the Celery worker.
        """
        instance_type = "t2.micro"
        ami_id = "ami-02d3fd86e6a2f5122"
        key_name = "NEW_KCR"
        security_group = ["sg-09ac434d5bead2ab1"]
        try:
            result = self.aws_tasks.create_instance.apply_async(
                args=(self.hostname, instance_type, key_name, security_group, ami_id),
            )
        except Exception as err:
            logger.error(f"Error submitting task: {err}", exc_info=True)
            raise

        logger.info(f"Task sent for hostname: {self.hostname}, task_id: {result.id}")
        return result.get(timeout=60)

# Example usage
if __name__ == "__main__":
    # AWS credentials (use environment variables if None)
    aws_access_key = "your-access-key"
    aws_secret_key = "your-secret-key"

    # Initialize TaskSubmitter
    task_submitter = TaskSubmitter(aws_access_key, aws_secret_key, region="us-east-1")

    # Task parameters
    instance_type = "t2.micro"
    key_name = "my-key-pair"
    security_group = "default"
    ami_id = "ami-12345678"

    try:
        # Submit the task
        result = task_submitter.submit_task(instance_type, key_name, security_group, ami_id)
        logger.info(f"Task Result: {result}")
    except Exception as e:
        logger.error(f"Error submitting task: {e}", exc_info=True)
