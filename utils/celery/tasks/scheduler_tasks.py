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
from server.nodes.cluster_worker_distribution import ClusterWorkerDistribution
from server.scheduler.scheduler import (
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


@celery_app.task
@log_to_file(logger)
def evaluate_deployment_requirements_task(yaml_content: str) -> Dict[str, Any]:
    """Evaluate deployment requirements and check existing resources.
    
    This is the first task in the chain. It:
    1. Parses the deployment YAML
    2. Queries Redis for available resources
    3. Determines if AWS nodes are needed
    4. Returns evaluation results for next task
    
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
        # Parse YAML
        logger.info("Step 1: Parsing deployment YAML...")
        deployment = DeploymentParser.parse_yaml(yaml_content)
        logger.info(
            f"Deployment: {deployment.name}, "
            f"Replicas: {deployment.replicas}, "
            f"CPU: {deployment.resource_requirements.cpu_cores} cores, "
            f"Memory: {deployment.resource_requirements.memory_mb} MB"
        )
        
        # Get available resources
        logger.info("Step 1: Querying available resources from Redis...")
        redis_interface = RedisInterface()
        host_store = HostPodStore(redis_interface)
        resource_calculator = HostResourceCalculator(host_store)
        available_hosts = resource_calculator.get_available_resources()
        
        if not available_hosts:
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
        
        # Prepare data for distribution algorithm
        worker_nodes = [
            {
                'cpu': host['cpu'],
                'memory': host['memory']
            }
            for host in available_hosts
        ]
        
        cluster_info = {
            deployment.app_label: {
                'cpu': deployment.resource_requirements.cpu_cores,
                'memory': deployment.resource_requirements.memory_mb,
                'instances': deployment.replicas
            }
        }
        
        # Try to distribute
        logger.info("Step 1: Calculating distribution...")
        distribution = ClusterWorkerDistribution(worker_nodes, cluster_info)
        placement = distribution.distribute_cluster_nodes()
        
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
            'placement': placement,
        }
        
        if needs_aws:
            logger.info(f"Step 1: Need {required_nodes} AWS nodes")
        else:
            logger.info("Step 1: Sufficient resources available, no AWS nodes needed")
        
        return result
    
    except Exception as e:
        logger.error(f"Step 1: Evaluation failed: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e),
            'needs_aws_nodes': False,
            'required_nodes': 0,
        }


@celery_app.task
@log_to_file(logger)
def create_aws_nodes_if_needed_task(evaluation_result: Dict[str, Any]) -> Dict[str, Any]:
    """Create AWS nodes if needed based on evaluation.
    
    This is the second task in the chain. It:
    1. Checks if AWS nodes are needed
    2. Creates AWS nodes if needed
    3. Returns updated evaluation result with AWS task info
    
    Args:
        evaluation_result: Result from evaluate_deployment_requirements_task
        
    Returns:
        Updated evaluation result with AWS node creation info
    """
    try:
        if evaluation_result.get('status') == 'error':
            logger.error("Step 2: Skipping AWS node creation due to previous error")
            return evaluation_result
        
        needs_aws = evaluation_result.get('needs_aws_nodes', False)
        
        if not needs_aws:
            logger.info("Step 2: No AWS nodes needed, proceeding to pod placement")
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


@celery_app.task
@log_to_file(logger)
def place_and_create_pods_task(evaluation_result: Dict[str, Any]) -> Dict[str, Any]:
    """Place and create pods on hosts (existing + new).
    
    This is the third task in the chain. It:
    1. Gets all available hosts (existing + newly created)
    2. Recalculates placement if AWS nodes were created
    3. Creates pods on assigned hosts using containerd_tasks
    
    Args:
        evaluation_result: Result from create_aws_nodes_if_needed_task
        
    Returns:
        Dictionary with final scheduling results
    """
    try:
        if evaluation_result.get('status') == 'error':
            logger.error("Step 3: Skipping pod creation due to previous error")
            return evaluation_result
        
        logger.info("Step 3: Placing and creating pods...")
        
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
        
        # Get all available hosts (including newly created ones)
        redis_interface = RedisInterface()
        host_store = HostPodStore(redis_interface)
        resource_calculator = HostResourceCalculator(host_store)
        all_available_hosts = resource_calculator.get_available_resources()
        
        if not all_available_hosts:
            logger.warning("Step 3: No hosts available after AWS node creation")
            return {
                'status': 'error',
                'error': 'No hosts available for pod placement',
                'deployment': deployment_name,
            }
        
        # If AWS nodes were created, recalculate placement
        aws_nodes_created = evaluation_result.get('aws_nodes_created', 0)
        if aws_nodes_created > 0:
            logger.info(f"Step 3: Recalculating placement with {len(all_available_hosts)} hosts (including new AWS nodes)")
            
            # Prepare data for distribution
            worker_nodes = [
                {
                    'cpu': host['cpu'],
                    'memory': host['memory']
                }
                for host in all_available_hosts
            ]
            
            cluster_info = {
                app_label: {
                    'cpu': resource_requirements.cpu_cores,
                    'memory': resource_requirements.memory_mb,
                    'instances': replicas
                }
            }
            
            # Recalculate distribution
            distribution = ClusterWorkerDistribution(worker_nodes, cluster_info)
            placement = distribution.distribute_cluster_nodes()
            
            if not placement:
                return {
                    'status': 'error',
                    'error': 'Could not place all replicas even after AWS node creation',
                    'deployment': deployment_name,
                }
        else:
            # Use existing placement
            placement = evaluation_result.get('placement')
            if not placement:
                return {
                    'status': 'error',
                    'error': 'No placement available',
                    'deployment': deployment_name,
                }
        
        # Create pods on assigned hosts
        logger.info("Step 3: Creating pods on assigned hosts...")
        read_config = rc()
        key = read_config.encryption_config['key']
        encode_util = UtilitiesExtension(key)
        
        pods_created = []
        pods_failed = []
        
        for node_index, placements in placement.items():
            if node_index >= len(all_available_hosts):
                logger.warning(f"Step 3: Node index {node_index} out of range")
                continue
            
            host = all_available_hosts[node_index]
            hostname = host['hostname']
            
            # Create pods for each placement on this host
            for app_name, instance_num in placements:
                if app_name != app_label:
                    continue
                
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
    
    logger.info("Creating deployment scheduling chain...")
    
    # Create the chain
    workflow = chain(
        evaluate_deployment_requirements_task.s(yaml_content),
        create_aws_nodes_if_needed_task.s(),
        place_and_create_pods_task.s()
    )
    
    # Execute the chain asynchronously
    return workflow.apply_async()

