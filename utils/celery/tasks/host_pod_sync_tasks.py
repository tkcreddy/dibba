"""
Periodic tasks for syncing host and pod information to Redis queue.

This module provides tasks that run on worker nodes to collect host and pod
information and send it to a Redis queue for processing by a consumer service.
"""
import json
import subprocess
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from socket import gethostname
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.os.os_interface import get_system_info, get_system_usage, host_ip
from utils.containerd.containerd_interface import ContainerdClient, PodManager
from utils.ReadConfig import ReadConfig as rc
from utils.redis.redis_interface import RedisInterface

logger = LogKCld()

# Defaults
DEFAULT_CONTAINERD_SOCKET = "unix:///run/containerd/containerd.sock"
DEFAULT_NAMESPACE = "k8s.io"

# Redis queue name for host/pod information
INFO_QUEUE_NAME = "host_pod_info_queue"


@celery_app.task
@log_to_file(logger)
def collect_and_send_host_pod_info(
    containerd_socket: Optional[str] = None,
    namespace: Optional[str] = None
) -> Dict[str, Any]:
    """Collect host and pod information and send to Redis queue.
    
    This task runs on each worker node every 30 seconds to:
    1. Collect host system information (CPU, memory, IP)
    2. Collect pod information from containerd
    3. Package and send to Redis queue for processing
    
    Args:
        containerd_socket: Containerd socket path (optional)
        namespace: Containerd namespace (optional)
        
    Returns:
        Dictionary with collection status and message count
    """
    hostname = gethostname()
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    ns = namespace or DEFAULT_NAMESPACE
    
    try:
        # Initialize Redis interface
        redis_client = RedisInterface()
        
        # Collect host information
        host_info = _collect_host_info(hostname)
        
        # Collect pod information
        pod_info = _collect_pod_info(hostname, sock, ns)
        
        # Package the information
        info_package = {
            "hostname": hostname,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "host_info": host_info,
            "pod_info": pod_info,
            "metadata": {
                "containerd_socket": sock,
                "namespace": ns,
                "collection_version": "1.0"
            }
        }
        
        # Send to Redis queue
        message_count = _send_to_queue(redis_client, info_package)
        
        logger.info(
            f"Collected and sent host/pod info for {hostname} "
            f"(pods: {len(pod_info.get('pods', []))}, queue_size: {message_count})"
        )
        
        return {
            "status": "success",
            "hostname": hostname,
            "host_info_collected": host_info is not None,
            "pods_count": len(pod_info.get("pods", [])),
            "namespaces_count": len(pod_info.get("namespaces", [])),
            "queue_size": message_count,
            "timestamp": info_package["timestamp"]
        }
        
    except Exception as e:
        logger.error(
            f"Failed to collect and send host/pod info for {hostname}: {e}",
            exc_info=True
        )
        return {
            "status": "error",
            "hostname": hostname,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


@log_to_file(logger)
def _extract_ip_from_netns(pid: int, ifname: str = "eth0") -> Optional[str]:
    """Extract IPv4 address from network namespace using pause container PID.
    
    Args:
        pid: Process ID of the pause container
        ifname: Interface name (default: "eth0")
        
    Returns:
        IP address string or None if extraction fails
    """
    try:
        out = subprocess.check_output(
            ["nsenter", f"--target={pid}", "--net", "ip", "-j", "addr", "show", "dev", ifname],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2
        )
        data = json.loads(out)
        if data:
            for ifc in data:
                for addr in ifc.get("addr_info", []):
                    if addr.get("family") == "inet" and addr.get("local"):
                        return addr["local"]
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, ValueError) as e:
        logger.debug(f"Could not extract IP from network namespace (PID: {pid}): {e}")
    except Exception as e:
        logger.debug(f"Error extracting IP from network namespace (PID: {pid}): {e}")
    return None


