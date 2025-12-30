"""
Redis data model implementation for storing and querying host and pod information.

This module provides methods to:
- Store host information (IP, CPU, memory, system metrics)
- Store pod information (containers, namespaces, applications)
- Query by host, namespace, or application
"""
import json
from enum import Enum
from typing import Optional, Dict, Any, List, Set
from datetime import datetime, timezone
from logpkg.log_kcld import LogKCld, log_to_file
from utils.redis.redis_interface import RedisInterface
from utils.exceptions import RedisError
from utils.error_handlers import handle_errors

logger = LogKCld()


class HostStatus(str, Enum):
    """Host status enumeration."""
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class PodStatus(str, Enum):
    """Pod status enumeration."""
    RUNNING = "running"
    STOPPED = "stopped"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RedisKeyPatterns:
    """Constants for Redis key patterns."""
    HOST_DATA = "host:{hostname}"
    HOST_INDEX_IP = "host:index:ip"
    HOST_INDEX_ALL = "host:index:all"
    HOST_INDEX_NAMESPACE = "host:index:namespace:{namespace}"
    HOST_INDEX_APP = "host:index:app:{app_name}"
    HOST_PODS = "host:{hostname}:pods"
    
    POD_DATA = "pod:{pod_id}"
    POD_INDEX_ALL = "pod:index:all"
    POD_INDEX_HOST = "pod:index:host:{hostname}"
    POD_INDEX_NAMESPACE = "pod:index:namespace:{namespace}"
    POD_INDEX_APP = "pod:index:app:{app_name}"
    POD_INDEX_HOST_NAMESPACE = "pod:index:host:{hostname}:namespace:{namespace}"
    
    APP_DATA = "app:{app_name}"
    APP_INDEX_ALL = "app:index:all"
    APP_INDEX_NAMESPACE = "app:index:namespace:{namespace}"
    APP_INDEX_HOST = "app:index:host:{hostname}"


