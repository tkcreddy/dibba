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
            
            # If placement fails, try to create AWS nodes
            if not placement_by_index:
                logger.warning(f"Could not find placement for {missing_replicas} missing replicas on existing nodes. Attempting to create AWS nodes...")
                
                # Calculate required AWS nodes
                required_nodes = max(1, (missing_replicas + 1) // 2)
                logger.info(f"Creating {required_nodes} AWS node(s) to accommodate {missing_replicas} missing replicas")
                
                # Get AWS config
                from utils.celery.tasks.aws_tasks import create_worker_nodes
                
                read_config = rc()
                aws_config = read_config.aws_config
                
                if not aws_config:
                    logger.error("AWS configuration not found. Cannot create nodes.")
                    return {
                        "status": "error",
                        "error": "AWS configuration not found. Cannot create nodes for scaling.",
                        "pods_created": 0,
                        "pods_terminated": 0,
                    }
                
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
                            break
                        else:
                            aws_status = "error"
                            error_msg = str(aws_task.result) if aws_task.result else "Unknown error"
                            logger.error(f"AWS node creation task {aws_task_id} failed: {error_msg}")
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
            
            # Create pods
            utilities = UtilitiesExtension()
            pods_created_tasks = []  # Track all task IDs for verification
            for hostname, pod_info_list in placement.items():
                logger.info(f"Creating {len(pod_info_list)} pods on {hostname}")
                for app_name, instance_num in pod_info_list:
                    try:
                        host_queue_info = create_host_queue_info(hostname, utilities)
                        
                        # Get labels from deployment
                        labels = {
                            "app": app_label,
                            "app_label": app_label,
                            "instance": str(instance_num),
                        }
                        
                        logger.info(f"Submitting create_pod_task for {app_label} instance {instance_num} on {hostname}")
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
                        
                        task_id = result.get("data", {}).get("task_id")
                        if task_id:
                            pods_created += 1
                            pods_created_tasks.append({"hostname": hostname, "instance": instance_num, "task_id": task_id})
                            logger.info(f"Successfully submitted pod creation for {app_label} instance {instance_num} on {hostname} (task_id: {task_id})")
                        else:
                            logger.error(f"Failed to create pod for {app_label} instance {instance_num} on {hostname}: no task_id returned. Result: {result}")
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

