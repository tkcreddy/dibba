"""
Celery tasks for cleaning up lingering containers that are not active.

This module provides tasks to:
- Identify containers that don't have corresponding active tasks
- Remove lingering/orphaned containers from containerd
"""
from typing import Dict, Any, List, Set
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.containerd.containerd_interface import ContainerdClient, RuntimeManager
from utils.containerd.containerd_interface import _iter_list_tasks, _task_id
from utils.error_handlers import handle_errors
from utils.celery.getnode_info import get_celery_nodes
from utils.ReadConfig import ReadConfig as rc
from utils.extensions.utilities_extention import UtilitiesExtension
from kombu import Exchange
import grpc
from generated.api.services.containers.v1 import containers_pb2
from generated.api.services.tasks.v1 import tasks_pb2
import re

logger = LogKCld()

# Configuration for encoding queue names
read_config = rc()
key = read_config.encryption_config['key']
encode_util = UtilitiesExtension(key)
secure_exchange = Exchange('secure_exchange', type='direct')


def _list_all_tasks(client: ContainerdClient) -> Set[str]:
    """List all active task IDs in the namespace.
    
    Args:
        client: ContainerdClient instance
        
    Returns:
        Set of container IDs that have active tasks
    """
    active_task_ids = set()
    try:
        # Use the helper function to iterate tasks
        from generated.api.services.tasks.v1 import tasks_pb2 as tpb
        for task in _iter_list_tasks(client, tpb):
            tid = _task_id(task)
            if tid:
                active_task_ids.add(tid)
    except Exception as e:
        logger.warning(f"Error listing tasks: {e}", exc_info=True)
    
    return active_task_ids


def _list_all_containers(client: ContainerdClient) -> List[str]:
    """List all container IDs in the namespace.
    
    Args:
        client: ContainerdClient instance
        
    Returns:
        List of container IDs
    """
    container_ids = []
    try:
        resp = client.containers.List(containers_pb2.ListContainersRequest())
        containers = getattr(resp, "containers", None) or []
        for container in containers:
            cid = getattr(container, "id", None)
            if cid:
                container_ids.append(cid)
    except grpc.RpcError as e:
        logger.error(f"Error listing containers: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error listing containers: {e}", exc_info=True)
    
    return container_ids


