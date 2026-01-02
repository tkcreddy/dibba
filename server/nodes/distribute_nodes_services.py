import re
from logpkg.log_kcld import LogKCld,log_to_file
from typing import Optional, List, Dict, Any
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore, HostStatus

logger = LogKCld()

@log_to_file(logger)
def _parse_cpu_to_cores(cpu_str: str) -> float:
    """Parse CPU string to cores.
    
    Args:
        cpu_str: CPU string (e.g., '250m', '1', '0.5', '1.5')
        
    Returns:
        CPU in cores (float)
    """
    if not cpu_str:
        return 0.0
    
    cpu_str = str(cpu_str).strip().lower()
    
    # Handle millicores (e.g., '250m', '500m')
    if cpu_str.endswith('m'):
        try:
            millicores = float(cpu_str[:-1])
            return millicores / 1000.0
        except ValueError:
            return 0.0
    
    # Handle cores (e.g., '1', '0.5', '2.5')
    try:
        return float(cpu_str)
    except ValueError:
        logger.warning(f"Invalid CPU format: {cpu_str}")
        return 0.0

@log_to_file(logger)
def _parse_memory_to_mb(memory_str: str) -> float:
    """Parse memory string to MB.
    
    Args:
        memory_str: Memory string (e.g., '100Mi', '1Gi', '512M', '2G')
        
    Returns:
        Memory in MB (float)
    """
    if not memory_str:
        return 0.0
    
    memory_str = str(memory_str).strip()
    
    # Parse number and unit
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT]i?|Mi?|Gi?|Ki?|Ti?)?$', memory_str, re.IGNORECASE)
    if not match:
        logger.warning(f"Invalid memory format: {memory_str}")
        return 0.0
    
    value = float(match.group(1))
    unit = (match.group(2) or "").upper()
    
    # Convert to MB
    if unit in ("KI", "KIB"):
        return value / 1024.0
    elif unit in ("MI", "MIB"):
        return value
    elif unit in ("GI", "GIB"):
        return value * 1024.0
    elif unit in ("TI", "TIB"):
        return value * 1024.0 * 1024.0
    elif unit in ("K", "KB"):
        return value / 1000.0
    elif unit in ("M", "MB"):
        return value
    elif unit in ("G", "GB"):
        return value * 1000.0
    elif unit in ("T", "TB"):
        return value * 1000.0 * 1000.0
    else:
        # Assume bytes if no unit
        return value / (1024.0 * 1024.0)

