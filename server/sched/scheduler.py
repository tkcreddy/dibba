"""
Deployment Scheduler for Dibba

This sched:
1. Parses Kubernetes-like deployment YAML
2. Queries Redis for available resources on worker nodes
3. Uses cluster_worker_distribution and initial_load_distribution for placement
4. Creates AWS nodes if resources are insufficient
5. Creates pods using containerd_tasks
"""
import yaml
import re
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from logpkg.log_kcld import LogKCld, log_to_file
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore, HostStatus
from server.nodes.distribute_nodes_services import get_worker_nodes_from_redis,ClusterWorkerDistribution
from utils.celery.tasks.aws_tasks import create_worker_nodes
from utils.celery.tasks.containerd_tasks import create_pod_task
from utils.celery.queue_utils import create_host_queue_info, create_queue_info, submit_celery_task
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.ReadConfig import ReadConfig as rc

logger = LogKCld()


@dataclass
class ResourceRequirements:
    """Container resource requirements."""
    cpu_millicores: int  # CPU in millicores (e.g., 250m = 250)
    memory_mb: float  # Memory in MB (e.g., 100Mi = ~104.86 MB)
    cpu_cores: float  # CPU in cores (e.g., 250m = 0.25)
    memory_bytes: int  # Memory in bytes


@dataclass
class DeploymentSpec:
    """Parsed deployment specification."""
    name: str
    namespace: str
    app_label: str
    replicas: int
    containers: List[Dict[str, Any]]
    resource_requirements: ResourceRequirements
    min_replicas: Optional[int] = None  # Minimum replicas (for auto-scaling)
    max_replicas: Optional[int] = None  # Maximum replicas (for auto-scaling)
    volumes: Optional[List[Dict[str, Any]]] = None  # Pod-level volumes
    health_checks: Optional[Dict[str, Any]] = None  # Health check configuration


class ResourceConverter:
    """Convert Kubernetes resource units to internal format."""
    
    @staticmethod
    def parse_cpu(cpu_str: str) -> int:
        """Parse CPU string (e.g., '250m', '1', '0.5') to millicores.
        
        Args:
            cpu_str: CPU string (e.g., '250m', '1', '0.5')
            
        Returns:
            CPU in millicores (int)
        """
        if not cpu_str:
            return 0
        
        cpu_str = cpu_str.strip().lower()
        
        # Handle millicores (e.g., '250m', '500m')
        if cpu_str.endswith('m'):
            return int(float(cpu_str[:-1]))
        
        # Handle cores (e.g., '1', '0.5', '2.5')
        try:
            cores = float(cpu_str)
            return int(cores * 1000)
        except ValueError:
            logger.error(f"Invalid CPU format: {cpu_str}")
            return 0
    
    @staticmethod
    def parse_memory(memory_str: str) -> Tuple[float, int]:
        """Parse memory string to MB and bytes.
        
        Args:
            memory_str: Memory string (e.g., '100Mi', '1Gi', '512M')
            
        Returns:
            Tuple of (MB, bytes)
        """
        if not memory_str:
            return 0.0, 0
        
        memory_str = memory_str.strip()
        
        # Parse number and unit
        match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT]i?|Mi?|Gi?|Ki?|Ti?)?$', memory_str, re.IGNORECASE)
        if not match:
            logger.error(f"Invalid memory format: {memory_str}")
            return 0.0, 0
        
        value = float(match.group(1))
        unit = (match.group(2) or '').upper()
        
        # Convert to bytes first
        multipliers = {
            'K': 1000,
            'KI': 1024,
            'M': 1000**2,
            'MI': 1024**2,
            'G': 1000**3,
            'GI': 1024**3,
            'T': 1000**4,
            'TI': 1024**4,
        }
        
        multiplier = multipliers.get(unit, 1)
        bytes_value = int(value * multiplier)
        
        # Convert to MB (using 1024^2 for consistency with Mi)
        mb_value = bytes_value / (1024 ** 2)
        
        return mb_value, bytes_value
    
    @staticmethod
    def parse_resources(resources: Dict[str, Any]) -> ResourceRequirements:
        """Parse Kubernetes resources dict.
        
        Supports two formats:
        1. Kubernetes-style: resources: { limits: { cpu: "500m", memory: "256Mi" }, requests: {...} }
        2. Direct format: resources: { cpu_millicores: 500, memory: "256Mi" }
        
        Args:
            resources: Resources dict with 'requests' and/or 'limits', or direct cpu_millicores/memory
            
        Returns:
            ResourceRequirements object
        """
        # Check for direct format first (cpu_millicores and memory directly in resources)
        if 'cpu_millicores' in resources or ('memory' in resources and 'limits' not in resources and 'requests' not in resources):
            # Direct format
            cpu_millicores = resources.get('cpu_millicores', 0)
            if isinstance(cpu_millicores, str):
                cpu_millicores = ResourceConverter.parse_cpu(cpu_millicores)
            memory_str = resources.get('memory', '0')
            memory_mb, memory_bytes = ResourceConverter.parse_memory(memory_str)
            cpu_cores = cpu_millicores / 1000.0
            
            return ResourceRequirements(
                cpu_millicores=cpu_millicores,
                memory_mb=memory_mb,
                cpu_cores=cpu_cores,
                memory_bytes=memory_bytes
            )
        
        # Kubernetes-style format: use limits if available, otherwise requests
        cpu_str = None
        memory_str = None
        
        if 'limits' in resources:
            cpu_str = resources['limits'].get('cpu')
            memory_str = resources['limits'].get('memory')
        
        if not cpu_str and 'requests' in resources:
            cpu_str = resources['requests'].get('cpu')
        if not memory_str and 'requests' in resources:
            memory_str = resources['requests'].get('memory')
        
        cpu_millicores = ResourceConverter.parse_cpu(cpu_str or '0')
        memory_mb, memory_bytes = ResourceConverter.parse_memory(memory_str or '0')
        cpu_cores = cpu_millicores / 1000.0
        
        return ResourceRequirements(
            cpu_millicores=cpu_millicores,
            memory_mb=memory_mb,
            cpu_cores=cpu_cores,
            memory_bytes=memory_bytes
        )


