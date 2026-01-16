"""
Deployment recovery tasks for automatic pod recreation on node crashes.

This module provides tasks to:
- Monitor deployments for missing replicas
- Detect node crashes
- Automatically recreate pods using distribute_nodes_services
"""
from typing import Dict, Any, List, Optional
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore, HostStatus
from utils.redis.deployment_store import DeploymentStore
from server.nodes.distribute_nodes_services import ClusterWorkerDistribution, get_worker_nodes_from_redis
from server.sched.scheduler import DeploymentScheduler
from utils.celery.queue_utils import create_host_queue_info, create_queue_info, submit_celery_task
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.ReadConfig import ReadConfig as rc
from celery.result import AsyncResult
from time import sleep
from utils.exceptions import AWSError

logger = LogKCld()


@celery_app.task(name="deployment.recover_missing_replicas")
@log_to_file(logger)
def recover_missing_replicas_task() -> Dict[str, Any]:
    """Recover missing replicas and scale down excess pods for all deployments.
    
    This task:
    1. Gets all deployments from Redis
    2. Counts running pods for each deployment
    3. Compares with min_replicas and max_replicas requirements
    4. Terminates excess pods if count exceeds max_replicas (priority)
    5. Creates missing pods if count is below min_replicas
    6. Uses distribute_nodes_services to find placement for missing pods
    
    Returns:
        Dictionary with recovery results including pods_terminated
    """
    try:
        logger.info("=" * 80)
        logger.info("DEPLOYMENT RECOVERY: Checking for missing replicas")
        logger.info("=" * 80)
        
        redis_interface = RedisInterface()
        deployment_store = DeploymentStore(redis_interface)
        host_pod_store = HostPodStore(redis_interface)
        redis_client = redis_interface.redis_client
        
        # Key pattern for tracking pods currently being terminated
        PODS_TERMINATING_KEY = "pods:terminating"
        # Key pattern for tracking pods currently being created
        PODS_CREATING_KEY = "pods:creating"
        # Key pattern for tracking AWS nodes currently being created (per namespace)
        AWS_NODES_CREATING_KEY_PREFIX = "aws_nodes:creating"
        # Key pattern for tracking when we first detected creating keys with no pods (for stale detection)
        STALE_CREATING_KEYS_TRACKER_PREFIX = "stale_creating_keys:tracker"
        
        # Get all deployments from Redis
        deployments = deployment_store.get_all_deployments()
        logger.info(f"Found {len(deployments)} deployments in Redis")
        
        recovery_results = {
            'deployments_checked': len(deployments),
            'deployments_recovered': 0,
            'pods_recreated': 0,
            'pods_terminated': 0,
            'errors': [],
        }
        
        for deployment in deployments:
            try:
                namespace = deployment.get('namespace')
                name = deployment.get('name')
                app_label = deployment.get('app_label')
                min_replicas = deployment.get('min_replicas', deployment.get('replicas', 1))
                max_replicas = deployment.get('max_replicas', deployment.get('replicas', 1))
                deployment_spec = deployment.get('deployment_spec', {})
                containers = deployment_spec.get('containers', [])
                resource_reqs = deployment_spec.get('resource_requirements', {})
                
                logger.info(f"Checking deployment {namespace}/{name} (app: {app_label}, min: {min_replicas}, max: {max_replicas})")
                
                # Count running pods for this deployment
                # Try to get pods by app label first, then filter by namespace
                try:
                    app_pods = host_pod_store.get_pods_by_application(app_label)
                    candidate_pods = [
                        pod for pod in app_pods
                        if pod.get('namespace') == namespace
                    ]
                except Exception as e:
                    logger.warning(f"Failed to get pods by app, trying by namespace: {e}")
                    # Fallback: get by namespace and filter by app label
                    all_pods = host_pod_store.get_pods_by_namespace(namespace)
                    candidate_pods = [
                        pod for pod in all_pods
                        if pod.get('labels', {}).get('app') == app_label or pod.get('app_label') == app_label
                    ]
                    # If no label matching, try all pods in namespace (will filter later)
                    if not candidate_pods:
                        candidate_pods = all_pods
                
                # Separate pods by status: RUNNING vs UNKNOWN/other
                # First, check for UNKNOWN pods to terminate them
                unknown_pods = [
                    pod for pod in candidate_pods
                    if pod.get('status', '').upper() in ('UNKNOWN', 'UNKOWN')  # Handle typo too
                ]
                
                pods_terminated = 0
                
                # Priority 1: Terminate UNKNOWN pods first (they should always be cleaned up)
                if unknown_pods:
                    logger.warning(f"Deployment {namespace}/{name}: Found {len(unknown_pods)} pods in UNKNOWN state")
                    logger.info(f"UNKNOWN pod IDs: {[p.get('pod_id') for p in unknown_pods[:10]]}{'...' if len(unknown_pods) > 10 else ''} (showing first 10)")
                    
                    # Get list of pods currently being terminated to avoid duplicate attempts
                    try:
                        terminating_pod_ids = redis_client.smembers(PODS_TERMINATING_KEY)
                        terminating_pod_ids = {pid.decode('utf-8') if isinstance(pid, bytes) else pid for pid in terminating_pod_ids}
                        logger.info(f"Found {len(terminating_pod_ids)} pods already in terminating set")
                    except Exception as e:
                        logger.warning(f"Failed to check terminating pods: {e}", exc_info=True)
                        terminating_pod_ids = set()
                    
                    logger.info(f"Processing {len(unknown_pods)} UNKNOWN pods for cleanup")
                    
                    # Check host status to determine if hosts are down
                    # Host sync task runs every 30 seconds, so a host is considered down if:
                    # - status is not 'online' OR
                    # - last_updated is > 90 seconds ago (3 missed sync cycles)
                    from datetime import datetime, timezone, timedelta
                    now = datetime.now(timezone.utc)
                    HOST_DOWN_THRESHOLD = 90.0  # seconds
                    
                    # Use centralized scheduler function for pod termination
                    scheduler = DeploymentScheduler()
                    for pod in unknown_pods:
                        pod_id = pod.get("pod_id")
                        hostname = pod.get("hostname")
                        
                        if not pod_id or not hostname:
                            logger.warning(f"Skipping UNKNOWN pod with missing pod_id or hostname: {pod}")
                            continue
                        
                        # Skip if already being terminated
                        if pod_id in terminating_pod_ids:
                            logger.debug(f"Skipping UNKNOWN pod {pod_id}: already being terminated")
                            # Remove from candidate_pods so it's not counted
                            candidate_pods = [p for p in candidate_pods if p.get('pod_id') != pod_id]
                            continue
                        
                        # Check if host is down by checking host status and last_updated
                        host_is_down = False
                        host_info = host_pod_store.get_host(hostname)
                        if host_info:
                            host_status = host_info.get('status', '').lower()
                            host_last_updated_str = host_info.get('last_updated')
                            
                            if host_status != 'online':
                                host_is_down = True
                                logger.info(f"Host {hostname} status is '{host_status}', not 'online' - marking as down")
                            elif host_last_updated_str:
                                try:
                                    host_last_updated = datetime.fromisoformat(host_last_updated_str.replace('Z', '+00:00'))
                                    time_since_host_update = (now - host_last_updated).total_seconds()
                                    if time_since_host_update > HOST_DOWN_THRESHOLD:
                                        host_is_down = True
                                        logger.info(f"Host {hostname} last updated {time_since_host_update:.1f}s ago (>90s), host appears to be down")
                                except Exception as e:
                                    logger.warning(f"Failed to parse host last_updated for {hostname}: {e}")
                                    # If we can't parse, assume host might be down
                                    host_is_down = True
                            else:
                                # No last_updated, assume host might be down
                                host_is_down = True
                                logger.info(f"Host {hostname} has no last_updated timestamp - marking as down")
                        else:
                            # Host not found in Redis, consider it down
                            host_is_down = True
                            logger.info(f"Host {hostname} not found in Redis - marking as down")
                        
                        # If host is down, remove pod directly from Redis
                        if host_is_down:
                            logger.warning(f"UNKNOWN pod {pod_id} on down host {hostname}: removing directly from Redis")
                            try:
                                host_pod_store.delete_pod(pod_id)
                                pods_terminated += 1
                                logger.info(f"Removed UNKNOWN pod {pod_id} from Redis (host {hostname} is down)")
                                # Remove from candidate_pods so it's not counted
                                candidate_pods = [p for p in candidate_pods if p.get('pod_id') != pod_id]
                                continue
                            except Exception as e:
                                logger.error(f"Failed to remove UNKNOWN pod {pod_id} from Redis: {e}", exc_info=True)
                                continue
                        
                        # For UNKNOWN pods, even if host appears online, try to remove them directly
                        # UNKNOWN state usually means the pod is in a bad state and should be cleaned up
                        logger.warning(f"UNKNOWN pod {pod_id} on host {hostname} (host appears online): removing directly from Redis (UNKNOWN pods should be cleaned up)")
                        try:
                            host_pod_store.delete_pod(pod_id)
                            pods_terminated += 1
                            logger.info(f"Removed UNKNOWN pod {pod_id} from Redis (UNKNOWN pods are cleaned up directly)")
                            # Remove from candidate_pods so it's not counted
                            candidate_pods = [p for p in candidate_pods if p.get('pod_id') != pod_id]
                            continue
                        except Exception as e:
                            logger.error(f"Failed to remove UNKNOWN pod {pod_id} from Redis: {e}", exc_info=True)
                            # Continue to termination attempt as fallback
                        
                        try:
                            result = scheduler.terminate_pod_on_host(
                                pod_id=pod_id,
                                namespace=namespace,
                                hostname=hostname
                            )
                            
                            if result.get('status') == 'submitted':
                                pods_terminated += 1
                                logger.info(f"Terminated UNKNOWN pod {pod_id} (task_id: {result.get('task_id')})")
                                # Mark pod as being terminated to prevent recovery task from trying again
                                try:
                                    redis_client.sadd(PODS_TERMINATING_KEY, pod_id)
                                    redis_client.expire(PODS_TERMINATING_KEY, 600)  # 10 minutes TTL
                                    logger.debug(f"Marked UNKNOWN pod {pod_id} as terminating")
                                except Exception as mark_error:
                                    logger.warning(f"Failed to mark UNKNOWN pod {pod_id} as terminating: {mark_error}")
                                # Remove from candidate_pods so it's not counted
                                candidate_pods = [p for p in candidate_pods if p.get('pod_id') != pod_id]
                            else:
                                logger.warning(f"Failed to terminate UNKNOWN pod {pod_id}: {result.get('error', 'Unknown error')}")
                        except Exception as e:
                            logger.error(f"Failed to terminate UNKNOWN pod {pod_id}: {e}", exc_info=True)
                    
                    logger.info(f"Terminated {pods_terminated} UNKNOWN pods")
                    recovery_results['pods_terminated'] = recovery_results.get('pods_terminated', 0) + pods_terminated
                
                # Now filter to only RUNNING pods for validation
                running_candidate_pods = [
                    pod for pod in candidate_pods
                    if pod.get('status', '').upper() == 'RUNNING'
                ]
                
                # Validate pods: filter out stale pods and pods on down hosts
                # IMPORTANT: Also check if the host is still online
                from datetime import datetime, timezone, timedelta
                validated_pods = []
                pods_on_down_hosts = []  # Track pods on down hosts for termination
                now = datetime.now(timezone.utc)
                
                # Host sync task runs every 30 seconds, so a host is considered down if:
                # - last_updated is > 90 seconds ago (3 missed sync cycles)
                HOST_DOWN_THRESHOLD = 60.0  # seconds
                
                for pod in running_candidate_pods:
                    # Check if pod has valid container information
                    containers = pod.get('containers', [])
                    pause_container = pod.get('pause_container', {})
                    has_containers = containers or (pause_container and pause_container.get("pid"))
                    
                    if not has_containers:
                        logger.debug(f"Pod {pod.get('pod_id')} excluded: no valid containers")
                        continue
                    
                    # Check if pod's host is still online
                    pod_hostname = pod.get('hostname')
                    host_is_online = False
                    if pod_hostname:
                        host_info = host_pod_store.get_host(pod_hostname)
                        if host_info:
                            host_status = host_info.get('status', '').lower()
                            host_last_updated_str = host_info.get('last_updated')
                            
                            if host_status == 'online' and host_last_updated_str:
                                try:
                                    host_last_updated = datetime.fromisoformat(host_last_updated_str.replace('Z', '+00:00'))
                                    time_since_host_update = (now - host_last_updated).total_seconds()
                                    if time_since_host_update <= HOST_DOWN_THRESHOLD:
                                        host_is_online = True
                                    else:
                                        logger.warning(f"Pod {pod.get('pod_id')} on host {pod_hostname}: host last updated {time_since_host_update:.1f}s ago (>90s), host appears to be down")
                                except Exception as e:
                                    logger.warning(f"Failed to parse host last_updated for {pod_hostname}: {e}")
                            else:
                                logger.debug(f"Pod {pod.get('pod_id')} on host {pod_hostname}: host status is '{host_status}' or missing last_updated")
                        else:
                            logger.warning(f"Pod {pod.get('pod_id')} on host {pod_hostname}: host info not found in Redis, host appears to be down")
                    
                    # If host is down, mark pod for termination
                    if not host_is_online:
                        pods_on_down_hosts.append(pod)
                        logger.warning(f"Pod {pod.get('pod_id')} on down host {pod_hostname} will be terminated and recreated")
                        continue
                    
                    # Check if pod was updated recently
                    # For newly created pods, they might not have last_updated yet or it might be very recent
                    # Use a longer window (5 minutes) to account for pod sync delays
                    last_updated_str = pod.get('last_updated')
                    pod_status = pod.get('status', '').upper()
                    
                    # For CREATED/PENDING pods, be more lenient (they're being created)
                    # These pods will be counted in pending_pods and we'll wait for them
                    if pod_status in ('CREATED', 'PENDING', 'RESTARTING'):
                        # These pods are in transition, accept them even without last_updated
                        # They'll be counted in pending_pods and we'll wait for them
                        pass  # Don't exclude based on last_updated for pending pods
                    elif last_updated_str:
                        try:
                            last_updated = datetime.fromisoformat(last_updated_str.replace('Z', '+00:00'))
                            time_since_update = (now - last_updated).total_seconds()
                            # Use 5 minutes (300s) instead of 60s to account for sync delays
                            if time_since_update > 300:
                                logger.debug(
                                    f"Pod {pod.get('pod_id')} excluded: last updated {time_since_update:.1f}s ago (>300s)"
                                )
                                continue
                        except Exception as e:
                            logger.warning(f"Failed to parse last_updated for pod {pod.get('pod_id')}: {e}")
                            # If we can't parse, exclude it to be safe
                            continue
                    else:
                        # No last_updated timestamp - for RUNNING pods, this might indicate a stale entry
                        # But for CREATED/PENDING pods, this is normal (they're new)
                        if pod_status == 'RUNNING':
                            logger.debug(f"Pod {pod.get('pod_id')} excluded: RUNNING but no last_updated timestamp (likely stale)")
                            continue
                        # For CREATED/PENDING pods without last_updated, we'll include them (they're being created)
                    
                    validated_pods.append(pod)
                
                # Terminate pods on down hosts
                if pods_on_down_hosts:
                    logger.warning(f"Deployment {namespace}/{name}: Found {len(pods_on_down_hosts)} pods on down hosts, terminating them")
                    scheduler = DeploymentScheduler()
                    for pod in pods_on_down_hosts:
                        pod_id = pod.get("pod_id")
                        hostname = pod.get("hostname")
                        
                        if not pod_id or not hostname:
                            logger.warning(f"Skipping pod on down host with missing pod_id or hostname: {pod}")
                            continue
                        
                        try:
                            # Try to terminate the pod (even though host is down, we try to clean up)
                            result = scheduler.terminate_pod_on_host(
                                pod_id=pod_id,
                                namespace=namespace,
                                hostname=hostname
                            )
                            
                            if result.get('status') == 'submitted':
                                pods_terminated += 1
                                logger.info(f"Terminated pod {pod_id} on down host {hostname} (task_id: {result.get('task_id')})")
                                # Mark pod as being terminated to prevent recovery task from trying again
                                try:
                                    redis_client.sadd(PODS_TERMINATING_KEY, pod_id)
                                    redis_client.expire(PODS_TERMINATING_KEY, 300)  # 5 minutes TTL
                                    logger.debug(f"Marked pod {pod_id} on down host as terminating")
                                except Exception as mark_error:
                                    logger.warning(f"Failed to mark pod {pod_id} as terminating: {mark_error}")
                            else:
                                logger.warning(f"Failed to terminate pod {pod_id} on down host {hostname}: {result.get('error', 'Unknown error')}. Will remove from Redis.")
                            
                            # Remove pod from Redis since host is down (cleanup)
                            try:
                                host_pod_store.delete_pod(pod_id)
                                # Also remove from terminating set since we're removing it from Redis
                                redis_client.srem(PODS_TERMINATING_KEY, pod_id)
                                logger.info(f"Removed pod {pod_id} from Redis (host {hostname} is down)")
                            except Exception as e:
                                logger.warning(f"Failed to remove pod {pod_id} from Redis: {e}")
                                
                        except Exception as e:
                            logger.error(f"Failed to terminate pod {pod_id} on down host {hostname}: {e}", exc_info=True)
                            # Still try to remove from Redis
                            try:
                                host_pod_store.delete_pod(pod_id)
                                # Also remove from terminating set since we're removing it from Redis
                                redis_client.srem(PODS_TERMINATING_KEY, pod_id)
                                logger.info(f"Removed pod {pod_id} from Redis after termination error (host {hostname} is down)")
                            except Exception as cleanup_error:
                                logger.warning(f"Failed to remove pod {pod_id} from Redis: {cleanup_error}")
                    
                    recovery_results['pods_terminated'] = recovery_results.get('pods_terminated', 0) + pods_terminated
                    logger.info(f"Terminated {pods_terminated} pods on down hosts. These pods will be recreated on other nodes.")
                
                running_pods = validated_pods
                current_replicas = len(running_pods)
                logger.info(f"Deployment {namespace}/{name}: {current_replicas} running pods (min: {min_replicas}, max: {max_replicas})")
                
                pods_terminated = 0
                
                # Priority 1: Terminate UNKNOWN pods first (they should always be cleaned up)
                unknown_pods = [
                    pod for pod in validated_pods
                    if pod.get('status', '').upper() in ('UNKNOWN', 'UNKOWN')  # Handle typo too
                ]
                if unknown_pods:
                    logger.warning(f"Deployment {namespace}/{name}: Found {len(unknown_pods)} pods in UNKNOWN state, terminating them")
                    logger.info(f"UNKNOWN pod IDs: {[p.get('pod_id') for p in unknown_pods]}")
                    
                    # Use centralized scheduler function for pod termination
                    scheduler = DeploymentScheduler()
                    for pod in unknown_pods:
                        pod_id = pod.get("pod_id")
                        hostname = pod.get("hostname")
                        
                        if not pod_id or not hostname:
                            logger.warning(f"Skipping UNKNOWN pod with missing pod_id or hostname: {pod}")
                            continue
                        
                        try:
                            result = scheduler.terminate_pod_on_host(
                                pod_id=pod_id,
                                namespace=namespace,
                                hostname=hostname
                            )
                            
                            if result.get('status') == 'submitted':
                                pods_terminated += 1
                                logger.info(f"Terminated UNKNOWN pod {pod_id} (task_id: {result.get('task_id')})")
                                # Remove from running_pods list so it's not counted
                                running_pods = [p for p in running_pods if p.get('pod_id') != pod_id]
                            else:
                                logger.warning(f"Failed to terminate UNKNOWN pod {pod_id}: {result.get('error', 'Unknown error')}")
                        except Exception as e:
                            logger.error(f"Failed to terminate UNKNOWN pod {pod_id}: {e}", exc_info=True)
                    
                    logger.info(f"Terminated {pods_terminated} UNKNOWN pods")
                    # Update current_replicas after removing UNKNOWN pods
                    current_replicas = len(running_pods)
                    recovery_results['pods_terminated'] = recovery_results.get('pods_terminated', 0) + pods_terminated
                
                # Priority 2: Scale down if above max_replicas (this takes precedence over scaling up)
                # Then scale up if below min_replicas
                # This prevents creating pods when we should be terminating them
                if current_replicas > max_replicas:
                    excess_replicas = current_replicas - max_replicas
                    logger.warning(f"Deployment {namespace}/{name}: {current_replicas} running pods exceeds max_replicas={max_replicas}, need to terminate {excess_replicas} pods")
                    logger.info(f"Current running pod IDs: {[p.get('pod_id') for p in running_pods]}")
                    
                    # Filter out pods that are currently being terminated
                    # This prevents the recovery task from trying to terminate the same pods repeatedly
                    try:
                        terminating_pod_ids = redis_client.smembers(PODS_TERMINATING_KEY)
                        terminating_pod_ids = {pid.decode('utf-8') if isinstance(pid, bytes) else pid for pid in terminating_pod_ids}
                        if terminating_pod_ids:
                            logger.debug(f"Deployment {namespace}/{name}: Found {len(terminating_pod_ids)} pods already being terminated: {terminating_pod_ids}")
                            running_pods = [p for p in running_pods if p.get('pod_id') not in terminating_pod_ids]
                            current_replicas = len(running_pods)
                            logger.info(f"Deployment {namespace}/{name}: After filtering terminating pods: {current_replicas} running pods")
                    except Exception as e:
                        logger.warning(f"Failed to check terminating pods: {e}", exc_info=True)
                    
                    # Re-check if we still need to terminate after filtering
                    if current_replicas <= max_replicas:
                        logger.info(f"Deployment {namespace}/{name}: No excess pods to terminate after filtering terminating pods")
                        continue
                    
                    excess_replicas = current_replicas - max_replicas
                    
                    # Terminate excess pods while preserving redundancy across nodes
                    # Strategy: Group pods by hostname, then terminate to maintain distribution
                    pods_by_host = {}
                    for pod in running_pods:
                        hostname = pod.get("hostname")
                        if hostname:
                            if hostname not in pods_by_host:
                                pods_by_host[hostname] = []
                            pods_by_host[hostname].append(pod)
                    
                    # Sort pods within each host by creation_time (oldest first)
                    for hostname in pods_by_host:
                        pods_by_host[hostname].sort(key=lambda p: p.get("creation_time", ""))
                    
                    # Select pods to terminate: prioritize removing from nodes with more pods
                    # This helps maintain distribution across nodes
                    pods_to_terminate = []
                    remaining_to_terminate = excess_replicas
                    
                    # First pass: Try to balance by removing from nodes with most pods
                    while remaining_to_terminate > 0 and pods_by_host:
                        # Find node with most pods
                        max_host = max(pods_by_host.keys(), key=lambda h: len(pods_by_host[h]))
                        if pods_by_host[max_host]:
                            pod = pods_by_host[max_host].pop(0)  # Remove oldest from this node
                            pods_to_terminate.append(pod)
                            remaining_to_terminate -= 1
                            # Remove host from dict if no more pods
                            if not pods_by_host[max_host]:
                                del pods_by_host[max_host]
                        else:
                            break
                    
                    # If still need to terminate more, continue with remaining pods
                    if remaining_to_terminate > 0:
                        remaining_pods = []
                        for host_pods in pods_by_host.values():
                            remaining_pods.extend(host_pods)
                        remaining_pods.sort(key=lambda p: p.get("creation_time", ""))
                        pods_to_terminate.extend(remaining_pods[:remaining_to_terminate])
                    
                    logger.info(f"Selected {len(pods_to_terminate)} pods to terminate: {[(p.get('pod_id'), p.get('hostname')) for p in pods_to_terminate]}")
                    
                    # Use centralized scheduler function for pod termination
                    scheduler = DeploymentScheduler()
                    for pod in pods_to_terminate:
                        pod_id = pod.get("pod_id")
                        hostname = pod.get("hostname")
                        
                        if not pod_id or not hostname:
                            continue
                        
                        try:
                            result = scheduler.terminate_pod_on_host(
                                pod_id=pod_id,
                                namespace=namespace,
                                hostname=hostname
                            )
                            
                            if result.get('status') == 'submitted':
                                pods_terminated += 1
                                logger.info(f"Terminated pod {pod_id} for scaling down (task_id: {result.get('task_id')})")
                                # Mark pod as being terminated to prevent recovery task from trying again
                                # TTL of 10 minutes (600 seconds) to auto-cleanup if termination task fails silently
                                try:
                                    redis_client.sadd(PODS_TERMINATING_KEY, pod_id)
                                    redis_client.expire(PODS_TERMINATING_KEY, 600)  # 10 minutes TTL on the set
                                    logger.debug(f"Marked pod {pod_id} as terminating")
                                except Exception as mark_error:
                                    logger.warning(f"Failed to mark pod {pod_id} as terminating: {mark_error}")
                            else:
                                logger.warning(f"Failed to terminate pod {pod_id}: {result.get('error', 'Unknown error')}")
                        except Exception as e:
                            logger.error(f"Failed to terminate pod {pod_id}: {e}", exc_info=True)
                    
                    logger.info(f"Scaling down complete: terminated {pods_terminated} pods (expected {excess_replicas})")
                    if pods_terminated != excess_replicas:
                        logger.warning(f"Pod termination count mismatch: terminated {pods_terminated} but expected {excess_replicas}")
                    
                    recovery_results['pods_terminated'] = recovery_results.get('pods_terminated', 0) + pods_terminated
                
                # Check if we need to recover pods (only if not scaling down)
                # IMPORTANT: Count ALL pods (RUNNING + PENDING + CREATED) to avoid creating too many
                # Only scale up if total pods < min_replicas AND total pods < max_replicas
                
                # Count all pods (not just RUNNING) - includes PENDING, CREATED, etc.
                # IMPORTANT: Use candidate_pods but exclude pods that were just terminated or are on down hosts
                # Also count pods currently being created to avoid duplicate creation attempts
                try:
                    creating_pod_keys = redis_client.smembers(PODS_CREATING_KEY)
                    creating_pod_keys = {key.decode('utf-8') if isinstance(key, bytes) else key for key in creating_pod_keys}
                    # Filter to only keys for this deployment (format: namespace:name:instance)
                    creating_pod_keys_for_deployment = {key for key in creating_pod_keys if key.startswith(f"{namespace}:{name}:")}
                    
                    # Clean up: Remove keys for pods that have already appeared in Redis
                    # Extract instance numbers from creating keys and build a map
                    creating_key_map = {}  # instance_str -> creating_key
                    for key in creating_pod_keys_for_deployment:
                        parts = key.split(':')
                        if len(parts) >= 3:
                            instance_str = parts[2]  # namespace:name:instance
                            creating_key_map[instance_str] = key
                    
                    # Check if any creating pods have already appeared in Redis
                    pods_to_remove_from_creating = []
                    existing_instance_set = set()
                    for pod in candidate_pods:
                        # Try multiple ways to get instance number
                        pod_instance = (
                            pod.get('labels', {}).get('instance') or 
                            pod.get('instance') or
                            pod.get('replica_num')
                        )
                        if pod_instance:
                            instance_str = str(pod_instance)
                            existing_instance_set.add(instance_str)
                            # If this instance is in the creating set, mark it for removal
                            if instance_str in creating_key_map:
                                creating_key = creating_key_map[instance_str]
                                pods_to_remove_from_creating.append(creating_key)
                    
                    # Also clean up stale entries: Only remove creating keys if we have RUNNING pods that match
                    # DO NOT remove creating keys just because we have enough pods - pods might still be syncing to Redis
                    # Only remove creating keys when:
                    # 1. The pod actually appears in Redis with matching instance (handled above)
                    # 2. We have RUNNING pods >= min_replicas AND the creating keys are old (stale)
                    # For now, we only remove keys when pods appear in Redis (handled above)
                    # Creating keys will expire automatically via TTL (600s) if pods never appear
                    
                    # Remove pods that have appeared in Redis from the creating set
                    if pods_to_remove_from_creating:
                        try:
                            redis_client.srem(PODS_CREATING_KEY, *pods_to_remove_from_creating)
                            logger.debug(f"Deployment {namespace}/{name}: Cleaned up {len(pods_to_remove_from_creating)} pods from creating set (already in Redis or sufficient pods exist)")
                        except Exception as cleanup_error:
                            logger.warning(f"Failed to cleanup creating pods: {cleanup_error}")
                    
                    # Update the creating set after cleanup
                    creating_pod_keys_for_deployment = creating_pod_keys_for_deployment - set(pods_to_remove_from_creating)
                    
                    if creating_pod_keys_for_deployment:
                        logger.debug(f"Deployment {namespace}/{name}: Found {len(creating_pod_keys_for_deployment)} pods currently being created: {creating_pod_keys_for_deployment}")
                except Exception as e:
                    logger.warning(f"Failed to check creating pods: {e}", exc_info=True)
                    creating_pod_keys_for_deployment = set()
                
                # Count existing pods + in-flight pod creations for this deployment
                # IMPORTANT: Also count pods in CREATED/PENDING states as they're being created
                # These pods might not have last_updated yet or might be too new
                creating_state_pods = [
                    p for p in candidate_pods
                    if p.get('status', '').upper() in ('CREATED', 'PENDING', 'RESTARTING')
                ]
                # Count all pods including those being created (both in Redis and in-flight)
                all_pods_count = len(candidate_pods) + len(creating_pod_keys_for_deployment)
                
                # CRITICAL: Clean up stale creating keys if no pods exist
                # If we have creating keys but no candidate_pods, and we have zero running replicas,
                # the creating keys might be stale. However, pods can take 30-60 seconds to sync to Redis,
                # so we should wait a reasonable time before cleaning up.
                # 
                # Strategy: Track when we first detect creating keys with no pods
                # If this state persists for more than 90 seconds (3x the sync interval), clean up
                if (creating_pod_keys_for_deployment and 
                    len(candidate_pods) == 0 and 
                    current_replicas == 0 and 
                    all_pods_count < min_replicas):
                    # Track when we first detected this stale state
                    stale_tracker_key = f"{STALE_CREATING_KEYS_TRACKER_PREFIX}:{namespace}:{name}"
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)
                    
                    try:
                        # Check if we have a timestamp for when we first detected this state
                        tracker_timestamp_str = redis_client.get(stale_tracker_key)
                        if tracker_timestamp_str:
                            # We've seen this state before - check how long it's been
                            tracker_timestamp = datetime.fromisoformat(tracker_timestamp_str.decode('utf-8') if isinstance(tracker_timestamp_str, bytes) else tracker_timestamp_str)
                            time_since_first_detection = (now - tracker_timestamp).total_seconds()
                            
                            # STALE_KEY_TIMEOUT: If creating keys exist but no pods appear after 90 seconds,
                            # they're likely stale (pods should sync within 30-60 seconds)
                            STALE_KEY_TIMEOUT = 90  # seconds
                            
                            if time_since_first_detection > STALE_KEY_TIMEOUT:
                                # Keys are stale - clean them up
                                logger.warning(f"Deployment {namespace}/{name}: Found {len(creating_pod_keys_for_deployment)} creating keys but zero pods in Redis for {time_since_first_detection:.1f}s (>{STALE_KEY_TIMEOUT}s). These keys are stale - cleaning them up.")
                                stale_keys_list = list(creating_pod_keys_for_deployment)
                                redis_client.srem(PODS_CREATING_KEY, *stale_keys_list)
                                redis_client.delete(stale_tracker_key)  # Clear tracker
                                logger.info(f"Deployment {namespace}/{name}: Removed {len(stale_keys_list)} stale creating keys: {stale_keys_list}")
                                creating_pod_keys_for_deployment = set()
                                all_pods_count = len(candidate_pods)  # Update count after cleanup
                            else:
                                # Still waiting - pods might be syncing
                                logger.warning(f"Deployment {namespace}/{name}: Found {len(creating_pod_keys_for_deployment)} creating keys but zero pods in Redis (waiting {time_since_first_detection:.1f}s / {STALE_KEY_TIMEOUT}s). Pods may still be syncing.")
                        else:
                            # First time detecting this state - record timestamp
                            redis_client.setex(stale_tracker_key, 120, now.isoformat())  # 2 minute TTL
                            logger.warning(f"Deployment {namespace}/{name}: Found {len(creating_pod_keys_for_deployment)} creating keys but zero pods in Redis. Tracking timestamp - will clean up if no pods appear within 90 seconds.")
                    except Exception as tracker_error:
                        logger.warning(f"Failed to track/cleanup stale creating keys: {tracker_error}", exc_info=True)
                        # Fallback: if tracking fails and we're definitely below min_replicas, be more aggressive
                        # Only do this if we're significantly below min (e.g., 0 vs min_replicas)
                        if current_replicas == 0 and all_pods_count == 0:
                            logger.warning(f"Deployment {namespace}/{name}: Tracking failed, but have 0 pods and need {min_replicas}. Cleaning up creating keys as fallback.")
                            try:
                                stale_keys_list = list(creating_pod_keys_for_deployment)
                                redis_client.srem(PODS_CREATING_KEY, *stale_keys_list)
                                logger.info(f"Deployment {namespace}/{name}: Removed {len(stale_keys_list)} creating keys (fallback cleanup): {stale_keys_list}")
                                creating_pod_keys_for_deployment = set()
                                all_pods_count = len(candidate_pods)
                            except Exception as cleanup_error:
                                logger.warning(f"Failed to cleanup creating keys (fallback): {cleanup_error}")
                else:
                    # We have pods or creating keys are valid - clear any stale tracker
                    stale_tracker_key = f"{STALE_CREATING_KEYS_TRACKER_PREFIX}:{namespace}:{name}"
                    try:
                        redis_client.delete(stale_tracker_key)
                    except Exception:
                        pass
                
                # Log detailed pod state for debugging
                logger.warning(f"Deployment {namespace}/{name}: Pod state breakdown - Total: {all_pods_count} (candidate_pods: {len(candidate_pods)}, creating_keys: {len(creating_pod_keys_for_deployment)}, creating_state: {len(creating_state_pods)}), min: {min_replicas}, max: {max_replicas}, current_replicas: {current_replicas}")
                if creating_pod_keys_for_deployment:
                    logger.warning(f"Deployment {namespace}/{name}: Pods marked as creating: {creating_pod_keys_for_deployment}")
                else:
                    logger.warning(f"Deployment {namespace}/{name}: No pods marked as creating (checking if pods were just created)")
                
                # Get health check configuration to check readiness wait period
                # Check for initialDelaySeconds in readinessProbe (default: 5 seconds)
                readiness_initial_delay = 5  # Default 5 seconds
                deployment_spec = deployment.get('deployment_spec', {})
                containers = deployment_spec.get('containers', [])
                if containers:
                    # Get readinessProbe initialDelaySeconds from first container (if available)
                    first_container = containers[0] if containers else {}
                    readiness_probe = first_container.get('readinessProbe', {})
                    if readiness_probe:
                        readiness_initial_delay = readiness_probe.get('initialDelaySeconds', 5)
                
                # IMPORTANT: Count ALL pods including CREATED/PENDING states
                # These pods are being created but may not be in Redis yet
                # We need to count them to prevent creating too many pods
                
                # Count pending/unready pods (non-RUNNING pods in transition states)
                # We wait for pods in CREATED, PENDING, RESTARTING states
                # But NOT for FAILED, STOPPED, UNKNOWN (these are final/problem states)
                pending_pods = [
                    p for p in candidate_pods
                    if p.get('status', '').upper() in ('CREATED', 'PENDING', 'RESTARTING')
                ]
                pending_count = len(pending_pods)
                
                # Count pods that are in readiness wait period (RUNNING but started < initialDelaySeconds ago)
                # These pods have startup_time but are still within initialDelaySeconds
                # We should wait for these pods to become ready before creating more
                from datetime import datetime, timezone, timedelta
                now = datetime.now(timezone.utc)
                pods_in_readiness_wait = []
                for pod in candidate_pods:
                    pod_status = pod.get('status', '').upper()
                    # Only check RUNNING pods (pods that have started)
                    if pod_status == 'RUNNING':
                        startup_time_str = pod.get('startup_time') or pod.get('creation_time')
                        if startup_time_str:
                            try:
                                startup_time = datetime.fromisoformat(startup_time_str.replace('Z', '+00:00'))
                                time_since_startup = (now - startup_time).total_seconds()
                                # If pod started less than initialDelaySeconds ago, it's still in readiness wait
                                if time_since_startup < readiness_initial_delay:
                                    pods_in_readiness_wait.append(pod)
                            except Exception as e:
                                logger.debug(f"Failed to parse startup_time for pod {pod.get('pod_id')}: {e}")
                
                # Count pods in readiness wait period (started but not yet ready)
                readiness_wait_count = len(pods_in_readiness_wait)
                
                # Count pods in final/problem states (these shouldn't prevent scaling)
                failed_stopped_pods = [
                    p for p in candidate_pods
                    if p.get('status', '').upper() in ('FAILED', 'STOPPED', 'UNKNOWN')
                ]
                failed_stopped_count = len(failed_stopped_pods)
                
                # Calculate non-running pods (all pods that aren't RUNNING)
                non_running_count = all_pods_count - current_replicas
                
                # CRITICAL SAFETY CHECK: If we already have enough pods (including in-flight), don't create more
                if all_pods_count >= max_replicas:
                    logger.warning(f"Deployment {namespace}/{name}: Already at or above max_replicas (all_pods_count={all_pods_count} >= max={max_replicas}). Skipping pod creation.")
                    continue
                
                # Log why we might not be creating pods
                if all_pods_count >= min_replicas:
                    logger.warning(f"Deployment {namespace}/{name}: Already at or above min_replicas (all_pods_count={all_pods_count} >= min={min_replicas}). No pods to create.")
                    continue
                
                # Only scale up if we're below min_replicas AND below max_replicas
                if all_pods_count < min_replicas and all_pods_count < max_replicas:
                    # Calculate how many pods we can create without exceeding max_replicas
                    # IMPORTANT: Never create more than max_replicas total
                    # Calculate pods needed to reach min_replicas, but cap at max_replicas
                    pods_needed_for_min = min_replicas - all_pods_count
                    max_pods_we_can_create = max_replicas - all_pods_count
                    
                    # Create only what's needed (up to min_replicas), but never exceed max_replicas
                    pods_to_create = min(pods_needed_for_min, max_pods_we_can_create)
                    
                    # CRITICAL SAFETY CHECK: If we're already creating pods, don't create more
                    # This prevents race conditions where multiple tasks try to create pods
                    # Check both: pods marked as creating AND pods in CREATED/PENDING states
                    total_creating = len(creating_pod_keys_for_deployment) + len(creating_state_pods)
                    if total_creating > 0:
                        # We're already creating pods, wait for them to finish
                        logger.info(f"Deployment {namespace}/{name}: Already creating {total_creating} pods (creating_keys: {len(creating_pod_keys_for_deployment)}, creating_state: {len(creating_state_pods)}). Waiting for them to complete before creating more. (all_pods_count={all_pods_count})")
                        continue
                    
                    # Safety check: never create negative or zero pods
                    if pods_to_create <= 0:
                        logger.info(f"Deployment {namespace}/{name}: No pods to create (all_pods_count={all_pods_count}, min={min_replicas}, max={max_replicas})")
                        continue
                    
                    # IMPORTANT: If there are pending/unready pods or pods in readiness wait period, wait for them
                    # This prevents creating pods while others are still starting up
                    # We check for:
                    # 1. Pending pods (CREATED/PENDING/RESTARTING states)
                    # 2. Pods in readiness wait period (RUNNING but started < initialDelaySeconds ago)
                    # But we don't wait for FAILED/STOPPED pods (these are handled separately)
                    if pending_count > 0 or readiness_wait_count > 0:
                        logger.info(f"Deployment {namespace}/{name}: Found {pending_count} pending pods and {readiness_wait_count} pods in readiness wait period (initialDelaySeconds={readiness_initial_delay}s). Waiting for them to become ready before creating more. Total pods: {all_pods_count} (Running: {current_replicas}, Pending: {pending_count}, InReadinessWait: {readiness_wait_count}, Failed/Stopped: {failed_stopped_count})")
                        continue
                    
                    missing_count = pods_to_create
                    target_replicas = all_pods_count + pods_to_create
                    logger.warning(f"Deployment {namespace}/{name}: Need to create {missing_count} replicas. Total pods: {all_pods_count} (Running: {current_replicas}, Pending: {pending_count}, InReadinessWait: {readiness_wait_count}, Failed/Stopped: {failed_stopped_count}), Target: {target_replicas} (min: {min_replicas}, max: {max_replicas}, pods_to_create={pods_to_create})")
                    
                    # CRITICAL SAFETY CHECK: Ensure we never create more than max_replicas
                    if target_replicas > max_replicas:
                        logger.error(f"Deployment {namespace}/{name}: ERROR - Calculated target_replicas ({target_replicas}) exceeds max_replicas ({max_replicas}). Adjusting pods_to_create.")
                        pods_to_create = max(0, max_replicas - all_pods_count)
                        missing_count = pods_to_create
                        target_replicas = all_pods_count + pods_to_create
                        if pods_to_create <= 0:
                            logger.warning(f"Deployment {namespace}/{name}: After safety check, no pods to create. Skipping.")
                            continue
                    
                    # Check if AWS nodes are already being created for this namespace
                    # This prevents duplicate AWS node creation attempts
                    aws_nodes_creating_key = f"{AWS_NODES_CREATING_KEY_PREFIX}:{namespace}"
                    try:
                        lock_exists = redis_client.exists(aws_nodes_creating_key)
                        if lock_exists:
                            logger.info(f"Deployment {namespace}/{name}: AWS nodes are already being created for namespace {namespace}. Waiting for them to be ready.")
                            continue
                    except Exception as e:
                        logger.warning(f"Failed to check AWS nodes creating lock: {e}", exc_info=True)
                    
                    # Count existing pods per host per service to ensure distribution
                    # This prevents placing multiple replicas on the same host
                    # Format: {hostname: {service_name: count}}
                    existing_pods_per_host = {}
                    for pod in running_pods:
                        pod_hostname = pod.get('hostname')
                        if pod_hostname:
                            if pod_hostname not in existing_pods_per_host:
                                existing_pods_per_host[pod_hostname] = {}
                            # Count pods for this service (app_label) on this host
                            existing_pods_per_host[pod_hostname][app_label] = existing_pods_per_host[pod_hostname].get(app_label, 0) + 1
                    
                    logger.info(f"Deployment {namespace}/{name}: Existing pods per host: {existing_pods_per_host}")
                    
                    # Acquire AWS node creation lock before calling scheduler
                    # The scheduler will check for worker nodes and create AWS nodes if needed
                    aws_lock_acquired = False
                    try:
                        lock_acquired = redis_client.set(aws_nodes_creating_key, "1", nx=True, ex=300)  # 5 minute TTL
                        if lock_acquired:
                            aws_lock_acquired = True
                            logger.info(f"Acquired AWS node creation lock for namespace {namespace}")
                    except Exception as e:
                        logger.warning(f"Failed to acquire AWS nodes creating lock: {e}", exc_info=True)
                    
                    # Use scheduler's schedule_recovery_pods method to handle everything
                    # This method will:
                    # 1. Check for worker nodes (calls get_worker_nodes_from_redis)
                    # 2. Create AWS nodes if needed (calls create_aws_nodes_for_recovery)
                    # 3. Use distribution algorithm to find placement (calls ClusterWorkerDistribution)
                    # 4. Create pods on assigned hosts (calls create_pod_on_host)
                    logger.info(f"Deployment {namespace}/{name}: Calling scheduler.schedule_recovery_pods for {missing_count} replicas")
                    scheduler = DeploymentScheduler()
                    try:
                        result = scheduler.schedule_recovery_pods(
                            namespace=namespace,
                            deployment_name=name,
                            app_label=app_label,
                            missing_replicas=missing_count,
                            containers=containers,
                            resource_reqs=resource_reqs,
                            existing_pods_per_host=existing_pods_per_host
                        )
                        logger.info(f"Deployment {namespace}/{name}: Scheduler returned: status={result.get('status')}, pods_created={result.get('pods_created', 0)}, error={result.get('error')}")
                    except Exception as scheduler_error:
                        logger.error(f"Deployment {namespace}/{name}: Exception calling scheduler: {scheduler_error}", exc_info=True)
                        result = {
                            'status': 'error',
                            'pods_created': 0,
                            'error': str(scheduler_error)
                        }
                    
                    # Release lock if AWS node creation failed
                    if aws_lock_acquired and result.get('status') == 'error' and result.get('aws_task_id') is None:
                        try:
                            redis_client.delete(aws_nodes_creating_key)
                            logger.info(f"Released AWS node creation lock due to error")
                        except Exception:
                            pass
                    
                    pods_recreated = result.get('pods_created', 0)
                    result_status = result.get('status', 'error')
                    
                    # Handle different result statuses
                    if pods_recreated > 0:
                        # Pods were created successfully
                        # Mark each created pod instance as creating
                        created_instances = result.get('created_instances', [])
                        for instance_num in created_instances:
                            creating_pod_key = f"{namespace}:{name}:{instance_num}"
                            try:
                                redis_client.sadd(PODS_CREATING_KEY, creating_pod_key)
                                redis_client.expire(PODS_CREATING_KEY, 300)  # 10 minutes TTL (safety net - stale keys cleaned up after 90s)
                                logger.info(f"Marked pod creation {creating_pod_key} as in-flight (TTL: 600s, stale detection: 90s)")
                            except Exception as mark_error:
                                logger.warning(f"Failed to mark pod creation {creating_pod_key} as in-flight: {mark_error}")
                        
                        recovery_results['deployments_recovered'] += 1
                        recovery_results['pods_recreated'] += pods_recreated
                        logger.info(f"Recovered {pods_recreated} pods for deployment {namespace}/{name} (expected: {missing_count}). Will wait for readiness before creating more.")
                    elif result_status == 'submitted':
                        # AWS nodes are being created (no pods created yet)
                        logger.info(f"Deployment {namespace}/{name}: AWS node creation initiated (task_id: {result.get('aws_task_id')}). Pods will be created once nodes are ready.")
                        recovery_results['deployments_recovered'] += 1
                        # Lock will expire automatically via TTL when nodes come online
                    elif result_status == 'success' and pods_recreated == 0:
                        # This shouldn't happen, but handle gracefully
                        logger.warning(f"Deployment {namespace}/{name}: Scheduler returned success but no pods were created. This may indicate an issue.")
                    elif result_status == 'error':
                        logger.error(f"Deployment {namespace}/{name}: Scheduler returned error: {result.get('error', 'Unknown error')}")
                        recovery_results['errors'].append({
                            'deployment': f"{namespace}/{name}",
                            'error': result.get('error', 'Scheduler error')
                        })
                    else:
                        # Unknown status
                        logger.warning(f"Deployment {namespace}/{name}: Scheduler returned unknown status: {result_status}, pods_created: {pods_recreated}")
                elif all_pods_count >= max_replicas:
                    logger.info(f"Deployment {namespace}/{name}: At or above max_replicas (Total pods: {all_pods_count} >= {max_replicas}, Running: {current_replicas})")
                elif all_pods_count >= min_replicas:
                    logger.info(f"Deployment {namespace}/{name}: Sufficient replicas (Total pods: {all_pods_count} >= {min_replicas}, Running: {current_replicas})")
                else:
                    # This case should not happen (should be caught above), but log for safety
                    logger.warning(f"Deployment {namespace}/{name}: Unexpected state - Total pods: {all_pods_count}, Running: {current_replicas}, min: {min_replicas}, max: {max_replicas}")
            
            except Exception as e:
                logger.error(f"Failed to process deployment {namespace}/{name}: {e}", exc_info=True)
                recovery_results['errors'].append({
                    'deployment': f"{namespace}/{name}",
                    'error': str(e)
                })
        
        logger.info(f"Recovery complete: {recovery_results.get('pods_recreated', 0)} pods recreated, {recovery_results.get('pods_terminated', 0)} pods terminated for {recovery_results['deployments_recovered']} deployments")
        return {
            'status': 'success',
            **recovery_results
        }
        
    except Exception as e:
        logger.error(f"Deployment recovery task failed: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e)
        }


