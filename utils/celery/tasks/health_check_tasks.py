"""
Health check tasks for Dibba pods.

This module provides Celery tasks to:
- Check health of individual pods
- Check health of all running pods

Helper functions are in utils.healthcheck
"""
from typing import Dict, Any, List, Optional
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.celery.async_task_base import AsyncTask
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from utils.redis.deployment_store import DeploymentStore
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from socket import gethostname
import hashlib
import re
import time
import asyncio
import aiohttp
from utils.healthcheck import (
    get_probe_state,
    should_check_probe,
    perform_probe,
    perform_probe_async,
    evaluate_probe_result,
    save_probe_state,
    update_pod_health_status
)

logger = LogKCld()


def _extract_host_from_worker_name(worker_name: str) -> str:
    """Extract hostname from Celery worker name.
    
    Examples:
        "celery@ip-172-31-19-101" -> "ip-172-31-19-101"
        "celery@worker-1" -> "worker-1"
    """
    match = re.search(r'@(.+)$', worker_name)
    if match:
        return match.group(1)
    return worker_name


def pod_has_health_checks(pod: Dict[str, Any], deployment_store: DeploymentStore) -> bool:
    """Check if a pod has health check configurations (readiness or liveness probes).
    
    Args:
        pod: Pod dictionary
        deployment_store: DeploymentStore instance
        
    Returns:
        True if pod has health checks configured, False otherwise
    """
    try:
        namespace = pod.get('namespace')
        if not namespace:
            return False
        
        # Get deployment to find health check configuration
        deployment_name = pod.get('labels', {}).get('deployment_name') or pod.get('deployment_name')
        deployment = None
        
        if deployment_name:
            # Try to find by deployment name first
            deployment = deployment_store.get_deployment(deployment_name, namespace)
        else:
            # Try to find by app_label if deployment_name is not available
            app_label = pod.get('labels', {}).get('app') or pod.get('labels', {}).get('app_label')
            if app_label:
                # Find all deployments with this app_label in the namespace
                deployments_by_app = deployment_store.get_deployments_by_app(app_label)
                # Filter by namespace and pick the first one
                for dep in deployments_by_app:
                    if dep.get('namespace') == namespace:
                        deployment = dep
                        break
                if not deployment and deployments_by_app:
                    # If no exact namespace match, try any deployment with this app_label
                    deployment = deployments_by_app[0]
        
        if not deployment:
            return False
        
        # Check if deployment has health check configurations
        deployment_spec = deployment.get('deployment_spec', {})
        health_checks = deployment_spec.get('health_checks', {})
        
        if not health_checks:
            return False
        
        # Check if any container has readiness or liveness probes
        containers = pod.get('containers', [])
        for container in containers:
            container_name = container.get('name')
            if not container_name:
                continue
            
            if container_name in health_checks:
                container_health_checks = health_checks[container_name]
                if container_health_checks.get('readinessProbe') or container_health_checks.get('livenessProbe'):
                    return True
        
        return False
    except Exception as e:
        logger.warning(f"Error checking health checks for pod {pod.get('pod_id', 'unknown')}: {e}")
        return False


# Module-level cache for health check workers (updated every 60 seconds by beat task)
_health_check_workers_cache = None
_health_check_workers_cache_time = 0

def _discover_health_check_workers_sync() -> List[str]:
    """Synchronous function to discover health check workers.
    
    This is called by the beat task every 60 seconds to update the cache.
    It should NOT be called directly from async tasks.
    
    Returns:
        List of worker hostnames that are running health check tasks
    """
    try:
        from utils.celery.worker_discovery import discover_workers
        
        # Use reasonable timeout for discovery
        workers = discover_workers(celery_app, timeout=3)
        health_check_workers = []
        
        # Health check queue name (same encoding as in health_check_worker.py)
        from utils.ReadConfig import ReadConfig as rc
        from utils.extensions.utilities_extention import UtilitiesExtension
        
        read_config = rc()
        key = read_config.encryption_config['key']
        encode_util = UtilitiesExtension(key)
        health_check_queue_name = encode_util.encode_hostname_with_key('health_check')
        
        for worker_name, worker_info in workers.items():
            # Check if this worker listens on the health_check queue
            if worker_info.queues and health_check_queue_name in worker_info.queues:
                hostname = _extract_host_from_worker_name(worker_name)
                health_check_workers.append(hostname)
                logger.debug(f"Found health check worker: {worker_name} (host: {hostname})")
        
        result = sorted(health_check_workers) if health_check_workers else [gethostname()]
        logger.info(f"Discovered {len(result)} health check worker(s): {result}")
        return result
    except Exception as e:
        logger.warning(f"Failed to discover health check workers: {e}", exc_info=True)
        # Fallback: return current hostname if discovery fails
        fallback = [gethostname()]
        logger.warning(f"Using fallback worker list: {fallback}")
        return fallback


def get_health_check_workers() -> List[str]:
    """Get cached health check workers list (non-blocking read-only).
    
    This function only reads from the cache - it does NOT perform discovery.
    The cache is updated every 60 seconds by a separate beat task.
    
    Returns:
        List of worker hostnames that are running health check tasks
    """
    global _health_check_workers_cache
    
    # If cache is empty, initialize with current hostname as fallback
    if _health_check_workers_cache is None:
        logger.debug("Health check workers cache is empty, using fallback")
        _health_check_workers_cache = [gethostname()]
    
    return _health_check_workers_cache