@log_to_file(logger)
def get_worker_nodes_from_redis(
    redis_interface: Optional[RedisInterface] = None
) -> List[Dict[str, Any]]:
    """Get worker nodes from Redis host_pod_store.
    
    This function retrieves all online hosts from Redis and converts them
    to the format expected by ClusterWorkerDistribution (list of dicts with
    'cpu' and 'memory' keys).
    
    Args:
        redis_interface: Optional RedisInterface instance. If None, creates a new one.
        
    Returns:
        List of dictionaries with 'cpu' and 'memory' keys, plus 'hostname' and 'ip_address'.
        Returns empty list if no hosts found or on error.
    """
    try:
        if redis_interface is None:
            redis_interface = RedisInterface()
        
        store = HostPodStore(redis_interface)
        hosts = store.get_all_hosts()
        
        if not hosts:
            logger.warning("No hosts found in Redis")
            return []
        
        worker_nodes = []
        for host in hosts:
            # Only include online hosts
            if host.get("status") != HostStatus.ONLINE.value:
                continue
            
            # Extract system info and usage metrics
            system_info = host.get("system_info", {})
            usage_metrics = host.get("usage_metrics", {})
            
            # Calculate total CPU cores
            cpu_count = system_info.get("cpu_count", 0)
            logical_cpu_count = system_info.get("logical_cpu_count", cpu_count)
            total_cpu_cores = float(logical_cpu_count) if logical_cpu_count > 0 else float(cpu_count) if cpu_count > 0 else 0.0
            
            # Calculate total memory (MB)
            # system_info stores 'Memory' in GB (from get_system_info)
            # Also check usage_metrics.virtual_memory.total which is in bytes
            total_memory_mb = 0.0
            
            # First try to get from system_info['Memory'] (in GB)
            memory_gb = system_info.get("Memory") or system_info.get("memory")
            if memory_gb:
                try:
                    if isinstance(memory_gb, str):
                        memory_gb = float(memory_gb)
                    total_memory_mb = float(memory_gb) * 1024.0  # Convert GB to MB
                    logger.debug(f"Host {host.get('hostname')}: Got memory from system_info['Memory']: {memory_gb} GB = {total_memory_mb} MB")
                except (ValueError, TypeError) as e:
                    logger.warning(f"Host {host.get('hostname')}: Failed to parse memory_gb '{memory_gb}': {e}")
            
            # If not found, try usage_metrics.virtual_memory.total (in bytes)
            if total_memory_mb == 0 and usage_metrics:
                virtual_memory = usage_metrics.get("Virtual Memory") or usage_metrics.get("virtual_memory")
                if isinstance(virtual_memory, dict):
                    total_memory_bytes = virtual_memory.get("total", 0)
                    if total_memory_bytes:
                        try:
                            if isinstance(total_memory_bytes, str):
                                total_memory_bytes = int(total_memory_bytes)
                            total_memory_mb = float(total_memory_bytes) / (1024 ** 2)  # Convert bytes to MB
                            logger.debug(f"Host {host.get('hostname')}: Got memory from virtual_memory.total: {total_memory_bytes} bytes = {total_memory_mb} MB")
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Host {host.get('hostname')}: Failed to parse total_memory_bytes '{total_memory_bytes}': {e}")
            
            # Fallback: try system_info['total_memory'] (if it exists, assume bytes)
            if total_memory_mb == 0:
                total_memory_bytes = system_info.get("total_memory", 0)
                if total_memory_bytes:
                    try:
                        if isinstance(total_memory_bytes, str):
                            total_memory_bytes = int(total_memory_bytes)
                        total_memory_mb = float(total_memory_bytes) / (1024 ** 2)  # Convert bytes to MB
                        logger.debug(f"Host {host.get('hostname')}: Got memory from system_info['total_memory']: {total_memory_bytes} bytes = {total_memory_mb} MB")
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Host {host.get('hostname')}: Failed to parse total_memory '{total_memory_bytes}': {e}")
                        total_memory_mb = 0.0
            
            if total_memory_mb == 0:
                logger.warning(f"Host {host.get('hostname')}: Could not determine total memory. system_info keys: {list(system_info.keys())}, usage_metrics keys: {list(usage_metrics.keys()) if usage_metrics else 'None'}")
            
            # Get current usage percentages
            cpu_usage_percent = usage_metrics.get("cpu_usage", [])
            if isinstance(cpu_usage_percent, list) and cpu_usage_percent:
                # Average CPU usage if it's a list
                avg_cpu_usage = sum(cpu_usage_percent) / len(cpu_usage_percent) if cpu_usage_percent else 0.0
            else:
                avg_cpu_usage = usage_metrics.get("cpu_percent", 0.0)
            
            memory_usage_percent = usage_metrics.get("memory_percent", 0.0)
            if not memory_usage_percent:
                # Try to calculate from virtual_memory
                virtual_memory = usage_metrics.get("virtual_memory", {})
                if isinstance(virtual_memory, dict):
                    memory_usage_percent = virtual_memory.get("percent", 0.0)
            
            # Get pods on this host to calculate reserved resources (using LIMITS as maximum)
            # This gets ALL pods across ALL namespaces on this host
            hostname = host.get("hostname", "")
            pods = store.get_pods_by_host(hostname)
            reserved_cpu_cores = 0.0
            reserved_memory_mb = 0.0
            
            logger.debug(f"Calculating reserved resources for host {hostname}: found {len(pods)} pods across all namespaces")
            
            # Track namespaces and pod counts for logging
            namespace_counts = {}
            total_containers_processed = 0
            
            for pod in pods:
                if not isinstance(pod, dict):
                    continue
                
                pod_namespace = pod.get("namespace", "unknown")
                namespace_counts[pod_namespace] = namespace_counts.get(pod_namespace, 0) + 1
                pod_id = pod.get("pod_id", "unknown")
                
                # Skip pause containers - they use minimal resources and shouldn't count toward reservations
                # Pause containers are identified by having "pause" in their name or being the pause container
                pause_container = pod.get("pause_container", {})
                if isinstance(pause_container, dict) and pause_container.get("cid"):
                    # Pause container exists but uses minimal resources (100m CPU, 64Mi memory typically)
                    # We'll account for it separately if needed, but for now focus on app containers
                    pass
                
                # Check pod-level resources first
                pod_resources = pod.get("resources", {})
                
                # Get containers - these are the application containers (excluding pause)
                containers = pod.get("containers", [])
                if not isinstance(containers, list):
                    containers = []
                
                # Filter out pause containers from the containers list
                # Pause containers are typically named "pause" or have "pause" in the image name
                app_containers = []
                for container in containers:
                    if not isinstance(container, dict):
                        continue
                    container_name = str(container.get("name", "")).lower()
                    container_image = str(container.get("image", "")).lower()
                    # Skip pause containers
                    if "pause" in container_name or "pause" in container_image:
                        logger.debug(f"Skipping pause container {container.get('name')} in pod {pod_id}")
                        continue
                    app_containers.append(container)
                
                containers = app_containers
                total_containers_processed += len(containers)
                
                # Process each application container's resources
                for container in containers:
                    if not isinstance(container, dict):
                        continue
                    
                    container_name = container.get("name", "unknown")
                    
                    # Get container resources (may be in different formats)
                    container_resources = container.get("resources", {})
                    if not container_resources:
                        # Try to get from pod-level resources if container doesn't have it
                        container_resources = pod_resources
                    
                    # If still no resources, try to look up from deployment store
                    if not container_resources:
                        try:
                            # Try to get resources from deployment store by matching app_label and namespace
                            pod_namespace = pod.get("namespace")
                            pod_labels = pod.get("labels", {})
                            pod_app_label = pod_labels.get("app_label") or pod_labels.get("app")
                            
                            if pod_namespace and pod_app_label:
                                from utils.redis.deployment_store import DeploymentStore
                                from utils.redis.redis_interface import RedisInterface
                                dep_store = DeploymentStore(RedisInterface())
                                deployments = dep_store.get_deployments_by_namespace(pod_namespace)
                                
                                # Find matching deployment
                                for dep in deployments:
                                    if dep.get("app_label") == pod_app_label:
                                        dep_spec = dep.get("deployment_spec", {})
                                        dep_containers = dep_spec.get("containers", [])
                                        # Match container by name or image
                                        for dep_container in dep_containers:
                                            if (dep_container.get("name") == container_name or 
                                                dep_container.get("image") == container.get("image")):
                                                container_resources = dep_container.get("resources", {})
                                                if container_resources:
                                                    logger.debug(f"Found resources for pod {pod_id} container {container_name} from deployment store")
                                                    break
                                        if container_resources:
                                            break
                        except Exception as e:
                            logger.debug(f"Failed to look up resources from deployment store for pod {pod_id}: {e}")
                    
                    if not container_resources:
                        logger.debug(f"Container {container_name} in pod {pod_id} has no resources (checked pod data and deployment store)")
                        continue
                    
                    # Extract LIMITS only (limits are the maximum resource allocation)
                    cpu_str = None
                    memory_str = None
                    
                    if isinstance(container_resources, dict):
                        # Try Kubernetes-style limits first
                        if "limits" in container_resources:
                            limits = container_resources.get("limits", {})
                            if isinstance(limits, dict):
                                cpu_str = limits.get("cpu")
                                memory_str = limits.get("memory")
                        
                        # Fallback to direct format (cpu_millicores, memory)
                        if not cpu_str and "cpu_millicores" in container_resources:
                            cpu_millicores = container_resources.get("cpu_millicores", 0)
                            if cpu_millicores:
                                cpu_str = f"{cpu_millicores}m"  # Convert to string format for parsing
                        
                        if not memory_str and "memory" in container_resources:
                            memory_val = container_resources.get("memory", "")
                            if memory_val and isinstance(memory_val, str):
                                memory_str = memory_val
                            elif memory_val:
                                # If it's a number, assume MB
                                memory_str = f"{memory_val}Mi"
                    
                    # Parse CPU (convert to cores)
                    if cpu_str:
                        cpu_cores = _parse_cpu_to_cores(cpu_str)
                        reserved_cpu_cores += cpu_cores
                        logger.debug(f"Pod {pod_id} container {container_name}: reserved {cpu_cores} CPU cores (from {cpu_str})")
                    
                    # Parse Memory (convert to MB)
                    if memory_str:
                        memory_mb = _parse_memory_to_mb(memory_str)
                        reserved_memory_mb += memory_mb
                        logger.debug(f"Pod {pod_id} container {container_name}: reserved {memory_mb} MB memory (from {memory_str})")
                
                # If no containers found, try pod-level resources (LIMITS only)
                if not containers:
                    # If pod_resources is empty, try to get from deployment store
                    if not pod_resources:
                        try:
                            pod_namespace = pod.get("namespace")
                            pod_labels = pod.get("labels", {})
                            pod_app_label = pod_labels.get("app_label") or pod_labels.get("app")
                            
                            if pod_namespace and pod_app_label:
                                from utils.redis.deployment_store import DeploymentStore
                                from utils.redis.redis_interface import RedisInterface
                                dep_store = DeploymentStore(RedisInterface())
                                deployments = dep_store.get_deployments_by_namespace(pod_namespace)
                                
                                # Find matching deployment and get resource_requirements
                                for dep in deployments:
                                    if dep.get("app_label") == pod_app_label:
                                        dep_spec = dep.get("deployment_spec", {})
                                        resource_reqs = dep_spec.get("resource_requirements", {})
                                        if resource_reqs:
                                            # Create a pod_resources dict from resource_requirements
                                            pod_resources = {
                                                "cpu_millicores": resource_reqs.get("cpu_millicores", 0),
                                                "memory": f"{resource_reqs.get('memory_mb', 0)}Mi"
                                            }
                                            logger.debug(f"Found pod-level resources for pod {pod_id} from deployment store")
                                            break
                        except Exception as e:
                            logger.debug(f"Failed to look up pod-level resources from deployment store for pod {pod_id}: {e}")
                    
                    if pod_resources:
                        if isinstance(pod_resources, dict):
                            cpu_str = None
                            memory_str = None
                            
                            # Only use limits (maximum resource allocation)
                            if "limits" in pod_resources:
                                limits = pod_resources.get("limits", {})
                                if isinstance(limits, dict):
                                    cpu_str = limits.get("cpu")
                                    memory_str = limits.get("memory")
                            
                            # Fallback to direct format
                            if not cpu_str and "cpu_millicores" in pod_resources:
                                cpu_millicores = pod_resources.get("cpu_millicores", 0)
                                if cpu_millicores:
                                    cpu_str = f"{cpu_millicores}m"
                            
                            if not memory_str and "memory" in pod_resources:
                                memory_val = pod_resources.get("memory", "")
                                if memory_val and isinstance(memory_val, str):
                                    memory_str = memory_val
                                elif memory_val:
                                    memory_str = f"{memory_val}Mi"
                            
                            if cpu_str:
                                cpu_cores = _parse_cpu_to_cores(cpu_str)
                                reserved_cpu_cores += cpu_cores
                                logger.debug(f"Pod {pod_id} (pod-level): reserved {cpu_cores} CPU cores (from {cpu_str})")
                            
                            if memory_str:
                                memory_mb = _parse_memory_to_mb(memory_str)
                                reserved_memory_mb += memory_mb
                                logger.debug(f"Pod {pod_id} (pod-level): reserved {memory_mb} MB memory (from {memory_str})")
                        else:
                            # Legacy format: assume direct values are limits
                            pod_cpu = pod_resources.get("cpu_millicores", 0)
                            if pod_cpu:
                                reserved_cpu_cores += pod_cpu / 1000.0  # Convert millicores to cores
                                logger.debug(f"Pod {pod_id} (legacy format): reserved {pod_cpu / 1000.0} CPU cores")
                            
                            pod_memory = pod_resources.get("memory", "")
                            if pod_memory:
                                memory_mb = _parse_memory_to_mb(pod_memory)
                                reserved_memory_mb += memory_mb
                                logger.debug(f"Pod {pod_id} (legacy format): reserved {memory_mb} MB memory")
            
            logger.info(f"Host {hostname}: Reserved resources calculated from {len(pods)} pods across {len(namespace_counts)} namespaces ({namespace_counts}), {total_containers_processed} app containers. Reserved: {reserved_cpu_cores:.2f} CPU cores, {reserved_memory_mb:.2f} MB memory")
            
            # Calculate available resources
            # Available = Total - Reserved (using LIMITS as maximum)
            # We use total CPU/memory directly, not adjusted by usage percentage,
            # because reserved resources already account for what's allocated
            # The usage percentage reflects current utilization, but for scheduling
            # we care about what's actually reserved/allocated, not current usage
            final_available_cpu = max(0, total_cpu_cores - reserved_cpu_cores)
            final_available_memory = max(0, total_memory_mb - reserved_memory_mb)
            
            logger.debug(f"Host {hostname}: Total CPU={total_cpu_cores}, Reserved CPU={reserved_cpu_cores}, Available CPU={final_available_cpu}")
            logger.debug(f"Host {hostname}: Total Memory={total_memory_mb} MB, Reserved Memory={reserved_memory_mb} MB, Available Memory={final_available_memory} MB")
            
            worker_nodes.append({
                "cpu": final_available_cpu,
                "memory": final_available_memory,
                "hostname": host.get("hostname"),
                "ip_address": host.get("ip_address"),
                "total_cpu_cores": total_cpu_cores,
                "total_memory_mb": total_memory_mb,
            })
        
        logger.info(f"Retrieved {len(worker_nodes)} worker nodes from Redis")
        return worker_nodes
        
    except Exception as e:
        logger.error(f"Failed to get worker nodes from Redis: {e}", exc_info=True)
        return []


