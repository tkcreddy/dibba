"""
Helper functions for getting AWS node configuration from Redis with fallback to config file.

This module provides a unified way to access AWS node configuration:
- First checks Redis cache (for manual updates outside frontend)
- Falls back to config.json file (for initial setup/backward compatibility)
"""
from typing import Dict, Any, Optional
from utils.redis.redis_interface import RedisInterface
from utils.ReadConfig import ReadConfig
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


@log_to_file(logger)
def get_aws_node_config() -> Dict[str, Any]:
    """
    Get AWS node configuration from Redis (if available) or fall back to config file.
    
    This allows AWS node configuration to be updated manually in Redis outside the frontend,
    while maintaining backward compatibility with config.json.
    
    Returns:
        Dictionary containing only the requested AWS node configuration fields:
        - ami_id: AMI ID
        - key_name: Key pair name
        - security_group_ids: List of security group IDs
        - subnet_id: Subnet ID
    
    The configuration from Redis takes precedence over config.json if both are present.
    If a field is missing from Redis, it will be filled from config.json.
    Only the 4 requested fields are returned, not the entire AWS config.
    """
    redis_interface = RedisInterface()
    
    # Only these 4 fields are stored in Redis (as requested by user)
    requested_fields = ['ami_id', 'key_name', 'security_group_ids', 'subnet_id']
    
    # Try to get from Redis first
    redis_config = redis_interface.get_aws_node_config()
    
    # Get config file as fallback (only for the requested fields)
    read_config = ReadConfig()
    file_config = read_config.aws_config
    
    # Merge: Redis config takes precedence, but fill missing fields from file config
    # Only include the 4 requested fields
    config = {}
    
    if redis_config:
        logger.debug("Found AWS node configuration in Redis, using it as primary source")
        # Only copy the requested fields from Redis
        for key in requested_fields:
            if key in redis_config and redis_config[key]:
                config[key] = redis_config[key]
        
        # Fill in any missing fields from file config
        for key in requested_fields:
            if key not in config or not config[key]:
                if key in file_config and file_config[key]:
                    config[key] = file_config[key]
                    logger.debug(f"Filled missing {key} from config file: {file_config[key]}")
    else:
        logger.debug("No AWS node configuration in Redis, using config file (filtered to requested fields)")
        # Only copy the requested fields from file config
        for key in requested_fields:
            if key in file_config and file_config[key]:
                config[key] = file_config[key]
    
    # Ensure security_group_ids is a list
    if 'security_group_ids' in config:
        if isinstance(config['security_group_ids'], str):
            # Handle comma-separated string or single value
            if ',' in config['security_group_ids']:
                config['security_group_ids'] = [sg.strip() for sg in config['security_group_ids'].split(',')]
            else:
                config['security_group_ids'] = [config['security_group_ids']]
    
    logger.debug(f"Final AWS node configuration: {list(config.keys())}")
    return config


@log_to_file(logger)
def update_aws_node_config(config: Dict[str, Any]) -> None:
    """
    Update AWS node configuration in Redis.
    
    This allows manual updates of AWS node configuration outside the frontend.
    
    Args:
        config: Dictionary containing AWS configuration fields to update:
            - ami_id: (optional) AMI ID
            - key_name: (optional) Key pair name
            - security_group_ids: (optional) List of security group IDs
            - subnet_id: (optional) Subnet ID
    
    Only the 4 requested fields can be updated. Only provided fields will be updated.
    """
    redis_interface = RedisInterface()
    redis_interface.save_aws_node_config(config)
    logger.info(f"Updated AWS node configuration in Redis: {list(config.keys())}")