@celery_app.task(name="containerd.discover_lingering_containers")
@log_to_file(logger)
@handle_errors("discover_lingering_containers", "CONTAINER_CLEANUP_ERROR")
def discover_lingering_containers_task(namespace: str = "Production", auto_cleanup: bool = False) -> Dict[str, Any]:
    """Discover lingering containers on this worker node and optionally dispatch cleanup tasks.
    
    This task runs on worker nodes to discover containers that need cleanup.
    If auto_cleanup=True, it will automatically dispatch destroy_container_by_id_task
    for each lingering container found.
    
    Args:
        namespace: Containerd namespace to check (default: "Production")
        auto_cleanup: If True, automatically dispatch cleanup tasks for lingering containers
        
    Returns:
        Dictionary with discovery results (includes lingering_containers list)
    """
    try:
        logger.info(f"Discovering lingering containers in namespace: {namespace} (auto_cleanup={auto_cleanup})")
        
        # Create containerd client for the namespace
        client = ContainerdClient(namespace=namespace)
        
        # Get all containers
        container_ids = _list_all_containers(client)
        total_containers = len(container_ids)
        logger.info(f"Found {total_containers} containers in namespace {namespace}")
        
        # Get all active tasks
        active_task_ids = _list_all_tasks(client)
        logger.info(f"Found {len(active_task_ids)} active tasks in namespace {namespace}")
        
        # Find lingering containers (containers without active tasks)
        lingering_containers = [cid for cid in container_ids if cid not in active_task_ids]
        logger.info(f"Found {len(lingering_containers)} lingering containers without active tasks")
        
        # If auto_cleanup is enabled, dispatch cleanup tasks for each lingering container
        cleanup_tasks_dispatched = []
        if auto_cleanup and lingering_containers:
            from utils.celery.tasks.containerd_tasks import destroy_container_by_id_task
            from socket import gethostname
            
            hostname = gethostname()
            hostname_queue_name = encode_util.encode_hostname_with_key(hostname)
            host_queue_info = {
                'exchange': secure_exchange,
                'queue': hostname_queue_name,
                'routing_key': hostname_queue_name,
                'delivery_mode': 2,
                'expires': 60,
            }
            
            for cid in lingering_containers:
                try:
                    destroy_result = destroy_container_by_id_task.apply_async(
                        args=(namespace, cid),
                        **host_queue_info
                    )
                    cleanup_tasks_dispatched.append(cid)
                    logger.info(f"Auto-dispatched cleanup task for lingering container {cid} (task_id: {destroy_result.id})")
                except Exception as e:
                    logger.error(f"Failed to dispatch cleanup task for container {cid}: {e}", exc_info=True)
        
        return {
            "success": True,
            "namespace": namespace,
            "total_containers": total_containers,
            "active_tasks": len(active_task_ids),
            "lingering_containers": lingering_containers,
            "cleanup_tasks_dispatched": cleanup_tasks_dispatched if auto_cleanup else [],
            "message": f"Found {len(lingering_containers)} lingering containers" + 
                      (f", dispatched {len(cleanup_tasks_dispatched)} cleanup tasks" if auto_cleanup else "")
        }
    
    except Exception as e:
        logger.error(f"Error in discover_lingering_containers_task: {e}", exc_info=True)
        return {
            "success": False,
            "namespace": namespace,
            "total_containers": 0,
            "active_tasks": 0,
            "lingering_containers": [],
            "cleanup_tasks_dispatched": [],
            "message": f"Error: {str(e)}"
        }


