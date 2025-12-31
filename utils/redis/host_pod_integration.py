"""
Integration code to automatically update Redis with host and pod information.

This module provides functions to:
- Update host information from worker_node_tasks results
- Update pod information from containerd_tasks results
- Sync data automatically when tasks complete
"""
from typing import Dict, Any, Optional, List
from logpkg.log_kcld import LogKCld, log_to_file
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from utils.exceptions import RedisError

logger = LogKCld()


class HostPodIntegration:
    """Integration class to sync host and pod data from task results."""
    
    def __init__(self, redis_interface: RedisInterface) -> None:
        """Initialize integration with Redis interface.
        
        Args:
            redis_interface: RedisInterface instance
            
        Raises:
            TypeError: If redis_interface is not a RedisInterface instance
        """
        if not isinstance(redis_interface, RedisInterface):
            raise TypeError(
                f"redis_interface must be RedisInterface instance, got {type(redis_interface)}"
            )
        self.store = HostPodStore(redis_interface)
        self.redis_interface = redis_interface
    
    @log_to_file(logger)
    def update_host_from_task_result(
        self,
        hostname: str,
        system_info: Optional[Dict[str, Any]] = None,
        usage_metrics: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None
    ) -> None:
        """Update host information from worker_node_tasks results.
        
        This should be called when:
        - get_worker_node_info task completes (system_info)
        - get_usage task completes (usage_metrics)
        - get_host_ip task completes (ip_address)
        
        Args:
            hostname: Host identifier (must be non-empty)
            system_info: Result from get_worker_node_info task
            usage_metrics: Result from get_usage task
            ip_address: Result from get_host_ip task
            
        Raises:
            ValueError: If hostname is empty or invalid
            RedisError: If Redis operation fails
        """
        if not hostname or not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("hostname must be a non-empty string")
        
        try:
            self.store.save_host_info(
                hostname=hostname,
                ip_address=ip_address,
                system_info=system_info,
                usage_metrics=usage_metrics,
                status="online"
            )
            logger.info(
                f"Updated host {hostname} from task results "
                f"(IP: {ip_address}, has_system_info: {system_info is not None}, "
                f"has_usage_metrics: {usage_metrics is not None})"
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid input for host {hostname}: {e}", exc_info=True)
            raise
        except RedisError as e:
            logger.error(f"Redis error updating host {hostname}: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error updating host {hostname}: {e}", exc_info=True)
            raise RedisError(
                message=f"Failed to update host {hostname}",
                error_code="HOST_UPDATE_ERROR",
                details={"hostname": hostname},
                cause=e
            ) from e
    
    @log_to_file(logger)
    def update_pod_from_task_result(
        self,
        pod_result: Dict[str, Any],
        hostname: str
    ) -> None:
        """Update pod information from create_pod_task result.
        
        This should be called when create_pod_task completes.
        
        Args:
            pod_result: Result dictionary from create_pod_task
            hostname: Host where pod was created (must be non-empty)
            
        Raises:
            ValueError: If hostname is empty or pod_result is invalid
            RedisError: If Redis operation fails
        """
        if not hostname or not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("hostname must be a non-empty string")
        
        if not isinstance(pod_result, dict):
            raise ValueError(f"pod_result must be a dictionary, got {type(pod_result)}")
        
        try:
            # Extract pod information from task result
            pod_data = pod_result.get("pod", {})
            if not isinstance(pod_data, dict):
                pod_data = {}
            
            namespace = pod_result.get("namespace", "default")
            if not isinstance(namespace, str):
                namespace = "default"
            
            pod_ipv4 = pod_result.get("pod_ipv4")
            apps = pod_result.get("apps", [])
            if not isinstance(apps, list):
                apps = []
            
            cni = pod_result.get("cni", {})
            if not isinstance(cni, dict):
                cni = {}
            
            # Extract pod_id from pod data
            pod_id = pod_data.get("name") or pod_data.get("pod_id")
            if not pod_id or not isinstance(pod_id, str):
                logger.warning(f"No valid pod_id found in pod result: {pod_result}")
                return
            
            # Extract pause container info
            pause_container = pod_data.get("pause", {})
            if not isinstance(pause_container, dict):
                pause_container = {}
            
            # Build containers list
            containers: List[Dict[str, Any]] = []
            for app in apps:
                if not isinstance(app, dict):
                    continue
                container_info = {
                    "cid": app.get("cid"),
                    "name": app.get("name"),
                    "image": app.get("image"),
                    "pid": app.get("pid"),
                    "status": app.get("status", "running")
                }
                containers.append(container_info)
            
            # Extract labels if available (from task result, additional_data, or use empty dict)
            labels = pod_result.get("labels", {})
            # Also check additional_data for labels (passed from scheduler)
            if not labels or not isinstance(labels, dict):
                additional_data = pod_result.get("additional_data", {})
                if isinstance(additional_data, dict):
                    labels = additional_data.get("labels", {})
            if not isinstance(labels, dict):
                labels = {}
            
            # Extract creation_time and startup_time from task result
            creation_time = pod_result.get("creation_time")
            startup_time = pod_result.get("startup_time")
            
            # Save pod information
            self.store.save_pod(
                pod_id=pod_id,
                pod_name=pod_id,  # Use pod_id as name if not specified
                namespace=namespace,
                hostname=hostname,
                ip_address=pod_ipv4,
                pause_container=pause_container,
                containers=containers,
                cni_network=cni,
                labels=labels,
                status="running",
                creation_time=creation_time,
                startup_time=startup_time
            )
            
            logger.info(
                f"Updated pod {pod_id} on host {hostname} in namespace {namespace} "
                f"(IP: {pod_ipv4}, containers: {len(containers)})"
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid input for pod update: {e}", exc_info=True)
            raise
        except RedisError as e:
            logger.error(f"Redis error updating pod: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error updating pod: {e}", exc_info=True)
            raise RedisError(
                message="Failed to update pod from task result",
                error_code="POD_UPDATE_ERROR",
                details={"hostname": hostname, "pod_result_keys": list(pod_result.keys())},
                cause=e
            ) from e
    
    @log_to_file(logger)
    def update_pod_from_list_result(
        self,
        pods_list: List[Dict[str, Any]],
        hostname: str,
        namespace: str
    ) -> None:
        """Update pod information from list_pods_by_namespace_task result.
        
        This should be called when list_pods_by_namespace_task completes.
        
        Args:
            pods_list: List of pod dictionaries from list_pods_by_namespace_task
            hostname: Host where pods are running (must be non-empty)
            namespace: Namespace name (must be non-empty)
            
        Raises:
            ValueError: If hostname or namespace is empty, or pods_list is invalid
            RedisError: If Redis operation fails
        """
        if not hostname or not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("hostname must be a non-empty string")
        
        if not namespace or not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        
        if not isinstance(pods_list, list):
            raise ValueError(f"pods_list must be a list, got {type(pods_list)}")
        
        updated_count = 0
        error_count = 0
        
        try:
            for pod_data in pods_list:
                if not isinstance(pod_data, dict):
                    logger.warning(f"Invalid pod_data type in list: {type(pod_data)}")
                    error_count += 1
                    continue
                
                pod_id = pod_data.get("pod_id")
                if not pod_id or not isinstance(pod_id, str):
                    logger.warning(f"Invalid or missing pod_id in pod_data: {pod_data}")
                    error_count += 1
                    continue
                
                pause = pod_data.get("pause", {})
                if not isinstance(pause, dict):
                    pause = {}
                
                apps = pod_data.get("apps", [])
                if not isinstance(apps, list):
                    apps = []
                
                # Build containers list
                containers: List[Dict[str, Any]] = []
                for app in apps:
                    if not isinstance(app, dict):
                        continue
                    container_info = {
                        "id": app.get("id"),
                        "name": app.get("name"),
                        "image": app.get("image"),
                        "pid": app.get("pid"),
                        "status": app.get("status", "unknown")
                    }
                    containers.append(container_info)
                
                # Extract creation_time and startup_time from pod_data if available
                creation_time = pod_data.get("creation_time")
                startup_time = pod_data.get("startup_time")
                
                # Preserve existing labels from Redis if pod already exists
                existing_pod = self.store.get_pod(pod_id)
                labels = {}
                if existing_pod and existing_pod.get("labels"):
                    labels = existing_pod.get("labels")
                    logger.debug(f"Preserving existing labels for pod {pod_id}: {labels}")
                
                # If no labels exist, try multiple strategies to find app_label
                if not labels or not labels.get("app"):
                    # Strategy 1: Check pod index for app_label (reverse lookup)
                    try:
                        app_index_pattern = "pod:index:app:*"
                        for app_index_key in self.store.redis_interface.redis_client.scan_iter(match=app_index_pattern):
                            if self.store.redis_interface.redis_client.sismember(app_index_key, pod_id):
                                app_label = app_index_key.split(":")[-1]
                                if app_label:
                                    labels = {
                                        "app": app_label,
                                        "app_label": app_label,
                                    }
                                    logger.info(f"Found app_label {app_label} for pod {pod_id} via pod index reverse lookup")
                                    break
                        if labels.get("app"):
                            pass  # Found via index, skip other strategies
                    except Exception as e:
                        logger.debug(f"Could not check pod index for app_label for pod {pod_id}: {e}")
                    
                    # Strategy 2: Try to get from deployment store based on namespace and container info
                    if not labels or not labels.get("app"):
                        try:
                            from utils.redis.deployment_store import DeploymentStore
                            deployment_store = DeploymentStore(self.store.redis_interface)
                            
                            # Get deployments in the namespace
                            deployments = deployment_store.get_deployments_by_namespace(namespace)
                            logger.debug(f"Found {len(deployments)} deployments in namespace {namespace} for pod {pod_id}")
                            
                            # If only one deployment in namespace, use it (simplest case)
                            if len(deployments) == 1:
                                deployment = deployments[0]
                                app_label = deployment.get("app_label")
                                if app_label:
                                    labels = {
                                        "app": app_label,
                                        "app_label": app_label,
                                    }
                                    logger.info(f"Found app_label {app_label} for pod {pod_id} from single deployment in namespace {namespace}")
                            elif len(deployments) > 1:
                                # Multiple deployments - need to match by container
                                # Try to match by container name or image
                                for deployment in deployments:
                                    deployment_spec = deployment.get("deployment_spec", {})
                                    containers_spec = deployment_spec.get("containers", [])
                                    logger.debug(f"Checking deployment {deployment.get('name')} with {len(containers_spec)} containers")
                                    
                                    # Try to match by container name or image
                                    for container in containers:
                                        if isinstance(container, dict):
                                            container_name = container.get("name")
                                            container_image = container.get("image")
                                            logger.debug(f"Pod container: name={container_name}, image={container_image}")
                                            
                                            for container_spec in containers_spec:
                                                if isinstance(container_spec, dict):
                                                    spec_name = container_spec.get("name")
                                                    spec_image = container_spec.get("image")
                                                    logger.debug(f"Deployment container spec: name={spec_name}, image={spec_image}")
                                                    
                                                    # Match by name or image (exact or partial)
                                                    name_match = container_name and spec_name and (container_name == spec_name or container_name in spec_name or spec_name in container_name)
                                                    image_match = container_image and spec_image and (container_image == spec_image or container_image in spec_image or spec_image in container_image)
                                                    
                                                    if name_match or image_match:
                                                        app_label = deployment.get("app_label")
                                                        if app_label:
                                                            labels = {
                                                                "app": app_label,
                                                                "app_label": app_label,
                                                            }
                                                            logger.info(f"Found app_label {app_label} for pod {pod_id} from deployment store by container match (name_match={name_match}, image_match={image_match})")
                                                            break
                                            if labels.get("app"):
                                                break
                                    if labels.get("app"):
                                        break
                        except Exception as e:
                            logger.warning(f"Could not get labels from deployment store for pod {pod_id}: {e}", exc_info=True)
                
                # Extract IP address if available (from etcd, network namespace, or pod_data)
                ip_address = pod_data.get("ip_address")
                
                # Log IP extraction for debugging
                if ip_address:
                    logger.info(f"Extracted IP {ip_address} for pod {pod_id} from pod_data")
                else:
                    logger.debug(f"No IP address found in pod_data for pod {pod_id}")
                
                # Save pod information
                try:
                    self.store.save_pod(
                        pod_id=pod_id,
                        namespace=namespace,
                        hostname=hostname,
                        ip_address=ip_address,
                        pause_container=pause,
                        containers=containers,
                        labels=labels,  # Include labels (preserved from existing pod or from deployment store)
                        status=pause.get("status", "unknown"),
                        creation_time=creation_time,
                        startup_time=startup_time
                    )
                    if ip_address:
                        logger.info(f"Saved IP {ip_address} to Redis for pod {pod_id}")
                    updated_count += 1
                except Exception as e:
                    logger.warning(f"Failed to save pod {pod_id}: {e}")
                    error_count += 1
                    continue
            
            logger.info(
                f"Updated {updated_count} pods from list result on host {hostname} "
                f"in namespace {namespace} (errors: {error_count}, total: {len(pods_list)})"
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid input for pod list update: {e}", exc_info=True)
            raise
        except RedisError as e:
            logger.error(f"Redis error updating pod list: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error updating pod list: {e}", exc_info=True)
            raise RedisError(
                message="Failed to update pods from list result",
                error_code="POD_LIST_UPDATE_ERROR",
                details={
                    "hostname": hostname,
                    "namespace": namespace,
                    "pods_count": len(pods_list)
                },
                cause=e
            ) from e
    
    @log_to_file(logger)
    def remove_pod(self, pod_id: str) -> None:
        """Remove pod when it's terminated.
        
        Args:
            pod_id: Pod identifier (must be non-empty)
            
        Raises:
            ValueError: If pod_id is empty or invalid
            RedisError: If Redis operation fails
        """
        if not pod_id or not isinstance(pod_id, str) or not pod_id.strip():
            raise ValueError("pod_id must be a non-empty string")
        
        try:
            self.store.delete_pod(pod_id)
            logger.info(f"Removed pod {pod_id}")
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid input removing pod {pod_id}: {e}", exc_info=True)
            raise
        except RedisError as e:
            logger.error(f"Redis error removing pod {pod_id}: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error removing pod {pod_id}: {e}", exc_info=True)
            raise RedisError(
                message=f"Failed to remove pod {pod_id}",
                error_code="POD_REMOVE_ERROR",
                details={"pod_id": pod_id},
                cause=e
            ) from e
    
    @log_to_file(logger)
    def mark_host_offline(self, hostname: str) -> None:
        """Mark host as offline.
        
        Args:
            hostname: Host identifier (must be non-empty)
            
        Raises:
            ValueError: If hostname is empty or invalid
            RedisError: If Redis operation fails
        """
        if not hostname or not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("hostname must be a non-empty string")
        
        try:
            self.store.save_host_info(
                hostname=hostname,
                status="offline"
            )
            logger.info(f"Marked host {hostname} as offline")
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid input marking host {hostname} offline: {e}", exc_info=True)
            raise
        except RedisError as e:
            logger.error(f"Redis error marking host {hostname} offline: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error marking host {hostname} offline: {e}", exc_info=True)
            raise RedisError(
                message=f"Failed to mark host {hostname} offline",
                error_code="HOST_OFFLINE_ERROR",
                details={"hostname": hostname},
                cause=e
            ) from e


# Convenience functions for direct use
@log_to_file(logger)
def update_host_from_system_info(
    redis_interface: RedisInterface,
    hostname: str,
    system_info: Dict[str, Any]
) -> None:
    """Convenience function to update host from system info.
    
    Args:
        redis_interface: RedisInterface instance
        hostname: Host identifier
        system_info: System information dictionary
    """
    integration = HostPodIntegration(redis_interface)
    integration.update_host_from_task_result(
        hostname=hostname,
        system_info=system_info
    )

@log_to_file(logger)
def update_host_from_usage(
    redis_interface: RedisInterface,
    hostname: str,
    usage_metrics: Dict[str, Any]
) -> None:
    """Convenience function to update host from usage metrics.
    
    Args:
        redis_interface: RedisInterface instance
        hostname: Host identifier
        usage_metrics: Usage metrics dictionary
    """
    integration = HostPodIntegration(redis_interface)
    integration.update_host_from_task_result(
        hostname=hostname,
        usage_metrics=usage_metrics
    )

@log_to_file(logger)
def update_host_from_ip(
    redis_interface: RedisInterface,
    hostname: str,
    ip_address: str
) -> None:
    """Convenience function to update host IP address.
    
    Args:
        redis_interface: RedisInterface instance
        hostname: Host identifier
        ip_address: IP address
    """
    integration = HostPodIntegration(redis_interface)
    integration.update_host_from_task_result(
        hostname=hostname,
        ip_address=ip_address
    )

@log_to_file(logger)
def update_pod_from_create_result(
    redis_interface: RedisInterface,
    pod_result: Dict[str, Any],
    hostname: str
) -> None:
    """Convenience function to update pod from create result.
    
    Args:
        redis_interface: RedisInterface instance
        pod_result: Pod creation result
        hostname: Host identifier
    """
    integration = HostPodIntegration(redis_interface)
    integration.update_pod_from_task_result(
        pod_result=pod_result,
        hostname=hostname
    )

