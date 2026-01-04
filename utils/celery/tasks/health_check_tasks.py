"""
Health check tasks for Dibba pods.

This module provides tasks to:
- Perform liveness and readiness probes on pods
- Update pod health status in Redis
- Mark pods as ready/unready based on health checks
"""
import requests
import socket
import subprocess
from typing import Dict, Any, Optional
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from utils.redis.deployment_store import DeploymentStore
from datetime import datetime, timezone

logger = LogKCld()


def _check_http_probe(http_get: Dict[str, Any], pod_ip: str, container_port: int) -> bool:
    """Check HTTP health probe.
    
    Args:
        http_get: HTTP probe configuration with 'path' and optional 'port'
        pod_ip: Pod IP address
        container_port: Default container port
        
    Returns:
        True if probe succeeds, False otherwise
    """
    try:
        path = http_get.get('path', '/')
        port = http_get.get('port', container_port)
        scheme = http_get.get('scheme', 'HTTP').upper()
        
        if scheme == 'HTTPS':
            url = f"https://{pod_ip}:{port}{path}"
        else:
            url = f"http://{pod_ip}:{port}{path}"
        
        timeout = http_get.get('timeoutSeconds', 1)
        response = requests.get(url, timeout=timeout, verify=False)
        
        # Check status code (default: 200-399 is success)
        http_headers = http_get.get('httpHeaders', [])
        expected_status = None
        for header in http_headers:
            if header.get('name', '').lower() == 'expected-status':
                expected_status = int(header.get('value', '200'))
        
        if expected_status:
            return response.status_code == expected_status
        else:
            # Default: 200-399 is success
            return 200 <= response.status_code < 400
            
    except Exception as e:
        logger.warning(f"HTTP probe failed for {pod_ip}:{port}{path}: {e}")
        return False


def _check_tcp_probe(tcp_socket: Dict[str, Any], pod_ip: str, container_port: int) -> bool:
    """Check TCP health probe.
    
    Args:
        tcp_socket: TCP probe configuration with optional 'port'
        pod_ip: Pod IP address
        container_port: Default container port
        
    Returns:
        True if TCP connection succeeds, False otherwise
    """
    try:
        port = tcp_socket.get('port', container_port)
        timeout = tcp_socket.get('timeoutSeconds', 1)
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((pod_ip, port))
        sock.close()
        
        return result == 0
        
    except Exception as e:
        logger.warning(f"TCP probe failed for {pod_ip}:{port}: {e}")
        return False


def _check_exec_probe(exec_cmd: Dict[str, Any], hostname: str, namespace: str, pod_id: str, container_name: str) -> bool:
    """Check exec health probe by running command in container.
    
    Args:
        exec_cmd: Exec probe configuration with 'command' list
        hostname: Host where pod is running
        namespace: Pod namespace
        pod_id: Pod ID
        container_name: Container name
        
    Returns:
        True if command exits with code 0, False otherwise
    """
    try:
        command = exec_cmd.get('command', [])
        if not command:
            logger.warning(f"Exec probe has no command for pod {pod_id}")
            return False
        
        # Use ctr to exec into the container
        # Format: ctr -n <namespace> task exec --exec-id <id> <container-id> <command>
        container_id = f"{pod_id}-{container_name}"
        exec_id = f"healthcheck-{datetime.now(timezone.utc).timestamp()}"
        
        # Build ctr command
        ctr_cmd = [
            'ctr',
            '-n', namespace,
            'task', 'exec',
            '--exec-id', exec_id,
            container_id
        ] + command
        
        # Execute command (this would need to run on the host)
        # For now, we'll use a simplified approach
        # In production, this should be executed via SSH or containerd API
        result = subprocess.run(
            ctr_cmd,
            capture_output=True,
            timeout=exec_cmd.get('timeoutSeconds', 1),
            text=True
        )
        
        return result.returncode == 0
        
    except subprocess.TimeoutExpired:
        logger.warning(f"Exec probe timed out for pod {pod_id} container {container_name}")
        return False
    except Exception as e:
        logger.warning(f"Exec probe failed for pod {pod_id} container {container_name}: {e}")
        return False