class DeploymentParser:
    """Parse Kubernetes-like deployment YAML."""
    
    @staticmethod
    @log_to_file(logger)
    def parse_yaml(yaml_content: str) -> DeploymentSpec:
        """Parse deployment YAML.
        
        Args:
            yaml_content: YAML string
            
        Returns:
            DeploymentSpec object
        """
        try:
            data = yaml.safe_load(yaml_content)
            metadata = data.get('metadata', {})
            spec = data.get('spec', {})
            template = spec.get('template', {})
            template_metadata = template.get('metadata', {})
            template_spec = template.get('spec', {})
            containers = template_spec.get('containers', [])
            
            if not containers:
                raise ValueError("No containers found in deployment spec")
            
            # Get app label (used for identification)
            labels = template_metadata.get('labels', {})
            app_label = labels.get('app', metadata.get('name', 'unknown'))
            
            # Parse resource requirements from first container (or aggregate)
            # For simplicity, we'll use the first container's resources
            # In production, you might want to aggregate all containers
            container = containers[0]
            resources_dict = container.get('resources', {})
            resource_reqs = ResourceConverter.parse_resources(resources_dict)
            
            # Parse replicas configuration (support multiple formats)
            # Format 1: replicas: 2, minReplicas: 1, maxReplicas: 5
            # Format 2: replicas: { min: 2, max: 10 }
            replicas_value = spec.get('replicas', 1)
            
            # Check if replicas is a dict (Format 2: replicas: { min: 2, max: 10 })
            if isinstance(replicas_value, dict):
                logger.info(f"Parsing replicas as dict format: {replicas_value}")
                min_replicas = replicas_value.get('min')
                max_replicas = replicas_value.get('max')
                # Use min as default replicas, or average if both are provided
                if min_replicas is not None and max_replicas is not None:
                    replicas = min_replicas  # Start with min
                    logger.info(f"Using replicas dict format: min={min_replicas}, max={max_replicas}, starting with replicas={replicas}")
                elif min_replicas is not None:
                    replicas = min_replicas
                    max_replicas = min_replicas
                    logger.info(f"Only min provided in replicas dict: min={min_replicas}, setting max={max_replicas}")
                elif max_replicas is not None:
                    replicas = max_replicas
                    min_replicas = max_replicas
                    logger.info(f"Only max provided in replicas dict: max={max_replicas}, setting min={min_replicas}")
                else:
                    replicas = 1
                    min_replicas = 1
                    max_replicas = 1
                    logger.warning("Replicas dict provided but no min/max found, defaulting to 1")
            else:
                # Format 1: replicas is a number, check for separate minReplicas/maxReplicas
                replicas = int(replicas_value) if replicas_value else 1
                min_replicas = spec.get('minReplicas') or spec.get('min_replicas')
                max_replicas = spec.get('maxReplicas') or spec.get('max_replicas')
                
                logger.info(f"Parsing replicas as number format: replicas={replicas}, minReplicas={min_replicas}, maxReplicas={max_replicas}")
                
                # If minReplicas/maxReplicas are provided, use them
                # Otherwise, use replicas as both min and max
                if min_replicas is None:
                    min_replicas = replicas
                if max_replicas is None:
                    max_replicas = replicas
            
            # Ensure min <= replicas <= max
            if min_replicas > replicas:
                logger.warning(f"min_replicas ({min_replicas}) > replicas ({replicas}), adjusting min_replicas to {replicas}")
                min_replicas = replicas
            if max_replicas < replicas:
                logger.warning(f"max_replicas ({max_replicas}) < replicas ({replicas}), adjusting max_replicas to {replicas}")
                max_replicas = replicas
            if min_replicas > max_replicas:
                logger.warning(f"min_replicas ({min_replicas}) > max_replicas ({max_replicas}), adjusting min_replicas to {max_replicas}")
                min_replicas = max_replicas
            
            logger.info(f"Final replicas configuration: replicas={replicas}, min_replicas={min_replicas}, max_replicas={max_replicas}")
            
            # Extract volumes from pod spec (if present)
            volumes = template_spec.get('volumes', [])
            if volumes:
                logger.info(f"Found {len(volumes)} volumes in pod spec")
            
            # Extract health check configuration from containers
            # Health checks are defined per container (like Kubernetes)
            health_checks = {}
            for container in containers:
                container_name = container.get('name', 'unknown')
                container_health = {}
                
                # Parse livenessProbe
                liveness_probe = container.get('livenessProbe')
                if liveness_probe:
                    container_health['livenessProbe'] = {
                        'httpGet': liveness_probe.get('httpGet'),
                        'tcpSocket': liveness_probe.get('tcpSocket'),
                        'exec': liveness_probe.get('exec'),
                        'initialDelaySeconds': liveness_probe.get('initialDelaySeconds', 0),
                        'periodSeconds': liveness_probe.get('periodSeconds', 10),
                        'timeoutSeconds': liveness_probe.get('timeoutSeconds', 1),
                        'successThreshold': liveness_probe.get('successThreshold', 1),
                        'failureThreshold': liveness_probe.get('failureThreshold', 3),
                    }
                    logger.info(f"Found livenessProbe for container {container_name}")
                
                # Parse readinessProbe
                readiness_probe = container.get('readinessProbe')
                if readiness_probe:
                    container_health['readinessProbe'] = {
                        'httpGet': readiness_probe.get('httpGet'),
                        'tcpSocket': readiness_probe.get('tcpSocket'),
                        'exec': readiness_probe.get('exec'),
                        'initialDelaySeconds': readiness_probe.get('initialDelaySeconds', 0),
                        'periodSeconds': readiness_probe.get('periodSeconds', 10),
                        'timeoutSeconds': readiness_probe.get('timeoutSeconds', 1),
                        'successThreshold': readiness_probe.get('successThreshold', 1),
                        'failureThreshold': readiness_probe.get('failureThreshold', 3),
                    }
                    logger.info(f"Found readinessProbe for container {container_name}")
                
                if container_health:
                    health_checks[container_name] = container_health
            
            if health_checks:
                logger.info(f"Parsed health checks for {len(health_checks)} container(s)")
            
            # Extract metadata.name - this is the deployment name that will be stored in Redis
            deployment_name = metadata.get('name', 'unknown')
            logger.info(f"Parsed deployment name (metadata.name): {deployment_name}, app_label: {app_label}")
            
            return DeploymentSpec(
                name=deployment_name,  # metadata.name from YAML - this is stored in Redis as the deployment name
                namespace=metadata.get('namespace', 'default'),
                app_label=app_label,
                replicas=replicas,
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                containers=containers,
                resource_requirements=resource_reqs,
                volumes=volumes if volumes else None,
                health_checks=health_checks if health_checks else None
            )
        
        except Exception as e:
            logger.error(f"Failed to parse deployment YAML: {e}", exc_info=True)
            raise