class ClusterWorkerDistribution:
    @log_to_file(logger)
    def __init__(self, worker_nodes: list[dict[str, int]], cluster_infos: dict[str, dict[str, int]]) -> None:
        if not isinstance(worker_nodes, list):
            logger.error("Error: Worker nodes must be a list.")
            return
        if not isinstance(cluster_infos, dict) or not cluster_infos:
            logger.error("Error: cluster_info must be a non-empty dictionary.")
            return
        self.worker_nodes = worker_nodes
        self.cluster_infos = cluster_infos

    @log_to_file(logger)
    def calculate_nodes_needed(self) -> int:
        """Calculate the number of nodes needed if no nodes are provided."""
        if not self.worker_nodes:
            # Calculate total CPU and memory required
            total_cpu_per_cluster = {cluster_info: values['cpu'] * values['instances'] for cluster_info, values in
                                     self.cluster_infos.items()}
            total_memory_per_cluster = {cluster_info: values['memory'] * values['instances'] for cluster_info, values in
                                        self.cluster_infos.items()}
            total_cpus_need = sum(total_cpu_per_cluster.values())
            total_memory_need = sum(total_memory_per_cluster.values())

            # Assume a default node configuration if no nodes are provided
            default_node = {'cpu': 20, 'memory': 24}  # You can change this to your preferred default
            cpu_per_node = default_node['cpu']
            memory_per_node = default_node['memory']

            # Calculate the number of nodes needed
            nodes_for_cpu = total_cpus_need / cpu_per_node
            nodes_for_memory = total_memory_need / memory_per_node
            nodes_needed = max(nodes_for_cpu, nodes_for_memory)

            # Round up to the nearest integer
            return int(nodes_needed) if nodes_needed == int(nodes_needed) else int(nodes_needed) + 1
        else:
            return len(self.worker_nodes)

    @log_to_file(logger)
    def distribute_cluster_nodes(self) -> dict[int, list] | None:
        """Distributes microservice instances across worker nodes based on CPU and memory limits."""
        if not self.worker_nodes:
            nodes_needed = self.calculate_nodes_needed()
            logger.warning(f"No worker nodes provided. {nodes_needed} nodes are needed.")
            return None

        # Rest of the distribution logic remains the same
        for node in self.worker_nodes:
            if not isinstance(node, dict) or "cpu" not in node or "memory" not in node:
                logger.error("Error: Each worker node must have 'cpu' and 'memory' keys")
                return None
            if not isinstance(node["cpu"], (int, float)) or node["cpu"] < 0 or not isinstance(node["memory"],
                                                                                              (int, float)) or node[
                "memory"] < 0:
                logger.error("Error: Worker node cpu and memory must be non-negative numbers")
                return None

        for service in self.cluster_infos.values():
            if not isinstance(service,
                              dict) or "cpu" not in service or "memory" not in service or "instances" not in service:
                logger.error("Error: Each cluster inf must have 'cpu', 'memory' and 'instances' keys")
                return None
            if not isinstance(service["cpu"], (int, float)) or service["cpu"] < 0 or not isinstance(service["memory"],
                                                                                                    (int, float)) or \
                    service["memory"] < 0 or not isinstance(service["instances"], int) or service["instances"] <= 0:
                logger.error(
                    "Error: Microservice cpu, memory must be non-negative numbers and instances must be positive integer")
                return None

        num_nodes = len(self.worker_nodes)
        distribution = {i: [] for i in range(num_nodes)}
        total_worker_cpu = sum(self.worker_nodes[i]['cpu'] for i in range(num_nodes))
        total_worker_memory = sum(self.worker_nodes[i]['memory'] for i in range(num_nodes))

        total_cpu_per_cluster = {cluster_info: values['cpu'] * values['instances'] for cluster_info, values in
                                 self.cluster_infos.items()}
        total_memory_per_cluster = {cluster_info: values['memory'] * values['instances'] for cluster_info, values in
                                    self.cluster_infos.items()}
        total_memory_need = sum(total_memory_per_cluster.values())
        total_cpus_need = sum(total_cpu_per_cluster.values())

        all_instances = []
        for cluster_name, requirements in self.cluster_infos.items():
            for instance_num in range(requirements['instances']):
                all_instances.append((cluster_name, instance_num))

        sorted_instances = sorted(all_instances,
                                  key=lambda item: total_cpu_per_cluster[item[0]] + total_memory_per_cluster[item[0]],
                                  reverse=True)

        # Track which nodes have instances of each service for load balancing
        service_node_counts = {}  # {service_name: {node_index: count}}
        
        for service_name, instance_num in sorted_instances:
            requirements = self.cluster_infos[service_name]
            best_node = -1
            min_resource_usage = float('inf')
            
            # Initialize service node counts if needed
            if service_name not in service_node_counts:
                service_node_counts[service_name] = {}
                for i in range(num_nodes):
                    service_node_counts[service_name][i] = 0
            
            logger.info(f"Placing {service_name} instance {instance_num} (CPU: {requirements['cpu']}, Memory: {requirements['memory']})")

            # First pass: Find all nodes that can fit this instance
            candidate_nodes = []
            for i in range(num_nodes):
                node = self.worker_nodes[i]
                current_cpu_usage = sum(
                    self.cluster_infos[s]['cpu'] for s, _ in distribution[i] if s in self.cluster_infos)
                current_memory_usage = sum(
                    self.cluster_infos[s]['memory'] for s, _ in distribution[i] if s in self.cluster_infos)
                
                logger.info(f"  Node {i}: available CPU={node['cpu']}, used CPU={current_cpu_usage}, available Memory={node['memory']}, used Memory={current_memory_usage}")
                logger.info(f"  Node {i}: After adding this instance: CPU={current_cpu_usage + requirements['cpu']}/{node['cpu']}, Memory={current_memory_usage + requirements['memory']}/{node['memory']}")

                if (node['cpu'] >= current_cpu_usage + requirements['cpu'] and
                        node['memory'] >= current_memory_usage + requirements['memory']):
                    resource_usage = current_cpu_usage + requirements['cpu'] + current_memory_usage + requirements['memory']
                    instance_count_on_node = service_node_counts[service_name][i]
                    # Calculate available resources (for redundancy prioritization)
                    available_cpu = node['cpu'] - current_cpu_usage
                    available_memory = node['memory'] - current_memory_usage
                    candidate_nodes.append({
                        'node_index': i,
                        'resource_usage': resource_usage,
                        'instance_count': instance_count_on_node,
                        'available_cpu': available_cpu,
                        'available_memory': available_memory,
                        'node': node
                    })
                    logger.info(f"  Node {i}: Can fit! Resource usage would be: {resource_usage}, current instances of {service_name}: {instance_count_on_node}, available CPU: {available_cpu}, available Memory: {available_memory}")
                else:
                    logger.info(f"  Node {i}: Cannot fit (CPU: {current_cpu_usage + requirements['cpu']} > {node['cpu']} or Memory: {current_memory_usage + requirements['memory']} > {node['memory']})")

            if not candidate_nodes:
                logger.warning(
                    f"Could not place instance {instance_num} of microservice {service_name}. Insufficient resources on all nodes. As requested CPUs are {total_cpus_need} available cpus are {total_worker_cpu} and Memory need is {total_memory_need} and available is {total_worker_memory}")
                return None

            # Enhanced load balancing for redundancy:
            # 1. PRIORITY: Prefer nodes with 0 instances of this service (for redundancy)
            # 2. Among nodes with 0 instances, prefer nodes with more available resources (to balance load)
            # 3. If all nodes have instances, prefer the one with fewer instances
            # 4. If tied on instance count, prefer lower resource usage
            
            # Separate candidates into two groups: nodes with 0 instances vs nodes with instances
            nodes_with_zero_instances = [c for c in candidate_nodes if c['instance_count'] == 0]
            nodes_with_instances = [c for c in candidate_nodes if c['instance_count'] > 0]
            
            if nodes_with_zero_instances:
                # Prioritize nodes with 0 instances for redundancy
                # Among these, prefer nodes with more available resources (to balance load)
                # Sort by total available capacity in descending order (more available = better)
                nodes_with_zero_instances.sort(
                    key=lambda x: x['available_cpu'] + x['available_memory'],
                    reverse=True  # Descending order: more available resources first
                )
                best_candidate = nodes_with_zero_instances[0]
                logger.info(f"  Selected node {best_candidate['node_index']} (0 instances of {service_name}, available CPU: {best_candidate['available_cpu']}, available Memory: {best_candidate['available_memory']}) for redundancy")
            else:
                # All nodes already have instances, use original logic: prefer fewer instances, then lower resource usage
                candidate_nodes.sort(key=lambda x: (x['instance_count'], x['resource_usage']))
                best_candidate = candidate_nodes[0]
                logger.info(f"  Selected node {best_candidate['node_index']} (instances of {service_name}: {best_candidate['instance_count']}, resource usage: {best_candidate['resource_usage']})")
            
            best_node = best_candidate['node_index']
            min_resource_usage = best_candidate['resource_usage']

            # Place the instance
            distribution[best_node].append((service_name, instance_num))
            service_node_counts[service_name][best_node] += 1
            logger.info(f"Placed {service_name} instance {instance_num} on node {best_node}. Current distribution: {distribution}")

        logger.info(f"Final distribution result: {distribution}")
        return distribution