@celery_app.task(name="containerd.cleanup_lingering_containers")
@log_to_file(logger)
@handle_errors("cleanup_lingering_containers", "CONTAINER_CLEANUP_ERROR")
def cleanup_lingering_containers_task(namespace: str = "Production") -> Dict[str, Any]:
    """Clean up containers that don't have corresponding active tasks.
    
    This task:
    1. Lists all containers in the namespace
    2. Lists all active tasks in the namespace
    3. Identifies containers without active tasks (lingering containers)
    4. Deletes those containers
    
    Args:
        namespace: Containerd namespace to clean up (default: "Production")
        
    Returns:
        Dictionary with cleanup results:
        {
            "success": True/False,
            "namespace": "Production",
            "total_containers": 10,
            "active_tasks": 8,
            "lingering_containers": ["cid1", "cid2"],
            "deleted_containers": ["cid1", "cid2"],
            "failed_deletions": [],
            "message": "..."
        }
    """
    try:
        logger.info(f"Starting cleanup of lingering containers in namespace: {namespace}")
        
        # Create containerd client for the namespace
        client = ContainerdClient(namespace=namespace)
        runtime = RuntimeManager(client)
        
        # Get all containers
        container_ids = _list_all_containers(client)
        total_containers = len(container_ids)
        logger.info(f"Found {total_containers} containers in namespace {namespace}")
        
        # Get all active tasks
        active_task_ids = _list_all_tasks(client)
        logger.info(f"Found {len(active_task_ids)} active tasks in namespace {namespace}")
        
        # Find lingering containers (containers without active tasks)
        lingering_containers = [cid for cid in container_ids if cid not in active_task_ids]
        logger.info(f"Found {len(lingering_containers)} lingering containers without active tasks")
        
        if not lingering_containers:
            logger.info(f"No lingering containers found in namespace {namespace}")
            return {
                "success": True,
                "namespace": namespace,
                "total_containers": total_containers,
                "active_tasks": len(active_task_ids),
                "lingering_containers": [],
                "deleted_containers": [],
                "failed_deletions": [],
                "message": "No lingering containers found"
            }
        
        # Delete lingering containers
        deleted_containers = []
        failed_deletions = []
        
        for cid in lingering_containers:
            try:
                logger.info(f"Deleting lingering container: {cid}")
                
                # Try to delete using runtime manager (handles tasks first, then containers)
                try:
                    # Try deleting task first (if exists) - use runtime manager
                    try:
                        runtime.delete_task_only(cid)
                    except Exception:
                        pass  # Task may not exist, which is fine
                    
                    # Delete container object
                    try:
                        client.containers.Delete(containers_pb2.DeleteContainerRequest(id=cid))
                        deleted_containers.append(cid)
                        logger.info(f"Successfully deleted lingering container: {cid}")
                    except grpc.RpcError as e:
                        if e.code() == grpc.StatusCode.NOT_FOUND:
                            # Container already deleted, consider it success
                            deleted_containers.append(cid)
                            logger.info(f"Container {cid} already deleted (not found)")
                        else:
                            raise
                except grpc.RpcError as e:
                    failed_deletions.append({"container_id": cid, "error": f"{e.code().name}: {e.details()}"})
                    logger.warning(f"Failed to delete container {cid}: {e.code().name}: {e.details()}")
                    
            except Exception as e:
                failed_deletions.append({"container_id": cid, "error": str(e)})
                logger.error(f"Error deleting lingering container {cid}: {e}", exc_info=True)
        
        result = {
            "success": len(failed_deletions) == 0,
            "namespace": namespace,
            "total_containers": total_containers,
            "active_tasks": len(active_task_ids),
            "lingering_containers": lingering_containers,
            "deleted_containers": deleted_containers,
            "failed_deletions": failed_deletions,
            "message": f"Deleted {len(deleted_containers)} lingering containers, {len(failed_deletions)} failed"
        }
        
        logger.info(
            f"Cleanup completed for namespace {namespace}: "
            f"{len(deleted_containers)} containers deleted, "
            f"{len(failed_deletions)} deletions failed"
        )
        
        return result
    
    except Exception as e:
        logger.error(f"Error in cleanup_lingering_containers_task: {e}", exc_info=True)
        return {
            "success": False,
            "namespace": namespace,
            "total_containers": 0,
            "active_tasks": 0,
            "lingering_containers": [],
            "deleted_containers": [],
            "failed_deletions": [],
            "message": f"Error: {str(e)}"
        }


