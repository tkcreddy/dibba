class ClusterWorkerDistribution:
    def __init__(self, worker_nodes: list[dict[str, int]], cluster_infos: dict[str, dict[str, int]]) -> None:
        if not isinstance(worker_nodes, list):
            print("Error: Worker nodes must be a list.")
            return
        if not isinstance(cluster_infos, dict) or not cluster_infos:
            print("Error: cluster_info must be a non-empty dictionary.")
            return
        self.worker_nodes = worker_nodes
        self.cluster_infos = cluster_infos

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

    def distribute_cluster_nodes(self) -> dict[int, list] | None:
        """Distributes microservice instances across worker nodes based on CPU and memory limits."""
        if not self.worker_nodes:
            nodes_needed = self.calculate_nodes_needed()
            print(f"No worker nodes provided. {nodes_needed} nodes are needed.")
            return None

        # Rest of the distribution logic remains the same
        for node in self.worker_nodes:
            if not isinstance(node, dict) or "cpu" not in node or "memory" not in node:
                print("Error: Each worker node must have 'cpu' and 'memory' keys")
                return None
            if not isinstance(node["cpu"], (int, float)) or node["cpu"] < 0 or not isinstance(node["memory"],
                                                                                              (int, float)) or node[
                "memory"] < 0:
                print("Error: Worker node cpu and memory must be non-negative numbers")
                return None

        for service in self.cluster_infos.values():
            if not isinstance(service,
                              dict) or "cpu" not in service or "memory" not in service or "instances" not in service:
                print("Error: Each cluster inf must have 'cpu', 'memory' and 'instances' keys")
                return None
            if not isinstance(service["cpu"], (int, float)) or service["cpu"] < 0 or not isinstance(service["memory"],
                                                                                                    (int, float)) or \
                    service["memory"] < 0 or not isinstance(service["instances"], int) or service["instances"] <= 0:
                print(
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

        for service_name, instance_num in sorted_instances:
            requirements = self.cluster_infos[service_name]
            best_node = -1
            min_resource_usage = float('inf')

            for i in range(num_nodes):
                node = self.worker_nodes[i]
                current_cpu_usage = sum(
                    self.cluster_infos[s]['cpu'] for s, _ in distribution[i] if s in self.cluster_infos)
                current_memory_usage = sum(
                    self.cluster_infos[s]['memory'] for s, _ in distribution[i] if s in self.cluster_infos)

                if (node['cpu'] >= current_cpu_usage + requirements['cpu'] and
                        node['memory'] >= current_memory_usage + requirements['memory']):
                    resource_usage = current_cpu_usage + requirements['cpu'] + current_memory_usage + requirements[
                        'memory']
                    if resource_usage < min_resource_usage:
                        min_resource_usage = resource_usage
                        best_node = i

            if best_node != -1:
                distribution[best_node].append((service_name, instance_num))
            else:
                print(
                    f"Warning: Could not place instance {instance_num} of microservice {service_name}. Insufficient resources on all nodes. As requested CPUs are {total_cpus_need} available cpus are {total_worker_cpu} and Memory need is {total_memory_need} and available is {total_worker_memory}")
                return None

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
    print(f"Nodes needed: {nodes_needed}")

    # Example with worker nodes
    worker_nodes = [
        {'cpu': 20, 'memory': 24},
        {'cpu': 20, 'memory': 24},
        {'cpu': 20, 'memory': 24},
        {'cpu': 20, 'memory': 24}
    ]
    cwn = ClusterWorkerDistribution(worker_nodes, microservices)
    distribution = cwn.distribute_cluster_nodes()
    if distribution:
        for node_index, services in distribution.items():
            print(f"Node {node_index + 1}: {services}")
            node_cpu_usage = sum(microservices[s]['cpu'] for s, _ in services if s in microservices)
            node_mem_usage = sum(microservices[s]['memory'] for s, _ in services if s in microservices)
            print(
                f"  CPU Usage: {node_cpu_usage}/{worker_nodes[node_index]['cpu']}, Memory Usage: {node_mem_usage}/{worker_nodes[node_index]['memory']}")


if __name__ == "__main__":
    main()