def main():
    # Example with no worker nodes
    microservices = {
        'service_a': {'cpu': 3, 'memory': 5, 'instances': 2},
        'service_b': {'cpu': 2, 'memory': 3, 'instances': 20},
        'service_c': {'cpu': 5, 'memory': 8, 'instances': 4},
        'service_d': {'cpu': 4, 'memory': 4, 'instances': 3}
    }
    cwn = ClusterWorkerDistribution([], microservices)
    nodes_needed = cwn.calculate_nodes_needed()
    logger.info(f"Nodes needed: {nodes_needed}")

    # Example with worker nodes
    # worker_nodes = [
    #     {'cpu': 20, 'memory': 24},
    #     {'cpu': 20, 'memory': 24},
    #     {'cpu': 20, 'memory': 24},
    #     {'cpu': 20, 'memory': 24}
    # ]
    worker_nodes = get_worker_nodes_from_redis()
    cwn = ClusterWorkerDistribution(worker_nodes, microservices)
    distribution = cwn.distribute_cluster_nodes()
    if distribution:
        for node_index, services in distribution.items():
            logger.info(f"Node {node_index + 1}: {services}")
            node_cpu_usage = sum(microservices[s]['cpu'] for s, _ in services if s in microservices)
            node_mem_usage = sum(microservices[s]['memory'] for s, _ in services if s in microservices)
            logger.info(
                f"  CPU Usage: {node_cpu_usage}/{worker_nodes[node_index]['cpu']}, Memory Usage: {node_mem_usage}/{worker_nodes[node_index]['memory']}")


# if __name__ == "__main__":
#     main()