@celery_app.task(name="containerd.dispatch_cleanup_to_workers")
@log_to_file(logger)
@handle_errors("dispatch_cleanup_to_workers", "CONTAINER_CLEANUP_ERROR")
def dispatch_cleanup_to_workers_task(namespace: str = "Production") -> Dict[str, Any]:
    """Discover lingering containers on worker nodes and dispatch cleanup tasks.
    
    This task runs on aws_worker (scheduler_queue_name) and:
    1. Discovers all active Celery worker nodes
    2. For each worker, identifies lingering containers (containers without active tasks)
    3. Sends destroy_container_by_id_task to each worker's hostname_queue_name for each lingering container
    4. Each worker processes cleanup from its hostname_queue_name and deletes the container
    
    Args:
        namespace: Containerd namespace to clean up (default: "Production")
        
    Returns:
        Dictionary with dispatch results:
        {
            "success": True/False,
            "workers_discovered": ["ip-172-31-16-125", ...],
            "lingering_containers_found": {"ip-172-31-16-125": ["cid1", "cid2"], ...},
            "tasks_dispatched": {"ip-172-31-16-125": ["cid1", "cid2"], ...},
            "failed_dispatches": [],
            "message": "..."
        }
    """
    try:
        logger.info(f"Starting cleanup dispatch for namespace: {namespace}")
        
        # Import task here to avoid circular imports
        from utils.celery.tasks.containerd_tasks import list_pods_by_namespace_task
        from utils.celery.tasks.containerd_tasks import destroy_container_by_id_task
        
        # Get all active Celery worker nodes
        active_nodes = get_celery_nodes()
        
        if not active_nodes:
            logger.warning("No active worker nodes found for cleanup dispatch")
            return {
                "success": False,
                "workers_discovered": [],
                "lingering_containers_found": {},
                "tasks_dispatched": {},
                "failed_dispatches": [],
                "message": "No active worker nodes found"
            }
        
        logger.info(f"Found {len(active_nodes)} active worker nodes: {active_nodes}")
        
        # Extract hostnames (remove @ prefix if present)
        worker_hostnames = []
        for node in active_nodes:
            # node might be "celery@ip-172-31-16-125" or just "ip-172-31-16-125"
            hostname = re.sub(r'^.*@', '', node)
            worker_hostnames.append(hostname)
        
        lingering_containers_found = {}  # {hostname: [cid1, cid2, ...]}
        tasks_dispatched = {}  # {hostname: [cid1, cid2, ...]}
        failed_dispatches = []
        
        # For each worker, discover lingering containers and auto-dispatch cleanup tasks
        # We use auto_cleanup=True so discovery task dispatches cleanup tasks automatically
        # This avoids blocking on result.get() which is not allowed in Celery tasks
        for hostname in worker_hostnames:
            try:
                # Create hostname-specific queue info for discovery
                hostname_queue_name = encode_util.encode_hostname_with_key(hostname)
                host_queue_info = {
                    'exchange': secure_exchange,
                    'queue': hostname_queue_name,
                    'routing_key': hostname_queue_name,
                    'delivery_mode': 2,
                    'expires': 60,  # Expire if not processed within 60 seconds
                }
                
                # Send discovery task with auto_cleanup=True
                # This will discover lingering containers AND automatically dispatch cleanup tasks
                # We fire-and-forget since we can't use result.get() in a Celery task
                try:
                    discovery_result = discover_lingering_containers_task.apply_async(
                        args=(namespace, True),  # namespace, auto_cleanup=True
                        **host_queue_info
                    )
                    logger.info(f"Dispatched discovery task with auto-cleanup to worker {hostname} (task_id: {discovery_result.id})")
                    # Note: We don't wait for results since that would require result.get() which blocks
                    # The discovery task will handle cleanup automatically via auto_cleanup=True
                    tasks_dispatched[hostname] = ["auto-cleanup-enabled"]  # Mark that cleanup was auto-dispatched
                except Exception as e:
                    failed_dispatches.append({"hostname": hostname, "cid": None, "error": f"Failed to dispatch discovery task: {str(e)}"})
                    logger.error(f"Failed to dispatch discovery task to worker {hostname}: {e}", exc_info=True)
                
            except Exception as e:
                failed_dispatches.append({"hostname": hostname, "cid": None, "error": str(e)})
                logger.error(f"Failed to process worker {hostname}: {e}", exc_info=True)
        
        result = {
            "success": len(failed_dispatches) == 0,
            "workers_discovered": worker_hostnames,
            "lingering_containers_found": lingering_containers_found,
            "tasks_dispatched": tasks_dispatched,
            "failed_dispatches": failed_dispatches,
            "message": f"Discovered {sum(len(cids) for cids in lingering_containers_found.values())} lingering containers, dispatched {sum(len(cids) for cids in tasks_dispatched.values())} cleanup tasks, {len(failed_dispatches)} failed"
        }
        
        logger.info(
            f"Cleanup dispatch completed: {sum(len(cids) for cids in lingering_containers_found.values())} containers found, "
            f"{sum(len(cids) for cids in tasks_dispatched.values())} cleanup tasks dispatched, "
            f"{len(failed_dispatches)} failed"
        )
        
        return result
    
    except Exception as e:
        logger.error(f"Error in dispatch_cleanup_to_workers_task: {e}", exc_info=True)
        return {
            "success": False,
            "workers_discovered": [],
            "lingering_containers_found": {},
            "tasks_dispatched": {},
            "failed_dispatches": [],
            "message": f"Error: {str(e)}"
        }