@celery_app.task(name="deployment.scale_deployment")
@log_to_file(logger)
def scale_deployment_task(deployment_name: str, namespace: str) -> Dict[str, Any]:
    """Scale a deployment to match min_replicas requirement.
    
    This task:
    1. Gets the deployment from Redis
    2. Counts current running pods
    3. Compares with min_replicas
    4. Creates or terminates pods as needed
    
    Args:
        deployment_name: Deployment name (metadata.name)
        namespace: Namespace
        
    Returns:
        Dictionary with scaling results
    """
    try:
        logger.info("=" * 80)
        logger.info(f"DEPLOYMENT SCALING: Scaling {namespace}/{deployment_name}")
        logger.info("=" * 80)
        
        redis_interface = RedisInterface()
        deployment_store = DeploymentStore(redis_interface)
        host_pod_store = HostPodStore(redis_interface)
        
        # Get deployment from Redis
        deployment = deployment_store.get_deployment(deployment_name, namespace)
        if not deployment:
            logger.error(f"Deployment {namespace}/{deployment_name} not found in Redis")
            return {
                "status": "error",
                "error": f"Deployment {namespace}/{deployment_name} not found",
                "pods_created": 0,
                "pods_terminated": 0,
            }
        
        app_label = deployment.get("app_label")
        min_replicas = deployment.get("min_replicas", 0)
        max_replicas = deployment.get("max_replicas", 0)
        
        # Count current running pods
        pods = host_pod_store.get_pods_by_application(app_label)
        namespace_pods = [p for p in pods if p.get("namespace") == namespace]
        
        # Debug: Log status values for first few pods
        if namespace_pods:
            status_samples = [p.get("status") for p in namespace_pods[:3]]
            logger.info(f"Sample pod statuses: {status_samples}")
        
        # Separate pods by status: RUNNING vs UNKNOWN/other
        running_pods = [p for p in namespace_pods if p.get("status", "").upper() == "RUNNING"]
        unknown_pods = [
            p for p in namespace_pods 
            if p.get("status", "").upper() in ("UNKNOWN", "UNKOWN")  # Handle typo too
        ]
        
        pods_terminated = 0
        
        # Priority 1: Terminate UNKNOWN pods first (they should always be cleaned up)
        if unknown_pods:
            logger.warning(f"Deployment {namespace}/{deployment_name}: Found {len(unknown_pods)} pods in UNKNOWN state, terminating them")
            logger.info(f"UNKNOWN pod IDs: {[p.get('pod_id') for p in unknown_pods]}")
            
            # Use centralized scheduler function for pod termination
            scheduler = DeploymentScheduler()
            for pod in unknown_pods:
                pod_id = pod.get("pod_id")
                hostname = pod.get("hostname")
                
                if not pod_id or not hostname:
                    logger.warning(f"Skipping UNKNOWN pod with missing pod_id or hostname: {pod}")
                    continue
                
                try:
                    result = scheduler.terminate_pod_on_host(
                        pod_id=pod_id,
                        namespace=namespace,
                        hostname=hostname
                    )
                    
                    if result.get('status') == 'submitted':
                        pods_terminated += 1
                        logger.info(f"Terminated UNKNOWN pod {pod_id} (task_id: {result.get('task_id')})")
                    else:
                        logger.warning(f"Failed to terminate UNKNOWN pod {pod_id}: {result.get('error', 'Unknown error')}")
                except Exception as e:
                    logger.error(f"Failed to terminate UNKNOWN pod {pod_id}: {e}", exc_info=True)
            
            logger.info(f"Terminated {pods_terminated} UNKNOWN pods for deployment {namespace}/{deployment_name}")
        
        running_pods_count = len(running_pods)
        
        # Additional validation: Check if pods actually exist and were updated recently
        # This helps filter out stale Redis entries
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        cutoff_time = now - timedelta(seconds=60)  # Pods must be updated in last 60 seconds
        
        validated_pods = []
        for pod in running_pods:
            # Check 1: Pod must have containers or valid pause container
            containers = pod.get("containers", [])
            pause_container = pod.get("pause_container", {})
            has_containers = containers or (pause_container and pause_container.get("pid"))
            
            if not has_containers:
                logger.warning(f"Pod {pod.get('pod_id')} marked as RUNNING but has no containers - likely stale entry")
                continue
            
            # Check 2: Pod must have been updated in the last 60 seconds
            last_updated_str = pod.get("last_updated")
            if last_updated_str:
                try:
                    # Parse ISO format timestamp
                    if isinstance(last_updated_str, str):
                        # Handle both with and without timezone info
                        if last_updated_str.endswith('Z'):
                            last_updated_str = last_updated_str[:-1] + '+00:00'
                        last_updated = datetime.fromisoformat(last_updated_str.replace('Z', '+00:00'))
                        # Ensure timezone-aware
                        if last_updated.tzinfo is None:
                            last_updated = last_updated.replace(tzinfo=timezone.utc)
                    else:
                        # If it's already a datetime object
                        last_updated = last_updated_str
                        if last_updated.tzinfo is None:
                            last_updated = last_updated.replace(tzinfo=timezone.utc)
                    
                    if last_updated < cutoff_time:
                        logger.warning(
                            f"Pod {pod.get('pod_id')} last updated {last_updated} is older than 60 seconds "
                            f"(cutoff: {cutoff_time}) - likely stale entry"
                        )
                        continue
                except Exception as e:
                    logger.warning(f"Failed to parse last_updated timestamp for pod {pod.get('pod_id')}: {e}")
                    # If we can't parse the timestamp, we'll still include it but log a warning
                    logger.warning(f"Including pod {pod.get('pod_id')} despite timestamp parse error")
            
            validated_pods.append(pod)
        
        running_pods = validated_pods
        running_pods_count = len(running_pods)
        
        logger.info(f"Deployment {namespace}/{deployment_name}: {running_pods_count} validated running pods (updated in last 60s), min={min_replicas}, max={max_replicas}")
        logger.info(f"Found {len(pods)} total pods for app_label {app_label}, {len(namespace_pods)} in namespace {namespace}, {running_pods_count} validated running")
        
        # If we found pods but none are running, log all statuses for debugging
        if len(namespace_pods) > 0 and running_pods_count == 0:
            all_statuses = [p.get("status") for p in namespace_pods]
            logger.warning(f"Found {len(namespace_pods)} pods but none match RUNNING status. All statuses: {set(all_statuses)}")
        
        # Fallback: If no pods found by app_label, try finding by querying all hosts
        if running_pods_count == 0:
            logger.warning(f"No pods found by app_label {app_label} in namespace {namespace}. Attempting fallback: querying all hosts...")
            try:
                # get_worker_nodes_from_redis is already imported at the top of the file
                worker_nodes_raw = get_worker_nodes_from_redis(redis_interface)
                all_pods = []
                for node in worker_nodes_raw:
                    hostname = node.get("hostname")
                    if hostname:
                        host_pods = host_pod_store.get_pods_by_host(hostname)
                        all_pods.extend(host_pods)
                
                # Filter by namespace and app_label
                namespace_pods = []
                for pod in all_pods:
                    pod_ns = pod.get("namespace")
                    if pod_ns != namespace:
                        continue
                    
                    # Check labels for app_label
                    pod_labels = pod.get("labels", {})
                    pod_app_label = pod_labels.get("app") or pod_labels.get("app_label") or pod.get("app_label")
                    
                    if pod_app_label == app_label:
                        namespace_pods.append(pod)
                
                # More robust status check: handle case, whitespace, and type variations
                # Also validate pods have containers and were updated recently
                running_pods = []
                for p in namespace_pods:
                    status = p.get("status")
                    if status:
                        status_str = str(status).strip().upper()
                        if status_str == "RUNNING":
                            # Validate pod has containers
                            containers = p.get("containers", [])
                            pause_container = p.get("pause_container", {})
                            has_containers = containers or (pause_container and pause_container.get("pid"))
                            
                            if not has_containers:
                                logger.warning(f"Fallback: Pod {p.get('pod_id')} marked as RUNNING but has no containers - skipping")
                                continue
                            
                            # Validate pod was updated in last 60 seconds
                            last_updated_str = p.get("last_updated")
                            if last_updated_str:
                                try:
                                    if isinstance(last_updated_str, str):
                                        if last_updated_str.endswith('Z'):
                                            last_updated_str = last_updated_str[:-1] + '+00:00'
                                        last_updated = datetime.fromisoformat(last_updated_str.replace('Z', '+00:00'))
                                        if last_updated.tzinfo is None:
                                            last_updated = last_updated.replace(tzinfo=timezone.utc)
                                    else:
                                        last_updated = last_updated_str
                                        if last_updated.tzinfo is None:
                                            last_updated = last_updated.replace(tzinfo=timezone.utc)
                                    
                                    if last_updated < cutoff_time:
                                        logger.warning(f"Fallback: Pod {p.get('pod_id')} last updated {last_updated} is older than 60s - skipping")
                                        continue
                                except Exception as e:
                                    logger.warning(f"Fallback: Failed to parse last_updated for pod {p.get('pod_id')}: {e}")
                            
                            running_pods.append(p)
                running_pods_count = len(running_pods)
                logger.info(f"Fallback search: Found {len(namespace_pods)} pods in namespace {namespace} with app_label {app_label}, {running_pods_count} running")
                if running_pods_count > 0:
                    logger.info(f"Fallback found running pod IDs: {[p.get('pod_id') for p in running_pods]}")
                else:
                    logger.warning(f"Fallback found {len(namespace_pods)} pods but none are RUNNING. Pod statuses: {[p.get('status') for p in namespace_pods]}")
            except Exception as e:
                logger.warning(f"Fallback pod search failed: {e}")
        
        # Double-check: also count by matching container info if app_label matching fails
        if running_pods_count == 0 and namespace_pods:
            logger.warning(f"No pods found by app_label {app_label}, but {len(namespace_pods)} pods found in namespace. Attempting container-based matching...")
            deployment_spec = deployment.get("deployment_spec", {})
            containers_spec = deployment_spec.get("containers", [])
            matched_pods = []
            for pod in namespace_pods:
                pod_containers = pod.get("containers", [])
                if not pod_containers:
                    continue
                for pod_container in pod_containers:
                    if not isinstance(pod_container, dict):
                        continue
                    pod_container_name = pod_container.get("name")
                    pod_container_image = pod_container.get("image")
                    for container_spec in containers_spec:
                        if not isinstance(container_spec, dict):
                            continue
                        spec_name = container_spec.get("name")
                        spec_image = container_spec.get("image")
                        if (pod_container_name and spec_name and pod_container_name == spec_name) or \
                           (pod_container_image and spec_image and pod_container_image == spec_image):
                            status = pod.get("status")
                            if status and str(status).strip().upper() == "RUNNING" and pod not in matched_pods:
                                matched_pods.append(pod)
                                break
                    if pod in matched_pods:
                        break
            if matched_pods:
                running_pods = matched_pods
                running_pods_count = len(matched_pods)
                logger.info(f"Found {running_pods_count} running pods via container matching")
        
        pods_created = 0
        # pods_terminated already initialized above from UNKNOWN pod termination
        
        # Priority: Scale down first if above max_replicas (this takes precedence)
        # Then scale up if below min_replicas
        # This prevents creating pods when we should be terminating them
        if running_pods_count > max_replicas:
            excess_replicas = running_pods_count - max_replicas
            logger.info(f"Scaling down: {running_pods_count} running pods, max_replicas={max_replicas}, need to terminate {excess_replicas} pods")
            logger.info(f"Current running pod IDs: {[p.get('pod_id') for p in running_pods]}")
            
            # Terminate excess pods while preserving redundancy across nodes
            # Strategy: Group pods by hostname, then terminate to maintain distribution
            pods_by_host = {}
            for pod in running_pods:
                hostname = pod.get("hostname")
                if hostname:
                    if hostname not in pods_by_host:
                        pods_by_host[hostname] = []
                    pods_by_host[hostname].append(pod)
            
            # Sort pods within each host by creation_time (oldest first)
            for hostname in pods_by_host:
                pods_by_host[hostname].sort(key=lambda p: p.get("creation_time", ""))
            
            # Select pods to terminate: prioritize removing from nodes with more pods
            # This helps maintain distribution across nodes
            pods_to_terminate = []
            remaining_to_terminate = excess_replicas
            
            # First pass: Try to balance by removing from nodes with most pods
            while remaining_to_terminate > 0 and pods_by_host:
                # Find node with most pods
                max_host = max(pods_by_host.keys(), key=lambda h: len(pods_by_host[h]))
                if pods_by_host[max_host]:
                    pod = pods_by_host[max_host].pop(0)  # Remove oldest from this node
                    pods_to_terminate.append(pod)
                    remaining_to_terminate -= 1
                    # Remove host from dict if no more pods
                    if not pods_by_host[max_host]:
                        del pods_by_host[max_host]
                else:
                    break
            
            # If still need to terminate more, continue with remaining pods
            if remaining_to_terminate > 0:
                remaining_pods = []
                for host_pods in pods_by_host.values():
                    remaining_pods.extend(host_pods)
                remaining_pods.sort(key=lambda p: p.get("creation_time", ""))
                pods_to_terminate.extend(remaining_pods[:remaining_to_terminate])
            
            logger.info(f"Selected {len(pods_to_terminate)} pods to terminate for redundancy: {[(p.get('pod_id'), p.get('hostname')) for p in pods_to_terminate]}")
            
            utilities = UtilitiesExtension()
            for pod in pods_to_terminate:
                pod_id = pod.get("pod_id")
                hostname = pod.get("hostname")
                
                if not pod_id or not hostname:
                    continue
                
                try:
                    # Use centralized scheduler function for pod termination
                    scheduler = DeploymentScheduler()
                    result = scheduler.terminate_pod_on_host(
                        pod_id=pod_id,
                        namespace=namespace,
                        hostname=hostname
                    )
                    
                    if result.get('status') == 'submitted':
                        pods_terminated += 1
                        logger.info(f"Terminated pod {pod_id} for scaling down (task_id: {result.get('task_id')})")
                    else:
                        logger.warning(f"Failed to terminate pod {pod_id}: {result.get('error', 'Unknown error')}")
                except Exception as e:
                    logger.error(f"Failed to terminate pod {pod_id}: {e}")
            
            logger.info(f"Scaling down complete: terminated {pods_terminated} pods (expected {excess_replicas})")
            if pods_terminated != excess_replicas:
                logger.warning(f"Pod termination count mismatch: terminated {pods_terminated} but expected {excess_replicas}")
        
        # Scale up if below min_replicas (only if not scaling down)
        elif running_pods_count < min_replicas:
            missing_replicas = min_replicas - running_pods_count
            logger.info(f"Scaling up: {running_pods_count} running pods, min_replicas={min_replicas}, need {missing_replicas} more pods")
            logger.info(f"Current running pod IDs: {[p.get('pod_id') for p in running_pods]}")
            
            # Get available worker nodes
            worker_nodes_raw = get_worker_nodes_from_redis(redis_interface)
            if not worker_nodes_raw:
                logger.warning("No worker nodes available for scaling")
                return {
                    "status": "error",
                    "error": "No worker nodes available",
                    "pods_created": 0,
                    "pods_terminated": 0,
                }
            
            # Convert worker nodes to millicores format for distribution
            worker_nodes = [
                {
                    'cpu': int(node.get('cpu', 0) * 1000),  # Convert cores to millicores
                    'memory': node.get('memory', 0),  # Already in MB
                    'hostname': node.get('hostname'),
                    'ip_address': node.get('ip_address'),
                }
                for node in worker_nodes_raw
            ]
            
            deployment_spec = deployment.get("deployment_spec", {})
            containers = deployment_spec.get("containers", [])
            
            # Calculate resource requirements
            total_cpu_millicores = 0
            total_memory_mb = 0
            for container in containers:
                resources = container.get("resources", {})
                if isinstance(resources, dict):
                    cpu_millicores = resources.get("cpu_millicores", 0)
                    memory_str = resources.get("memory", "0Mi")
                    # Parse memory (simplified - assumes Mi or Gi)
                    if isinstance(memory_str, str):
                        if memory_str.endswith("Mi"):
                            memory_mb = float(memory_str[:-2])
                        elif memory_str.endswith("Gi"):
                            memory_mb = float(memory_str[:-2]) * 1024
                        else:
                            memory_mb = 0
                    else:
                        memory_mb = float(memory_str)
                    total_cpu_millicores += cpu_millicores
                    total_memory_mb += memory_mb
            
            # Create cluster_info for distribution (required parameter)
            cluster_info = {
                app_label: {
                    'cpu': total_cpu_millicores,
                    'memory': total_memory_mb,
                    'instances': missing_replicas
                }
            }
            
            # Use distribution service to find placement
            distribution = ClusterWorkerDistribution(worker_nodes, cluster_info)
            placement_by_index = distribution.distribute_cluster_nodes()
            
            # If placement fails, try to create AWS nodes
            if not placement_by_index:
                logger.warning(f"Could not find placement for {missing_replicas} missing replicas on existing nodes. Attempting to create AWS nodes...")
                
                # Check if AWS nodes are already being created for this namespace to prevent duplicate creation
                aws_nodes_creating_key = f"{AWS_NODES_CREATING_KEY_PREFIX}:{namespace}"
                try:
                    is_creating = redis_client.exists(aws_nodes_creating_key)
                    if is_creating:
                        logger.info(f"AWS nodes are already being created for namespace {namespace}. Skipping duplicate creation attempt.")
                        return {
                            "status": "error",
                            "error": f"AWS nodes already being created for namespace {namespace}",
                            "pods_created": 0,
                            "pods_terminated": 0,
                        }
                except Exception as e:
                    logger.warning(f"Failed to check AWS nodes creating status: {e}", exc_info=True)
                
                # Calculate required AWS nodes
                required_nodes = max(1, (missing_replicas + 1) // 2)
                logger.info(f"Creating {required_nodes} AWS node(s) to accommodate {missing_replicas} missing replicas")
                
                # Mark AWS nodes as being created to prevent concurrent creation
                try:
                    redis_client.setex(aws_nodes_creating_key, 300, "1")  # 5 minute TTL
                    logger.debug(f"Marked AWS node creation in progress for namespace {namespace}")
                except Exception as e:
                    logger.warning(f"Failed to mark AWS node creation: {e}", exc_info=True)
                
                # Get AWS config (from Redis with fallback to config file)
                # Only the 4 requested fields (ami_id, key_name, security_group_ids, subnet_id) come from Redis
                # instance_type and region still come from config.json
                from utils.celery.tasks.aws_tasks import create_worker_nodes
                from utils.aws.config_helper import get_aws_node_config
                
                node_config = get_aws_node_config()  # Only the 4 requested fields from Redis (or config.json fallback)
                read_config = rc()
                file_aws_config = read_config.aws_config  # For instance_type and region
                
                # Merge: node_config (4 fields from Redis/fallback) + instance_type/region from config file
                # get_aws_node_config() already handles fallback to config.json for the 4 fields
                aws_config = node_config.copy() if node_config else {}
                
                # Always get instance_type and region from config.json
                aws_config['instance_type'] = file_aws_config.get('instance_type', 't3.medium')
                aws_config['region'] = file_aws_config.get('region')
                
                # Validate required fields are present (check if None or empty string/list)
                required_fields = ['ami_id', 'key_name', 'security_group_ids', 'subnet_id']
                missing_fields = []
                for field in required_fields:
                    value = aws_config.get(field)
                    if not value or (isinstance(value, list) and len(value) == 0):
                        missing_fields.append(field)
                
                if missing_fields:
                    logger.error(f"AWS configuration missing required fields: {missing_fields}. Cannot create nodes.")
                    logger.error(f"Current aws_config keys: {list(aws_config.keys())}")
                    logger.error(f"Config from Redis/fallback: {list(node_config.keys()) if node_config else 'None'}")
                    logger.error(f"Config from file (instance_type, region): {file_aws_config.get('instance_type')}, {file_aws_config.get('region')}")
                    # Clear the lock since we can't create nodes
                    try:
                        redis_client.delete(aws_nodes_creating_key)
                    except Exception:
                        pass
                    return {
                        "status": "error",
                        "error": f"AWS configuration missing required fields: {missing_fields}. Cannot create nodes for scaling.",
                        "pods_created": 0,
                        "pods_terminated": 0,
                    }
                
                logger.info(f"AWS configuration validated. Fields: {list(aws_config.keys())}")
                
                # Create AWS queue info with proper encoding
                key = read_config.encryption_config['key']
                encode_util = UtilitiesExtension(key)
                aws_queue_info = create_queue_info("aws_interface", utilities_extension=encode_util)
                logger.info(f"Routing AWS node creation to queue: {aws_queue_info.get('queue')}")
                
                # Submit AWS node creation task (matching scheduler_tasks.py pattern)
                aws_result = submit_celery_task(
                    task=create_worker_nodes,
                    args=(
                        None,  # aws_access_key - deprecated, read from config
                        None,  # aws_secret_key - deprecated, read from config
                        aws_config.get("region"),  # Optional region override
                    ),
                    kwargs={
                        'instance_type': aws_config.get('instance_type', 't3.medium'),
                        'ami_id': aws_config.get('ami_id'),
                        'key_name': aws_config.get('key_name'),
                        'security_group_ids': aws_config.get('security_group_ids', []),
                        'subnet_id': aws_config.get('subnet_id'),
                        'namespace': namespace,
                        'MaxCount': required_nodes,
                    },
                    queue_info=aws_queue_info,
                    operation_name="create_aws_nodes_for_scaling",
                    error_code="AWS_NODE_CREATION_ERROR",
                )
                
                aws_task_id = aws_result.get("data", {}).get("task_id")
                if not aws_task_id:
                    logger.error("AWS node creation task failed to submit")
                    # Clear the lock since creation failed
                    try:
                        redis_client.delete(aws_nodes_creating_key)
                    except Exception:
                        pass
                    return {
                        "status": "error",
                        "error": "Failed to submit AWS node creation task",
                        "pods_created": 0,
                        "pods_terminated": 0,
                    }
                
                logger.info(f"Waiting for AWS node creation task {aws_task_id} to complete...")
                aws_task = AsyncResult(aws_task_id, app=celery_app)
                
                # Wait for AWS task to complete (max 300 seconds)
                max_wait_time = 300
                wait_interval = 5
                elapsed_time = 0
                aws_status = "pending"
                
                while elapsed_time < max_wait_time:
                    if aws_task.ready():
                        if aws_task.successful():
                            aws_status = "success"
                            logger.info(f"AWS node creation task {aws_task_id} completed successfully")
                            # Clear the lock on success - nodes will be detected when they come online
                            try:
                                redis_client.delete(aws_nodes_creating_key)
                            except Exception:
                                pass
                            break
                        else:
                            aws_status = "error"
                            error_msg = str(aws_task.result) if aws_task.result else "Unknown error"
                            logger.error(f"AWS node creation task {aws_task_id} failed: {error_msg}")
                            # Clear the lock on error
                            try:
                                redis_client.delete(aws_nodes_creating_key)
                            except Exception:
                                pass
                            raise AWSError(
                                message=f"AWS node creation failed: {error_msg}",
                                error_code="AWS_NODE_CREATION_FAILED",
                                details={"task_id": aws_task_id, "required_nodes": required_nodes}
                            )
                    sleep(wait_interval)
                    elapsed_time += wait_interval
                    logger.debug(f"Waiting for AWS nodes... ({elapsed_time}/{max_wait_time}s)")
                
                if aws_status != "success":
                    aws_status = "timeout"
                    logger.error(f"AWS node creation task {aws_task_id} timed out after {max_wait_time} seconds")
                    # Clear the lock on timeout
                    try:
                        redis_client.delete(aws_nodes_creating_key)
                    except Exception:
                        pass
                    raise AWSError(
                        message=f"AWS node creation timed out after {max_wait_time} seconds",
                        error_code="AWS_NODE_CREATION_TIMEOUT",
                        details={"task_id": aws_task_id, "required_nodes": required_nodes}
                    )
                
                # Wait for nodes to register in Redis (10 seconds)
                logger.info("Waiting 240 seconds for new AWS nodes to register in Redis...")
                sleep(240)
                
                # Retry getting worker nodes (should now include new nodes)
                logger.info("Retrying placement with updated worker nodes (including new AWS nodes)...")
                worker_nodes_raw = get_worker_nodes_from_redis(redis_interface)
                if not worker_nodes_raw:
                    logger.error("Still no worker nodes available after AWS node creation")
                    return {
                        "status": "error",
                        "error": "No worker nodes available after AWS node creation",
                        "pods_created": 0,
                        "pods_terminated": 0,
                    }
                
                # Convert to millicores format again
                worker_nodes = [
                    {
                        'cpu': int(node.get('cpu', 0) * 1000),  # Convert cores to millicores
                        'memory': node.get('memory', 0),  # Already in MB
                        'hostname': node.get('hostname'),
                        'ip_address': node.get('ip_address'),
                    }
                    for node in worker_nodes_raw
                ]
                
                # Retry distribution with updated nodes
                logger.info(f"Retrying distribution with {len(worker_nodes)} worker nodes (including new AWS nodes)")
                distribution = ClusterWorkerDistribution(worker_nodes, cluster_info)
                placement_by_index = distribution.distribute_cluster_nodes()
                
                if not placement_by_index:
                    logger.error(f"Still could not find placement for {missing_replicas} missing replicas even after creating AWS nodes")
                    return {
                        "status": "error",
                        "error": "Could not find placement for missing replicas even after creating AWS nodes",
                        "pods_created": 0,
                        "pods_terminated": 0,
                    }
                
                logger.info(f"Successfully found placement after creating AWS nodes: {placement_by_index}")
            
            # Convert placement from indices to hostnames and offset instance numbers
            # The distribution algorithm creates instances 0..(missing_replicas-1)
            # We need to offset by running_pods_count to avoid conflicts
            placement = {}
            total_placement_count = 0
            
            # Log the order of worker nodes for debugging
            logger.info(f"Worker nodes order: {[node.get('hostname') for node in worker_nodes_raw]}")
            logger.info(f"Distribution result (by index): {placement_by_index}")
            
            for node_idx, pod_info_list in placement_by_index.items():
                if node_idx >= len(worker_nodes_raw):
                    logger.warning(f"Node index {node_idx} is out of range (max: {len(worker_nodes_raw) - 1})")
                    continue
                node = worker_nodes_raw[node_idx]
                hostname = node.get("hostname")
                if not hostname:
                    logger.warning(f"Node at index {node_idx} has no hostname, skipping")
                    continue
                
                # Offset instance numbers by running_pods_count
                offset_pod_list = []
                for app_name, instance_num in pod_info_list:
                    offset_instance_num = running_pods_count + instance_num
                    offset_pod_list.append((app_name, offset_instance_num))
                    total_placement_count += 1
                placement[hostname] = offset_pod_list
                logger.info(f"Placement for {hostname} (node index {node_idx}): {len(offset_pod_list)} pods with instance numbers {[inst for _, inst in offset_pod_list]}")
            
            logger.info(f"Total placement count: {total_placement_count} pods (expected: {missing_replicas})")
            if total_placement_count != missing_replicas:
                logger.warning(f"Mismatch: placement created {total_placement_count} pods but expected {missing_replicas}. This may indicate an issue with the distribution algorithm.")
            
            # Create pods using centralized scheduler function
            scheduler = DeploymentScheduler()
            pods_created_tasks = []  # Track all task IDs for verification
            for hostname, pod_info_list in placement.items():
                logger.info(f"Creating {len(pod_info_list)} pods on {hostname}")
                for app_name, instance_num in pod_info_list:
                    try:
                        # Prepare container specs with resources properly formatted
                        # The scheduler's create_pod_on_host will handle resource conversion
                        container_specs = []
                        for container in containers:
                            # Include resources in the container spec - scheduler will handle conversion
                            # Normalize args: if not provided or empty list, set to None
                            # Empty args [] would fail at runc level with "args must not be empty"
                            # None allows fallback logic to extract Entrypoint/Cmd from image
                            container_args = container.get('args')
                            if container_args is None or (isinstance(container_args, list) and len(container_args) == 0):
                                container_args = None
                            
                            container_spec = {
                                'name': container.get('name'),
                                'image': container.get('image'),
                                'args': container_args,  # None if not provided or empty, otherwise use as-is
                                'env': container.get('env', {}),
                                'ports': container.get('ports', []),
                            }
                            # Include resources if present (scheduler will convert if needed)
                            if 'resources' in container:
                                container_spec['resources'] = container['resources']
                            container_specs.append(container_spec)
                        
                        # Get labels from deployment
                        labels = {
                            "app": app_label,
                            "app_label": app_label,
                            "instance": str(instance_num),
                        }
                        
                        # Use centralized scheduler function to create pod
                        logger.info(f"Creating pod for {app_label} instance {instance_num} on {hostname}")
                        result = scheduler.create_pod_on_host(
                            containers=container_specs,
                            namespace=namespace,
                            hostname=hostname,
                            labels=labels,
                            deployment_name=deployment_name,
                            replica_num=instance_num
                        )
                        
                        if result.get('status') == 'submitted':
                            task_id = result.get('task_id')
                            pods_created += 1
                            pods_created_tasks.append({"hostname": hostname, "instance": instance_num, "task_id": task_id})
                            logger.info(f"Successfully submitted pod creation for {app_label} instance {instance_num} on {hostname} (task_id: {task_id})")
                        else:
                            logger.error(f"Failed to create pod for {app_label} instance {instance_num} on {hostname}: {result.get('error', 'Unknown error')}")
                    except Exception as e:
                        logger.error(f"Exception creating pod for {app_label} instance {instance_num} on {hostname}: {e}", exc_info=True)
            
            logger.info(f"Submitted {pods_created} pod creation tasks. Task details: {pods_created_tasks}")
            
            logger.info(f"Scaling up complete: created {pods_created} pods (expected {missing_replicas})")
            if pods_created != missing_replicas:
                logger.warning(f"Pod creation count mismatch: created {pods_created} but expected {missing_replicas}. This may indicate duplicate task execution or a race condition.")
        
        # If within bounds, no action needed
        else:
            logger.info(f"Deployment {namespace}/{deployment_name}: {running_pods_count} pods is within bounds (min={min_replicas}, max={max_replicas}). No scaling needed.")
        
        logger.info(f"Scaling complete: created {pods_created}, terminated {pods_terminated}")
        
        return {
            "status": "success",
            "deployment": deployment_name,
            "namespace": namespace,
            "pods_created": pods_created,
            "pods_terminated": pods_terminated,
            "current_replicas": running_pods_count,
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
        }
        
    except Exception as e:
        logger.error(f"Deployment scaling task failed: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "pods_created": 0,
            "pods_terminated": 0,
        }

