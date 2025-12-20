from typing import Dict, Any, Union
from utils.celery.celery_config import celery_app
from utils.os.os_interface import *
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


@celery_app.task
@log_to_file(logger)
def get_worker_node_info() -> Union[Dict[str, Any], str]:
    """Get worker node system information.
    
    Returns:
        Dictionary with system information or empty string on error
    """
    response_data = ""

    try:
        response_data = get_system_info()
        # return respose_data
    except Exception as err:
        logger.error(f"Error getting worker node info: {err}", exc_info=True)
    finally:
        return response_data


@celery_app.task
@log_to_file(logger)
def get_host_ip() -> str:
    """Get host IP address.
    
    Returns:
        IP address string or empty string on error
    """
    response_data=""
    try:
        response_data = host_ip()
    except Exception as err:
        logger.error(f"Error in get_host_ip: {err}", exc_info=True)

    return response_data



@celery_app.task
@log_to_file(logger)
def get_usage() -> Union[Dict[str, Any], str]:
    """Get system usage information (CPU, memory, etc.).
    
    Returns:
        Dictionary with usage information or empty string on error
    """
    response_data=""
    try:
        response_data = get_system_usage()
    except Exception as err:
        logger.error(f"Error getting system usage: {err}", exc_info=True)
    return response_data


