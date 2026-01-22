"""
TCP Health Check tasks for Dibba pods.

This module provides Celery tasks to:
- Perform TCP 3-way handshake health checks for pods with HTTP health checks
- Check TCP connectivity every 3 seconds independent of periodSeconds configuration
- Distribute work across multiple health check workers

Helper functions are in utils.healthcheck.tcp_health_check
"""
from typing import Dict, Any, List, Optional
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.celery.async_task_base import AsyncTask
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from utils.redis.deployment_store import DeploymentStore
from utils.celery.tasks.health_check_tasks import (
    get_health_check_workers,
    assign_pod_to_worker
)
from utils.healthcheck.tcp_health_check import (
    TCPHealthCheckBase,
    TCPHealthCheckResult,
    TCPHealthCheckSummary
)
from socket import gethostname
from datetime import datetime, timezone
import time

logger = LogKCld()


def _pod_has_http_health_checks(
    pod: Dict[str, Any],
    deployment_store: DeploymentStore
) -> bool:
    """
    Check if a pod has HTTP health check configurations.
    
    Args:
        pod: Pod dictionary
        deployment_store: DeploymentStore instance
        
    Returns:
        True if pod has HTTP health checks configured, False otherwise
    """
    try:
        namespace = pod.get('namespace')
        if not namespace:
            return False
        
        # Get deployment to find health check configuration
        deployment_name = pod.get('labels', {}).get('deployment_name') or pod.get('deployment_name')
        deployment = None
        
        if deployment_name:
            deployment = deployment_store.get_deployment(deployment_name, namespace)
        else:
            app_label = pod.get('labels', {}).get('app') or pod.get('labels', {}).get('app_label')
            if app_label:
                deployments_by_app = deployment_store.get_deployments_by_app(app_label)
                for dep in deployments_by_app:
                    if dep.get('namespace') == namespace:
                        deployment = dep
                        break
                if not deployment and deployments_by_app:
                    deployment = deployments_by_app[0]
        
        if not deployment:
            return False
        
        # Check if deployment has health check configurations
        deployment_spec = deployment.get('deployment_spec', {})
        health_checks = deployment_spec.get('health_checks', {})
        
        if not health_checks:
            return False
        
        # Check if any container has HTTP readiness or liveness probes
        containers = pod.get('containers', [])
        for container in containers:
            container_name = container.get('name')
            if not container_name:
                continue
            
            if container_name in health_checks:
                container_health_checks = health_checks[container_name]
                readiness_probe = container_health_checks.get('readinessProbe', {})
                liveness_probe = container_health_checks.get('livenessProbe', {})
                
                # Check if either probe uses HTTP
                if readiness_probe.get('httpGet') or liveness_probe.get('httpGet'):
                    return True
        
        return False
    except Exception as e:
        logger.warning(f"Error checking HTTP health checks for pod {pod.get('pod_id', 'unknown')}: {e}")
        return False


def _get_pods_with_http_health_checks() -> List[Dict[str, Any]]:
    """
    Get all running pods that have HTTP health checks configured.
    
    Returns:
        List of pod dictionaries with HTTP health checks
    """
    try:
        redis_interface = RedisInterface()
        host_pod_store = HostPodStore(redis_interface)
        deployment_store = DeploymentStore(redis_interface)
        
        pods_with_http_health_checks = []
        all_hosts = host_pod_store.get_all_hosts()
        
        for host in all_hosts:
            hostname = host.get('hostname')
            if not hostname:
                continue
            
            host_pods = host_pod_store.get_pods_by_host(hostname)
            for pod in host_pods:
                pod_status = pod.get('status', '').upper()
                if pod_status == 'RUNNING':
                    if _pod_has_http_health_checks(pod, deployment_store):
                        pods_with_http_health_checks.append(pod)
        
        return pods_with_http_health_checks
    except Exception as e:
        logger.error(f"Error getting pods with HTTP health checks: {e}", exc_info=True)
        return []