class HostResourceCalculator:
    """Calculate available resources on hosts from Redis."""

    @log_to_file(logger)
    def __init__(self, host_pod_store: HostPodStore):
        """Initialize with HostPodStore.
        
        Args:
            host_pod_store: HostPodStore instance
        """
        self.store = host_pod_store
    
    @log_to_file(logger)
    def get_available_resources(self) -> List[Dict[str, Any]]:
        """Get available resources for all online hosts.
        
        Returns:
            List of dicts with hostname, cpu, memory, and available resources
        """
        hosts = self.store.get_all_hosts()
        available_resources = []
        
        for host in hosts:
            if host.get('status') != HostStatus.ONLINE.value:
                continue
            
            hostname = host.get('hostname')
            if not hostname:
                continue
            
            # Get system info
            system_info = host.get('system_info', {})
            usage_metrics = host.get('usage_metrics', {})
            
            # Calculate total CPU (cores)
            # Assuming system_info has CPU info
            total_cpu_cores = system_info.get('cpu_count', 1)  # Default to 1 if not found
            if isinstance(total_cpu_cores, str):
                try:
                    total_cpu_cores = float(total_cpu_cores)
                except ValueError:
                    total_cpu_cores = 1.0
            
            # Calculate total memory (MB)
            # Assuming system_info has memory info in bytes
            total_memory_bytes = system_info.get('total_memory', 0)
            if isinstance(total_memory_bytes, str):
                try:
                    total_memory_bytes = int(total_memory_bytes)
                except ValueError:
                    total_memory_bytes = 0
            total_memory_mb = total_memory_bytes / (1024 ** 2) if total_memory_bytes > 0 else 0
            
            # Get current usage
            cpu_usage_percent = usage_metrics.get('cpu_percent', 0.0)
            memory_usage_percent = usage_metrics.get('memory_percent', 0.0)
            
            # Calculate available resources
            available_cpu_cores = total_cpu_cores * (1 - cpu_usage_percent / 100.0)
            available_memory_mb = total_memory_mb * (1 - memory_usage_percent / 100.0)
            
            # Get pods on this host to calculate reserved resources
            pods = self.store.get_pods_by_host(hostname)
            reserved_cpu_cores = 0.0
            reserved_memory_mb = 0.0
            
            for pod in pods:
                pod_resources = pod.get('resources', {})
                if pod_resources:
                    # Extract CPU and memory from pod resources
                    # This depends on how resources are stored
                    # For now, we'll use a simple approach
                    pass  # TODO: Calculate reserved resources from pods
            
            # Final available = total available - reserved
            final_available_cpu = max(0, available_cpu_cores - reserved_cpu_cores)
            final_available_memory = max(0, available_memory_mb - reserved_memory_mb)
            
            available_resources.append({
                'hostname': hostname,
                'ip_address': host.get('ip_address'),
                'total_cpu_cores': total_cpu_cores,
                'total_memory_mb': total_memory_mb,
                'available_cpu_cores': final_available_cpu,
                'available_memory_mb': final_available_memory,
                'cpu': final_available_cpu,  # For distribution algorithm
                'memory': final_available_memory,  # For distribution algorithm
            })
        
        return available_resources


