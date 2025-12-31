"""
Redis-based storage for deployment configurations.

This module provides methods to:
- Store deployment specifications persistently
- Retrieve deployment configurations
- Track deployment state and replicas
"""
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from logpkg.log_kcld import LogKCld, log_to_file
from utils.redis.redis_interface import RedisInterface
from utils.exceptions import RedisError
from utils.error_handlers import handle_errors

logger = LogKCld()


class RedisKeyPatterns:
    """Constants for Redis key patterns for deployments."""
    DEPLOYMENT_DATA = "deployment:{namespace}:{name}"
    DEPLOYMENT_INDEX_ALL = "deployment:index:all"
    DEPLOYMENT_INDEX_NAMESPACE = "deployment:index:namespace:{namespace}"
    DEPLOYMENT_INDEX_APP = "deployment:index:app:{app_label}"


class DeploymentStore:
    """Redis-based storage for deployment configurations."""
    
    # TTL for deployment data (no expiration - persistent)
    DEPLOYMENT_TTL: Optional[int] = None  # None = no expiration
    
    def __init__(self, redis_interface: RedisInterface) -> None:
        """Initialize DeploymentStore with Redis interface.
        
        Args:
            redis_interface: RedisInterface instance for Redis operations
        """
        if not isinstance(redis_interface, RedisInterface):
            raise TypeError(
                f"redis_interface must be RedisInterface instance, got {type(redis_interface)}"
            )
        self.redis = redis_interface.redis_client
        self.redis_interface = redis_interface
    
    @log_to_file(logger)
    @handle_errors("save_deployment", "REDIS_ERROR")
    def save_deployment(
        self,
        name: str,
        namespace: str,
        app_label: str,
        yaml_content: str,
        deployment_spec: Dict[str, Any],
        replicas: int,
        min_replicas: Optional[int] = None,
        max_replicas: Optional[int] = None,
    ) -> None:
        """Save deployment configuration to Redis.
        
        Args:
            name: Deployment name
            namespace: Namespace
            app_label: Application label
            yaml_content: Original YAML content
            deployment_spec: Parsed deployment specification dict
            replicas: Current replica count
            min_replicas: Minimum replicas (optional)
            max_replicas: Maximum replicas (optional)
        """
        deployment_key = RedisKeyPatterns.DEPLOYMENT_DATA.format(
            namespace=namespace,
            name=name
        )
        
        # name parameter is metadata.name from the deployment YAML
        deployment_data = {
            "name": name,  # metadata.name from YAML - this is the application name displayed in UI
            "namespace": namespace,
            "app_label": app_label,  # labels.app from YAML - used for pod matching
            "yaml_content": yaml_content,
            "deployment_spec": deployment_spec,
            "replicas": replicas,
            "min_replicas": min_replicas or replicas,
            "max_replicas": max_replicas or replicas,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        
        # Save deployment data
        self.redis.hset(deployment_key, "data", json.dumps(deployment_data))
        
        # Update indexes
        pipe = self.redis.pipeline()
        
        # Add to all deployments index
        pipe.sadd(RedisKeyPatterns.DEPLOYMENT_INDEX_ALL, f"{namespace}:{name}")
        
        # Add to namespace index
        namespace_index_key = RedisKeyPatterns.DEPLOYMENT_INDEX_NAMESPACE.format(namespace=namespace)
        pipe.sadd(namespace_index_key, name)
        
        # Add to app index
        app_index_key = RedisKeyPatterns.DEPLOYMENT_INDEX_APP.format(app_label=app_label)
        pipe.sadd(app_index_key, f"{namespace}:{name}")
        
        # Set TTL if specified
        if self.DEPLOYMENT_TTL:
            pipe.expire(deployment_key, self.DEPLOYMENT_TTL)
        
        pipe.execute()
        
        logger.info(f"Saved deployment {namespace}/{name} to Redis")
    
    @log_to_file(logger)
    @handle_errors("get_deployment", "REDIS_ERROR")
    def get_deployment(self, name: str, namespace: str) -> Optional[Dict[str, Any]]:
        """Get deployment configuration from Redis.
        
        Args:
            name: Deployment name
            namespace: Namespace
            
        Returns:
            Deployment data dictionary if found, None otherwise
        """
        deployment_key = RedisKeyPatterns.DEPLOYMENT_DATA.format(
            namespace=namespace,
            name=name
        )
        
        deployment_data_str = self.redis.hget(deployment_key, "data")
        if not deployment_data_str:
            return None
        
        try:
            return json.loads(deployment_data_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse deployment data for {namespace}/{name}: {e}")
            return None
    
    @log_to_file(logger)
    @handle_errors("get_all_deployments", "REDIS_ERROR")
    def get_all_deployments(self) -> List[Dict[str, Any]]:
        """Get all deployment configurations.
        
        Returns:
            List of deployment data dictionaries
        """
        deployment_keys = self.redis.smembers(RedisKeyPatterns.DEPLOYMENT_INDEX_ALL)
        deployments = []
        
        for deployment_key in deployment_keys:
            try:
                namespace, name = deployment_key.split(":", 1)
                deployment_data = self.get_deployment(name, namespace)
                if deployment_data:
                    deployments.append(deployment_data)
            except Exception as e:
                logger.warning(f"Failed to get deployment {deployment_key}: {e}")
                continue
        
        return deployments
    
    @log_to_file(logger)
    @handle_errors("get_deployments_by_namespace", "REDIS_ERROR")
    def get_deployments_by_namespace(self, namespace: str) -> List[Dict[str, Any]]:
        """Get all deployments in a namespace.
        
        Args:
            namespace: Namespace name
            
        Returns:
            List of deployment data dictionaries
        """
        namespace_index_key = RedisKeyPatterns.DEPLOYMENT_INDEX_NAMESPACE.format(namespace=namespace)
        deployment_names = self.redis.smembers(namespace_index_key)
        deployments = []
        
        for name in deployment_names:
            deployment_data = self.get_deployment(name, namespace)
            if deployment_data:
                deployments.append(deployment_data)
        
        return deployments
    
    @log_to_file(logger)
    @handle_errors("get_deployments_by_app", "REDIS_ERROR")
    def get_deployments_by_app(self, app_label: str) -> List[Dict[str, Any]]:
        """Get all deployments for an application.
        
        Args:
            app_label: Application label
            
        Returns:
            List of deployment data dictionaries
        """
        app_index_key = RedisKeyPatterns.DEPLOYMENT_INDEX_APP.format(app_label=app_label)
        deployment_keys = self.redis.smembers(app_index_key)
        deployments = []
        
        for deployment_key in deployment_keys:
            try:
                namespace, name = deployment_key.split(":", 1)
                deployment_data = self.get_deployment(name, namespace)
                if deployment_data:
                    deployments.append(deployment_data)
            except Exception as e:
                logger.warning(f"Failed to get deployment {deployment_key}: {e}")
                continue
        
        return deployments
    
    @log_to_file(logger)
    @handle_errors("delete_deployment", "REDIS_ERROR")
    def delete_deployment(self, name: str, namespace: str) -> None:
        """Delete deployment configuration from Redis.
        
        Args:
            name: Deployment name
            namespace: Namespace
        """
        deployment_key = RedisKeyPatterns.DEPLOYMENT_DATA.format(
            namespace=namespace,
            name=name
        )
        
        # Get deployment data to find app_label for index cleanup
        deployment_data = self.get_deployment(name, namespace)
        app_label = deployment_data.get("app_label") if deployment_data else None
        
        # Use pipeline for atomic deletion
        pipe = self.redis.pipeline()
        
        # Remove from indexes
        pipe.srem(RedisKeyPatterns.DEPLOYMENT_INDEX_ALL, f"{namespace}:{name}")
        
        namespace_index_key = RedisKeyPatterns.DEPLOYMENT_INDEX_NAMESPACE.format(namespace=namespace)
        pipe.srem(namespace_index_key, name)
        
        if app_label:
            app_index_key = RedisKeyPatterns.DEPLOYMENT_INDEX_APP.format(app_label=app_label)
            pipe.srem(app_index_key, f"{namespace}:{name}")
        
        # Delete deployment data
        pipe.delete(deployment_key)
        pipe.execute()
        
        logger.info(f"Deleted deployment {namespace}/{name} from Redis")
    
    @log_to_file(logger)
    @handle_errors("update_deployment_replicas", "REDIS_ERROR")
    def update_deployment_replicas(
        self,
        name: str,
        namespace: str,
        replicas: int
    ) -> None:
        """Update replica count for a deployment.
        
        Args:
            name: Deployment name
            namespace: Namespace
            replicas: New replica count
        """
        deployment_data = self.get_deployment(name, namespace)
        if not deployment_data:
            logger.warning(f"Deployment {namespace}/{name} not found, cannot update replicas")
            return
        
        deployment_data["replicas"] = replicas
        deployment_data["last_updated"] = datetime.now(timezone.utc).isoformat()
        
        deployment_key = RedisKeyPatterns.DEPLOYMENT_DATA.format(
            namespace=namespace,
            name=name
        )
        self.redis.hset(deployment_key, "data", json.dumps(deployment_data))
        
        logger.info(f"Updated replicas for deployment {namespace}/{name} to {replicas}")