@celery_app.task(name="health.refresh_worker_list")
@log_to_file(logger)
def refresh_health_check_workers_task() -> Dict[str, Any]:
    """Periodic task to refresh the health check workers list (runs every 60 seconds).
    
    This task runs asynchronously via Celery Beat and updates the module-level cache
    without blocking health check tasks.
    
    Returns:
        Dictionary with refresh results
    """
    global _health_check_workers_cache, _health_check_workers_cache_time
    
    try:
        logger.info("Refreshing health check workers list...")
        refresh_start = time.time()
        
        # Discover workers (this may take a few seconds, but runs in background)
        workers = _discover_health_check_workers_sync()
        
        # Update cache
        _health_check_workers_cache = workers
        _health_check_workers_cache_time = time.time()
        
        refresh_time = time.time() - refresh_start
        logger.info(f"✓ Refreshed health check workers list: {workers} (took {refresh_time:.3f}s)")
        
        return {
            'status': 'success',
            'workers': workers,
            'count': len(workers),
            'refresh_time': refresh_time
        }
    except Exception as e:
        logger.error(f"Failed to refresh health check workers list: {e}", exc_info=True)
        # Don't clear cache on error - keep using existing cached value
        return {
            'status': 'error',
            'error': str(e)
        }


def assign_pod_to_worker(pod_id: str, workers: List[str]) -> str:
    """Assign a pod to a worker using consistent hashing.
    
    Args:
        pod_id: Pod ID to assign
        workers: List of worker hostnames
        
    Returns:
        Hostname of the assigned worker
    """
    if not workers:
        return gethostname()  # Fallback to current host
    
    if len(workers) == 1:
        return workers[0]
    
    # Use consistent hashing: hash pod_id and map to worker index
    hash_value = int(hashlib.md5(pod_id.encode()).hexdigest(), 16)
    worker_index = hash_value % len(workers)
    assigned_worker = workers[worker_index]
    
    return assigned_worker