class DeploymentScheduler:
    """Main sched class."""
    
    def __init__(self):
        """Initialize sched."""
        self.redis_interface = RedisInterface()
        self.host_store = HostPodStore(self.redis_interface)
        self.resource_calculator = HostResourceCalculator(self.host_store)
        self.read_config = rc()
        # Get AWS config: 4 fields (ami_id, key_name, security_group_ids, subnet_id) from Redis
        # instance_type and region come from config.json
        from utils.aws.config_helper import get_aws_node_config
        node_config = get_aws_node_config()  # Only the 4 requested fields from Redis
        file_aws_config = self.read_config.aws_config  # For instance_type and region
        
        # Merge: node_config (4 fields from Redis) + instance_type/region from config file
        self.aws_config = node_config.copy() if node_config else {}
        self.aws_config['instance_type'] = file_aws_config.get('instance_type', 't3.medium')
        self.aws_config['region'] = file_aws_config.get('region')
        key = self.read_config.encryption_config['key']
        self.encode_util = UtilitiesExtension(key)
    
    @log_to_file(logger)
    def schedule_deployment(self, yaml_content: str) -> Dict[str, Any]:
        """Schedule a deployment based on YAML spec.
        
        Args:
            yaml_content: Deployment YAML string
            
        Returns:
            Dictionary with scheduling results
        """
        try:
            # 1. Parse YAML
            logger.info("Parsing deployment YAML...")
            deployment = DeploymentParser.parse_yaml(yaml_content)
            logger.info(
                f"Deployment: {deployment.name}, "
                f"Replicas: {deployment.replicas}, "
                f"CPU: {deployment.resource_requirements.cpu_cores} cores, "
                f"Memory: {deployment.resource_requirements.memory_mb} MB"
            )
            
            # 2. Get available resources
            logger.info("Querying available resources from Redis...")
            available_hosts = self.resource_calculator.get_available_resources()
            
            if not available_hosts:
                logger.warning("No online hosts found in Redis")
                # Create new AWS nodes
                return self._create_aws_nodes_and_schedule(deployment)
            
            # 3. Prepare data for distribution algorithm
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
            
            # 4. Use cluster_worker_distribution
            logger.info("Calculating distribution...")
            distribution = ClusterWorkerDistribution(worker_nodes, cluster_info)
            placement = distribution.distribute_cluster_nodes()
            
            if not placement or any(not placements for placements in placement.values()):
                logger.warning("Could not place all replicas on existing nodes, creating AWS nodes...")
                return self._create_aws_nodes_and_schedule(deployment)
            
            # 5. Create pods on assigned hosts
            logger.info("Creating pods on assigned hosts...")
            results = self._create_pods_on_hosts(deployment, placement, available_hosts)
            
            return {
                'status': 'success',
                'deployment': deployment.name,
                'namespace': deployment.namespace,
                'placement': placement,
                'pods_created': results,
                'message': f"Successfully scheduled {deployment.replicas} replicas"
            }
        
        except Exception as e:
            logger.error(f"Scheduling failed: {e}", exc_info=True)
            return {
                'status': 'error',
                'error': str(e)
            }
    
    @log_to_file(logger)
    def _create_aws_nodes_and_schedule(self, deployment: DeploymentSpec) -> Dict[str, Any]:
        """Create AWS nodes and schedule deployment.
        
        Args:
            deployment: Deployment specification
            
        Returns:
            Dictionary with results
        """
        logger.info("Creating AWS worker nodes...")
        
        # Calculate how many nodes we need
        # For simplicity, create 1 node per replica (can be optimized)
        num_nodes = max(1, (deployment.replicas + 1) // 2)  # At least 1, or half of replicas
        
        # Submit AWS node creation task
        try:
            result = submit_celery_task(
                task=create_worker_nodes,
                args=(
                    None,  # aws_access_key - deprecated, read from config
                    None,  # aws_secret_key - deprecated, read from config
                    self.aws_config.get("region"),  # Optional region override
                ),
                kwargs={
                    'instance_type': self.aws_config.get('instance_type', 't3.medium'),
                    'ami_id': self.aws_config.get('ami_id'),
                    'key_name': self.aws_config.get('key_name'),
                    'security_group_ids': self.aws_config.get('security_group_ids', []),
                    'subnet_id': self.aws_config.get('subnet_id'),
                    'namespace': deployment.namespace,
                    'MaxCount': num_nodes,
                },
                operation_name="create_aws_nodes",
                error_code="AWS_NODE_CREATION_ERROR",
            )
            
            logger.info(f"AWS nodes creation task submitted: {result}")
            
            # Wait a bit for nodes to be ready (in production, poll Redis)
            # For now, return a pending status
            return {
                'status': 'pending',
                'message': f"Creating {num_nodes} AWS nodes. Deployment will be scheduled once nodes are ready.",
                'aws_task_id': result.get('task_id'),
            }
        
        except Exception as e:
            logger.error(f"Failed to create AWS nodes: {e}", exc_info=True)
            return {
                'status': 'error',
                'error': f"Failed to create AWS nodes: {str(e)}"
            }
    
    @log_to_file(logger)
    def _create_pods_on_hosts(
        self,
        deployment: DeploymentSpec,
        placement: Dict[int, List[Tuple[str, int]]],
        available_hosts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Create pods on assigned hosts.
        
        Args:
            deployment: Deployment specification
            placement: Placement dictionary from distribution algorithm
            available_hosts: List of available hosts with resources
            
        Returns:
            List of pod creation results
        """
        results = []
        
        for node_index, placements in placement.items():
            if node_index >= len(available_hosts):
                logger.warning(f"Node index {node_index} out of range")
                continue
            
            host = available_hosts[node_index]
            hostname = host['hostname']
            
            # Create pods for each placement on this host
            for app_name, instance_num in placements:
                if app_name != deployment.app_label:
                    continue
                
                # Prepare container specs for containerd with resources properly formatted
                containers = []
                for container in deployment.containers:
                    # Normalize args: if not provided or empty list, set to None
                    # Empty args [] would fail at runc level with "args must not be empty"
                    # None allows fallback logic to extract Entrypoint/Cmd from image
                    container_args = container.get('args')
                    if container_args is None or (isinstance(container_args, list) and len(container_args) == 0):
                        container_args = None
                    
                    container_spec = {
                        'image': container.get('image'),
                        'name': container.get('name'),
                        'command': container.get('command'),
                        'args': container_args,  # None if not provided or empty, otherwise use as-is
                        'env': container.get('env'),
                        'resources': {
                            'cpu_millicores': deployment.resource_requirements.cpu_millicores,
                            'memory': f"{int(deployment.resource_requirements.memory_mb)}Mi"
                        }
                    }
                    # Add ports if present
                    if 'ports' in container:
                        container_spec['ports'] = container['ports']
                    containers.append(container_spec)
                
                # Use centralized function to create pod
                try:
                    result = self.create_pod_on_host(
                        containers=containers,
                        namespace=deployment.namespace,
                        hostname=hostname,
                        labels=deployment.app_label and {'app': deployment.app_label} or None,
                        deployment_name=deployment.name,
                        replica_num=instance_num
                    )
                    
                    if result.get('status') == 'submitted':
                        results.append({
                            'hostname': hostname,
                            'replica': instance_num,
                            'task_id': result.get('task_id'),
                            'status': 'submitted'
                        })
                    else:
                        results.append({
                            'hostname': hostname,
                            'replica': instance_num,
                            'status': 'error',
                            'error': result.get('error', 'Unknown error')
                        })
                
                except Exception as e:
                    logger.error(
                        f"Failed to create pod for {deployment.name} "
                        f"replica {instance_num} on {hostname}: {e}",
                        exc_info=True
                    )
                    results.append({
                        'hostname': hostname,
                        'replica': instance_num,
                        'status': 'error',
                        'error': str(e)
                    })
        
        return results
    
    @log_to_file(logger)
    def create_pod_on_host(
        self,
        containers: List[Dict[str, Any]],
        namespace: str,
        hostname: str,
        labels: Optional[Dict[str, str]] = None,
        deployment_name: Optional[str] = None,
        replica_num: Optional[int] = None
    ) -> Dict[str, Any]:
        """Centralized function to create a pod on a specific host.
        
        This is the correct way to create pods - used by scheduler and should be used
        by deployment recovery and other components.
        
        Args:
            containers: List of container specs (must have resources properly formatted)
            namespace: Pod namespace
            hostname: Host where pod should be created
            labels: Optional pod labels
            deployment_name: Optional deployment name for logging
            replica_num: Optional replica number for logging
            
        Returns:
            Dictionary with task_id and status
        """
        # Prepare container specs in the correct format
        container_specs = []
        for container in containers:
            # Extract resources - handle both direct format and nested format
            resources = container.get('resources', {})
            if isinstance(resources, dict):
                # If resources already has cpu_millicores and memory, use it
                if 'cpu_millicores' in resources and 'memory' in resources:
                    # Normalize args: if not provided or empty list, set to None
                    # Empty args [] would fail at runc level with "args must not be empty"
                    # None allows fallback logic to extract Entrypoint/Cmd from image
                    container_args = container.get('args')
                    if container_args is None or (isinstance(container_args, list) and len(container_args) == 0):
                        container_args = None
                    
                    container_spec = {
                        'image': container.get('image'),
                        'name': container.get('name'),
                        'command': container.get('command'),
                        'args': container_args,  # None if not provided or empty, otherwise use as-is
                        'env': container.get('env', {}),
                        'resources': {
                            'cpu_millicores': resources['cpu_millicores'],
                            'memory': resources['memory'] if isinstance(resources['memory'], str) else f"{resources['memory']}Mi"
                        }
                    }
                else:
                    # Try to extract from nested format (limits/requests)
                    cpu_millicores = 0
                    memory_str = "0Mi"
                    
                    if 'limits' in resources:
                        cpu_str = resources['limits'].get('cpu', '0')
                        memory_str = resources['limits'].get('memory', '0Mi')
                        cpu_millicores = ResourceConverter.parse_cpu(cpu_str)
                    elif 'requests' in resources:
                        cpu_str = resources['requests'].get('cpu', '0')
                        memory_str = resources['requests'].get('memory', '0Mi')
                        cpu_millicores = ResourceConverter.parse_cpu(cpu_str)
                    
                    # Normalize args: if not provided or empty list, set to None
                    # Empty args [] would fail at runc level with "args must not be empty"
                    # None allows fallback logic to extract Entrypoint/Cmd from image
                    container_args = container.get('args')
                    if container_args is None or (isinstance(container_args, list) and len(container_args) == 0):
                        container_args = None
                    
                    container_spec = {
                        'image': container.get('image'),
                        'name': container.get('name'),
                        'command': container.get('command'),
                        'args': container_args,  # None if not provided or empty, otherwise use as-is
                        'env': container.get('env', {}),
                        'resources': {
                            'cpu_millicores': cpu_millicores,
                            'memory': memory_str
                        }
                    }
            else:
                # No resources specified, use defaults
                # Normalize args: if not provided or empty list, set to None
                # Empty args [] would fail at runc level with "args must not be empty"
                # None allows fallback logic to extract Entrypoint/Cmd from image
                container_args = container.get('args')
                if container_args is None or (isinstance(container_args, list) and len(container_args) == 0):
                    container_args = None
                
                container_spec = {
                    'image': container.get('image'),
                    'name': container.get('name'),
                    'command': container.get('command'),
                    'args': container_args,  # None if not provided or empty, otherwise use as-is
                    'env': container.get('env', {}),
                    'resources': {
                        'cpu_millicores': 0,
                        'memory': '0Mi'
                    }
                }
            
            # Add ports if present
            if 'ports' in container:
                container_spec['ports'] = container['ports']
            
            container_specs.append(container_spec)
        
        # Submit pod creation task
        host_queue_info = create_host_queue_info(hostname, self.encode_util)
        
        try:
            result = submit_celery_task(
                task=create_pod_task,
                kwargs={
                    'containers': container_specs,
                    'app_namespace': namespace,
                    'labels': labels or {},
                },
                queue_info=host_queue_info,
                operation_name="create_pod",
                error_code="POD_CREATION_ERROR",
                additional_data={
                    'deployment': deployment_name,
                    'replica': replica_num,
                    'hostname': hostname,
                }
            )
            
            logger.info(
                f"Submitted pod creation on {hostname} "
                f"{f'for {deployment_name} ' if deployment_name else ''}"
                f"{f'replica {replica_num} ' if replica_num else ''}"
                f"(task_id: {result.get('task_id')})"
            )
            
            return {
                'status': 'submitted',
                'task_id': result.get('task_id'),
                'hostname': hostname
            }
        
        except Exception as e:
            logger.error(
                f"Failed to submit pod creation on {hostname}: {e}",
                exc_info=True
            )
            return {
                'status': 'error',
                'error': str(e),
                'hostname': hostname
            }
    
    @log_to_file(logger)
    def schedule_recovery_pods(
        self,
        namespace: str,
        deployment_name: str,
        app_label: str,
        missing_replicas: int,
        containers: List[Dict[str, Any]],
        resource_reqs: Dict[str, Any],
        existing_pods_per_host: Optional[Dict[str, Dict[str, int]]] = None
    ) -> Dict[str, Any]:
        """Schedule pods for deployment recovery.
        
        This method handles the full recovery flow:
        1. Checks for available worker nodes
        2. Creates AWS nodes if none exist
        3. Creates pods on available nodes using distribution algorithm
        
        Args:
            namespace: Deployment namespace
            deployment_name: Deployment name
            app_label: Application label
            missing_replicas: Number of replicas needed
            containers: Container specifications
            resource_reqs: Resource requirements dict with cpu_millicores and memory_mb
            existing_pods_per_host: Optional dict mapping hostname to service name to count
            
        Returns:
            Dictionary with status, pods_created, and any errors
        """
        logger.info(f"Scheduling recovery pods for {namespace}/{deployment_name}: {missing_replicas} replicas needed")
        
        # 1. Get available worker nodes
        worker_nodes = get_worker_nodes_from_redis(self.redis_interface)
        
        if not worker_nodes:
            # No worker nodes - create AWS nodes
            logger.warning(f"No worker nodes available for {namespace}/{deployment_name}. Creating AWS nodes.")
            
            # Check if AWS nodes are already being created (lock check should be done by caller)
            # But we'll create them here if needed
            result = self.create_aws_nodes_for_recovery(
                namespace=namespace,
                deployment_name=deployment_name,
                app_label=app_label,
                missing_replicas=missing_replicas,
                containers=containers,
                resource_reqs=resource_reqs
            )
            return {
                'status': result.get('status', 'error'),
                'pods_created': 0,
                'aws_task_id': result.get('task_id'),
                'message': result.get('message', 'Creating AWS nodes'),
                'error': result.get('error')
            }
        
        # 2. Convert worker nodes to millicores format for distribution
        worker_nodes_millicores = [
            {
                'cpu': int(node['cpu'] * 1000),  # Convert cores to millicores
                'memory': node['memory'],  # Already in MB
                'hostname': node.get('hostname'),
                'ip_address': node.get('ip_address'),
            }
            for node in worker_nodes
        ]
        
        # 3. Prepare cluster_info for distribution
        cpu_millicores = resource_reqs.get('cpu_millicores', 0)
        memory_mb = resource_reqs.get('memory_mb', 0)
        
        cluster_info = {
            app_label: {
                'cpu': cpu_millicores,
                'memory': memory_mb,
                'instances': missing_replicas
            }
        }
        
        # 4. Use distribute_nodes_services to find placement
        distribution = ClusterWorkerDistribution(
            worker_nodes_millicores, 
            cluster_info, 
            existing_pods_per_host=existing_pods_per_host
        )
        placement_by_index = distribution.distribute_cluster_nodes()
        
        if not placement_by_index:
            # Could not place pods - create AWS nodes
            logger.warning(f"Could not find placement for {missing_replicas} replicas. Creating AWS nodes.")
            result = self.create_aws_nodes_for_recovery(
                namespace=namespace,
                deployment_name=deployment_name,
                app_label=app_label,
                missing_replicas=missing_replicas,
                containers=containers,
                resource_reqs=resource_reqs
            )
            return {
                'status': result.get('status', 'error'),
                'pods_created': 0,
                'aws_task_id': result.get('task_id'),
                'message': result.get('message', 'Creating AWS nodes'),
                'error': result.get('error')
            }
        
        # 5. Convert placement from indices to hostnames
        placement = {}
        total_placement_count = 0
        for node_index, placements in placement_by_index.items():
            if node_index < len(worker_nodes):
                hostname = worker_nodes[node_index]['hostname']
                placement[hostname] = placements
                total_placement_count += len(placements)
        
        # 6. Verify placement count matches missing_replicas
        if total_placement_count != missing_replicas:
            logger.warning(f"Placement count ({total_placement_count}) != missing_replicas ({missing_replicas}). Limiting to {missing_replicas}.")
            placement_limited = {}
            count_so_far = 0
            for hostname, placements in placement.items():
                if count_so_far >= missing_replicas:
                    break
                remaining = missing_replicas - count_so_far
                placement_limited[hostname] = placements[:remaining]
                count_so_far += len(placement_limited[hostname])
            placement = placement_limited
        
        # 7. Create pods using existing method
        pods_created = 0
        created_instances = []  # Track instance numbers for marking as creating
        for hostname, placements in placement.items():
            for app_name, instance_num in placements:
                if pods_created >= missing_replicas:
                    break
                
                # Prepare container specs
                container_specs = []
                for container in containers:
                    container_args = container.get('args')
                    if container_args is None or (isinstance(container_args, list) and len(container_args) == 0):
                        container_args = None
                    
                    container_spec = {
                        'name': container.get('name'),
                        'image': container.get('image'),
                        'args': container_args,
                        'env': container.get('env', {}),
                        'ports': container.get('ports', []),
                    }
                    if 'resources' in container:
                        container_spec['resources'] = container['resources']
                    container_specs.append(container_spec)
                
                # Create pod
                result = self.create_pod_on_host(
                    containers=container_specs,
                    namespace=namespace,
                    hostname=hostname,
                    labels={'app': app_label, 'instance': str(instance_num)},
                    deployment_name=deployment_name,
                    replica_num=instance_num
                )
                
                if result.get('status') == 'submitted':
                    pods_created += 1
                    created_instances.append(instance_num)
                    logger.info(f"Created pod {pods_created}/{missing_replicas} for {namespace}/{deployment_name} on {hostname} (instance: {instance_num})")
                else:
                    logger.error(f"Failed to create pod: {result.get('error', 'Unknown error')}")
        
        return {
            'status': 'success' if pods_created > 0 else 'error',
            'pods_created': pods_created,
            'expected': missing_replicas,
            'created_instances': created_instances,  # Return instance numbers for tracking
            'message': f"Created {pods_created} pods for {namespace}/{deployment_name}"
        }
    
    @log_to_file(logger)
    def create_aws_nodes_for_recovery(
        self,
        namespace: str,
        deployment_name: str,
        app_label: str,
        missing_replicas: int,
        containers: List[Dict[str, Any]],
        resource_reqs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create AWS nodes for deployment recovery when no worker nodes are available.
        
        This method is used by the recovery task when there are no worker nodes.
        It creates AWS nodes and returns a result that can be used to track the creation.
        
        Args:
            namespace: Deployment namespace
            deployment_name: Deployment name
            app_label: Application label
            missing_replicas: Number of replicas needed
            containers: Container specifications
            resource_reqs: Resource requirements dict with cpu_millicores and memory_mb
            
        Returns:
            Dictionary with status and task_id
        """
        logger.info(f"Creating AWS nodes for recovery: {namespace}/{deployment_name}, missing_replicas={missing_replicas}")
        
        # Calculate required AWS nodes
        required_nodes = max(1, (missing_replicas + 1) // 2)
        logger.info(f"Creating {required_nodes} AWS node(s) to accommodate {missing_replicas} missing replicas")
        
        # Create AWS queue info with proper encoding
        aws_queue_info = create_queue_info("aws_interface", utilities_extension=self.encode_util)
        logger.info(f"Routing AWS node creation to queue: {aws_queue_info.get('queue')}")
        
        # Submit AWS node creation task using scheduler's AWS config
        try:
            result = submit_celery_task(
                task=create_worker_nodes,
                args=(
                    None,  # aws_access_key - deprecated, read from config
                    None,  # aws_secret_key - deprecated, read from config
                    self.aws_config.get("region"),  # Optional region override
                ),
                kwargs={
                    'instance_type': self.aws_config.get('instance_type', 't3.medium'),
                    'ami_id': self.aws_config.get('ami_id'),
                    'key_name': self.aws_config.get('key_name'),
                    'security_group_ids': self.aws_config.get('security_group_ids', []),
                    'subnet_id': self.aws_config.get('subnet_id'),
                    'namespace': namespace,
                    'MaxCount': required_nodes,
                },
                queue_info=aws_queue_info,
                operation_name="create_aws_nodes_for_recovery",
                error_code="AWS_NODE_CREATION_ERROR",
            )
            
            # submit_celery_task returns {"status": "success", "data": {"task_id": ...}}
            aws_task_id = result.get('data', {}).get('task_id')
            if aws_task_id:
                logger.info(f"AWS node creation task submitted for {namespace}/{deployment_name}: {aws_task_id}")
                return {
                    'status': 'submitted',
                    'task_id': aws_task_id,
                    'required_nodes': required_nodes,
                    'message': f"Creating {required_nodes} AWS nodes. Pods will be created once nodes are ready."
                }
            else:
                logger.error(f"AWS node creation task failed to submit for {namespace}/{deployment_name}. Result: {result}")
                return {
                    'status': 'error',
                    'error': 'AWS node creation task failed to submit'
                }
        
        except Exception as e:
            logger.error(f"Failed to create AWS nodes for {namespace}/{deployment_name}: {e}", exc_info=True)
            return {
                'status': 'error',
                'error': f"Failed to create AWS nodes: {str(e)}"
            }
    
    @log_to_file(logger)
    def terminate_pod_on_host(
        self,
        pod_id: str,
        namespace: str,
        hostname: str
    ) -> Dict[str, Any]:
        """Centralized function to terminate a pod on a specific host.
        
        This is the correct way to terminate pods - used by scheduler and should be
        used by health checks and other components.
        
        Args:
            pod_id: Pod ID to terminate
            namespace: Pod namespace
            hostname: Host where pod is running
            
        Returns:
            Dictionary with task_id and status
        """
        from utils.celery.tasks.containerd_tasks import terminate_pod_task
        
        host_queue_info = create_host_queue_info(hostname, self.encode_util)
        
        try:
            result = submit_celery_task(
                task=terminate_pod_task,
                args=(namespace, pod_id),
                kwargs={},
                queue_info=host_queue_info,
                operation_name="terminate_pod",
                error_code="POD_TERMINATION_ERROR",
                additional_data={
                    'pod_id': pod_id,
                    'hostname': hostname,
                }
            )
            
            logger.info(
                f"Submitted pod termination for {pod_id} on {hostname} "
                f"(task_id: {result.get('task_id')})"
            )
            
            return {
                'status': 'submitted',
                'task_id': result.get('task_id'),
                'hostname': hostname
            }
        
        except Exception as e:
            logger.error(
                f"Failed to submit pod termination for {pod_id} on {hostname}: {e}",
                exc_info=True
            )
            return {
                'status': 'error',
                'error': str(e),
                'hostname': hostname
            }

@log_to_file(logger)
def schedule_deployment_from_yaml(yaml_content: str, use_chain: bool = True) -> Dict[str, Any]:
    """Convenience function to schedule a deployment from YAML.
    
    Args:
        yaml_content: Deployment YAML string
        use_chain: If True, use Celery chain tasks (default: True)
        
    Returns:
        Dictionary with scheduling results
    """
    if use_chain:
        # Use Celery chain tasks
        from utils.celery.tasks.scheduler_tasks import schedule_deployment_chain
        logger.info("Using Celery chain for deployment scheduling")
        result = schedule_deployment_chain(yaml_content)
        return {
            'status': 'submitted',
            'task_id': result.id,
            'message': 'Deployment scheduling chain submitted. Use task_id to check status.',
        }
    else:
        # Use synchronous sched
        scheduler = DeploymentScheduler()
        return scheduler.schedule_deployment(yaml_content)


if __name__ == "__main__":
    # Example usage
    example_yaml = """
metadata:
  name: my-app-deployment
  labels:
    app: my-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: my-app-container
        image: polinux/stress
        resources:
          requests:
            memory: "100Mi"
            cpu: "250m"
          limits:
            memory: "200Mi"
            cpu: "500m"
        command: ["stress"]
        args: ["--vm", "1", "--vm-bytes", "150M", "--vm-hang", "1"]
        ports:
        - containerPort: 80
"""
    
    result = schedule_deployment_from_yaml(example_yaml, use_chain=True)
    logger.info(f"Scheduling result: {result}")

