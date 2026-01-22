"""
TCP 3-Way Handshake Health Check Module

This module provides an async, multi-threaded base class for performing TCP
3-way handshake health checks on multiple IP:port endpoints concurrently.

Features:
- Async TCP connection checks using asyncio
- Multi-threaded execution using ThreadPoolExecutor
- Concurrent checking of multiple endpoints
- Detailed failure detection (timeout, connection refused, etc.)
- Configurable timeouts and thread pool size
- Periodic task for checking pods with HTTP health checks every 3 seconds
"""

import asyncio
import socket
import errno
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()

# Import Celery task decorator and dependencies
try:
    from utils.celery.celery_config import celery_app
    from utils.celery.async_task_base import AsyncTask
    from utils.redis.redis_interface import RedisInterface
    from utils.redis.host_pod_store import HostPodStore
    from utils.redis.deployment_store import DeploymentStore
    from utils.celery.tasks.health_check_tasks import (
        get_health_check_workers,
        assign_pod_to_worker
    )
    from socket import gethostname
    import hashlib
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    logger.warning("Celery not available, TCP health check tasks will not be available")


class TCPConnectionStatus(str, Enum):
    """TCP connection status enumeration."""
    SUCCESS = "success"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    NETWORK_UNREACHABLE = "network_unreachable"
    HOST_UNREACHABLE = "host_unreachable"
    RESET = "connection_reset"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class TCPHealthCheckResult:
    """Result of a single TCP health check."""
    ip: str
    port: int
    status: TCPConnectionStatus
    success: bool
    error_message: Optional[str] = None
    response_time_ms: Optional[float] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary."""
        return {
            "ip": self.ip,
            "port": self.port,
            "status": self.status.value,
            "success": self.success,
            "error_message": self.error_message,
            "response_time_ms": self.response_time_ms,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class TCPHealthCheckSummary:
    """Summary of TCP health check results."""
    total_checks: int
    successful_checks: int
    failed_checks: int
    success_rate: float
    results: List[TCPHealthCheckResult] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert summary to dictionary."""
        return {
            "total_checks": self.total_checks,
            "successful_checks": self.successful_checks,
            "failed_checks": self.failed_checks,
            "success_rate": self.success_rate,
            "results": [r.to_dict() for r in self.results]
        }