def _extract_endpoints_from_pods(pods: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Extract IP:port endpoints from pods.
    
    Args:
        pods: List of pod dictionaries
        
    Returns:
        Dictionary mapping IP addresses to port numbers
    """
    endpoints: Dict[str, int] = {}
    
    for pod in pods:
        ip_address = pod.get('ip_address')
        if not ip_address:
            continue
        
        # Strip CIDR notation if present
        if '/' in ip_address:
            ip_address = ip_address.split('/')[0]
        
        # Try to get port from pod's port information
        # Check containers for port information
        containers = pod.get('containers', [])
        for container in containers:
            # Look for port mappings or default to common HTTP port
            container_ports = container.get('ports', [])
            if container_ports:
                # If ports list, take first port
                if isinstance(container_ports, list) and container_ports:
                    port_info = container_ports[0]
                    if isinstance(port_info, dict):
                        port = port_info.get('containerPort') or port_info.get('port')
                    elif isinstance(port_info, (int, str)):
                        try:
                            port = int(port_info)
                        except (ValueError, TypeError):
                            continue
                    else:
                        continue
                else:
                    continue
            else:
                # Default to 8080 for HTTP health checks if no port specified
                port = 8080
            
            if port:
                endpoints[ip_address] = port
                break
        
        # If no port found in containers, default to 8080
        if ip_address not in endpoints:
            endpoints[ip_address] = 8080
    
    return endpoints


@celery_app.task(base=AsyncTask, name="tcp.check_http_pods_tcp_health")
@log_to_file(logger)
async def check_http_pods_tcp_health_task() -> Dict[str, Any]:
    """
    Periodic task that performs TCP 3-way handshake health checks every 3 seconds
    for all pods with HTTP health checks, independent of periodSeconds configuration.
    
    This task:
    - Finds all running pods with HTTP health checks
    - Distributes pods across multiple health check workers (same logic as HTTP health checks)
    - Only processes pods assigned to the current worker using consistent hashing
    - Extracts their IP:port endpoints
    - Performs TCP health checks using TCPHealthCheckBase
    - Logs results and failures
    - Runs every 3 seconds regardless of HTTP probe periodSeconds
    
    WORKER DISTRIBUTION:
    - Uses consistent hashing to assign pods to workers
    - Each worker only processes pods assigned to it
    - Same pod always goes to the same worker
    - Work is distributed evenly across all workers
    
    Returns:
        Dictionary with task execution summary
    """
    try:
        task_start_time = time.time()
        task_start_datetime = datetime.now(timezone.utc)
        logger.info("Starting TCP health check task for pods with HTTP health checks")
        
        redis_interface = RedisInterface()
        host_pod_store = HostPodStore(redis_interface)
        deployment_store = DeploymentStore(redis_interface)
        
        # Get all pods with HTTP health checks
        get_pods_start = time.time()
        all_pods = _get_pods_with_http_health_checks()
        get_pods_time = time.time() - get_pods_start
        logger.info(f"[TIMING] get_pods_with_http_health_checks: {get_pods_time:.3f}s ({len(all_pods)} pods)")
        
        if not all_pods:
            return {
                "status": "success",
                "pods_checked": 0,
                "endpoints_checked": 0,
                "successful_checks": 0,
                "failed_checks": 0,
                "message": "No pods with HTTP health checks found",
                "task_duration_seconds": time.time() - task_start_time
            }
        
        # Get health check workers from cache (non-blocking read-only)
        # Worker list is updated every 60 seconds by a separate beat task
        worker_dist_start = time.time()
        current_worker_hostname = gethostname()
        health_check_workers = get_health_check_workers()
        
        # Distribute pods across workers (same logic as HTTP health checks)
        if len(health_check_workers) == 1:
            logger.debug(f"Single health check worker detected ({health_check_workers[0]}), skipping worker distribution")
            assigned_pods = all_pods
            worker_dist_time = time.time() - worker_dist_start
            logger.info(f"[TIMING] worker_distribution (single worker, skipped): {worker_dist_time:.3f}s")
            logger.info(f"Single health check worker: {health_check_workers[0]}, checking all {len(assigned_pods)} pods")
        else:
            # Multiple workers - need distribution
            logger.info(f"Found {len(health_check_workers)} health check worker(s): {health_check_workers}")
            logger.info(f"Current worker hostname: {current_worker_hostname}")
            
            # Filter pods: only check pods assigned to this worker
            assigned_pods = []
            pod_distribution = {}  # Track distribution for logging
            
            for pod in all_pods:
                pod_id = pod.get('pod_id')
                if not pod_id:
                    continue
                
                assigned_worker = assign_pod_to_worker(pod_id, health_check_workers)
                
                # Track distribution
                if assigned_worker not in pod_distribution:
                    pod_distribution[assigned_worker] = 0
                pod_distribution[assigned_worker] += 1
                
                # Only include pods assigned to this worker
                if assigned_worker == current_worker_hostname:
                    assigned_pods.append(pod)
            
            worker_dist_time = time.time() - worker_dist_start
            logger.info(f"[TIMING] worker_distribution: {worker_dist_time:.3f}s")
            logger.info(f"Pod distribution across workers: {pod_distribution}")
            logger.info(f"Pods assigned to this worker ({current_worker_hostname}): {len(assigned_pods)}/{len(all_pods)}")
        
        if not assigned_pods:
            return {
                "status": "success",
                "pods_checked": 0,
                "total_pods": len(all_pods),
                "endpoints_checked": 0,
                "successful_checks": 0,
                "failed_checks": 0,
                "message": f"No pods assigned to this worker ({current_worker_hostname})",
                "worker_hostname": current_worker_hostname,
                "total_workers": len(health_check_workers),
                "task_duration_seconds": time.time() - task_start_time
            }
        
        # Extract endpoints from assigned pods
        extract_start = time.time()
        endpoints = _extract_endpoints_from_pods(assigned_pods)
        extract_time = time.time() - extract_start
        logger.info(f"[TIMING] extract_endpoints_from_pods: {extract_time:.3f}s ({len(endpoints)} endpoints)")
        logger.info(f"Extracted {len(endpoints)} endpoints for TCP health checks from {len(assigned_pods)} assigned pods: {endpoints}")
        
        if not endpoints:
            return {
                "status": "success",
                "pods_checked": len(assigned_pods),
                "total_pods": len(all_pods),
                "endpoints_checked": 0,
                "successful_checks": 0,
                "failed_checks": 0,
                "message": "No endpoints extracted from assigned pods",
                "worker_hostname": current_worker_hostname,
                "total_workers": len(health_check_workers),
                "task_duration_seconds": time.time() - task_start_time
            }
        
        # Perform TCP health checks using async method
        check_start = time.time()
        checker = TCPHealthCheckBase(
            timeout=3.0,  # 3 second timeout for TCP checks
            max_workers=20,  # Allow concurrent checks
            enable_logging=True
        )
        
        summary = await checker.check_all_async(endpoints)
        check_time = time.time() - check_start
        
        task_duration = time.time() - task_start_time
        
        # Log summary
        logger.info(
            f"[TIMING] TCP health check task completed in {task_duration:.3f}s "
            f"(get_pods={get_pods_time:.3f}s, worker_dist={worker_dist_time:.3f}s, "
            f"extract={extract_time:.3f}s, checks={check_time:.3f}s): "
            f"{summary.successful_checks}/{summary.total_checks} successful "
            f"({summary.success_rate:.1f}%) for {len(assigned_pods)} assigned pods"
        )
        
        # Log failed endpoints
        failed_results = [r for r in summary.results if not r.success]
        if failed_results:
            logger.warning(f"TCP health check failures detected for {len(failed_results)} endpoints:")
            for result in failed_results:
                logger.warning(
                    f"  - {result.ip}:{result.port} - {result.status.value} "
                    f"({result.error_message})"
                )
        
        return {
            "status": "success",
            "pods_checked": len(assigned_pods),
            "total_pods": len(all_pods),
            "endpoints_checked": summary.total_checks,
            "successful_checks": summary.successful_checks,
            "failed_checks": summary.failed_checks,
            "success_rate": summary.success_rate,
            "task_duration_seconds": task_duration,
            "worker_hostname": current_worker_hostname,
            "total_workers": len(health_check_workers),
            "failed_endpoints": [
                {
                    "ip": r.ip,
                    "port": r.port,
                    "status": r.status.value,
                    "error": r.error_message
                }
                for r in failed_results
            ]
        }
        
    except Exception as e:
        logger.error(f"Error in TCP health check task: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "pods_checked": 0,
            "endpoints_checked": 0,
            "task_duration_seconds": time.time() - task_start_time if 'task_start_time' in locals() else 0
        }