@celery_app.task(name="health.check_pod_health")
@log_to_file(logger)
def check_pod_health_task(pod_id: str, hostname: str, namespace: str) -> Dict[str, Any]:
    """Check health of a single pod.
    
    Args:
        pod_id: Pod ID
        hostname: Host where pod is running
        namespace: Pod namespace
        
    Returns:
        Dictionary with health check results
    """
    try:
        redis_interface = RedisInterface()
        host_pod_store = HostPodStore(redis_interface)
        deployment_store = DeploymentStore(redis_interface)
        
        # Get pod information
        pod = host_pod_store.get_pod(pod_id)
        if not pod:
            logger.warning(f"Pod {pod_id} not found in Redis")
            return {
                'status': 'error',
                'error': f'Pod {pod_id} not found',
                'pod_id': pod_id
            }
        
        pod_ip = pod.get('ip_address')
        if not pod_ip:
            logger.warning(f"Pod {pod_id} has no IP address")
            return {
                'status': 'error',
                'error': f'Pod {pod_id} has no IP address',
                'pod_id': pod_id
            }
        
        # Get deployment to find health check configuration
        deployment_name = pod.get('labels', {}).get('deployment_name') or pod.get('deployment_name')
        if not deployment_name:
            # Try to find by app_label
            app_label = pod.get('labels', {}).get('app') or pod.get('labels', {}).get('app_label')
            if app_label:
                # Find deployment by app_label (this is a simplified lookup)
                # In production, you might want to store app_label -> deployment_name mapping
                logger.debug(f"Looking up deployment by app_label {app_label}")
        
        deployment = None
        if deployment_name:
            deployment = deployment_store.get_deployment(deployment_name, namespace)
        
        health_results = {
            'pod_id': pod_id,
            'hostname': hostname,
            'namespace': namespace,
            'ip_address': pod_ip,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'liveness': {'status': 'unknown', 'last_check': None},
            'readiness': {'status': 'unknown', 'last_check': None},
        }
        
        containers = pod.get('containers', [])
        if not containers:
            logger.warning(f"Pod {pod_id} has no containers")
            health_results['liveness']['status'] = 'failed'
            health_results['readiness']['status'] = 'failed'
            return health_results
        
        # Get health check configuration from deployment
        health_checks = None
        if deployment:
            deployment_spec = deployment.get('deployment_spec', {})
            health_checks = deployment_spec.get('health_checks', {})
        
        # Check each container
        for container in containers:
            container_name = container.get('name')
            if not container_name:
                continue
            
            # Get container ports
            ports = container.get('ports', [])
            container_port = 8080  # Default port
            if ports and isinstance(ports, list):
                for port_config in ports:
                    if isinstance(port_config, dict):
                        container_port = port_config.get('containerPort', container_port)
                        break
                    elif isinstance(port_config, int):
                        container_port = port_config
                        break
            
            # Check liveness probe
            if health_checks and container_name in health_checks:
                liveness_probe = health_checks[container_name].get('livenessProbe')
                if liveness_probe:
                    liveness_result = _perform_probe(liveness_probe, pod_ip, container_port, hostname, namespace, pod_id, container_name)
                    health_results['liveness'] = {
                        'status': 'success' if liveness_result else 'failed',
                        'last_check': datetime.now(timezone.utc).isoformat()
                    }
            
            # Check readiness probe
            if health_checks and container_name in health_checks:
                readiness_probe = health_checks[container_name].get('readinessProbe')
                if readiness_probe:
                    readiness_result = _perform_probe(readiness_probe, pod_ip, container_port, hostname, namespace, pod_id, container_name)
                    health_results['readiness'] = {
                        'status': 'success' if readiness_result else 'failed',
                        'last_check': datetime.now(timezone.utc).isoformat()
                    }
        
        # Update pod health status in Redis
        _update_pod_health_status(host_pod_store, pod_id, health_results)
        
        return health_results
        
    except Exception as e:
        logger.error(f"Health check failed for pod {pod_id}: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e),
            'pod_id': pod_id
        }


def _perform_probe(probe_config: Dict[str, Any], pod_ip: str, container_port: int,
                   hostname: str, namespace: str, pod_id: str, container_name: str) -> bool:
    """Perform a health probe based on configuration.
    
    Args:
        probe_config: Probe configuration (httpGet, tcpSocket, or exec)
        pod_ip: Pod IP address
        container_port: Default container port
        hostname: Host where pod is running
        namespace: Pod namespace
        pod_id: Pod ID
        container_name: Container name
        
    Returns:
        True if probe succeeds, False otherwise
    """
    if 'httpGet' in probe_config:
        return _check_http_probe(probe_config['httpGet'], pod_ip, container_port)
    elif 'tcpSocket' in probe_config:
        return _check_tcp_probe(probe_config['tcpSocket'], pod_ip, container_port)
    elif 'exec' in probe_config:
        return _check_exec_probe(probe_config['exec'], hostname, namespace, pod_id, container_name)
    else:
        logger.warning(f"No valid probe type found in probe config: {probe_config}")
        return False


