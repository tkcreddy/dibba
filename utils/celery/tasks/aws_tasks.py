from utils.celery.celery_config import celery_app
from utils.aws.aws_interface import AwsInterface
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()
read_config = rc()


def _get_aws_credentials(region: str = None):
    """Get AWS credentials from ReadConfig.
    
    Args:
        region: Optional region override. If not provided, uses region from config.
        
    Returns:
        tuple: (aws_access_key_id, aws_secret_access_key, region)
    """
    aws_config = read_config.aws_config
    aws_access_key_id = aws_config.get("aws_access_key_id")
    aws_secret_access_key = aws_config.get("aws_secret_access_key")
    aws_region = region or aws_config.get("region")
    
    if not aws_access_key_id or not aws_secret_access_key:
        raise ValueError("AWS credentials not found in config. Please configure aws_access_key_id and aws_secret_access_key in config.json")
    
    if not aws_region:
        raise ValueError("AWS region not found in config and not provided as argument. Please configure region in config.json or provide as argument")
    
    return aws_access_key_id, aws_secret_access_key, aws_region


@celery_app.task
@log_to_file(logger)
def get_ec2_instances(aws_access_key: str = None, aws_secret_key: str = None, region: str = None,
                      instance_type: str = None, key_name: str = None, security_group: list = None, ami_id: str = None):
    """Get EC2 instances information.
    
    Note: aws_access_key and aws_secret_key arguments are deprecated and ignored.
    Credentials are now read from ReadConfig.
    
    Args:
        aws_access_key: Deprecated - ignored, credentials read from config
        aws_secret_key: Deprecated - ignored, credentials read from config
        region: Optional region override. If not provided, uses region from config.
        instance_type: Optional instance type filter
        key_name: Optional key name filter
        security_group: Optional security group filter
        ami_id: Optional AMI ID filter
    """
    response_data = ""

    try:
        aws_access_key_id, aws_secret_access_key, aws_region = _get_aws_credentials(region)
        aws_interface = AwsInterface(aws_access_key_id, aws_secret_access_key, aws_region)
        response_data = aws_interface.get_ec2s_information()
        # return respose_data
    except Exception as err:
        logger.error(f"Error getting EC2 instances: {err}", exc_info=True)
    finally:
        return response_data


@celery_app.task
@log_to_file(logger)
def create_worker_nodes(aws_access_key: str = None, aws_secret_key: str = None, region: str = None,
                        instance_type: str = None, ami_id: str = None, key_name: str = None,
                        security_group_ids: list = None, subnet_id: str = None, namespace: str = None, **kwargs):
    """Create AWS EC2 worker nodes.
    
    Note: aws_access_key and aws_secret_key arguments are deprecated and ignored.
    Credentials are now read from ReadConfig.
    
    Args:
        aws_access_key: Deprecated - ignored, credentials read from config
        aws_secret_key: Deprecated - ignored, credentials read from config
        region: Optional region override. If not provided, uses region from config.
        instance_type: EC2 instance type (e.g., 't3.medium')
        ami_id: Amazon Machine Image ID
        key_name: EC2 key pair name
        security_group_ids: List of security group IDs
        subnet_id: VPC subnet ID
        namespace: Namespace for the worker nodes
        **kwargs: Additional arguments passed to create_ec2_instance
    """
    response_data = ""
    try:
        aws_access_key_id, aws_secret_access_key, aws_region = _get_aws_credentials(region)
        aws_interface = AwsInterface(aws_access_key_id, aws_secret_access_key, aws_region)
        response_data = aws_interface.create_ec2_instance(instance_type, ami_id, key_name, security_group_ids, subnet_id, namespace, **kwargs)
    except Exception as err:
        logger.error(f"Error creating worker nodes: {err}", exc_info=True)
    finally:
        return response_data


@celery_app.task
@log_to_file(logger)
def terminate_worker_node(aws_access_key: str = None, aws_secret_key: str = None, region: str = None,
                          instance_ids: list = None):
    """Terminate AWS EC2 worker nodes.
    
    Note: aws_access_key and aws_secret_key arguments are deprecated and ignored.
    Credentials are now read from ReadConfig.
    
    Args:
        aws_access_key: Deprecated - ignored, credentials read from config
        aws_secret_key: Deprecated - ignored, credentials read from config
        region: Optional region override. If not provided, uses region from config.
        instance_ids: List of EC2 instance IDs to terminate
    """
    response_data = ""
    try:
        aws_access_key_id, aws_secret_access_key, aws_region = _get_aws_credentials(region)
        aws_interface = AwsInterface(aws_access_key_id, aws_secret_access_key, aws_region)
        response_data = aws_interface.terminate_ec2_instances(instance_ids)
    except Exception as err:
        logger.error(f"Error terminating worker nodes: {err}", exc_info=True)
    finally:
        return response_data
