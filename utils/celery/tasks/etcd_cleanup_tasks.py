"""
Celery tasks for cleaning up orphaned Calico nodes from etcd.

This module provides tasks to:
- Compare active worker nodes in Redis with Calico nodes in etcd
- Remove Calico nodes that are not in the active worker pool
"""
from typing import Dict, Any, List, Set
from socket import gethostname
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore, HostStatus
from utils.etcd.etcd_interface import EtcdInterface, get_etcd_interface_from_config
from utils.error_handlers import handle_errors

logger = LogKCld()


@celery_app.task(name="etcd.cleanup_orphaned_nodes")
@log_to_file(logger)
@handle_errors("cleanup_orphaned_nodes", "ETCD_CLEANUP_ERROR")
def cleanup_orphaned_calico_nodes_task() -> Dict[str, Any]:
    """Clean up Calico nodes from etcd that are not in the active worker pool.
    
    This task:
    1. Gets all active worker nodes from Redis
    2. Gets all Calico nodes from etcd
    3. Compares them and identifies orphaned nodes (in etcd but not in Redis)
    4. Deletes orphaned nodes from etcd
    
    Returns:
        Dictionary with cleanup results:
        {
            "success": True/False,
            "active_workers": ["ip-172-31-16-125", ...],
            "calico_nodes": ["ip-172-31-16-125", ...],
            "orphaned_nodes": ["ip-172-31-18-50", ...],
            "deleted_nodes": ["ip-172-31-18-50", ...],
            "failed_deletions": ["ip-172-31-18-53", ...],
            "message": "..."
        }
    """
    try:
        # Get current hostname (where this task is running - should not be deleted)
        current_hostname = gethostname()
        logger.info(f"Current hostname (health_check node): {current_hostname}")
        
        # Initialize Redis interface
        redis_interface = RedisInterface()
        host_pod_store = HostPodStore(redis_interface)
        
        # Get all active worker nodes from Redis
        hosts = host_pod_store.get_all_hosts()
        active_worker_hostnames: Set[str] = set()
        
        for host in hosts:
            # Only include online hosts
            if host.get("status") == HostStatus.ONLINE.value:
                hostname = host.get("hostname")
                if hostname:
                    active_worker_hostnames.add(hostname)
        
        # Always include the current hostname (health_check node) to prevent deletion
        active_worker_hostnames.add(current_hostname)
        
        logger.info(f"Found {len(active_worker_hostnames)} active worker nodes in Redis (including current node): {sorted(active_worker_hostnames)}")
        
        # Get etcd interface
        etcd_interface = get_etcd_interface_from_config()
        if not etcd_interface:
            logger.warning("etcd interface not available, skipping cleanup")
            return {
                "success": False,
                "message": "etcd interface not available",
                "active_workers": list(active_worker_hostnames),
                "calico_nodes": [],
                "orphaned_nodes": [],
                "deleted_nodes": [],
                "failed_deletions": [],
                "deleted_blocks": [],
                "failed_block_deletions": [],
                "current_hostname": None
            }
        
        # Get all Calico nodes from etcd
        calico_nodes = etcd_interface.get_all_calico_nodes()
        calico_node_names: Set[str] = set(calico_nodes.keys())
        
        logger.info(f"Found {len(calico_node_names)} Calico nodes in etcd: {sorted(calico_node_names)}")
        
        # Find orphaned nodes (in etcd but not in active workers)
        orphaned_nodes = calico_node_names - active_worker_hostnames
        
        # Double-check: Never delete the current hostname (health_check node)
        if current_hostname in orphaned_nodes:
            logger.warning(f"Current hostname {current_hostname} was marked for deletion - removing from orphaned list")
            orphaned_nodes.discard(current_hostname)
        
        if not orphaned_nodes:
            logger.info("No orphaned Calico nodes found - all etcd nodes are in the active worker pool")
            return {
                "success": True,
                "message": "No orphaned nodes found",
                "active_workers": sorted(active_worker_hostnames),
                "calico_nodes": sorted(calico_node_names),
                "orphaned_nodes": [],
                "deleted_nodes": [],
                "failed_deletions": [],
                "deleted_blocks": [],
                "failed_block_deletions": [],
                "current_hostname": current_hostname
            }
        
        logger.warning(f"Found {len(orphaned_nodes)} orphaned Calico nodes (excluding current hostname {current_hostname}): {sorted(orphaned_nodes)}")
        
        # Delete orphaned nodes from etcd and their associated IPAM blocks
        deleted_nodes = []
        failed_deletions = []
        deleted_blocks = []
        failed_block_deletions = []
        
        for node_name in orphaned_nodes:
            try:
                # First, get all IPAM blocks for this node
                node_blocks = etcd_interface.get_ipam_blocks_for_node(node_name)
                logger.info(f"Node {node_name} has {len(node_blocks)} IPAM blocks: {node_blocks}")
                
                # Delete the node
                success = etcd_interface.delete_calico_node(node_name)
                if success:
                    deleted_nodes.append(node_name)
                    logger.info(f"Successfully deleted orphaned Calico node: {node_name}")
                    
                    # Delete associated IPAM blocks
                    for block_cidr in node_blocks:
                        try:
                            block_success = etcd_interface.delete_ipam_block(block_cidr)
                            if block_success:
                                deleted_blocks.append(block_cidr)
                                logger.info(f"Successfully deleted IPAM block {block_cidr} for node {node_name}")
                            else:
                                failed_block_deletions.append(block_cidr)
                                logger.warning(f"Failed to delete IPAM block {block_cidr} for node {node_name}")
                        except Exception as e:
                            failed_block_deletions.append(block_cidr)
                            logger.error(f"Error deleting IPAM block {block_cidr} for node {node_name}: {e}", exc_info=True)
                else:
                    failed_deletions.append(node_name)
                    logger.warning(f"Failed to delete Calico node {node_name} (node may not exist)")
            except Exception as e:
                failed_deletions.append(node_name)
                logger.error(f"Error deleting Calico node {node_name}: {e}", exc_info=True)
        
        result = {
            "success": len(failed_deletions) == 0 and len(failed_block_deletions) == 0,
            "message": f"Deleted {len(deleted_nodes)} orphaned nodes ({len(deleted_blocks)} IPAM blocks), "
                      f"{len(failed_deletions)} node deletions failed, {len(failed_block_deletions)} block deletions failed",
            "active_workers": sorted(active_worker_hostnames),
            "calico_nodes": sorted(calico_node_names),
            "orphaned_nodes": sorted(orphaned_nodes),
            "deleted_nodes": sorted(deleted_nodes),
            "failed_deletions": sorted(failed_deletions),
            "deleted_blocks": sorted(deleted_blocks),
            "failed_block_deletions": sorted(failed_block_deletions),
            "current_hostname": current_hostname
        }
        
        logger.info(
            f"Cleanup completed: {len(deleted_nodes)} nodes deleted ({len(deleted_blocks)} IPAM blocks), "
            f"{len(failed_deletions)} node deletions failed, {len(failed_block_deletions)} block deletions failed, "
            f"{len(active_worker_hostnames)} active workers remain"
        )
        
        return result
    
    except Exception as e:
        logger.error(f"Error in cleanup_orphaned_calico_nodes_task: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "active_workers": [],
            "calico_nodes": [],
            "orphaned_nodes": [],
            "deleted_nodes": [],
            "failed_deletions": [],
            "deleted_blocks": [],
            "failed_block_deletions": [],
            "current_hostname": None
        }