@log_to_file(logger)
def _collect_host_info(hostname: str) -> Optional[Dict[str, Any]]:
    """Collect host system information.
    
    Args:
        hostname: Host identifier
        
    Returns:
        Dictionary with host information or None on error
    """
    try:
        # Get system information
        system_info = get_system_info()
        if not isinstance(system_info, dict):
            logger.warning(f"Invalid system_info type for {hostname}: {type(system_info)}")
            system_info = {}
        
        # Get usage metrics
        usage_metrics = get_system_usage()
        if not isinstance(usage_metrics, dict):
            logger.warning(f"Invalid usage_metrics type for {hostname}: {type(usage_metrics)}")
            usage_metrics = {}
        
        # Get IP address
        ip_address = host_ip()
        if not isinstance(ip_address, str):
            ip_address = None
        
        return {
            "hostname": hostname,
            "system_info": system_info,
            "usage_metrics": usage_metrics,
            "ip_address": ip_address,
            "collected_at": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to collect host info for {hostname}: {e}", exc_info=True)
        return None


@log_to_file(logger)
def _collect_pod_info(
    hostname: str,
    containerd_socket: str,
    namespace: str
) -> Dict[str, Any]:
    """Collect pod information from containerd.
    
    Args:
        hostname: Host identifier
        containerd_socket: Containerd socket path
        namespace: Default namespace to query
        
    Returns:
        Dictionary with pod information
    """
    pods_by_namespace: Dict[str, List[Dict[str, Any]]] = {}
    all_namespaces: List[str] = []
    
    try:
        client = ContainerdClient(socket=containerd_socket)
        pod_mgr = PodManager(client)
        
        # Get all namespaces
        try:
            all_namespaces = pod_mgr.runtime.list_all_namespaces()
        except Exception as e:
            logger.warning(f"Failed to list namespaces on {hostname}: {e}")
            all_namespaces = [namespace]  # Fallback to default namespace
        
        # Collect pods from each namespace
        for ns in all_namespaces:
            try:
                pod_summaries = pod_mgr.runtime.list_pods_and_apps_in_namespace(ns)
                if isinstance(pod_summaries, list):
                    # Enrich pods with IP addresses from network namespace
                    enriched_pods = []
                    for pod_summary in pod_summaries:
                        if isinstance(pod_summary, dict):
                            # Try to extract IP from pause container's network namespace
                            pause_info = pod_summary.get("pause", {})
                            pause_pid = pause_info.get("pid")
                            
                            if isinstance(pause_pid, int) and pause_pid > 0:
                                pod_ip = _extract_ip_from_netns(pause_pid)
                                if pod_ip:
                                    pod_summary["ip_address"] = pod_ip
                                    logger.debug(f"Extracted IP {pod_ip} for pod {pod_summary.get('pod_id')} from network namespace (PID: {pause_pid})")
                            
                            enriched_pods.append(pod_summary)
                        else:
                            enriched_pods.append(pod_summary)
                    
                    pods_by_namespace[ns] = enriched_pods
                else:
                    pods_by_namespace[ns] = []
            except Exception as e:
                logger.warning(f"Failed to list pods in namespace {ns} on {hostname}: {e}")
                pods_by_namespace[ns] = []
        
    except Exception as e:
        logger.error(f"Failed to collect pod info for {hostname}: {e}", exc_info=True)
        pods_by_namespace = {}
        all_namespaces = []
    
    return {
        "hostname": hostname,
        "namespaces": all_namespaces,
        "pods": pods_by_namespace,
        "collected_at": datetime.now(timezone.utc).isoformat()
    }


@log_to_file(logger)
def _send_to_queue(redis_client: RedisInterface, info_package: Dict[str, Any]) -> int:
    """Send information package to Redis queue.
    
    This function is COMPLETELY INDEPENDENT of the consumer - messages are queued
    in Redis and will be processed whenever the consumer is available.
    Producers can continue sending messages even if the consumer is down or restarted.
    
    The queue is persistent and independent:
    - Messages accumulate in Redis queue even if consumer is offline
    - Consumer processes messages when it comes back online
    - No producer-restart required when consumer restarts
    - No registration or handshake needed
    
    Args:
        redis_client: RedisInterface instance
        info_package: Information package to send
        
    Returns:
        Current queue size after adding message
    """
    try:
        # Serialize the package
        message = json.dumps(info_package)
        
        # Push to Redis list (queue) - producers are completely independent of consumer
        # Using LPUSH (left push) - consumer uses RPOP (right pop) for FIFO
        # This is non-blocking, asynchronous, and works regardless of consumer status
        queue_size = redis_client.redis_client.lpush(INFO_QUEUE_NAME, message)
        
        # IMPORTANT: Do NOT set expiration on the queue - allow it to persist indefinitely
        # This ensures producers can queue messages independently of consumer status
        # The queue will only grow if consumer is down, but messages will be processed when consumer restarts
        # We rely on Redis maxmemory policies or manual cleanup instead of expiration
        # This allows producers to be completely independent - no producer-restart needed when consumer restarts
        #
        # Note: If you need to prevent unbounded growth, set Redis maxmemory policy (e.g., allkeys-lru)
        # or implement a separate cleanup job that removes old messages (not the entire queue)
        #
        # DO NOT call expire() here - it would cause the queue to disappear if empty for a while
        # Queue persistence ensures producers can queue messages even when consumer is offline
        
        logger.debug(
            f"Queued message for host {info_package.get('hostname', 'unknown')} "
            f"(queue_size: {queue_size}, producers independent of consumer status)"
        )
        
        return queue_size
        
    except Exception as e:
        logger.error(f"Failed to send message to queue: {e}", exc_info=True)
        raise