class HostPodStore:
    """Redis-based storage for host and pod information with efficient querying.
    
    This class provides a comprehensive interface for storing and querying
    host and pod information in Redis with automatic indexing for efficient
    lookups by host, namespace, or application.
    
    Attributes:
        HOST_TTL: Time-to-live for host data in seconds (default: 3600)
        POD_TTL: Time-to-live for pod data in seconds (default: 7200)
        APP_TTL: Time-to-live for application data in seconds (default: 86400)
    """
    
    # TTL values (in seconds)
    HOST_TTL: int = 3600  # 1 hour
    POD_TTL: int = 7200   # 2 hours
    APP_TTL: int = 86400  # 24 hours
    
    def __init__(self, redis_interface: RedisInterface) -> None:
        """Initialize HostPodStore with Redis interface.
        
        Args:
            redis_interface: RedisInterface instance for Redis operations
            
        Raises:
            TypeError: If redis_interface is not a RedisInterface instance
        """
        if not isinstance(redis_interface, RedisInterface):
            raise TypeError(
                f"redis_interface must be RedisInterface instance, got {type(redis_interface)}"
            )
        self.redis = redis_interface.redis_client
        self.redis_interface = redis_interface
    
    # ==================== Host Operations ====================
    
    @log_to_file(logger)
    @handle_errors("save_host_info", "REDIS_ERROR")
    def save_host_info(
        self,
        hostname: str,
        ip_address: Optional[str] = None,
        system_info: Optional[Dict[str, Any]] = None,
        usage_metrics: Optional[Dict[str, Any]] = None,
        status: str = HostStatus.ONLINE.value
    ) -> None:
        """Save or update host information.
        
        Args:
            hostname: Host identifier (must be non-empty)
            ip_address: IP address of the host
            system_info: System information from get_system_info()
            usage_metrics: Usage metrics from get_usage()
            status: Host status (default: "online")
            
        Raises:
            ValueError: If hostname is empty or invalid
        """
        if not hostname or not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("hostname must be a non-empty string")
        
        host_key = RedisKeyPatterns.HOST_DATA.format(hostname=hostname)
        
        # Get existing data or create new
        existing_data = self.redis.hget(host_key, "data")
        if existing_data:
            host_data = json.loads(existing_data)
        else:
            host_data = {
                "hostname": hostname,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        
        # Update fields
        if ip_address:
            if not isinstance(ip_address, str) or not ip_address.strip():
                logger.warning(f"Invalid IP address for host {hostname}: {ip_address}")
            else:
                host_data["ip_address"] = ip_address
                # Update IP index
                self.redis.hset(RedisKeyPatterns.HOST_INDEX_IP, ip_address, hostname)
        
        if system_info:
            if isinstance(system_info, dict):
                host_data["system_info"] = system_info
            else:
                logger.warning(f"Invalid system_info type for host {hostname}: {type(system_info)}")
        
        if usage_metrics:
            if isinstance(usage_metrics, dict):
                host_data["usage_metrics"] = usage_metrics
            else:
                logger.warning(f"Invalid usage_metrics type for host {hostname}: {type(usage_metrics)}")
        
        host_data["status"] = status
        host_data["last_updated"] = datetime.now(timezone.utc).isoformat()
        
        # Save to Redis using pipeline for atomicity
        pipe = self.redis.pipeline()
        pipe.hset(host_key, "data", json.dumps(host_data))
        pipe.expire(host_key, self.HOST_TTL)
        pipe.sadd(RedisKeyPatterns.HOST_INDEX_ALL, hostname)
        pipe.execute()
        
        logger.info(f"Saved host info for {hostname} (IP: {ip_address}, Status: {status})")
    
    @log_to_file(logger)
    @handle_errors("get_host", "REDIS_ERROR")
    def get_host(self, hostname: str) -> Optional[Dict[str, Any]]:
        """Get host information by hostname.
        
        Args:
            hostname: Host identifier
            
        Returns:
            Host data dictionary or None if not found
            
        Raises:
            ValueError: If hostname is empty or invalid
        """
        if not hostname or not isinstance(hostname, str):
            raise ValueError("hostname must be a non-empty string")
        
        host_key = RedisKeyPatterns.HOST_DATA.format(hostname=hostname)
        data = self.redis.hget(host_key, "data")
        
        if not data:
            return None
        
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse host data for {hostname}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    @handle_errors("get_host_by_ip", "REDIS_ERROR")
    def get_host_by_ip(self, ip_address: str) -> Optional[Dict[str, Any]]:
        """Get host information by IP address.
        
        Args:
            ip_address: IP address
            
        Returns:
            Host data dictionary or None if not found
            
        Raises:
            ValueError: If ip_address is empty or invalid
        """
        if not ip_address or not isinstance(ip_address, str):
            raise ValueError("ip_address must be a non-empty string")
        
        hostname = self.redis.hget(RedisKeyPatterns.HOST_INDEX_IP, ip_address)
        if hostname:
            return self.get_host(hostname)
        return None
    
    @log_to_file(logger)
    @handle_errors("get_all_hosts", "REDIS_ERROR")
    def get_all_hosts(self) -> List[Dict[str, Any]]:
        """Get all hosts.
        
        Returns:
            List of host data dictionaries, empty list if none found
        """
        hostnames = self.redis.smembers(RedisKeyPatterns.HOST_INDEX_ALL)
        hosts: List[Dict[str, Any]] = []
        
        for hostname in hostnames:
            try:
                host_data = self.get_host(hostname)
                if host_data:
                    hosts.append(host_data)
            except Exception as e:
                logger.warning(f"Failed to get host {hostname}: {e}")
                continue
        
        return hosts
    
    @log_to_file(logger)
    @handle_errors("delete_host", "REDIS_ERROR")
    def delete_host(self, hostname: str) -> None:
        """Delete host and all associated indexes.
        
        Args:
            hostname: Host identifier
        """
        host_data = self.get_host(hostname)
        
        # Use pipeline for atomic deletion
        pipe = self.redis.pipeline()
        
        if host_data:
            # Remove from indexes
            ip_address = host_data.get("ip_address")
            if ip_address:
                pipe.hdel(RedisKeyPatterns.HOST_INDEX_IP, ip_address)
            
            pipe.srem(RedisKeyPatterns.HOST_INDEX_ALL, hostname)
            
            # Remove from namespace indexes
            for namespace_key in self.redis.scan_iter(match="host:index:namespace:*"):
                pipe.srem(namespace_key, hostname)
            
            # Remove from app indexes
            for app_index_key in self.redis.scan_iter(match="host:index:app:*"):
                pipe.srem(app_index_key, hostname)
        
        # Delete host data and pod list
        host_key = RedisKeyPatterns.HOST_DATA.format(hostname=hostname)
        host_pods_key = RedisKeyPatterns.HOST_PODS.format(hostname=hostname)
        pipe.delete(host_key, host_pods_key)
        pipe.execute()
        
        logger.info(f"Deleted host {hostname} and all associated indexes")
    
    # ==================== Pod Operations ====================
    
    @log_to_file(logger)
    @handle_errors("save_pod", "REDIS_ERROR")
    def save_pod(
        self,
        pod_id: str,
        pod_name: Optional[str] = None,
        namespace: str = "default",
        hostname: str = "",
        ip_address: Optional[str] = None,
        pause_container: Optional[Dict[str, Any]] = None,
        containers: Optional[List[Dict[str, Any]]] = None,
        cni_network: Optional[Dict[str, Any]] = None,
        resources: Optional[Dict[str, Any]] = None,
        labels: Optional[Dict[str, str]] = None,
        status: str = "running",
        creation_time: Optional[str] = None,
        startup_time: Optional[str] = None
    ) -> None:
        """Save or update pod information.
        
        Args:
            pod_id: Unique pod identifier
            pod_name: Pod name
            namespace: Namespace name
            hostname: Host where pod is running
            ip_address: Pod IP address
            pause_container: Pause container information
            containers: List of container information
            cni_network: CNI network configuration
            resources: Resource specifications
            labels: Pod labels (used to extract application name)
            status: Pod status
            creation_time: ISO format timestamp when pod was created (optional)
            startup_time: ISO format timestamp when pod became running (optional)
        """
        if not pod_id or not isinstance(pod_id, str) or not pod_id.strip():
            raise ValueError("pod_id must be a non-empty string")
        
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        
        pod_key = RedisKeyPatterns.POD_DATA.format(pod_id=pod_id)
        
        # Get existing data or create new
        existing_data = self.redis.hget(pod_key, "data")
        if existing_data:
            try:
                pod_data = json.loads(existing_data)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse existing pod data for {pod_id}: {e}")
                pod_data = {
                    "pod_id": pod_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
        else:
            pod_data = {
                "pod_id": pod_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        
        # Set creation_time if provided, otherwise preserve existing or use created_at
        if creation_time:
            pod_data["creation_time"] = creation_time
        elif "creation_time" not in pod_data:
            # Use created_at as fallback for creation_time if not set
            pod_data["creation_time"] = pod_data.get("created_at", datetime.now(timezone.utc).isoformat())
        
        # Set startup_time if provided and pod is running, or preserve existing
        if startup_time:
            pod_data["startup_time"] = startup_time
        elif status == "running" and "startup_time" not in pod_data:
            # If pod is running and startup_time not set, set it now
            pod_data["startup_time"] = datetime.now(timezone.utc).isoformat()
        # If status is not running, don't update startup_time (preserve existing)
        
        # Update fields
        if pod_name:
            pod_data["pod_name"] = pod_name
        if namespace:
            pod_data["namespace"] = namespace
        if hostname:
            pod_data["hostname"] = hostname
        if ip_address:
            pod_data["ip_address"] = ip_address
            logger.info(f"Saving IP {ip_address} for pod {pod_id} to Redis")
        else:
            logger.debug(f"No IP address provided for pod {pod_id}")
        if pause_container:
            pod_data["pause_container"] = pause_container
        if containers:
            pod_data["containers"] = containers
        if cni_network:
            pod_data["cni_network"] = cni_network
        if resources:
            pod_data["resources"] = resources
        if labels:
            pod_data["labels"] = labels
        
        pod_data["status"] = status
        pod_data["last_updated"] = datetime.now(timezone.utc).isoformat()
        
        # Save to Redis using pipeline for atomicity
        pipe = self.redis.pipeline()
        pipe.hset(pod_key, "data", json.dumps(pod_data))
        pipe.expire(pod_key, self.POD_TTL)
        pipe.sadd(RedisKeyPatterns.POD_INDEX_ALL, pod_id)
        
        if hostname:
            pipe.sadd(RedisKeyPatterns.POD_INDEX_HOST.format(hostname=hostname), pod_id)
            pipe.sadd(RedisKeyPatterns.HOST_PODS.format(hostname=hostname), pod_id)
        
        if namespace:
            pipe.sadd(RedisKeyPatterns.POD_INDEX_NAMESPACE.format(namespace=namespace), pod_id)
            if hostname:
                pipe.sadd(
                    RedisKeyPatterns.POD_INDEX_HOST_NAMESPACE.format(
                        hostname=hostname,
                        namespace=namespace
                    ),
                    pod_id
                )
        
        # Extract application name from labels and update app index
        app_name: Optional[str] = None
        if labels and isinstance(labels, dict):
            app_name = labels.get("app") or labels.get("application")
            if app_name and isinstance(app_name, str):
                pipe.sadd(RedisKeyPatterns.POD_INDEX_APP.format(app_name=app_name), pod_id)
        
        pipe.execute()
        
        # Update application metadata (separate operation to avoid pipeline complexity)
        if app_name and hostname:
            self._update_application_metadata(app_name, namespace, hostname, pod_id)
        
        logger.info(
            f"Saved pod {pod_id} on host {hostname} in namespace {namespace} "
            f"(app: {app_name}, status: {status})"
        )
    
    @log_to_file(logger)
    @handle_errors("get_pod", "REDIS_ERROR")
    def get_pod(self, pod_id: str) -> Optional[Dict[str, Any]]:
        """Get pod information by pod ID.
        
        Args:
            pod_id: Pod identifier
            
        Returns:
            Pod data dictionary or None if not found
            
        Raises:
            ValueError: If pod_id is empty or invalid
        """
        if not pod_id or not isinstance(pod_id, str):
            raise ValueError("pod_id must be a non-empty string")
        
        pod_key = RedisKeyPatterns.POD_DATA.format(pod_id=pod_id)
        data = self.redis.hget(pod_key, "data")
        
        if not data:
            return None
        
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse pod data for {pod_id}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    @handle_errors("get_pods_by_host", "REDIS_ERROR")
    def get_pods_by_host(self, hostname: str) -> List[Dict[str, Any]]:
        """Get all pods on a specific host.
        
        Args:
            hostname: Host identifier
            
        Returns:
            List of pod data dictionaries, empty list if none found
            
        Raises:
            ValueError: If hostname is empty or invalid
        """
        if not hostname or not isinstance(hostname, str):
            raise ValueError("hostname must be a non-empty string")
        
        pod_ids = self.redis.smembers(RedisKeyPatterns.POD_INDEX_HOST.format(hostname=hostname))
        pods: List[Dict[str, Any]] = []
        
        for pod_id in pod_ids:
            try:
                pod_data = self.get_pod(pod_id)
                if pod_data:
                    pods.append(pod_data)
            except Exception as e:
                logger.warning(f"Failed to get pod {pod_id} for host {hostname}: {e}")
                continue
        
        return pods
    
    @log_to_file(logger)
    @handle_errors("get_pods_by_namespace", "REDIS_ERROR")
    def get_pods_by_namespace(self, namespace: str) -> List[Dict[str, Any]]:
        """Get all pods in a namespace.
        
        Args:
            namespace: Namespace name
            
        Returns:
            List of pod data dictionaries, empty list if none found
            
        Raises:
            ValueError: If namespace is empty or invalid
        """
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        
        pod_ids = self.redis.smembers(
            RedisKeyPatterns.POD_INDEX_NAMESPACE.format(namespace=namespace)
        )
        pods: List[Dict[str, Any]] = []
        
        for pod_id in pod_ids:
            try:
                pod_data = self.get_pod(pod_id)
                if pod_data:
                    pods.append(pod_data)
            except Exception as e:
                logger.warning(f"Failed to get pod {pod_id} in namespace {namespace}: {e}")
                continue
        
        return pods
    
    @log_to_file(logger)
    @handle_errors("get_pods_by_application", "REDIS_ERROR")
    def get_pods_by_application(self, app_name: str) -> List[Dict[str, Any]]:
        """Get all pods for an application.
        
        Args:
            app_name: Application name
            
        Returns:
            List of pod data dictionaries
        """
        pod_ids = self.redis.smembers(f"pod:index:app:{app_name}")
        pods = []
        for pod_id in pod_ids:
            pod_data = self.get_pod(pod_id)
            if pod_data:
                pods.append(pod_data)
        return pods
    
    @log_to_file(logger)
    @handle_errors("get_pods_by_host_and_namespace", "REDIS_ERROR")
    def get_pods_by_host_and_namespace(
        self,
        hostname: str,
        namespace: str
    ) -> List[Dict[str, Any]]:
        """Get pods on a host in a specific namespace.
        
        Args:
            hostname: Host identifier
            namespace: Namespace name
            
        Returns:
            List of pod data dictionaries, empty list if none found
            
        Raises:
            ValueError: If hostname or namespace is empty or invalid
        """
        if not hostname or not isinstance(hostname, str):
            raise ValueError("hostname must be a non-empty string")
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        
        pod_ids = self.redis.smembers(
            RedisKeyPatterns.POD_INDEX_HOST_NAMESPACE.format(
                hostname=hostname,
                namespace=namespace
            )
        )
        pods: List[Dict[str, Any]] = []
        
        for pod_id in pod_ids:
            try:
                pod_data = self.get_pod(pod_id)
                if pod_data:
                    pods.append(pod_data)
            except Exception as e:
                logger.warning(
                    f"Failed to get pod {pod_id} on host {hostname} "
                    f"in namespace {namespace}: {e}"
                )
                continue
        
        return pods
    
    @log_to_file(logger)
    @handle_errors("delete_pod", "REDIS_ERROR")
    def delete_pod(self, pod_id: str) -> None:
        """Delete pod and all associated indexes.
        
        Args:
            pod_id: Pod identifier
        """
        pod_data = self.get_pod(pod_id)
        if pod_data:
            hostname = pod_data.get("hostname")
            namespace = pod_data.get("namespace")
            labels = pod_data.get("labels", {})
            app_name = labels.get("app") or labels.get("application")
            
            # Use pipeline for atomic deletion
            pipe = self.redis.pipeline()
            pipe.srem(RedisKeyPatterns.POD_INDEX_ALL, pod_id)
            
            if hostname:
                pipe.srem(RedisKeyPatterns.POD_INDEX_HOST.format(hostname=hostname), pod_id)
                pipe.srem(RedisKeyPatterns.HOST_PODS.format(hostname=hostname), pod_id)
            
            if namespace:
                pipe.srem(
                    RedisKeyPatterns.POD_INDEX_NAMESPACE.format(namespace=namespace),
                    pod_id
                )
                if hostname:
                    pipe.srem(
                        RedisKeyPatterns.POD_INDEX_HOST_NAMESPACE.format(
                            hostname=hostname,
                            namespace=namespace
                        ),
                        pod_id
                    )
            
            if app_name:
                pipe.srem(RedisKeyPatterns.POD_INDEX_APP.format(app_name=app_name), pod_id)
            
            pod_key = RedisKeyPatterns.POD_DATA.format(pod_id=pod_id)
            pipe.delete(pod_key)
            pipe.execute()
            
            # Remove from application metadata (separate operation)
            if app_name:
                self._remove_pod_from_application(app_name, pod_id, hostname)
        else:
            # Delete pod data even if metadata is missing
            pod_key = RedisKeyPatterns.POD_DATA.format(pod_id=pod_id)
            self.redis.delete(pod_key)
        
        logger.info(f"Deleted pod {pod_id} and all associated indexes")
    
    # ==================== Application Operations ====================
    
    @log_to_file(logger)
    def _update_application_metadata(
        self,
        app_name: str,
        namespace: str,
        hostname: str,
        pod_id: str
    ) -> None:
        """Update application metadata when pod is added.
        
        Args:
            app_name: Application name
            namespace: Namespace name
            hostname: Host identifier
            pod_id: Pod identifier
        """
        if not app_name or not isinstance(app_name, str):
            logger.warning(f"Invalid app_name: {app_name}")
            return
        
        app_key = RedisKeyPatterns.APP_DATA.format(app_name=app_name)
        
        # Get existing data or create new
        existing_data = self.redis.hget(app_key, "data")
        if existing_data:
            try:
                app_data = json.loads(existing_data)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse existing app data for {app_name}: {e}")
                app_data = {
                    "name": app_name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "pods": [],
                    "hosts": [],
                }
        else:
            app_data = {
                "name": app_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "pods": [],
                "hosts": [],
            }
        
        # Update pods list
        if pod_id not in app_data.get("pods", []):
            app_data.setdefault("pods", []).append(pod_id)
        
        # Update hosts list
        if hostname and hostname not in app_data.get("hosts", []):
            app_data.setdefault("hosts", []).append(hostname)
        
        app_data["namespace"] = namespace
        app_data["last_updated"] = datetime.now(timezone.utc).isoformat()
        
        # Count containers from all pods
        pod_ids = app_data.get("pods", [])
        total_containers = 0
        for pid in pod_ids:
            pod = self.get_pod(pid)
            if pod:
                containers = pod.get("containers", [])
                total_containers += len(containers)
        
        app_data["total_containers"] = total_containers
        app_data["status"] = "running"  # Could be computed from pod statuses
        
        # Save to Redis using pipeline
        pipe = self.redis.pipeline()
        pipe.hset(app_key, "data", json.dumps(app_data))
        pipe.expire(app_key, self.APP_TTL)
        pipe.sadd(RedisKeyPatterns.APP_INDEX_ALL, app_name)
        
        if namespace:
            pipe.sadd(
                RedisKeyPatterns.APP_INDEX_NAMESPACE.format(namespace=namespace),
                app_name
            )
        if hostname:
            pipe.sadd(RedisKeyPatterns.APP_INDEX_HOST.format(hostname=hostname), app_name)
        
        pipe.execute()
    
    @log_to_file(logger)
    def _remove_pod_from_application(
        self,
        app_name: str,
        pod_id: str,
        hostname: Optional[str] = None
    ) -> None:
        """Remove pod from application metadata.
        
        Args:
            app_name: Application name
            pod_id: Pod identifier
            hostname: Host identifier (optional)
        """
        if not app_name or not isinstance(app_name, str):
            logger.warning(f"Invalid app_name: {app_name}")
            return
        
        app_key = RedisKeyPatterns.APP_DATA.format(app_name=app_name)
        app_data_str = self.redis.hget(app_key, "data")
        
        if app_data_str:
            try:
                app_data = json.loads(app_data_str)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse app data for {app_name}: {e}", exc_info=True)
                return
            
            # Remove pod from list
            if pod_id in app_data.get("pods", []):
                app_data["pods"].remove(pod_id)
            
            # Remove host if no pods left on it
            if hostname and hostname in app_data.get("hosts", []):
                # Check if any other pods on this host
                pods_on_host = [
                    pid for pid in app_data.get("pods", [])
                    if self.get_pod(pid) and self.get_pod(pid).get("hostname") == hostname
                ]
                if not pods_on_host:
                    app_data["hosts"].remove(hostname)
            
            # Update container count
            total_containers = 0
            for pid in app_data.get("pods", []):
                pod = self.get_pod(pid)
                if pod:
                    containers = pod.get("containers", [])
                    total_containers += len(containers)
            
            app_data["total_containers"] = total_containers
            app_data["last_updated"] = datetime.now(timezone.utc).isoformat()
            
            # Save updated data
            self.redis.hset(app_key, "data", json.dumps(app_data))
    
    @log_to_file(logger)
    @handle_errors("get_application", "REDIS_ERROR")
    def get_application(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Get application information.
        
        Args:
            app_name: Application name
            
        Returns:
            Application data dictionary or None if not found
            
        Raises:
            ValueError: If app_name is empty or invalid
        """
        if not app_name or not isinstance(app_name, str):
            raise ValueError("app_name must be a non-empty string")
        
        app_key = RedisKeyPatterns.APP_DATA.format(app_name=app_name)
        data = self.redis.hget(app_key, "data")
        
        if not data:
            return None
        
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse app data for {app_name}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    @handle_errors("get_applications_by_namespace", "REDIS_ERROR")
    def get_applications_by_namespace(self, namespace: str) -> List[Dict[str, Any]]:
        """Get all applications in a namespace.
        
        Args:
            namespace: Namespace name
            
        Returns:
            List of application data dictionaries, empty list if none found
            
        Raises:
            ValueError: If namespace is empty or invalid
        """
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        
        app_names = self.redis.smembers(
            RedisKeyPatterns.APP_INDEX_NAMESPACE.format(namespace=namespace)
        )
        apps: List[Dict[str, Any]] = []
        
        for app_name in app_names:
            try:
                app_data = self.get_application(app_name)
                if app_data:
                    apps.append(app_data)
            except Exception as e:
                logger.warning(f"Failed to get app {app_name} in namespace {namespace}: {e}")
                continue
        
        return apps
    
    @log_to_file(logger)
    @handle_errors("get_applications_by_host", "REDIS_ERROR")
    def get_applications_by_host(self, hostname: str) -> List[Dict[str, Any]]:
        """Get all applications on a host.
        
        Args:
            hostname: Host identifier
            
        Returns:
            List of application data dictionaries, empty list if none found
            
        Raises:
            ValueError: If hostname is empty or invalid
        """
        if not hostname or not isinstance(hostname, str):
            raise ValueError("hostname must be a non-empty string")
        
        app_names = self.redis.smembers(
            RedisKeyPatterns.APP_INDEX_HOST.format(hostname=hostname)
        )
        apps: List[Dict[str, Any]] = []
        
        for app_name in app_names:
            try:
                app_data = self.get_application(app_name)
                if app_data:
                    apps.append(app_data)
            except Exception as e:
                logger.warning(f"Failed to get app {app_name} on host {hostname}: {e}")
                continue
        
        return apps
    
    @log_to_file(logger)
    @handle_errors("get_hosts_by_namespace", "REDIS_ERROR")
    def get_hosts_by_namespace(self, namespace: str) -> List[Dict[str, Any]]:
        """Get all hosts in a namespace (hosts that have pods in the namespace).
        
        Args:
            namespace: Namespace name
            
        Returns:
            List of host data dictionaries, empty list if none found
            
        Raises:
            ValueError: If namespace is empty or invalid
        """
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        
        pod_ids = self.redis.smembers(
            RedisKeyPatterns.POD_INDEX_NAMESPACE.format(namespace=namespace)
        )
        hostnames: Set[str] = set()
        
        for pod_id in pod_ids:
            try:
                pod_data = self.get_pod(pod_id)
                if pod_data:
                    hostname = pod_data.get("hostname")
                    if hostname and isinstance(hostname, str):
                        hostnames.add(hostname)
            except Exception as e:
                logger.warning(f"Failed to get pod {pod_id} for namespace {namespace}: {e}")
                continue
        
        hosts: List[Dict[str, Any]] = []
        for hostname in hostnames:
            try:
                host_data = self.get_host(hostname)
                if host_data:
                    hosts.append(host_data)
            except Exception as e:
                logger.warning(f"Failed to get host {hostname} for namespace {namespace}: {e}")
                continue
        
        return hosts
    
    @log_to_file(logger)
    @handle_errors("get_hosts_by_application", "REDIS_ERROR")
    def get_hosts_by_application(self, app_name: str) -> List[Dict[str, Any]]:
        """Get all hosts running an application.
        
        Args:
            app_name: Application name
            
        Returns:
            List of host data dictionaries, empty list if none found
            
        Raises:
            ValueError: If app_name is empty or invalid
        """
        if not app_name or not isinstance(app_name, str):
            raise ValueError("app_name must be a non-empty string")
        
        app_data = self.get_application(app_name)
        if not app_data:
            return []
        
        hosts: List[Dict[str, Any]] = []
        for hostname in app_data.get("hosts", []):
            if not isinstance(hostname, str):
                continue
            try:
                host_data = self.get_host(hostname)
                if host_data:
                    hosts.append(host_data)
            except Exception as e:
                logger.warning(f"Failed to get host {hostname} for app {app_name}: {e}")
                continue
        
        return hosts
    
    # ==================== Complex Queries ====================
    
    @log_to_file(logger)
    @handle_errors("get_host_with_pods_and_apps", "REDIS_ERROR")
    def get_host_with_pods_and_apps(self, hostname: str) -> Dict[str, Any]:
        """Get host information with all pods and applications.
        
        Args:
            hostname: Host identifier
            
        Returns:
            Dictionary with host info, pods, and applications.
            Returns empty dict if host not found.
            
        Raises:
            ValueError: If hostname is empty or invalid
        """
        if not hostname or not isinstance(hostname, str):
            raise ValueError("hostname must be a non-empty string")
        
        host_data = self.get_host(hostname)
        if not host_data:
            logger.debug(f"Host {hostname} not found")
            return {}
        
        pods = self.get_pods_by_host(hostname)
        apps = self.get_applications_by_host(hostname)
        
        return {
            "host": host_data,
            "pods": pods,
            "applications": apps,
            "pod_count": len(pods),
            "application_count": len(apps)
        }
    
    @log_to_file(logger)
    @handle_errors("get_namespace_summary", "REDIS_ERROR")
    def get_namespace_summary(self, namespace: str) -> Dict[str, Any]:
        """Get comprehensive namespace summary.
        
        Args:
            namespace: Namespace name
            
        Returns:
            Dictionary with namespace summary including hosts, pods, and apps.
            Returns empty dict with counts if namespace not found.
            
        Raises:
            ValueError: If namespace is empty or invalid
        """
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        
        pods = self.get_pods_by_namespace(namespace)
        hosts = self.get_hosts_by_namespace(namespace)
        apps = self.get_applications_by_namespace(namespace)
        
        # Calculate totals
        total_containers = sum(
            len(pod.get("containers", []))
            for pod in pods
            if isinstance(pod.get("containers"), list)
        )
        
        return {
            "namespace": namespace,
            "hosts": hosts,
            "pods": pods,
            "applications": apps,
            "host_count": len(hosts),
            "pod_count": len(pods),
            "application_count": len(apps),
            "total_containers": total_containers
        }

