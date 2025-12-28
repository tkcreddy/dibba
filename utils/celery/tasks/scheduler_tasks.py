"""
Celery tasks for deployment scheduling using chains.

This module provides chainable tasks for:
1. Evaluating deployment requirements from existing resources
2. Creating AWS nodes if needed
3. Placing and creating pods on hosts
"""
import yaml
from typing import Dict, Any, List, Optional, Tuple
from logpkg.log_kcld import LogKCld, log_to_file
from utils.celery.celery_config import celery_app
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore, HostStatus
from server.nodes.distribute_nodes_services import ClusterWorkerDistribution,get_worker_nodes_from_redis
from server.sched.scheduler import (
    DeploymentParser,
    ResourceConverter,
    HostResourceCalculator,
    DeploymentSpec,
    ResourceRequirements
)
from utils.celery.tasks.aws_tasks import create_worker_nodes
from utils.celery.tasks.containerd_tasks import create_pod_task
from utils.celery.queue_utils import create_host_queue_info, submit_celery_task
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.ReadConfig import ReadConfig as rc

logger = LogKCld()


@celery_app.task(name="sched.evaluate_deployment_requirements")
@log_to_file(logger)
def evaluate_deployment_requirements_task(yaml_content: str) -> Dict[str, Any]:
    """Evaluate deployment requirements and check existing resources.
    
    This is the first task in the DEPLOYMENT SCHEDULING chain. It:
    1. Parses the deployment YAML
    2. Queries Redis for available resources
    3. Determines if AWS nodes are needed
    4. Returns evaluation results for next task
    
    NOTE: This task is part of the deployment scheduling workflow.
    It does NOT terminate any pods or nodes.
    
    Args:
        yaml_content: Deployment YAML string
        
    Returns:
        Dictionary with:
            - deployment: Parsed deployment spec
            - available_hosts: List of available hosts with resources
            - needs_aws_nodes: Boolean indicating if AWS nodes are needed
            - required_nodes: Number of AWS nodes needed (if any)
            - placement: Placement result (if resources sufficient)
    """
    try:
        logger.info("=" * 80)
        logger.info("DEPLOYMENT SCHEDULING CHAIN - Step 1: Evaluating Requirements")
        logger.info("=" * 80)
        # Parse YAML
        logger.info("Step 1: Parsing deployment YAML...")
        deployment = DeploymentParser.parse_yaml(yaml_content)
        logger.info(
            f"Deployment: {deployment.name}, "
            f"Replicas: {deployment.replicas}, "
            f"CPU: {deployment.resource_requirements.cpu_millicores} millicores ({deployment.resource_requirements.cpu_cores} cores), "
            f"Memory: {deployment.resource_requirements.memory_mb} MB"
        )
        
        # Get available resources using distribute_nodes_services
        logger.info("Step 1: Querying available resources from Redis using distribute_nodes_services...")
        redis_interface = RedisInterface()
        worker_nodes_raw = get_worker_nodes_from_redis(redis_interface)
        
        if not worker_nodes_raw:
            logger.warning("Step 1: No online hosts found in Redis")
            # Calculate required nodes
            num_nodes = max(1, (deployment.replicas + 1) // 2)
            return {
                'status': 'needs_aws_nodes',
                'deployment': {
                    'name': deployment.name,
                    'namespace': deployment.namespace,
                    'app_label': deployment.app_label,
                    'replicas': deployment.replicas,
                    'containers': deployment.containers,
                    'resource_requirements': {
                        'cpu_millicores': deployment.resource_requirements.cpu_millicores,
                        'memory_mb': deployment.resource_requirements.memory_mb,
                        'cpu_cores': deployment.resource_requirements.cpu_cores,
                        'memory_bytes': deployment.resource_requirements.memory_bytes,
                    }
                },
                'available_hosts': [],
                'needs_aws_nodes': True,
                'required_nodes': num_nodes,
                'placement': None,
            }
        
        # Convert worker nodes to millicores and MB format
        # get_worker_nodes_from_redis returns CPU in cores, convert to millicores
        worker_nodes = [
            {
                'cpu': int(node['cpu'] * 1000),  # Convert cores to millicores
                'memory': node['memory'],  # Already in MB
                'hostname': node.get('hostname'),
                'ip_address': node.get('ip_address'),
            }
            for node in worker_nodes_raw
        ]
        
        # Prepare cluster_info using millicores and MB
        cluster_info = {
            deployment.app_label: {
                'cpu': deployment.resource_requirements.cpu_millicores,  # Use millicores
                'memory': deployment.resource_requirements.memory_mb,  # Use MB
                'instances': deployment.replicas
            }
        }
        
        # Store available_hosts for later use (with both millicores and cores for reference)
        available_hosts = [
            {
                'hostname': raw_node.get('hostname'),
                'ip_address': raw_node.get('ip_address'),
                'cpu_cores': raw_node['cpu'],  # Original cores from Redis
                'cpu_millicores': int(raw_node['cpu'] * 1000),  # Converted to millicores
                'memory_mb': raw_node['memory'],
            }
            for raw_node in worker_nodes_raw
        ]
        
        # Try to distribute (using millicores and MB)
        logger.info("Step 1: Calculating distribution using millicores and MB...")
        logger.info(f"Step 1: Worker nodes: {len(worker_nodes)} hosts with total {sum(n['cpu'] for n in worker_nodes)} millicores CPU, {sum(n['memory'] for n in worker_nodes)} MB memory")
        logger.info(f"Step 1: Deployment requires: {deployment.resource_requirements.cpu_millicores * deployment.replicas} millicores CPU, {deployment.resource_requirements.memory_mb * deployment.replicas} MB memory")
        logger.info(f"Step 1: Worker nodes details: {[(i, n['hostname'], n['cpu'], n['memory']) for i, n in enumerate(worker_nodes)]}")
        distribution = ClusterWorkerDistribution(worker_nodes, cluster_info)
        placement_by_index = distribution.distribute_cluster_nodes()
        logger.info(f"Step 1: Distribution result (by index): {placement_by_index}")
        
        # Convert placement from node indices to hostnames
        placement = {}
        if placement_by_index:
            for node_index, placements in placement_by_index.items():
                if node_index < len(available_hosts):
                    hostname = available_hosts[node_index]['hostname']
                    placement[hostname] = placements
                    logger.info(f"Step 1: Node {node_index} ({hostname}) will host: {placements} (count: {len(placements)})")
                else:
                    logger.warning(f"Step 1: Node index {node_index} out of range for available_hosts (available: {len(available_hosts)})")
        else:
            logger.warning("Step 1: Distribution returned None or empty result")
        
        # Check if all replicas can be placed
        if not placement:
            needs_aws = True
            required_nodes = max(1, (deployment.replicas + 1) // 2)
        else:
            # Check if all replicas are placed
            total_placed = sum(len(placements) for placements in placement.values())
            if total_placed < deployment.replicas:
                needs_aws = True
                remaining = deployment.replicas - total_placed
                required_nodes = max(1, (remaining + 1) // 2)
            else:
                needs_aws = False
                required_nodes = 0
        
        # Build placement details with hostname and pod attributes
        placement_details = {}
        if placement:
            logger.info(f"Step 1: Building placement_details from placement: {placement}")
            for hostname, placements in placement.items():
                logger.info(f"Step 1: Processing hostname {hostname} with placements: {placements} (type: {type(placements)})")
                # Find the host info for this hostname
                host_info = next((h for h in available_hosts if h['hostname'] == hostname), None)
                if host_info:
                    # Convert placements to list if it's not already
                    if not isinstance(placements, list):
                        placements = list(placements) if placements else []
                    
                    pods_list = []
                    for placement_item in placements:
                        # Handle both tuple format (app_name, instance_num) and list format [app_name, instance_num]
                        if isinstance(placement_item, (tuple, list)) and len(placement_item) >= 2:
                            app_name = placement_item[0]
                            instance_num = placement_item[1]
                        else:
                            logger.warning(f"Step 1: Invalid placement item format: {placement_item}")
                            continue
                        
                        pods_list.append({
                            'app_name': app_name,
                            'instance_num': instance_num,
                            'resource_requirements': {
                                'cpu_millicores': deployment.resource_requirements.cpu_millicores,
                                'memory_mb': deployment.resource_requirements.memory_mb,
                            }
                        })
                    
                    placement_details[hostname] = {
                        'hostname': hostname,
                        'ip_address': host_info.get('ip_address'),
                        'cpu_cores': host_info.get('cpu_cores'),
                        'cpu_millicores': host_info.get('cpu_millicores'),
                        'memory_mb': host_info.get('memory_mb'),
                        'pods': pods_list
                    }
                    logger.info(f"Step 1: Added {hostname} to placement_details with {len(pods_list)} pods: {pods_list}")
                else:
                    logger.warning(f"Step 1: Could not find host_info for hostname {hostname}")
        
        logger.info(f"Step 1: Final placement_details: {list(placement_details.keys())} with pod counts: {[(k, len(v.get('pods', []))) for k, v in placement_details.items()]}")
        
        result = {
            'status': 'evaluated',
            'deployment': {
                'name': deployment.name,
                'namespace': deployment.namespace,
                'app_label': deployment.app_label,
                'replicas': deployment.replicas,
                'containers': deployment.containers,
                'resource_requirements': {
                    'cpu_millicores': deployment.resource_requirements.cpu_millicores,
                    'memory_mb': deployment.resource_requirements.memory_mb,
                    'cpu_cores': deployment.resource_requirements.cpu_cores,
                    'memory_bytes': deployment.resource_requirements.memory_bytes,
                }
            },
            'available_hosts': available_hosts,
            'needs_aws_nodes': needs_aws,
            'required_nodes': required_nodes,
            'placement': placement,  # Keep for backward compatibility
            'placement_details': placement_details,  # New: detailed placement with hostnames and pod attributes
        }
        
        if needs_aws:
            logger.info(f"Step 1: Need {required_nodes} AWS nodes")
        else:
            logger.info("Step 1: Sufficient resources available, no AWS nodes needed")
        
        logger.info(f"Step 1: Returning result with placement_details containing {len(placement_details)} hosts: {list(placement_details.keys())}")
        logger.info(f"Step 1: Result will be passed to Step 2 (create_aws_nodes_if_needed_task)")
        return result
    
    except Exception as e:
        logger.error(f"Step 1: Evaluation failed: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e),
            'needs_aws_nodes': False,
            'required_nodes': 0,
        }


@celery_app.task(name="sched.create_aws_nodes_if_needed")
@log_to_file(logger)
def create_aws_nodes_if_needed_task(evaluation_result: Dict[str, Any]) -> Dict[str, Any]:
    """Create AWS nodes if needed based on evaluation.
    
    This is the second task in the DEPLOYMENT SCHEDULING chain. It:
    1. Checks if AWS nodes are needed
    2. Creates AWS nodes if needed
    3. Returns updated evaluation result with AWS task info
    
    NOTE: This task is part of the deployment scheduling workflow.
    It does NOT terminate any pods or nodes.
    
    Args:
        evaluation_result: Result from evaluate_deployment_requirements_task
        
    Returns:
        Updated evaluation result with AWS node creation info
    """
    try:
        logger.info("=" * 80)
        logger.info("DEPLOYMENT SCHEDULING CHAIN - Step 2: Creating AWS Nodes (if needed)")
        logger.info("=" * 80)
        logger.info(f"Step 2: Received evaluation_result type: {type(evaluation_result)}, keys: {list(evaluation_result.keys()) if isinstance(evaluation_result, dict) else 'Not a dict'}")
        if evaluation_result.get('status') == 'error':
            logger.error("Step 2: Skipping AWS node creation due to previous error")
            return evaluation_result
        
        needs_aws = evaluation_result.get('needs_aws_nodes', False)
        
        if not needs_aws:
            logger.info("Step 2: No AWS nodes needed, proceeding to pod placement")
            logger.info(f"Step 2: Passing through placement_details with {len(evaluation_result.get('placement_details', {}))} hosts")
            return evaluation_result
        
        logger.info("Step 2: Creating AWS worker nodes...")
        
        deployment = evaluation_result.get('deployment', {})
        required_nodes = evaluation_result.get('required_nodes', 1)
        namespace = deployment.get('namespace', 'default')
        
        # Get AWS config
        read_config = rc()
        aws_config = read_config.aws_config
        
        # Submit AWS node creation task
        try:
            aws_result = submit_celery_task(
                task=create_worker_nodes,
                args=(
                    aws_config.get("aws_access_key_id"),
                    aws_config.get("aws_secret_access_key"),
                    aws_config.get("region"),
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
                operation_name="create_aws_nodes",
                error_code="AWS_NODE_CREATION_ERROR",
            )
            
            logger.info(f"Step 2: AWS nodes creation task submitted: {aws_result.get('task_id')}")
            
            # Update evaluation result with AWS info
            evaluation_result['aws_task_id'] = aws_result.get('task_id')
            evaluation_result['aws_nodes_created'] = required_nodes
            evaluation_result['aws_status'] = 'submitted'
            
            # Note: In production, you might want to wait for nodes to be ready
            # For now, we'll proceed and let the next task handle it
            
        except Exception as e:
            logger.error(f"Step 2: Failed to create AWS nodes: {e}", exc_info=True)
            evaluation_result['aws_status'] = 'error'
            evaluation_result['aws_error'] = str(e)
            # Continue anyway - might have some existing nodes
        
        return evaluation_result
    
    except Exception as e:
        logger.error(f"Step 2: AWS node creation task failed: {e}", exc_info=True)
        evaluation_result['status'] = 'error'
        evaluation_result['error'] = f"AWS node creation failed: {str(e)}"
        return evaluation_result


@celery_app.task(name="sched.place_and_create_pods")
@log_to_file(logger)
def place_and_create_pods_task(evaluation_result: Dict[str, Any]) -> Dict[str, Any]:
    """Place and create pods on hosts (existing + new).
    
    This is the third task in the DEPLOYMENT SCHEDULING chain. It:
    1. Gets all available hosts (existing + newly created)
    2. Recalculates placement if AWS nodes were created
    3. Creates pods on assigned hosts using containerd_tasks
    
    NOTE: This task is part of the deployment scheduling workflow.
    It CREATES pods, it does NOT terminate any pods or nodes.
    
    Args:
        evaluation_result: Result from create_aws_nodes_if_needed_task
        
    Returns:
        Dictionary with final scheduling results
    """
    try:
        logger.info("=" * 80)
        logger.info("DEPLOYMENT SCHEDULING CHAIN - Step 3: Placing and Creating Pods")
        logger.info("=" * 80)
        logger.info(f"Step 3: Received evaluation_result type: {type(evaluation_result)}, keys: {list(evaluation_result.keys()) if isinstance(evaluation_result, dict) else 'Not a dict'}")
        if evaluation_result.get('status') == 'error':
            logger.error("Step 3: Skipping pod creation due to previous error")
            return evaluation_result
        
        logger.info("Step 3: Placing and creating pods...")
        logger.info(f"Step 3: Received evaluation_result with placement_details: {bool(evaluation_result.get('placement_details'))}, placement: {bool(evaluation_result.get('placement'))}")
        if evaluation_result.get('placement_details'):
            pd = evaluation_result.get('placement_details', {})
            logger.info(f"Step 3: placement_details has {len(pd)} hosts: {list(pd.keys())}")
            for hostname, details in pd.items():
                logger.info(f"Step 3: Host {hostname} has {len(details.get('pods', []))} pods")
        
        deployment_data = evaluation_result.get('deployment', {})
        deployment_name = deployment_data.get('name', 'unknown')
        namespace = deployment_data.get('namespace', 'default')
        app_label = deployment_data.get('app_label', 'unknown')
        replicas = deployment_data.get('replicas', 1)
        containers = deployment_data.get('containers', [])
        resource_reqs = deployment_data.get('resource_requirements', {})
        
        # Reconstruct deployment spec for easier handling
        resource_requirements = ResourceRequirements(
            cpu_millicores=resource_reqs.get('cpu_millicores', 0),
            memory_mb=resource_reqs.get('memory_mb', 0),
            cpu_cores=resource_reqs.get('cpu_cores', 0),
            memory_bytes=resource_reqs.get('memory_bytes', 0),
        )
        
        # Get all available hosts (including newly created ones) using distribute_nodes_services
        redis_interface = RedisInterface()
        worker_nodes_raw = get_worker_nodes_from_redis(redis_interface)
        
        if not worker_nodes_raw:
            logger.warning("Step 3: No hosts available after AWS node creation")
            return {
                'status': 'error',
                'error': 'No hosts available for pod placement',
                'deployment': deployment_name,
            }
        
        # Convert worker nodes to millicores and MB format
        worker_nodes = [
            {
                'cpu': int(node['cpu'] * 1000),  # Convert cores to millicores
                'memory': node['memory'],  # Already in MB
                'hostname': node.get('hostname'),
                'ip_address': node.get('ip_address'),
            }
            for node in worker_nodes_raw
        ]
        
        # Store available_hosts for later use (with both millicores and cores for reference)
        all_available_hosts = [
            {
                'hostname': raw_node.get('hostname'),
                'ip_address': raw_node.get('ip_address'),
                'cpu_cores': raw_node['cpu'],  # Original cores from Redis
                'cpu_millicores': int(raw_node['cpu'] * 1000),  # Converted to millicores
                'memory_mb': raw_node['memory'],
            }
            for raw_node in worker_nodes_raw
        ]
        
        # If AWS nodes were created, recalculate placement
        aws_nodes_created = evaluation_result.get('aws_nodes_created', 0)
        if aws_nodes_created > 0:
            logger.info(f"Step 3: Recalculating placement with {len(worker_nodes)} hosts (including new AWS nodes)")
            
            # Prepare cluster_info using millicores and MB
            cluster_info = {
                app_label: {
                    'cpu': resource_requirements.cpu_millicores,  # Use millicores
                    'memory': resource_requirements.memory_mb,  # Use MB
                    'instances': replicas
                }
            }
            
            # Recalculate distribution (using millicores and MB)
            logger.info(f"Step 3: Recalculating distribution using millicores and MB...")
            logger.info(f"Step 3: Worker nodes: {len(worker_nodes)} hosts with total {sum(n['cpu'] for n in worker_nodes)} millicores CPU, {sum(n['memory'] for n in worker_nodes)} MB memory")
            logger.info(f"Step 3: Deployment requires: {resource_requirements.cpu_millicores * replicas} millicores CPU, {resource_requirements.memory_mb * replicas} MB memory")
            distribution = ClusterWorkerDistribution(worker_nodes, cluster_info)
            placement_by_index = distribution.distribute_cluster_nodes()
            
            # Convert placement from node indices to hostnames and build placement_details
            placement = {}
            placement_details = {}
            if placement_by_index:
                for node_index, placements in placement_by_index.items():
                    if node_index < len(all_available_hosts):
                        hostname = all_available_hosts[node_index]['hostname']
                        host_info = all_available_hosts[node_index]
                        placement[hostname] = placements
                        
                        # Build detailed placement info
                        placement_details[hostname] = {
                            'hostname': hostname,
                            'ip_address': host_info.get('ip_address'),
                            'cpu_cores': host_info.get('cpu_cores'),
                            'cpu_millicores': host_info.get('cpu_millicores'),
                            'memory_mb': host_info.get('memory_mb'),
                            'pods': [
                                {
                                    'app_name': app_name,
                                    'instance_num': instance_num,
                                    'resource_requirements': {
                                        'cpu_millicores': resource_requirements.cpu_millicores,
                                        'memory_mb': resource_requirements.memory_mb,
                                    }
                                }
                                for app_name, instance_num in placements
                            ]
                        }
                        logger.info(f"Step 3: Node {node_index} ({hostname}) will host: {placements}")
                    else:
                        logger.warning(f"Step 3: Node index {node_index} out of range for all_available_hosts")
            
            # Update evaluation_result with new placement details
            evaluation_result['placement'] = placement
            evaluation_result['placement_details'] = placement_details
            evaluation_result['available_hosts'] = all_available_hosts
            
            if not placement:
                return {
                    'status': 'error',
                    'error': 'Could not place all replicas even after AWS node creation',
                    'deployment': deployment_name,
                }
        else:
            # Use existing placement (should already have placement_details from Step 1)
            placement = evaluation_result.get('placement')
            placement_details = evaluation_result.get('placement_details', {})
            if not placement:
                return {
                    'status': 'error',
                    'error': 'No placement available',
                    'deployment': deployment_name,
                }
        
        # Create pods on assigned hosts
        logger.info("Step 3: Creating pods on assigned hosts...")
        logger.info(f"Step 3: Placement details available: {bool(placement_details)}, keys: {list(placement_details.keys()) if placement_details else 'None'}")
        logger.info(f"Step 3: Placement available: {bool(placement)}, keys: {list(placement.keys()) if placement else 'None'}")
        read_config = rc()
        key = read_config.encryption_config['key']
        encode_util = UtilitiesExtension(key)
        
        pods_created = []
        pods_failed = []
        
        # Use placement_details if available (from Step 1 or Step 3), otherwise fall back to placement
        if placement_details:
            # Use detailed placement with hostnames and pod attributes
            logger.info(f"Step 3: Using placement_details with hostnames and pod attributes. Found {len(placement_details)} hosts")
            hosts_processed = 0
            pods_submitted = 0
            for hostname, host_placement in placement_details.items():
                hosts_processed += 1
                logger.info(f"Step 3: [{hosts_processed}/{len(placement_details)}] Processing host {hostname} with {len(host_placement.get('pods', []))} pods")
                host_info = {
                    'hostname': hostname,
                    'ip_address': host_placement.get('ip_address'),
                }
                pods = host_placement.get('pods', [])
                logger.info(f"Step 3: Host {hostname} has {len(pods)} pods. Looking for app_label: {app_label}")
                
                for pod_info in pods:
                    app_name = pod_info.get('app_name')
                    instance_num = pod_info.get('instance_num')
                    logger.info(f"Step 3: Pod info - app_name: {app_name}, instance_num: {instance_num}, app_label: {app_label}")
                    
                    if app_name != app_label:
                        logger.warning(f"Step 3: Skipping pod {app_name} (instance {instance_num}) - doesn't match app_label {app_label}")
                        continue
                    
                    logger.info(f"Step 3: Creating pod for {app_name} instance {instance_num} on {hostname}")
                    
                    # Use resource requirements from pod_info if available
                    pod_resource_reqs = pod_info.get('resource_requirements', {})
                    pod_cpu = pod_resource_reqs.get('cpu_millicores', resource_requirements.cpu_millicores)
                    pod_memory = pod_resource_reqs.get('memory_mb', resource_requirements.memory_mb)
                    
                    # Prepare container specs for containerd
                    container_specs = []
                    for container in containers:
                        container_spec = {
                            'image': container.get('image'),
                            'name': container.get('name'),
                            'command': container.get('command'),
                            'args': container.get('args'),
                            'env': container.get('env'),
                            'resources': {
                                'cpu_millicores': pod_cpu,
                                'memory': f"{int(pod_memory)}Mi"
                            }
                        }
                        container_specs.append(container_spec)
                    
                    # Submit pod creation task
                    host_queue_info = create_host_queue_info(hostname, encode_util)
                    
                    try:
                        result = submit_celery_task(
                            task=create_pod_task,
                            kwargs={
                                'containers': container_specs,
                                'app_namespace': namespace,
                            },
                            queue_info=host_queue_info,
                            operation_name=f"create_pod_{app_label}_{instance_num}",
                            error_code="POD_CREATION_ERROR",
                            additional_data={
                                'hostname': hostname,
                                'ip_address': host_info.get('ip_address'),
                                'app_name': app_name,
                                'instance_num': instance_num,
                                'namespace': namespace,
                                'cpu_millicores': pod_cpu,
                                'memory_mb': pod_memory,
                            }
                        )
                        
                        pods_created.append({
                            'hostname': hostname,
                            'ip_address': host_info.get('ip_address'),
                            'app_name': app_name,
                            'instance_num': instance_num,
                            'task_id': result.get('task_id'),
                            'namespace': namespace,
                            'cpu_millicores': pod_cpu,
                            'memory_mb': pod_memory,
                        })
                        logger.info(f"Step 3: Submitted pod creation for {app_name} instance {instance_num} on {hostname} (CPU: {pod_cpu}m, Memory: {pod_memory}MB)")
                    
                    except Exception as e:
                        logger.error(f"Step 3: Failed to create pod {app_name} instance {instance_num} on {hostname}: {e}", exc_info=True)
                        pods_failed.append({
                            'hostname': hostname,
                            'app_name': app_name,
                            'instance_num': instance_num,
                            'error': str(e),
                        })
        else:
            # Fallback to original placement format (hostnames as keys, placements as values)
            logger.info(f"Step 3: Using placement format (fallback). Found {len(placement)} hosts")
            for hostname, placements in placement.items():
                logger.info(f"Step 3: Processing host {hostname} with {len(placements)} placements. Looking for app_label: {app_label}")
                
                # Create pods for each placement on this host
                for app_name, instance_num in placements:
                    logger.info(f"Step 3: Placement - app_name: {app_name}, instance_num: {instance_num}, app_label: {app_label}")
                    if app_name != app_label:
                        logger.warning(f"Step 3: Skipping placement {app_name} (instance {instance_num}) - doesn't match app_label {app_label}")
                        continue
                    
                    logger.info(f"Step 3: Creating pod for {app_name} instance {instance_num} on {hostname}")
                    
                    # Prepare container specs for containerd
                    container_specs = []
                    for container in containers:
                        container_spec = {
                            'image': container.get('image'),
                            'name': container.get('name'),
                            'command': container.get('command'),
                            'args': container.get('args'),
                            'env': container.get('env'),
                            'resources': {
                                'cpu_millicores': resource_requirements.cpu_millicores,
                                'memory': f"{int(resource_requirements.memory_mb)}Mi"
                            }
                        }
                        container_specs.append(container_spec)
                    
                    # Submit pod creation task
                    host_queue_info = create_host_queue_info(hostname, encode_util)
                    
                    try:
                        result = submit_celery_task(
                            task=create_pod_task,
                            kwargs={
                                'containers': container_specs,
                                'app_namespace': namespace,
                            },
                            queue_info=host_queue_info,
                            operation_name="create_pod",
                            error_code="POD_CREATION_ERROR",
                            additional_data={
                                'deployment': deployment_name,
                                'replica': instance_num,
                                'hostname': hostname,
                            }
                        )
                        
                        pods_created.append({
                            'hostname': hostname,
                            'replica': instance_num,
                            'task_id': result.get('task_id'),
                            'status': 'submitted'
                        })
                        
                        logger.info(
                            f"Step 3: Submitted pod creation for {deployment_name} "
                            f"replica {instance_num} on {hostname}"
                        )
                    
                    except Exception as e:
                        logger.error(
                            f"Step 3: Failed to submit pod creation for {deployment_name} "
                            f"replica {instance_num} on {hostname}: {e}",
                            exc_info=True
                        )
                        pods_failed.append({
                            'hostname': hostname,
                            'replica': instance_num,
                            'status': 'error',
                            'error': str(e)
                        })
        
        # Return final result
        return {
            'status': 'success',
            'deployment': deployment_name,
            'namespace': namespace,
            'placement': placement,
            'pods_created': pods_created,
            'pods_failed': pods_failed,
            'total_replicas': replicas,
            'pods_created_count': len(pods_created),
            'pods_failed_count': len(pods_failed),
            'message': f"Successfully scheduled {len(pods_created)}/{replicas} replicas",
            'aws_nodes_created': aws_nodes_created,
        }
    
    except Exception as e:
        logger.error(f"Step 3: Pod placement and creation failed: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e),
            'deployment': evaluation_result.get('deployment', {}).get('name', 'unknown'),
        }


def schedule_deployment_chain(yaml_content: str) -> Any:
    """Create and return a Celery chain for deployment scheduling.
    
    This function creates a chain of:
    1. evaluate_deployment_requirements_task
    2. create_aws_nodes_if_needed_task
    3. place_and_create_pods_task
    
    Args:
        yaml_content: Deployment YAML string
        
    Returns:
        Celery chain result (AsyncResult)
    """
    from celery import chain
    from kombu import Queue, Exchange
    from utils.ReadConfig import ReadConfig as rc
    from utils.extensions.utilities_extention import UtilitiesExtension
    
    logger.info("Creating deployment scheduling chain...")
    
    # Create scheduler queue configuration
    read_config = rc()
    secure_exchange = Exchange('secure_exchange', type='direct')
    key = read_config.encryption_config['key']
    encode_util = UtilitiesExtension(key)
    scheduler_queue_name = encode_util.encode_hostname_with_key('scheduler')
    
    scheduler_queue_info = {
        'exchange': secure_exchange,
        'queue': scheduler_queue_name,
        'routing_key': scheduler_queue_name,
        'delivery_mode': 2,
    }
    
    # Create the chain
    # In Celery chains, all tasks should automatically use the same queue when apply_async is called with queue_info
    # However, we explicitly set the queue for each task to ensure they all go to the scheduler queue
    task1 = evaluate_deployment_requirements_task.s(yaml_content)
    task2 = create_aws_nodes_if_needed_task.s()
    task3 = place_and_create_pods_task.s()
    
    # Set queue and routing for all tasks in the chain
    task1.set(
        queue=scheduler_queue_name,
        routing_key=scheduler_queue_name,
        exchange=secure_exchange.name
    )
    task2.set(
        queue=scheduler_queue_name,
        routing_key=scheduler_queue_name,
        exchange=secure_exchange.name
    )
    task3.set(
        queue=scheduler_queue_name,
        routing_key=scheduler_queue_name,
        exchange=secure_exchange.name
    )
    
    workflow = chain(task1, task2, task3)
    
    # Execute the chain asynchronously with scheduler queue
    logger.info(f"Submitting scheduler chain to queue: {scheduler_queue_name}")
    logger.info(f"Chain will execute tasks: evaluate_deployment_requirements_task -> create_aws_nodes_if_needed_task -> place_and_create_pods_task")
    logger.info(f"All tasks in chain will be routed to queue: {scheduler_queue_name}")
    result = workflow.apply_async(**scheduler_queue_info)
    logger.info(f"Scheduler chain submitted with root task ID: {result.id}")
    return result

