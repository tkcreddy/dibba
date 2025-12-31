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
from utils.celery.tasks.containerd_tasks import create_pod_task, terminate_pod_task
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
        
        running_pods = [p for p in namespace_pods if p.get("status", "").upper() == "RUNNING"]
        running_pods_count = len(running_pods)
        
        logger.info(f"Deployment {namespace}/{deployment_name}: {running_pods_count} running pods, min={min_replicas}, max={max_replicas}")
        logger.info(f"Found {len(pods)} total pods for app_label {app_label}, {len(namespace_pods)} in namespace {namespace}, {running_pods_count} running")
        
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
                running_pods = []
                for p in namespace_pods:
                    status = p.get("status")
                    if status:
                        status_str = str(status).strip().upper()
                        if status_str == "RUNNING":
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
        pods_terminated = 0
        
        # Priority: Scale down first if above max_replicas (this takes precedence)
        # Then scale up if below min_replicas
        # This prevents creating pods when we should be terminating them
        if running_pods_count > max_replicas:
            excess_replicas = running_pods_count - max_replicas
            logger.info(f"Scaling down: {running_pods_count} running pods, max_replicas={max_replicas}, need to terminate {excess_replicas} pods")
            logger.info(f"Current running pod IDs: {[p.get('pod_id') for p in running_pods]}")
            
            # Terminate excess pods (terminate oldest first)
            pods_to_terminate = sorted(running_pods, key=lambda p: p.get("creation_time", ""))[:excess_replicas]
            
            utilities = UtilitiesExtension()
            for pod in pods_to_terminate:
                pod_id = pod.get("pod_id")
                hostname = pod.get("hostname")
                
                if not pod_id or not hostname:
                    continue
                
                try:
                    host_queue_info = create_host_queue_info(hostname, utilities)
                    
                    result = submit_celery_task(
                        task=terminate_pod_task,
                        args=(namespace, pod_id),  # Correct order: namespace, pod_name
                        kwargs={},  # host_name is not a parameter of terminate_pod_task
                        queue_info=host_queue_info,
                        operation_name="terminate_pod",
                        error_code="TERMINATE_POD_TASK_ERROR",
                        additional_data={
                            "namespace": namespace,
                            "pod_name": pod_id,
                            "host_name": hostname,
                            "deployment_name": deployment_name,
                        }
                    )
                    
                    if result.get("data", {}).get("task_id"):
                        pods_terminated += 1
                        logger.info(f"Terminated pod {pod_id} for scaling down (task_id: {result.get('data', {}).get('task_id')})")
                    else:
                        logger.warning(f"Failed to terminate pod {pod_id}: no task_id returned")
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
            
            if not placement_by_index:
                logger.error(f"Could not find placement for {missing_replicas} missing replicas")
                return {
                    "status": "error",
                    "error": "Could not find placement for missing replicas",
                    "pods_created": 0,
                    "pods_terminated": 0,
                }
            
            # Convert placement from indices to hostnames and offset instance numbers
            # The distribution algorithm creates instances 0..(missing_replicas-1)
            # We need to offset by running_pods_count to avoid conflicts
            placement = {}
            total_placement_count = 0
            for node_idx, pod_info_list in placement_by_index.items():
                if node_idx >= len(worker_nodes_raw):
                    continue
                node = worker_nodes_raw[node_idx]
                hostname = node.get("hostname")
                if hostname:
                    # Offset instance numbers by running_pods_count
                    offset_pod_list = []
                    for app_name, instance_num in pod_info_list:
                        offset_instance_num = running_pods_count + instance_num
                        offset_pod_list.append((app_name, offset_instance_num))
                        total_placement_count += 1
                    placement[hostname] = offset_pod_list
                    logger.info(f"Placement for {hostname}: {len(offset_pod_list)} pods with instance numbers {[inst for _, inst in offset_pod_list]}")
            
            logger.info(f"Total placement count: {total_placement_count} pods (expected: {missing_replicas})")
            if total_placement_count != missing_replicas:
                logger.warning(f"Mismatch: placement created {total_placement_count} pods but expected {missing_replicas}. This may indicate an issue with the distribution algorithm.")
            
            # Create pods
            utilities = UtilitiesExtension()
            for hostname, pod_info_list in placement.items():
                for app_name, instance_num in pod_info_list:
                    try:
                        host_queue_info = create_host_queue_info(hostname, utilities)
                        
                        # Get labels from deployment
                        labels = {
                            "app": app_label,
                            "app_label": app_label,
                            "instance": str(instance_num),
                        }
                        
                        result = submit_celery_task(
                            task=create_pod_task,
                            args=(containers, namespace),
                            kwargs={"host_name": hostname, "labels": labels},
                            queue_info=host_queue_info,
                            operation_name="create_pod",
                            error_code="CREATE_POD_TASK_ERROR",
                            additional_data={
                                "namespace": namespace,
                                "app_label": app_label,
                                "instance_num": instance_num,
                                "deployment_name": deployment_name,
                            }
                        )
                        
                        if result.get("data", {}).get("task_id"):
                            pods_created += 1
                            logger.info(f"Created pod for {app_label} instance {instance_num} on {hostname} (task_id: {result.get('data', {}).get('task_id')})")
                        else:
                            logger.warning(f"Failed to create pod for {app_label} instance {instance_num}: no task_id returned")
                    except Exception as e:
                        logger.error(f"Failed to create pod for {app_label} instance {instance_num}: {e}")
            
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