class TCPHealthCheckBase:
    """
    Base class for async, multi-threaded TCP 3-way handshake health checks.
    
    This class performs TCP connection checks on multiple IP:port endpoints
    concurrently using a combination of asyncio and ThreadPoolExecutor for
    optimal performance.
    
    Usage:
        check_config = {
            "192.168.1.1": 8080,
            "192.168.1.2": 9090,
            "192.168.1.3": 80
        }
        
        checker = TCPHealthCheckBase(
            timeout=0.25,  # 250ms timeout
            max_workers=10
        )
        
        # Async execution
        summary = await checker.check_all_async(check_config)
        
        # Or sync execution
        summary = checker.check_all(check_config)
    """
    
    def __init__(
        self,
        timeout: float = 0.25,
        max_workers: int = 10,
        enable_logging: bool = True
    ):
        """
        Initialize TCP health check base class.
        
        Args:
            timeout: Connection timeout in seconds (default: 0.25 = 250ms)
            max_workers: Maximum number of worker threads (default: 10)
            enable_logging: Enable detailed logging (default: True)
        """
        self.timeout = timeout
        self.max_workers = max_workers
        self.enable_logging = enable_logging
    
    @log_to_file(logger)
    async def _check_single_async(
        self,
        ip: str,
        port: int
    ) -> TCPHealthCheckResult:
        """
        Perform a single TCP 3-way handshake check asynchronously.
        
        Args:
            ip: IP address to check
            port: Port number to check
            
        Returns:
            TCPHealthCheckResult with connection status
        """
        start_time = datetime.now(timezone.utc)
        
        try:
            # Strip CIDR notation from IP if present
            if '/' in ip:
                ip = ip.split('/')[0]
            
            # Use asyncio to perform TCP connection (3-way handshake)
            try:
                # Create connection with timeout
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port),
                    timeout=self.timeout
                )
                
                # Successfully completed 3-way handshake
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                # Close connection properly
                writer.close()
                await writer.wait_closed()
                
                if self.enable_logging:
                    logger.debug(f"TCP handshake successful for {ip}:{port} (response time: {response_time:.2f}ms)")
                
                return TCPHealthCheckResult(
                    ip=ip,
                    port=port,
                    status=TCPConnectionStatus.SUCCESS,
                    success=True,
                    response_time_ms=response_time
                )
                
            except asyncio.TimeoutError:
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                if self.enable_logging:
                    logger.warning(f"TCP handshake timeout for {ip}:{port} after {self.timeout}s")
                
                return TCPHealthCheckResult(
                    ip=ip,
                    port=port,
                    status=TCPConnectionStatus.TIMEOUT,
                    success=False,
                    error_message=f"Connection timeout after {self.timeout}s",
                    response_time_ms=response_time
                )
                
            except ConnectionRefusedError:
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                if self.enable_logging:
                    logger.warning(f"TCP connection refused for {ip}:{port}")
                
                return TCPHealthCheckResult(
                    ip=ip,
                    port=port,
                    status=TCPConnectionStatus.CONNECTION_REFUSED,
                    success=False,
                    error_message="Connection refused",
                    response_time_ms=response_time
                )
                
            except OSError as e:
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                error_code = e.errno
                error_msg = str(e)
                
                # Map common OS error codes to status
                status = TCPConnectionStatus.ERROR
                if error_code == errno.ENETUNREACH:
                    status = TCPConnectionStatus.NETWORK_UNREACHABLE
                elif error_code == errno.EHOSTUNREACH:
                    status = TCPConnectionStatus.HOST_UNREACHABLE
                elif error_code == errno.ECONNRESET:
                    status = TCPConnectionStatus.RESET
                elif error_code == errno.ECONNREFUSED:
                    status = TCPConnectionStatus.CONNECTION_REFUSED
                
                if self.enable_logging:
                    logger.warning(f"TCP handshake failed for {ip}:{port}: {error_msg}")
                
                return TCPHealthCheckResult(
                    ip=ip,
                    port=port,
                    status=status,
                    success=False,
                    error_message=error_msg,
                    response_time_ms=response_time
                )
                
            except Exception as e:
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                if self.enable_logging:
                    logger.error(f"Unexpected error during TCP handshake for {ip}:{port}: {e}", exc_info=True)
                
                return TCPHealthCheckResult(
                    ip=ip,
                    port=port,
                    status=TCPConnectionStatus.ERROR,
                    success=False,
                    error_message=str(e),
                    response_time_ms=response_time
                )
                
        except Exception as e:
            if self.enable_logging:
                logger.error(f"Failed to perform TCP health check for {ip}:{port}: {e}", exc_info=True)
            
            return TCPHealthCheckResult(
                ip=ip,
                port=port,
                status=TCPConnectionStatus.ERROR,
                success=False,
                error_message=str(e)
            )
    
    @log_to_file(logger)
    async def check_all_async(
        self,
        endpoints: Dict[str, int]
    ) -> TCPHealthCheckSummary:
        """
        Perform TCP health checks on all endpoints concurrently.
        
        Args:
            endpoints: Dictionary mapping IP addresses to port numbers
                      Example: {"192.168.1.1": 8080, "192.168.1.2": 9090}
        
        Returns:
            TCPHealthCheckSummary with all results
        """
        if not endpoints:
            return TCPHealthCheckSummary(
                total_checks=0,
                successful_checks=0,
                failed_checks=0,
                success_rate=0.0,
                results=[]
            )
        
        if self.enable_logging:
            logger.info(f"Starting TCP health checks for {len(endpoints)} endpoints (timeout: {self.timeout}s)")
        
        # Create tasks for all endpoints
        tasks = [
            self._check_single_async(ip, port)
            for ip, port in endpoints.items()
        ]
        
        # Execute all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        health_results: List[TCPHealthCheckResult] = []
        for result in results:
            if isinstance(result, Exception):
                if self.enable_logging:
                    logger.error(f"Task raised exception: {result}", exc_info=True)
                # Create error result
                health_results.append(
                    TCPHealthCheckResult(
                        ip="unknown",
                        port=0,
                        status=TCPConnectionStatus.ERROR,
                        success=False,
                        error_message=str(result)
                    )
                )
            else:
                health_results.append(result)
        
        # Calculate summary
        total = len(health_results)
        successful = sum(1 for r in health_results if r.success)
        failed = total - successful
        success_rate = (successful / total * 100.0) if total > 0 else 0.0
        
        if self.enable_logging:
            logger.info(
                f"TCP health checks completed: {successful}/{total} successful "
                f"({success_rate:.1f}%), {failed} failed"
            )
        
        return TCPHealthCheckSummary(
            total_checks=total,
            successful_checks=successful,
            failed_checks=failed,
            success_rate=success_rate,
            results=health_results
        )
    
    @log_to_file(logger)
    def check_all(
        self,
        endpoints: Dict[str, int]
    ) -> TCPHealthCheckSummary:
        """
        Perform TCP health checks on all endpoints (synchronous wrapper).
        
        This method uses ThreadPoolExecutor to run the async checks in a
        synchronous context, providing multi-threaded execution.
        
        Args:
            endpoints: Dictionary mapping IP addresses to port numbers
        
        Returns:
            TCPHealthCheckSummary with all results
        """
        # Use ThreadPoolExecutor to run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            return loop.run_until_complete(self.check_all_async(endpoints))
        finally:
            loop.close()
    
    @log_to_file(logger)
    def check_all_multi_threaded(
        self,
        endpoints: Dict[str, int]
    ) -> TCPHealthCheckSummary:
        """
        Perform TCP health checks using ThreadPoolExecutor for true multi-threading.
        
        This method uses threads instead of async for cases where you want
        to avoid event loop conflicts or need thread-based concurrency.
        
        Args:
            endpoints: Dictionary mapping IP addresses to port numbers
        
        Returns:
            TCPHealthCheckSummary with all results
        """
        if not endpoints:
            return TCPHealthCheckSummary(
                total_checks=0,
                successful_checks=0,
                failed_checks=0,
                success_rate=0.0,
                results=[]
            )
        
        if self.enable_logging:
            logger.info(
                f"Starting multi-threaded TCP health checks for {len(endpoints)} endpoints "
                f"(timeout: {self.timeout}s, max_workers: {self.max_workers})"
            )
        
        def check_single_sync(ip_port: Tuple[str, int]) -> TCPHealthCheckResult:
            """Synchronous TCP check using socket."""
            ip, port = ip_port
            start_time = datetime.now(timezone.utc)
            
            try:
                # Strip CIDR notation from IP if present
                if '/' in ip:
                    ip = ip.split('/')[0]
                
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                
                result_code = sock.connect_ex((ip, port))
                sock.close()
                
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                if result_code == 0:
                    # Success - 3-way handshake completed
                    if self.enable_logging:
                        logger.debug(f"TCP handshake successful for {ip}:{port} (response time: {response_time:.2f}ms)")
                    
                    return TCPHealthCheckResult(
                        ip=ip,
                        port=port,
                        status=TCPConnectionStatus.SUCCESS,
                        success=True,
                        response_time_ms=response_time
                    )
                else:
                    # Connection failed
                    # Map error codes to status
                    error_map = {
                        errno.ECONNREFUSED: TCPConnectionStatus.CONNECTION_REFUSED,
                        errno.ETIMEDOUT: TCPConnectionStatus.TIMEOUT,
                        errno.ENETUNREACH: TCPConnectionStatus.NETWORK_UNREACHABLE,
                        errno.EHOSTUNREACH: TCPConnectionStatus.HOST_UNREACHABLE,
                        errno.ECONNRESET: TCPConnectionStatus.RESET,
                    }
                    
                    error_msg_map = {
                        errno.ECONNREFUSED: "Connection refused",
                        errno.ETIMEDOUT: "Connection timeout",
                        errno.ENETUNREACH: "Network unreachable",
                        errno.EHOSTUNREACH: "Host unreachable",
                        errno.ECONNRESET: "Connection reset by peer",
                    }
                    
                    status = error_map.get(result_code, TCPConnectionStatus.ERROR)
                    error_msg = error_msg_map.get(result_code, f"Connection failed with error code {result_code}")
                    
                    if self.enable_logging:
                        logger.warning(f"TCP handshake failed for {ip}:{port}: {error_msg}")
                    
                    return TCPHealthCheckResult(
                        ip=ip,
                        port=port,
                        status=status,
                        success=False,
                        error_message=error_msg,
                        response_time_ms=response_time
                    )
                    
            except socket.timeout:
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                if self.enable_logging:
                    logger.warning(f"TCP handshake timeout for {ip}:{port} after {self.timeout}s")
                
                return TCPHealthCheckResult(
                    ip=ip,
                    port=port,
                    status=TCPConnectionStatus.TIMEOUT,
                    success=False,
                    error_message=f"Connection timeout after {self.timeout}s",
                    response_time_ms=response_time
                )
                
            except Exception as e:
                end_time = datetime.now(timezone.utc)
                response_time = (end_time - start_time).total_seconds() * 1000
                
                if self.enable_logging:
                    logger.error(f"Unexpected error during TCP handshake for {ip}:{port}: {e}", exc_info=True)
                
                return TCPHealthCheckResult(
                    ip=ip,
                    port=port,
                    status=TCPConnectionStatus.ERROR,
                    success=False,
                    error_message=str(e),
                    response_time_ms=response_time
                )
        
        # Use ThreadPoolExecutor to check all endpoints concurrently
        results: List[TCPHealthCheckResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_endpoint = {
                executor.submit(check_single_sync, (ip, port)): (ip, port)
                for ip, port in endpoints.items()
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_endpoint):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    ip, port = future_to_endpoint[future]
                    if self.enable_logging:
                        logger.error(f"Task for {ip}:{port} raised exception: {e}", exc_info=True)
                    results.append(
                        TCPHealthCheckResult(
                            ip=ip,
                            port=port,
                            status=TCPConnectionStatus.ERROR,
                            success=False,
                            error_message=str(e)
                        )
                    )
        
        # Calculate summary
        total = len(results)
        successful = sum(1 for r in results if r.success)
        failed = total - successful
        success_rate = (successful / total * 100.0) if total > 0 else 0.0
        
        if self.enable_logging:
            logger.info(
                f"Multi-threaded TCP health checks completed: {successful}/{total} successful "
                f"({success_rate:.1f}%), {failed} failed"
            )
        
        return TCPHealthCheckSummary(
            total_checks=total,
            successful_checks=successful,
            failed_checks=failed,
            success_rate=success_rate,
            results=results
        )


# ============================================================================
# CELERY TASK FOR PERIODIC TCP HEALTH CHECKS
# ============================================================================

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
    if not CELERY_AVAILABLE:
        logger.warning("Celery not available, cannot get pods with HTTP health checks")
        return []
    
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


if CELERY_AVAILABLE:
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
        
        Returns:
            Dictionary with task execution summary
        """
        try:
            task_start_time = datetime.now(timezone.utc)
            logger.info("Starting TCP health check task for pods with HTTP health checks")
            
            # Get all pods with HTTP health checks
            all_pods = _get_pods_with_http_health_checks()
            logger.info(f"Found {len(all_pods)} pods with HTTP health checks")
            
            if not all_pods:
                return {
                    "status": "success",
                    "pods_checked": 0,
                    "endpoints_checked": 0,
                    "successful_checks": 0,
                    "failed_checks": 0,
                    "message": "No pods with HTTP health checks found"
                }
            
            # Get health check workers from cache (non-blocking read-only)
            # Worker list is updated every 60 seconds by a separate beat task
            current_worker_hostname = gethostname()
            health_check_workers = get_health_check_workers()
            
            # Distribute pods across workers (same logic as HTTP health checks)
            if len(health_check_workers) == 1:
                logger.debug(f"Single health check worker detected ({health_check_workers[0]}), skipping worker distribution")
                assigned_pods = all_pods
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
                
                logger.info(f"Pod distribution across workers: {pod_distribution}")
                logger.info(f"Pods assigned to this worker ({current_worker_hostname}): {len(assigned_pods)}/{len(all_pods)}")
            
            if not assigned_pods:
                return {
                    "status": "success",
                    "pods_checked": 0,
                    "endpoints_checked": 0,
                    "successful_checks": 0,
                    "failed_checks": 0,
                    "message": f"No pods assigned to this worker ({current_worker_hostname})"
                }
            
            # Extract endpoints from assigned pods
            endpoints = _extract_endpoints_from_pods(assigned_pods)
            logger.info(f"Extracted {len(endpoints)} endpoints for TCP health checks from {len(assigned_pods)} assigned pods: {endpoints}")
            
            if not endpoints:
                return {
                    "status": "success",
                    "pods_checked": len(assigned_pods),
                    "endpoints_checked": 0,
                    "successful_checks": 0,
                    "failed_checks": 0,
                    "message": "No endpoints extracted from assigned pods"
                }
            
            # Perform TCP health checks using async method
            checker = TCPHealthCheckBase(
                timeout=0.25,  # 250ms timeout for TCP checks (200-300ms range)
                max_workers=20,  # Allow concurrent checks
                enable_logging=True
            )
            
            summary = await checker.check_all_async(endpoints)
            
            task_end_time = datetime.now(timezone.utc)
            task_duration = (task_end_time - task_start_time).total_seconds()
            
            # Log summary
            logger.info(
                f"TCP health check task completed in {task_duration:.2f}s: "
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
                "endpoints_checked": 0
            }