def _update_pod_health_status(host_pod_store: HostPodStore, pod_id: str, health_results: Dict[str, Any]):
    """Update pod health status in Redis.
    
    Args:
        host_pod_store: HostPodStore instance
        pod_id: Pod ID
        health_results: Health check results
    """
    try:
        pod = host_pod_store.get_pod(pod_id)
        if not pod:
            return
        
        # Update pod with health check results
        pod['health_checks'] = {
            'liveness': health_results.get('liveness', {}),
            'readiness': health_results.get('readiness', {}),
            'last_check': health_results.get('timestamp')
        }
        
        # Determine overall pod health status
        liveness_status = health_results.get('liveness', {}).get('status', 'unknown')
        readiness_status = health_results.get('readiness', {}).get('status', 'unknown')
        
        # Update pod status based on health checks
        # If liveness fails, pod should be marked as failed
        # If readiness fails, pod should be marked as not ready (but still running)
        if liveness_status == 'failed':
            pod['status'] = 'FAILED'
            pod['health_status'] = 'unhealthy'
        elif readiness_status == 'success':
            pod['status'] = 'RUNNING'
            pod['health_status'] = 'ready'
        elif readiness_status == 'failed':
            pod['status'] = 'RUNNING'
            pod['health_status'] = 'not_ready'
        else:
            pod['health_status'] = 'unknown'
        
        # Save updated pod
        host_pod_store.save_pod(
            pod_id=pod_id,
            hostname=pod.get('hostname'),
            namespace=pod.get('namespace'),
            containers=pod.get('containers', []),
            pause_container=pod.get('pause_container', {}),
            labels=pod.get('labels', {}),
            creation_time=pod.get('creation_time'),
            startup_time=pod.get('startup_time'),
            ip_address=pod.get('ip_address'),
            cni_network=pod.get('cni_network')
        )
        
        logger.info(f"Updated health status for pod {pod_id}: liveness={liveness_status}, readiness={readiness_status}")
        
    except Exception as e:
        logger.error(f"Failed to update pod health status for {pod_id}: {e}", exc_info=True)


@celery_app.task(name="health.check_all_pods_health")
@log_to_file(logger)
def check_all_pods_health_task() -> Dict[str, Any]:
    """Check health of all running pods.
    
    This task should be scheduled periodically (e.g., every 10-30 seconds).
    
    Returns:
        Dictionary with summary of health checks
    """
    try:
        redis_interface = RedisInterface()
        host_pod_store = HostPodStore(redis_interface)
        
        # Get all running pods
        all_hosts = host_pod_store.get_all_hosts()
        all_pods = []
        
        for host in all_hosts:
            hostname = host.get('hostname')
            if not hostname:
                continue
            
            host_pods = host_pod_store.get_pods_by_host(hostname)
            for pod in host_pods:
                if pod.get('status', '').upper() == 'RUNNING':
                    all_pods.append(pod)
        
        results = {
            'total_pods': len(all_pods),
            'checked': 0,
            'healthy': 0,
            'unhealthy': 0,
            'not_ready': 0,
            'errors': []
        }
        
        # Check health for each pod
        for pod in all_pods:
            pod_id = pod.get('pod_id')
            hostname = pod.get('hostname')
            namespace = pod.get('namespace')
            
            if not pod_id or not hostname or not namespace:
                continue
            
            try:
                # Call the task function directly (not as a Celery task) to avoid async overhead
                health_result = check_pod_health_task(pod_id, hostname, namespace)
                results['checked'] += 1
                
                if health_result.get('status') == 'error':
                    results['errors'].append({
                        'pod_id': pod_id,
                        'error': health_result.get('error')
                    })
                else:
                    liveness = health_result.get('liveness', {}).get('status', 'unknown')
                    readiness = health_result.get('readiness', {}).get('status', 'unknown')
                    
                    if liveness == 'failed':
                        results['unhealthy'] += 1
                    elif readiness == 'success':
                        results['healthy'] += 1
                    elif readiness == 'failed':
                        results['not_ready'] += 1
                        
            except Exception as e:
                logger.error(f"Failed to check health for pod {pod_id}: {e}", exc_info=True)
                results['errors'].append({
                    'pod_id': pod_id,
                    'error': str(e)
                })
        
        logger.info(f"Health check summary: {results['checked']} pods checked, {results['healthy']} healthy, {results['unhealthy']} unhealthy, {results['not_ready']} not ready")
        
        return results
        
    except Exception as e:
        logger.error(f"Failed to check all pods health: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e)
        }
