import random

def distribute_microservices(worker_nodes, microservices):
    """Distributes microservice instances across worker nodes based on CPU and memory limits.

    Args:
        worker_nodes: A list of dictionaries, where each dictionary represents a worker node
            and contains 'cpu' and 'memory' (available resources).
        microservices: A dictionary where keys are microservice names and values are dictionaries
            containing 'cpu', 'memory', and 'instances' (number of instances required).

    Returns:
        A dictionary where keys are worker node indices and values are lists of tuples,
        where each tuple contains (microservice name, instance number). Returns None if input is invalid
        or distribution is impossible.
    """

    if not isinstance(worker_nodes, list) or not worker_nodes:
        print("Error: Worker nodes must be a non-empty list.")
        return None
    if not isinstance(microservices, dict) or not microservices:
        print("Error: Microservices must be a non-empty dictionary.")
        return None

    for node in worker_nodes:
        if not isinstance(node, dict) or "cpu" not in node or "memory" not in node:
            print("Error: Each worker node must have 'cpu' and 'memory' keys")
            return None
        if not isinstance(node["cpu"], (int, float)) or node["cpu"] < 0 or not isinstance(node["memory"], (int, float)) or node["memory"] < 0:
            print("Error: Worker node cpu and memory must be non-negative numbers")
            return None

    for service in microservices.values():
        if not isinstance(service, dict) or "cpu" not in service or "memory" not in service or "instances" not in service:
            print("Error: Each microservice must have 'cpu', 'memory' and 'instances' keys")
            return None
        if not isinstance(service["cpu"], (int, float)) or service["cpu"] < 0 or not isinstance(service["memory"], (int, float)) or service["memory"] < 0 or not isinstance(service["instances"],int) or service["instances"]<=0:
            print("Error: Microservice cpu, memory must be non-negative numbers and instances must be positive integer")
            return None

    num_nodes = len(worker_nodes)
    distribution = {i: [] for i in range(num_nodes)}

    # Create a list of all microservice instances to distribute
    all_instances = []
    for service_name, requirements in microservices.items():
        for instance_num in range(requirements['instances']):
            all_instances.append((service_name, instance_num))

    #Sort microservices by their combined resource requirements (descending) to attempt to fit larger services first
    sorted_instances = sorted(all_instances, key=lambda item: microservices[item[0]]['cpu'] + microservices[item[0]]['memory'], reverse=True)
    #print(sorted_instances)
    for service_name, instance_num in sorted_instances:
        requirements = microservices[service_name]
        print(requirements)
        best_node = -1
        min_resource_usage = float('inf')

        for i in range(num_nodes):
            node = worker_nodes[i]
            current_cpu_usage = sum(microservices[s]['cpu'] for s, _ in distribution[i] if s in microservices)
            current_memory_usage = sum(microservices[s]['memory'] for s, _ in distribution[i] if s in microservices)

            if (node['cpu'] >= current_cpu_usage + requirements['cpu'] and
                    node['memory'] >= current_memory_usage + requirements['memory']):
                resource_usage = current_cpu_usage + requirements['cpu'] + current_memory_usage + requirements['memory']
                if resource_usage < min_resource_usage:
                    min_resource_usage = resource_usage
                    best_node = i

        if best_node != -1:
            distribution[best_node].append((service_name, instance_num))
        else:
            print(f"Warning: Could not place instance {instance_num} of microservice '{service_name}'. Insufficient resources on all nodes.")
            return None

    return distribution

# Example usage:
worker_nodes = [
    {'cpu': 20, 'memory': 50},
    {'cpu': 28, 'memory': 30},
    {'cpu': 22, 'memory': 50}
]

microservices = {
    'service_a': {'cpu': 3, 'memory': 5, 'instances': 2},
    'service_b': {'cpu': 2, 'memory': 3, 'instances': 3},
    'service_c': {'cpu': 5, 'memory': 8, 'instances': 4}
}

distribution = distribute_microservices(worker_nodes, microservices)

if distribution:
    for node_index, services in distribution.items():
        print(f"Node {node_index + 1}: {services}")
        node_cpu_usage = sum(microservices[s]['cpu'] for s, _ in services if s in microservices)
        node_mem_usage = sum(microservices[s]['memory'] for s, _ in services if s in microservices)
        print(f"  CPU Usage: {node_cpu_usage}/{worker_nodes[node_index]['cpu']}, Memory Usage: {node_mem_usage}/{worker_nodes[node_index]['memory']}")

#Example where a service cannot be placed.
microservices_impossible = {
    'service_a': {'cpu': 3, 'memory': 5, 'instances': 2},
    'service_b': {'cpu': 2, 'memory': 3, 'instances': 3},
    'service_c': {'cpu': 15, 'memory': 20, 'instances': 1} #This service cannot be placed.
}
distribution_impossible = distribute_microservices(worker_nodes, microservices_impossible)

microservices_invalid = {
    'service_a': {'cpu': 3, 'memory': 5, 'instances': -2},
}
distribution_invalid_microservices = distribute_microservices(worker_nodes, microservices_invalid)

microservices_invalid_keys = {
    'service_a': {'cpu': 3, 'memory': 5},
}
distribution_invalid_microservices_keys = distribute_microservices(worker_nodes, microservices_invalid_keys)