async def _check_pod_health_async(pod_id: str, hostname: str, namespace: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Internal async function to check health of a single pod.
    
    This is the actual workhorse function that can be called directly from other async code
    or wrapped in a Celery task. This separation allows it to be called directly from
    check_all_pods_health_task without going through Celery's task execution.
    
    Args:
        pod_id: Pod ID
        hostname: Host where pod is running
        namespace: Pod namespace
        session: Optional shared aiohttp session for HTTP probes
        
    Returns:
        Dictionary with health check results
    """
    logger.info(f"🔵 [ASYNC] Starting health check for pod {pod_id} (hostname={hostname}, namespace={namespace})")
    pod_check_start = time.time()
    # Create a shared aiohttp session if not provided
    session_provided = session is not None
    if not session:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        logger.debug(f"Created new aiohttp session for pod {pod_id}")
    else:
        logger.debug(f"Using provided aiohttp session for pod {pod_id}")
    
    try:
        redis_interface = RedisInterface()
        host_pod_store = HostPodStore(redis_interface)
        deployment_store = DeploymentStore(redis_interface)
        logger.debug(f"Initialized Redis interfaces for pod {pod_id}")
        
        # Get pod information
        pod_lookup_start = time.time()
        pod = host_pod_store.get_pod(pod_id)
        pod_lookup_time = time.time() - pod_lookup_start
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
        deployment_lookup_start = time.time()
        deployment_name = pod.get('labels', {}).get('deployment_name') or pod.get('deployment_name')
        deployment = None
        
        if deployment_name:
            # Try to find by deployment name first
            deployment = deployment_store.get_deployment(deployment_name, namespace)
        else:
            # Try to find by app_label if deployment_name is not available
            app_label = pod.get('labels', {}).get('app') or pod.get('labels', {}).get('app_label')
            if app_label:
                logger.debug(f"Looking up deployment by app_label {app_label} in namespace {namespace}")
                # Find all deployments with this app_label in the namespace
                deployments_by_app = deployment_store.get_deployments_by_app(app_label)
                # Filter by namespace and pick the first one
                for dep in deployments_by_app:
                    if dep.get('namespace') == namespace:
                        deployment = dep
                        logger.debug(f"Found deployment {dep.get('name')} by app_label {app_label}")
                        break
                if not deployment and deployments_by_app:
                    # If no exact namespace match, try any deployment with this app_label
                    deployment = deployments_by_app[0]
                    logger.debug(f"Using deployment {deployment.get('name')} by app_label {app_label} (namespace mismatch)")
        
        deployment_lookup_time = time.time() - deployment_lookup_start
        
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
            logger.debug(f"Checking {len(containers)} container(s) for pod {pod_id}")
            for container in containers:
                container_name = container.get('name')
                if not container_name:
                    logger.debug(f"Skipping container without name in pod {pod_id}: {container}")
                    continue
                logger.info(f"🔵 [ASYNC] Checking container {container_name} in pod {pod_id}")
            
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
            
            # IMPORTANT: Health check sequence:
            # 1. Readiness probe checks first (from pod start, respecting initialDelaySeconds)
            # 2. Once readiness succeeds, stop checking readiness
            # 3. Liveness probe only starts AFTER readiness succeeds (respecting liveness initialDelaySeconds)
            
            # Check readiness probe first
            readiness_succeeded = False
            readiness_state = None
            readiness_success_time = None
            
            if health_checks and container_name in health_checks:
                readiness_probe = health_checks[container_name].get('readinessProbe')
                if readiness_probe:
                    # Get probe state
                    readiness_state = get_probe_state(redis_interface, pod_id, 'readiness', container_name)
                    current_readiness_status = readiness_state.get('current_status', 'unknown')
                    
                    # Also check pod's stored health_checks status as fallback
                    pod_readiness_status = pod.get('health_checks', {}).get('readiness', {}).get('status', 'unknown')
                    
                    logger.debug(f"Readiness state for pod {pod_id} container {container_name}: probe_state_status={current_readiness_status}, pod_health_status={pod_readiness_status}")
                    
                    # If readiness has already succeeded, skip further readiness checks
                    if current_readiness_status == 'success' or pod_readiness_status == 'success':
                        readiness_succeeded = True
                        # Store readiness success time for liveness probe timing
                        readiness_success_time_str = readiness_state.get('last_check_time')
                        if readiness_success_time_str:
                            try:
                                readiness_success_time = datetime.fromisoformat(readiness_success_time_str.replace('Z', '+00:00'))
                            except:
                                pass
                        
                        logger.info(f"Readiness probe for pod {pod_id} container {container_name} has already succeeded, skipping further readiness checks")
                        health_results['readiness'] = {
                            'status': 'success',
                            'last_check': readiness_state.get('last_check_time'),
                            'consecutive_failures': readiness_state.get('consecutive_failures', 0),
                            'consecutive_successes': readiness_state.get('consecutive_successes', 0),
                            'skipped': True,
                            'reason': 'Readiness already succeeded'
                        }
                    else:
                        # Readiness hasn't succeeded yet, continue checking
                        # Check if we should run the probe (respects initialDelaySeconds and periodSeconds)
                        # Use Redis history as source of truth for last_check_time (persists across worker refreshes)
                        should_check, reason = await should_check_probe(
                            readiness_probe, 
                            readiness_state, 
                            pod.get('creation_time') or pod.get('created_at'),
                            redis_interface=redis_interface,
                            pod_id=pod_id,
                            probe_type='readiness',
                            container_name=container_name
                        )
                        
                        if should_check:
                            # Perform the probe (async version)
                            readiness_probe_start = time.time()
                            probe_result = await perform_probe_async(readiness_probe, pod_ip, container_port, hostname, namespace, pod_id, container_name, session=session)
                            readiness_probe_time = time.time() - readiness_probe_start
                            logger.debug(f"[TIMING] pod {pod_id} readiness probe: {readiness_probe_time:.3f}s")
                            
                            # Evaluate result considering successThreshold and failureThreshold
                            # Also record in health check history
                            period_seconds = readiness_probe.get('periodSeconds', 10)
                            logger.info(f"🔵 [ASYNC] Evaluating readiness probe result for pod {pod_id} container {container_name} (periodSeconds={period_seconds}, probe_result={probe_result})")
                            logger.info(f"🔵 [ASYNC] Calling evaluate_probe_result with: redis_interface={redis_interface is not None}, pod_id={pod_id}, probe_type='readiness', container_name={container_name}")
                            final_status, updated_state = evaluate_probe_result(
                                probe_result, readiness_probe, readiness_state,
                                redis_interface=redis_interface, pod_id=pod_id,
                                probe_type='readiness', container_name=container_name
                            )
                            logger.info(f"🔵 [ASYNC] evaluate_probe_result returned: final_status={final_status}, updated_state keys={list(updated_state.keys())}")
                            
                            # Save updated state
                            save_probe_state(redis_interface, pod_id, 'readiness', container_name, updated_state)
                            readiness_state = updated_state  # Update for later use
                            
                            health_results['readiness'] = {
                                'status': final_status,
                                'last_check': datetime.now(timezone.utc).isoformat(),
                                'consecutive_failures': updated_state.get('consecutive_failures', 0),
                                'consecutive_successes': updated_state.get('consecutive_successes', 0),
                                'failure_threshold': readiness_probe.get('failureThreshold', 3),
                                'success_threshold': readiness_probe.get('successThreshold', 1)
                            }
                            
                            # If readiness just succeeded, mark it and store success time
                            if final_status == 'success':
                                readiness_succeeded = True
                                readiness_success_time = datetime.now(timezone.utc)
                                logger.info(f"Readiness probe succeeded for pod {pod_id} container {container_name}, will stop checking readiness and start liveness checks")
                        else:
                            # Not time to check yet, use existing state
                            health_results['readiness'] = {
                                'status': readiness_state.get('current_status', 'unknown'),
                                'last_check': readiness_state.get('last_check_time'),
                                'reason': reason,
                                'consecutive_failures': readiness_state.get('consecutive_failures', 0),
                                'consecutive_successes': readiness_state.get('consecutive_successes', 0)
                            }
            
            # Check liveness probe - ONLY if readiness has succeeded
            if health_checks and container_name in health_checks:
                liveness_probe = health_checks[container_name].get('livenessProbe')
                if liveness_probe:
                    if not readiness_succeeded:
                        # Readiness hasn't succeeded yet, don't check liveness
                        logger.debug(f"Liveness probe for pod {pod_id} container {container_name} waiting for readiness to succeed first")
                        health_results['liveness'] = {
                            'status': 'waiting_for_readiness',
                            'last_check': None,
                            'reason': 'Waiting for readiness probe to succeed before starting liveness checks',
                            'consecutive_failures': 0,
                            'consecutive_successes': 0
                        }
                    else:
                        # Readiness has succeeded, now check liveness
                        # Get probe state
                        liveness_state = get_probe_state(redis_interface, pod_id, 'liveness', container_name)
                        
                        # For liveness, use readiness success time as the "start time" for initialDelaySeconds
                        # This ensures liveness only starts after readiness succeeds + liveness initialDelaySeconds
                        # If we don't have readiness_success_time stored, try to get it from readiness_state
                        if not readiness_success_time and readiness_state:
                            readiness_success_time_str = readiness_state.get('last_check_time')
                            if readiness_success_time_str:
                                try:
                                    readiness_success_time = datetime.fromisoformat(readiness_success_time_str.replace('Z', '+00:00'))
                                except:
                                    pass
                        
                        # If we have readiness success time, use it; otherwise use pod creation time
                        # This ensures liveness starts checking after readiness succeeds + liveness initialDelaySeconds
                        liveness_start_time = readiness_success_time.isoformat() if readiness_success_time else (pod.get('creation_time') or pod.get('created_at'))
                        
                        # Check if we should run the probe (respects initialDelaySeconds from readiness success time and periodSeconds)
                        # Use Redis history as source of truth for last_check_time (persists across worker refreshes)
                        should_check, reason = await should_check_probe(
                            liveness_probe, 
                            liveness_state, 
                            liveness_start_time,
                            redis_interface=redis_interface,
                            pod_id=pod_id,
                            probe_type='liveness',
                            container_name=container_name
                        )
                        
                        if should_check:
                            # Perform the probe
                            liveness_probe_start = time.time()
                            probe_result = await perform_probe_async(liveness_probe, pod_ip, container_port, hostname, namespace, pod_id, container_name, session=session)
                            liveness_probe_time = time.time() - liveness_probe_start
                            logger.debug(f"[TIMING] pod {pod_id} liveness probe: {liveness_probe_time:.3f}s")
                            
                            # Evaluate result considering successThreshold and failureThreshold
                            # Also record in health check history
                            period_seconds = liveness_probe.get('periodSeconds', 10)
                            logger.info(f"🔵 [ASYNC] Evaluating liveness probe result for pod {pod_id} container {container_name} (periodSeconds={period_seconds}, probe_result={probe_result})")
                            logger.info(f"🔵 [ASYNC] Calling evaluate_probe_result with: redis_interface={redis_interface is not None}, pod_id={pod_id}, probe_type='liveness', container_name={container_name}")
                            final_status, updated_state = evaluate_probe_result(
                                probe_result, liveness_probe, liveness_state,
                                redis_interface=redis_interface, pod_id=pod_id,
                                probe_type='liveness', container_name=container_name
                            )
                            logger.info(f"🔵 [ASYNC] evaluate_probe_result returned: final_status={final_status}, updated_state keys={list(updated_state.keys())}")
                            
                            # Save updated state
                            save_probe_state(redis_interface, pod_id, 'liveness', container_name, updated_state)
                            
                            health_results['liveness'] = {
                                'status': final_status,
                                'last_check': datetime.now(timezone.utc).isoformat(),
                                'consecutive_failures': updated_state.get('consecutive_failures', 0),
                                'consecutive_successes': updated_state.get('consecutive_successes', 0),
                                'failure_threshold': liveness_probe.get('failureThreshold', 3),
                                'success_threshold': liveness_probe.get('successThreshold', 1)
                            }
                        else:
                            # Not time to check yet, use existing state
                            health_results['liveness'] = {
                                'status': liveness_state.get('current_status', 'waiting_for_readiness'),
                                'last_check': liveness_state.get('last_check_time'),
                                'reason': reason,
                                'consecutive_failures': liveness_state.get('consecutive_failures', 0),
                                'consecutive_successes': liveness_state.get('consecutive_successes', 0)
                            }
        
        # Update pod health status in Redis
        update_start = time.time()
        update_pod_health_status(host_pod_store, pod_id, health_results)
        update_time = time.time() - update_start
        
        total_pod_time = time.time() - pod_check_start
        logger.debug(f"[TIMING] pod {pod_id} check_pod_health_task TOTAL: {total_pod_time:.3f}s (pod_lookup={pod_lookup_time:.3f}s, deployment_lookup={deployment_lookup_time:.3f}s, update={update_time:.3f}s)")
        
        return health_results
        
    except Exception as e:
        logger.error(f"Health check failed for pod {pod_id}: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e),
            'pod_id': pod_id
        }
    finally:
        # Close aiohttp session only if we created it (not if it was provided)
        if not session_provided and session:
            await session.close()


@celery_app.task(base=AsyncTask, name="health.check_pod_health")
@log_to_file(logger)
async def check_pod_health_task(pod_id: str, hostname: str, namespace: str) -> Dict[str, Any]:
    """Celery task wrapper to check health of a single pod (ASYNC VERSION).
    
    This is the Celery task entry point that wraps _check_pod_health_async.
    Can be called via Celery (.delay() or .apply_async()) or directly with await.
    
    Args:
        pod_id: Pod ID
        hostname: Host where pod is running
        namespace: Pod namespace
        
    Returns:
        Dictionary with health check results
    """
    return await _check_pod_health_async(pod_id, hostname, namespace)


@celery_app.task(base=AsyncTask, name="health.check_all_pods_health")
@log_to_file(logger)
async def check_all_pods_health_task(max_concurrency: int = 100) -> Dict[str, Any]:
    """Check health of all running pods (ASYNC VERSION with non-blocking parallel execution).
    
    FOOL-PROOF DESIGN - This task is scheduled every 5 seconds for reliable checking:
    - Uses async/await with aiohttp for non-blocking HTTP probes
    - Uses asyncio.Semaphore for controlled concurrency (default: 100 parallel checks)
    - should_check_probe() enforces periodSeconds per probe (e.g., 10s for liveness)
    - If a check is overdue by >1.5x periodSeconds, catch-up mechanism always performs it
    - Pods with overdue checks are prioritized to prevent gaps
    - Multiple task executions are safe - should_check_probe() prevents duplicate checks
    - Tasks taking 15-25 seconds are fine - next beat (5s later) will catch up on overdue checks
    
    Args:
        max_concurrency: Maximum number of concurrent health checks (default: 100)
    
    Returns:
        Dictionary with summary of health checks
    """
    try:
        task_start_time = time.time()
        redis_interface = RedisInterface()
        host_pod_store = HostPodStore(redis_interface)
        deployment_store = DeploymentStore(redis_interface)
        
        # Get all pods (including NOTFOUND ones for cleanup)
        get_pods_start = time.time()
        all_hosts = host_pod_store.get_all_hosts()
        all_pods = []
        notfound_pods_for_cleanup = []
        
        for host in all_hosts:
            hostname = host.get('hostname')
            if not hostname:
                continue
            
            host_pods = host_pod_store.get_pods_by_host(hostname)
            for pod in host_pods:
                pod_status = pod.get('status', '').upper()
                if pod_status == 'RUNNING':
                    all_pods.append(pod)
                elif pod_status == 'NOTFOUND':
                    # Collect NOTFOUND pods for cleanup
                    notfound_pods_for_cleanup.append(pod)
        
        get_pods_time = time.time() - get_pods_start
        logger.info(f"[TIMING] get_all_pods: {get_pods_time:.3f}s ({len(all_pods)} RUNNING pods, {len(notfound_pods_for_cleanup)} NOTFOUND pods)")
        
        # Filter pods: only include RUNNING pods with health check configurations
        # Exclude STANDALONE pods and pods without deployments or health checks
        filter_start = time.time()
        deployment_cache: Dict[str, Dict[str, Any]] = {}
        pods_with_health_checks = []
        skipped_no_namespace = 0
        skipped_standalone = 0
        skipped_no_deployment = 0
        skipped_no_health_checks = 0
        
        for pod in all_pods:
            pod_id = pod.get('pod_id', 'unknown')
            pod_status = pod.get('status', '').upper()
            
            # Skip STANDALONE pods explicitly (should already be filtered by RUNNING check, but be explicit)
            if pod_status == 'STANDALONE':
                skipped_standalone += 1
                logger.debug(f"Skipping pod {pod_id}: status is STANDALONE (no health checks for standalone containers)")
                continue
            
            namespace = pod.get('namespace')
            if not namespace:
                skipped_no_namespace += 1
                logger.debug(f"Skipping pod {pod_id}: no namespace")
                continue
            
            # Get deployment info (with caching)
            deployment_name = pod.get('labels', {}).get('deployment_name') or pod.get('deployment_name')
            app_label = pod.get('labels', {}).get('app') or pod.get('labels', {}).get('app_label')
            
            cache_key = f"{namespace}:{deployment_name or app_label}"
            deployment = deployment_cache.get(cache_key)
            
            if deployment is None:
                if deployment_name:
                    deployment = deployment_store.get_deployment(deployment_name, namespace)
                elif app_label:
                    deployments_by_app = deployment_store.get_deployments_by_app(app_label)
                    for dep in deployments_by_app:
                        if dep.get('namespace') == namespace:
                            deployment = dep
                            break
                    if not deployment and deployments_by_app:
                        deployment = deployments_by_app[0]
                
                if deployment:
                    deployment_cache[cache_key] = deployment
            
            if not deployment:
                skipped_no_deployment += 1
                logger.debug(f"Skipping pod {pod_id}: no deployment found (namespace={namespace}, app_label={app_label}, deployment_name={deployment_name})")
                continue
            
            # Check if deployment has health check configurations
            deployment_spec = deployment.get('deployment_spec', {})
            health_checks = deployment_spec.get('health_checks', {})
            
            if not health_checks:
                skipped_no_health_checks += 1
                logger.debug(f"Skipping pod {pod_id}: deployment {deployment.get('name', 'unknown')} has no health check configurations")
                continue
            
            # Check if any container in this pod has health checks
            containers = pod.get('containers', [])
            has_health_check = False
            for container in containers:
                container_name = container.get('name')
                if container_name and container_name in health_checks:
                    container_health_checks = health_checks[container_name]
                    if container_health_checks.get('readinessProbe') or container_health_checks.get('livenessProbe'):
                        has_health_check = True
                        break
            
            if has_health_check:
                pods_with_health_checks.append(pod)
            else:
                skipped_no_health_checks += 1
                logger.debug(f"Skipping pod {pod_id}: no containers with health checks found")
        
        filter_time = time.time() - filter_start
        logger.info(
            f"[TIMING] filter_pods_with_health_checks: {filter_time:.3f}s "
            f"({len(pods_with_health_checks)}/{len(all_pods)} pods) - "
            f"skipped: {skipped_standalone} standalone, {skipped_no_namespace} no-namespace, "
            f"{skipped_no_deployment} no-deployment, {skipped_no_health_checks} no-health-checks"
        )
        
        # Update all_pods to only include pods with health checks
        all_pods = pods_with_health_checks
        
        # Get health check workers from cache (non-blocking read-only)
        # Worker list is updated every 60 seconds by a separate beat task
        worker_dist_start = time.time()
        current_worker_hostname = gethostname()
        
        # Read from cache (no blocking operations - just a simple variable read)
        health_check_workers = get_health_check_workers()
        
        # If only one worker, skip distribution entirely (check all pods on this worker)
        if len(health_check_workers) == 1:
            logger.debug(f"Single health check worker detected ({health_check_workers[0]}), skipping worker distribution")
            # Check all pods - no distribution needed
            assigned_pods = all_pods
            worker_dist_time = time.time() - worker_dist_start
            logger.info(f"[TIMING] worker_distribution (single worker, skipped): {worker_dist_time:.3f}s")
            logger.info(f"Single health check worker: {health_check_workers[0]}, checking all {len(assigned_pods)} pods")
        else:
            # Multiple workers - need distribution (but no blocking, just reading cached value)
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
        
        # Update all_pods to only include assigned pods
        all_pods = assigned_pods
        
        # FOOL-PROOF: Prioritize pods with overdue health checks
        # This ensures we catch up on missed checks first, preventing gaps
        now = datetime.now(timezone.utc)
        pods_with_overdue_checks = []
        pods_normal = []
        
        for pod in all_pods:
            pod_id = pod.get('pod_id')
            namespace = pod.get('namespace')
            if not pod_id or not namespace:
                continue
            
            # Check if this pod has overdue checks by looking at probe state
            try:
                # Get deployment to find probe configuration (use cache if available, otherwise look up)
                deployment_name = pod.get('labels', {}).get('deployment_name') or pod.get('deployment_name')
                app_label = pod.get('labels', {}).get('app') or pod.get('labels', {}).get('app_label')
                
                cache_key = f"{namespace}:{deployment_name or app_label}"
                deployment = deployment_cache.get(cache_key)
                
                # If not in cache, look it up (shouldn't happen often, but handle it)
                if not deployment:
                    if deployment_name:
                        deployment = deployment_store.get_deployment(deployment_name, namespace)
                    elif app_label:
                        deployments_by_app = deployment_store.get_deployments_by_app(app_label)
                        for dep in deployments_by_app:
                            if dep.get('namespace') == namespace:
                                deployment = dep
                                break
                        if not deployment and deployments_by_app:
                            deployment = deployments_by_app[0]
                    if deployment:
                        deployment_cache[cache_key] = deployment
                
                if deployment:
                    deployment_spec = deployment.get('deployment_spec', {})
                    health_checks = deployment_spec.get('health_checks', {})
                    containers = pod.get('containers', [])
                    
                    overdue = False
                    for container in containers:
                        container_name = container.get('name')
                        if container_name and container_name in health_checks:
                            container_health = health_checks[container_name]
                            # Check both readiness and liveness
                            for probe_type_key in ['readinessProbe', 'livenessProbe']:
                                probe_config = container_health.get(probe_type_key)
                                if probe_config:
                                    # Convert 'readinessProbe' to 'readiness', 'livenessProbe' to 'liveness'
                                    probe_type = probe_type_key.replace('Probe', '')
                                    probe_state = get_probe_state(redis_interface, pod_id, probe_type, container_name)
                                    period_seconds = probe_config.get('periodSeconds', 10)
                                    last_check = probe_state.get('last_check_time')
                                    if last_check:
                                        try:
                                            last_check_time = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                                            time_since = (now - last_check_time).total_seconds()
                                            # If overdue by >1.5x periodSeconds, prioritize this pod
                                            if time_since > 1.5 * period_seconds:
                                                overdue = True
                                                logger.debug(f"Pod {pod_id} has overdue {probe_type} check: {time_since:.1f}s since last check (expected {period_seconds}s)")
                                                break
                                        except Exception as e:
                                            logger.debug(f"Could not parse last check time for pod {pod_id}: {e}")
                                    # else: No previous check - might be first check (not overdue, but should check)
                            if overdue:
                                break
                    
                    if overdue:
                        pods_with_overdue_checks.append(pod)
                    else:
                        pods_normal.append(pod)
                else:
                    pods_normal.append(pod)
            except Exception as e:
                logger.debug(f"Could not determine if pod {pod_id} has overdue checks: {e}")
                pods_normal.append(pod)
        
        # Sort pods: overdue first, then normal (fool-proof prioritization)
        all_pods = pods_with_overdue_checks + pods_normal
        if pods_with_overdue_checks:
            logger.info(f"FOOL-PROOF: Found {len(pods_with_overdue_checks)} pod(s) with overdue health checks - prioritizing them")
        
        results = {
            'total_pods': len(all_pods),
            'checked': 0,
            'healthy': 0,
            'unhealthy': 0,
            'not_ready': 0,
            'errors': []
        }
        
        # Create a shared aiohttp session for all health checks (better performance)
        connector = aiohttp.TCPConnector(ssl=False, limit=max_concurrency)
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        shared_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        
        # Create semaphore for controlled parallel execution (NON-BLOCKING)
        # Must be created before the function that uses it
        semaphore = asyncio.Semaphore(max_concurrency)
        actual_concurrency = min(len(all_pods), max_concurrency)
        
        # ASYNC helper function to check a single pod's health with semaphore control
        # Note: semaphore is captured from outer scope (closure), not passed as parameter
        async def check_single_pod_async(pod):
            """Check health for a single pod asynchronously with concurrency control."""
            async with semaphore:
                pod_id = pod.get('pod_id')
                hostname = pod.get('hostname')
                namespace = pod.get('namespace')
                
                if not pod_id or not hostname or not namespace:
                    return None
                
                try:
                    # Call the internal async function directly (bypasses Celery task overhead)
                    # Pass shared session for better performance (connection pooling)
                    health_result = await _check_pod_health_async(pod_id, hostname, namespace, session=shared_session)
                    return {
                        'pod_id': pod_id,
                        'hostname': hostname,
                        'namespace': namespace,
                        'result': health_result
                    }
                except Exception as e:
                    logger.error(f"Failed to check health for pod {pod_id}: {e}", exc_info=True)
                    return {
                        'pod_id': pod_id,
                        'hostname': hostname,
                        'namespace': namespace,
                        'error': str(e)
                    }
        
        logger.info(f"Starting ASYNC parallel health checks for {len(all_pods)} pods with max_concurrency={max_concurrency} (using {actual_concurrency})")
        
        parallel_check_start = time.time()
        try:
            if len(all_pods) > 0:
                # Submit all health check tasks asynchronously
                submit_start = time.time()
                tasks = [check_single_pod_async(pod) for pod in all_pods]
                submit_time = time.time() - submit_start
                logger.info(f"[TIMING] submitted {len(tasks)} async health check tasks: {submit_time:.3f}s")
                
                # Collect results as they complete (non-blocking parallel execution)
                first_result_time = None
                for coro in asyncio.as_completed(tasks):
                    try:
                        check_result = await coro
                        
                        if first_result_time is None:
                            first_result_time = time.time()
                            first_result_delay = first_result_time - parallel_check_start
                            logger.info(f"[TIMING] first health check result received: {first_result_delay:.3f}s")
                        
                        if check_result is None:
                            continue
                        
                        results['checked'] += 1
                        pod_id = check_result.get('pod_id')
                        
                        if 'error' in check_result:
                            # Pod check failed (e.g., not found in Redis, no IP, etc.) - mark as unhealthy
                            error_msg = check_result.get('error', 'Unknown error')
                            logger.warning(f"Pod {pod_id} health check failed: {error_msg}")
                            results['unhealthy'] += 1
                            results['errors'].append({
                                'pod_id': pod_id,
                                'error': error_msg
                            })
                            # Track failed pods for recreation
                            if 'failed_pods' not in results:
                                results['failed_pods'] = []
                            results['failed_pods'].append({
                                'pod_id': pod_id,
                                'hostname': check_result.get('hostname'),
                                'namespace': check_result.get('namespace'),
                                'error': error_msg
                            })
                        else:
                            health_result = check_result.get('result', {})
                            
                            if health_result.get('status') == 'error':
                                # Pod not found or other error - mark as unhealthy
                                error_msg = health_result.get('error', 'Unknown error')
                                logger.warning(f"Pod {pod_id} health check error: {error_msg}")
                                results['unhealthy'] += 1
                                results['errors'].append({
                                    'pod_id': pod_id,
                                    'error': error_msg
                                })
                                # Track failed pods for recreation
                                if 'failed_pods' not in results:
                                    results['failed_pods'] = []
                                results['failed_pods'].append({
                                    'pod_id': pod_id,
                                    'hostname': health_result.get('hostname'),
                                    'namespace': health_result.get('namespace'),
                                    'health_result': health_result,
                                    'error': error_msg
                                })
                            else:
                                liveness = health_result.get('liveness', {}).get('status', 'unknown')
                                readiness = health_result.get('readiness', {}).get('status', 'unknown')
                                
                                if liveness == 'failed':
                                    results['unhealthy'] += 1
                                    # Track failed pods for recreation
                                    if 'failed_pods' not in results:
                                        results['failed_pods'] = []
                                    results['failed_pods'].append({
                                        'pod_id': pod_id,
                                        'hostname': health_result.get('hostname'),
                                        'namespace': health_result.get('namespace'),
                                        'health_result': health_result
                                    })
                                elif readiness == 'success' and liveness == 'success':
                                    results['healthy'] += 1
                                elif readiness != 'success':
                                    results['not_ready'] += 1
                    except Exception as e:
                        logger.error(f"Exception processing health check result: {e}", exc_info=True)
                        results['errors'].append({
                            'error': str(e)
                        })
            else:
                logger.info("No pods to check (all_pods is empty)")
            
            parallel_check_time = time.time() - parallel_check_start
            logger.info(f"[TIMING] parallel_health_checks (ASYNC): {parallel_check_time:.3f}s ({results['checked']} pods checked)")
            logger.info(f"Health check summary: {results['checked']} pods checked, {results['healthy']} healthy, {results['unhealthy']} unhealthy, {results['not_ready']} not ready")
        except Exception as parallel_error:
            logger.error(f"Error during parallel health checks: {parallel_error}", exc_info=True)
            parallel_check_time = time.time() - parallel_check_start
        
        # Handle NOTFOUND pods: terminate if host is up, remove from Redis if host is down
        notfound_cleanup_start = time.time()
        if notfound_pods_for_cleanup:
            logger.warning(f"Found {len(notfound_pods_for_cleanup)} NOTFOUND pods, handling cleanup")
            for pod in notfound_pods_for_cleanup:
                pod_id = pod.get('pod_id')
                hostname = pod.get('hostname')
                namespace = pod.get('namespace')
                
                if not pod_id or not hostname or not namespace:
                    continue
                
                # Check if host is online
                host_info = host_pod_store.get_host(hostname)
                host_status = host_info.get('status', 'unknown') if host_info else 'unknown'
                
                if host_status == 'online':
                    # Host is up, terminate the pod
                    logger.info(f"Terminating NOTFOUND pod {pod_id} on online host {hostname}")
                    try:
                        from utils.celery.queue_utils import create_host_queue_info, submit_celery_task
                        from utils.extensions.utilities_extention import UtilitiesExtension
                        from utils.ReadConfig import ReadConfig as rc
                        
                        read_config = rc()
                        key = read_config.encryption_config['key']
                        encode_util = UtilitiesExtension(key)
                        host_queue_info = create_host_queue_info(hostname, encode_util)
                        
                        # Use centralized scheduler function for pod termination
                        from server.sched.scheduler import DeploymentScheduler
                        try:
                            scheduler = DeploymentScheduler()
                            result = scheduler.terminate_pod_on_host(
                                pod_id=pod_id,
                                namespace=namespace,
                                hostname=hostname
                            )
                            if result.get('status') == 'submitted':
                                logger.info(f"Terminated NOTFOUND pod {pod_id} via scheduler (task_id: {result.get('task_id')})")
                            else:
                                logger.error(f"Failed to terminate NOTFOUND pod {pod_id}: {result.get('error')}")
                        except Exception as terminate_error:
                            logger.error(f"Failed to terminate NOTFOUND pod {pod_id} via scheduler: {terminate_error}", exc_info=True)
                    except Exception as e:
                        logger.error(f"Failed to terminate NOTFOUND pod {pod_id}: {e}", exc_info=True)
                else:
                    # Host is down, remove pod from Redis
                    logger.info(f"Removing NOTFOUND pod {pod_id} from Redis (host {hostname} is {host_status})")
                    try:
                        host_pod_store.delete_pod(pod_id)
                        logger.info(f"Removed NOTFOUND pod {pod_id} from Redis")
                    except Exception as e:
                        logger.error(f"Failed to remove NOTFOUND pod {pod_id} from Redis: {e}", exc_info=True)
        
        notfound_cleanup_time = time.time() - notfound_cleanup_start
        if notfound_pods_for_cleanup:
            logger.info(f"[TIMING] notfound_pod_cleanup: {notfound_cleanup_time:.3f}s ({len(notfound_pods_for_cleanup)} pods)")
        
        # Check if we need to trigger deployment recovery
        # Failed pods (liveness probe reached failureThreshold) will be handled by recovery/scaling tasks
        # Reasons: unhealthy pods detected OR we're below min_replicas for any deployment
        min_replicas_check_start = time.time()
        should_trigger_recovery = False
        recovery_reason = ""
        
        if results['unhealthy'] > 0 or results['not_ready'] > 0:
            should_trigger_recovery = True
            recovery_reason = f"Unhealthy pods detected (unhealthy: {results['unhealthy']}, not_ready: {results['not_ready']})"
        else:
            # Check if any deployment is below min_replicas
            # Count healthy pods per deployment and compare with min_replicas
            try:
                # Get all deployments to check min_replicas
                all_deployments = deployment_store.get_all_deployments()
                logger.info(f"Checking min_replicas for {len(all_deployments)} deployments")
                for deployment in all_deployments:
                    namespace = deployment.get('namespace')
                    app_label = deployment.get('app_label')
                    min_replicas = deployment.get('min_replicas', deployment.get('replicas', 1))
                    
                    if not namespace or not app_label:
                        continue
                    
                    # Count healthy pods for this deployment
                    # Get all pods for this app (including ones we didn't check due to worker distribution)
                    try:
                        app_pods = host_pod_store.get_pods_by_application(app_label)
                        # Filter pods by namespace and status
                        deployment_pods = [
                            pod for pod in app_pods
                            if pod.get('namespace') == namespace and
                            pod.get('status', '').upper() == 'RUNNING'
                        ]
                        
                        logger.debug(f"Deployment {namespace}/{deployment.get('name')}: found {len(deployment_pods)} RUNNING pods for app_label={app_label}")
                        
                        # Count healthy pods
                        # For pods with health checks: must have health_status == 'ready'
                        # For pods without health checks: count as healthy if they're RUNNING
                        now = datetime.now(timezone.utc)
                        validated_healthy_pods = []
                        validated_running_pods = []
                        
                        for pod in deployment_pods:
                            # Validate pod has containers
                            containers = pod.get('containers', [])
                            pause_container = pod.get('pause_container', {})
                            has_containers = containers or (pause_container and pause_container.get("pid"))
                            
                            if not has_containers:
                                continue
                            
                            # Check if pod is recent (updated within last 60 seconds)
                            last_updated_str = pod.get('last_updated')
                            is_recent = False
                            if last_updated_str:
                                try:
                                    last_updated = datetime.fromisoformat(last_updated_str.replace('Z', '+00:00'))
                                    time_since_update = (now - last_updated).total_seconds()
                                    if time_since_update <= 60:
                                        is_recent = True
                                except Exception:
                                    pass
                            
                            if not is_recent:
                                continue
                            
                            validated_running_pods.append(pod)
                            
                            # Check if pod has health checks configured
                            pod_has_health_checks_flag = pod_has_health_checks(pod, deployment_store)
                            
                            if pod_has_health_checks_flag:
                                # Pod has health checks - must have health_status == 'ready' to be considered healthy
                                health_status = pod.get('health_status')
                                if health_status == 'ready':
                                    validated_healthy_pods.append(pod)
                            else:
                                # Pod has no health checks - consider it healthy if it's RUNNING
                                validated_healthy_pods.append(pod)
                        
                        current_healthy_count = len(validated_healthy_pods)
                        total_running_count = len(validated_running_pods)
                        
                        logger.info(f"Deployment {namespace}/{deployment.get('name')}: total_running={total_running_count}, healthy={current_healthy_count}, min_replicas={min_replicas}")
                        
                        if current_healthy_count < min_replicas:
                            should_trigger_recovery = True
                            recovery_reason = f"Deployment {namespace}/{deployment.get('name')} below min_replicas (healthy: {current_healthy_count}, total_running: {total_running_count}, required: {min_replicas})"
                            logger.warning(f"{recovery_reason}, triggering recovery")
                            logger.info(f"Set should_trigger_recovery=True for deployment {namespace}/{deployment.get('name')}")
                            break
                    except Exception as e:
                        logger.debug(f"Failed to check pods for deployment {namespace}/{deployment.get('name')}: {e}")
                        continue
            except Exception as e:
                logger.warning(f"Failed to check min_replicas for deployments: {e}")
                # If we can't check, trigger recovery anyway if there are unhealthy pods
                if results['unhealthy'] > 0 or results['not_ready'] > 0:
                    should_trigger_recovery = True
                    recovery_reason = f"Unhealthy pods detected and min_replicas check failed"
        
        min_replicas_check_time = time.time() - min_replicas_check_start
        logger.info(f"[TIMING] min_replicas_check: {min_replicas_check_time:.3f}s")
        logger.info(f"should_trigger_recovery after min_replicas check: {should_trigger_recovery}")
        
        # Trigger deployment recovery if needed
        recovery_trigger_start = time.time()
        if should_trigger_recovery:
            logger.info(f"Triggering deployment recovery: {recovery_reason}")
            try:
                from utils.celery.queue_utils import create_queue_info
                from utils.extensions.utilities_extention import UtilitiesExtension
                from utils.ReadConfig import ReadConfig as rc
                from kombu import Exchange
                
                read_config = rc()
                key = read_config.encryption_config['key']
                encode_util = UtilitiesExtension(key)
                scheduler_queue_name = encode_util.encode_hostname_with_key('scheduler')
                secure_exchange = Exchange('secure_exchange', type='direct')
                
                # Use send_task directly to avoid circular imports
                try:
                    async_result = celery_app.send_task(
                        'deployment.recover_missing_replicas',
                        args=(),
                        kwargs={},
                        queue=scheduler_queue_name,
                        exchange=secure_exchange,
                        routing_key=scheduler_queue_name,
                        delivery_mode=2
                    )
                    logger.info(f"Triggered deployment recovery from health check (task_id: {async_result.id}, reason: {recovery_reason})")
                except Exception as send_task_error:
                    logger.error(f"Failed to trigger deployment recovery via send_task: {send_task_error}", exc_info=True)
            except Exception as e:
                logger.error(f"Failed to trigger deployment recovery: {e}", exc_info=True)
        
        recovery_trigger_time = time.time() - recovery_trigger_start
        if should_trigger_recovery:
            logger.info(f"[TIMING] recovery_trigger: {recovery_trigger_time:.3f}s")
        
        total_time = time.time() - task_start_time
        logger.info(f"[TIMING] check_all_pods_health_task TOTAL: {total_time:.3f}s (get_pods={get_pods_time:.3f}s, filter={filter_time:.3f}s, worker_dist={worker_dist_time:.3f}s, parallel_checks={parallel_check_time:.3f}s, notfound_cleanup={notfound_cleanup_time:.3f}s, min_replicas={min_replicas_check_time:.3f}s, recovery={recovery_trigger_time:.3f}s)")
        
        return results
        
    except Exception as e:
        logger.error(f"Failed to check all pods health: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e)
        }
    finally:
        # Close shared aiohttp session if it was created
        try:
            if 'shared_session' in locals() and shared_session:
                await shared_session.close()
                logger.debug("Closed shared aiohttp session")
        except Exception as session_error:
            logger.warning(f"Failed to close shared aiohttp session: {session_error}")
