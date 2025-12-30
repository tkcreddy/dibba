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
from utils.celery.tasks.containerd_tasks import create_pod_task
from utils.celery.queue_utils import create_host_queue_info, submit_celery_task
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.ReadConfig import ReadConfig as rc

logger = LogKCld()


@celery_app.task(name="deployment.recover_missing_replicas")
@log_to_file(logger)
def recover_missing_replicas_task() -> Dict[str, Any]:
    """Recover missing replicas for all deployments.
    
    This task:
    1. Gets all deployments from Redis
    2. Counts running pods for each deployment
    3. Compares with min_replicas requirement
    4. Uses distribute_nodes_services to find placement for missing pods
    5. Creates missing pods on available nodes
    
    Returns:
        Dictionary with recovery results
    """
    try:
        logger.info("=" * 80)
        logger.info("DEPLOYMENT RECOVERY: Checking for missing replicas")
        logger.info("=" * 80)
        
        redis_interface = RedisInterface()
        deployment_store = DeploymentStore(redis_interface)
        host_pod_store = HostPodStore(redis_interface)
        
        # Get all deployments from Redis
        deployments = deployment_store.get_all_deployments()
        logger.info(f"Found {len(deployments)} deployments in Redis")
        
        recovery_results = {
            'deployments_checked': len(deployments),
            'deployments_recovered': 0,
            'pods_recreated': 0,
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
                    running_pods = [
                        pod for pod in app_pods
                        if pod.get('status') == 'RUNNING' and
                        pod.get('namespace') == namespace
                    ]
                except Exception as e:
                    logger.warning(f"Failed to get pods by app, trying by namespace: {e}")
                    # Fallback: get by namespace and filter by app label
                    all_pods = host_pod_store.get_pods_by_namespace(namespace)
                    running_pods = [
                        pod for pod in all_pods
                        if pod.get('status') == 'RUNNING'
                    ]
                    # Try to match by labels if available
                    if running_pods and running_pods[0].get('labels'):
                        running_pods = [
                            pod for pod in running_pods
                            if pod.get('labels', {}).get('app') == app_label
                        ]
                
                current_replicas = len(running_pods)
                logger.info(f"Deployment {namespace}/{name}: {current_replicas} running pods (min required: {min_replicas})")
                
                # Check if we need to recover pods
                if current_replicas < min_replicas:
                    missing_count = min_replicas - current_replicas
                    logger.warning(f"Deployment {namespace}/{name}: Missing {missing_count} replicas. Current: {current_replicas}, Required: {min_replicas}")
                    
                    # Get available worker nodes
                    worker_nodes = get_worker_nodes_from_redis(redis_interface)
                    if not worker_nodes:
                        logger.error(f"Deployment {namespace}/{name}: No worker nodes available for recovery")
                        recovery_results['errors'].append({
                            'deployment': f"{namespace}/{name}",
                            'error': 'No worker nodes available'
                        })
                        continue
                    
                    # Convert to millicores and MB for distribution
                    worker_nodes_millicores = [
                        {
                            'cpu': int(node['cpu'] * 1000),  # Convert cores to millicores
                            'memory': node['memory'],  # Already in MB
                            'hostname': node.get('hostname'),
                            'ip_address': node.get('ip_address'),
                        }
                        for node in worker_nodes
                    ]
                    
                    # Prepare cluster_info for distribution
                    cpu_millicores = resource_reqs.get('cpu_millicores', 0)
                    memory_mb = resource_reqs.get('memory_mb', 0)
                    
                    cluster_info = {
                        app_label: {
                            'cpu': cpu_millicores,
                            'memory': memory_mb,
                            'instances': missing_count
                        }
                    }
                    
                    # Use distribute_nodes_services to find placement
                    distribution = ClusterWorkerDistribution(worker_nodes_millicores, cluster_info)
                    placement_by_index = distribution.distribute_cluster_nodes()
                    
                    if not placement_by_index:
                        logger.error(f"Deployment {namespace}/{name}: Could not find placement for {missing_count} missing replicas")
                        recovery_results['errors'].append({
                            'deployment': f"{namespace}/{name}",
                            'error': 'Could not find placement for missing replicas'
                        })
                        continue
                    
                    # Convert placement from indices to hostnames
                    placement = {}
                    for node_index, placements in placement_by_index.items():
                        if node_index < len(worker_nodes):
                            hostname = worker_nodes[node_index]['hostname']
                            placement[hostname] = placements
                    
                    # Create missing pods
                    pods_recreated = 0
                    read_config = rc()
                    key = read_config.encryption_config['key']
                    encode_util = UtilitiesExtension(key)
                    
                    for hostname, placements in placement.items():
                        host_info = next((n for n in worker_nodes if n.get('hostname') == hostname), None)
                        if not host_info:
                            continue
                        
                        for app_name, instance_num in placements:
                            try:
                                # Find the next available instance number (avoid conflicts)
                                existing_instances = [p.get('labels', {}).get('instance') for p in running_pods]
                                # Use a high instance number to avoid conflicts
                                recovery_instance_num = max([int(i) for i in existing_instances if i and str(i).isdigit()], default=-1) + 1 + instance_num
                                
                                # Prepare container specs
                                container_specs = []
                                for container in containers:
                                    container_specs.append({
                                        'name': container.get('name'),
                                        'image': container.get('image'),
                                        'args': container.get('args', []),
                                        'env': container.get('env', {}),
                                        'resources': container.get('resources', {}),
                                        'ports': container.get('ports', []),
                                    })
                                
                                # Create pod
                                host_queue_info = create_host_queue_info(hostname, encode_util)
                                result = submit_celery_task(
                                    task=create_pod_task,
                                    args=(
                                        namespace,
                                        f"{name}-recovery-{recovery_instance_num}",
                                        container_specs,
                                    ),
                                    kwargs={
                                        'cni_network': {'name': 'calico', 'ifname': 'eth0'},
                                        'labels': {'app': app_label, 'instance': str(recovery_instance_num)},
                                        'resources': {
                                            'cpu_millicores': cpu_millicores,
                                            'memory': f"{int(memory_mb)}Mi"
                                        },
                                    },
                                    queue_info=host_queue_info,
                                    operation_name="recover_pod",
                                    error_code="POD_RECOVERY_ERROR",
                                    additional_data={
                                        'deployment': name,
                                        'replica': recovery_instance_num,
                                        'hostname': hostname,
                                        'recovery': True,
                                    }
                                )
                                
                                pods_recreated += 1
                                logger.info(f"Recovered pod for {namespace}/{name} instance {recovery_instance_num} on {hostname}")
                                
                            except Exception as e:
                                logger.error(f"Failed to recover pod for {namespace}/{name} on {hostname}: {e}", exc_info=True)
                                recovery_results['errors'].append({
                                    'deployment': f"{namespace}/{name}",
                                    'hostname': hostname,
                                    'error': str(e)
                                })
                    
                    if pods_recreated > 0:
                        recovery_results['deployments_recovered'] += 1
                        recovery_results['pods_recreated'] += pods_recreated
                        logger.info(f"Recovered {pods_recreated} pods for deployment {namespace}/{name}")
                else:
                    logger.info(f"Deployment {namespace}/{name}: Sufficient replicas ({current_replicas} >= {min_replicas})")
            
            except Exception as e:
                logger.error(f"Failed to process deployment {namespace}/{name}: {e}", exc_info=True)
                recovery_results['errors'].append({
                    'deployment': f"{namespace}/{name}",
                    'error': str(e)
                })
        
        logger.info(f"Recovery complete: {recovery_results['pods_recreated']} pods recreated for {recovery_results['deployments_recovered']} deployments")
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

