import redis,ssl
import json
from typing import Optional, Dict, Any, List, Union
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld, log_to_file
from utils.exceptions import RedisError
from utils.error_handlers import handle_errors
logger = LogKCld()

read_conf = rc()
redis_config = read_conf.redis_db_config


class RedisInterface:
    @log_to_file(logger)
    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 1):
        logger.debug(f"Redis SSL config - CA: {redis_config.get('ssl_ca_certs')}, Cert: {redis_config.get('ssl_certfile')}, Key: {redis_config.get('ssl_keyfile')}")
        self.redis_client = redis.Redis(host=redis_config['redis_host'], port=redis_config['redis_port'], db=redis_config['redis_db'], decode_responses=True, ssl=True,
                                        ssl_ca_certs=redis_config['ssl_ca_certs'],
                                        ssl_certfile=redis_config['ssl_certfile'],
                                        ssl_keyfile=redis_config['ssl_keyfile'],
                                        ssl_cert_reqs=ssl.CERT_REQUIRED)

    @log_to_file(logger)
    def save_user_pass(self, user: str, password: str) -> None:
        """Save user password to Redis.
        
        Args:
            user: Username
            password: Password hash to store
        """
        self.redis_client.hset("authentication", user, password)

    @log_to_file(logger)
    def get_user_pass(self, user: str) -> Optional[str]:
        """Get user password from Redis.
        
        Args:
            user: Username
            
        Returns:
            Password hash if found, None otherwise
        """
        password = self.redis_client.hget("authentication", user)
        return password or None

    # Nodes Storage
    @log_to_file(logger)
    def save_node(self, name: str, data: Dict[str, Any]) -> None:
        """Save node data to Redis.
        
        Args:
            name: Node name/identifier
            data: Node data dictionary
        """
        logger.info(f"save_node {name} {data}")
        self.redis_client.hset("nodes", name, json.dumps(data))

    @log_to_file(logger)
    def get_nodes(self) -> Dict[str, Dict[str, Any]]:
        """Get all nodes from Redis.
        
        Returns:
            Dictionary mapping node names to node data
        """
        nodes = self.redis_client.hgetall("nodes")
        return {name: json.loads(data) for name, data in nodes.items()}

    @log_to_file(logger)
    def get_instance_ids(self) -> Optional[List[str]]:
        """Retrieves a list of instance IDs from Redis-stored node data.
        
        Returns:
            List of instance IDs if found, None otherwise
        """
        if nodes := self.redis_client.hgetall("nodes"):
            return [json.loads(data).get("InstanceId") for data in nodes.values() if "InstanceId" in json.loads(data)]
        else:
            return None

    @log_to_file(logger)
    def get_instance_ids_namespace(self, namespace: str) -> Optional[List[str]]:
        """Retrieves a list of instance IDs from Redis-stored node data for a namespace.
        
        Args:
            namespace: Namespace to filter by
            
        Returns:
            List of instance IDs if found, None otherwise
        """
        nodes = self.redis_client.hgetall("nodes")
        logger.debug(f"Retrieved nodes for namespace {namespace}: {nodes}")
        instance_ids = []
        if not nodes:
            return None
        for data in nodes.values():
            node_data = json.loads(data)
            if node_data.get("NameSpace") == namespace:
                instance_ids.append(node_data.get("InstanceId"))
        return instance_ids or None

    @log_to_file(logger)
    def delete_instance_ids(self, instance_ids: List[str]) -> Optional[bool]:
        """Delete nodes by instance IDs from Redis.
        
        Args:
            instance_ids: List of instance IDs to delete
            
        Returns:
            True if successful, None if no nodes found
        """
        nodes = self.redis_client.hgetall("nodes")
        instances_results = {}
        if not nodes:
            return None
        for name, data in nodes.items():
            node_data = json.loads(data)
            if node_data.get("InstanceId") in instance_ids:
                self.redis_client.hdel("nodes", name)
        return True

    # Get a Node by Name
    @log_to_file(logger)
    def get_node_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get node data by name.
        
        Args:
            name: Node name
            
        Returns:
            Node data dictionary if found, None otherwise
        """
        data = self.redis_client.hget("nodes", name)
        return json.loads(data) if data else None

    # Get a Node by IP Address (Search All Nodes)
    @log_to_file(logger)
    def get_node_by_ip(self, ip_address: str) -> Optional[Dict[str, str]]:
        """Retrieves node details by IP address from Redis-stored node data.
        
        Args:
            ip_address: IP address to search for
            
        Returns:
            Dictionary with name and IpAddress if found, None otherwise
        """
        nodes = self.redis_client.hgetall("nodes")

        for name, data in nodes.items():
            node_data = json.loads(data)
            if node_data.get("IpAddress") == ip_address:
                return {"name": name, "IpAddress": node_data["IpAddress"]}

        return None



    @log_to_file(logger)
    def save_node_config(self, name: str, cpu: Union[int, float, str], memory: Union[int, float, str]) -> None:
        """Save node configuration (CPU and memory) to Redis.
        
        Args:
            name: Node name
            cpu: CPU specification
            memory: Memory specification
        """
        self.redis_client.hset("node_config", name, json.dumps({"cpu": cpu, "memory": memory}))

    @log_to_file(logger)
    def get_node_config_more_cpu(self, cpu: Union[int, float, str]) -> List[str]:
        """Get nodes with CPU greater than specified value.
        
        Args:
            cpu: Minimum CPU value
            
        Returns:
            List of node names
        """
        return self._get_nodes_by_field_threshold("cpu", cpu)

    @log_to_file(logger)
    def get_node_config_more_mem(self, memory: Union[int, float, str]) -> List[str]:
        """Get nodes with memory greater than specified value.
        
        Args:
            memory: Minimum memory value
            
        Returns:
            List of node names
        """
        return self._get_nodes_by_field_threshold("memory", memory)



    @log_to_file(logger)
    def _get_nodes_by_field_threshold(
        self,
        field: str,
        value: Union[int, float, str]
    ) -> List[str]:
        """Helper method to get nodes with field value greater than specified.
        
        Args:
            field: Field name to compare ("cpu" or "memory")
            value: Minimum value
            
        Returns:
            List of node names matching criteria
        """
        nodes = self.redis_client.hgetall("node_config")
        result = []
        for name, data in nodes.items():
            node_data = json.loads(data)
            if field in node_data and node_data[field] > value:
                result.append(name)
        return result

    # Container Storage
    @log_to_file(logger)
    def save_container(self, container_name: str, ipaddress: str, node: str) -> None:
        """Save container information to Redis.
        
        Args:
            container_name: Container name/ID
            ipaddress: Container IP address
            node: Node name where container is running
        """
        self.redis_client.hset("containers", container_name, json.dumps({"ipaddress": ipaddress, "node": node}))

    @log_to_file(logger)
    def get_containers(self) -> Dict[str, Dict[str, str]]:
        """Get all containers from Redis.
        
        Returns:
            Dictionary mapping container IDs to container data
        """
        containers = self.redis_client.hgetall("containers")
        return {cid: json.loads(data) for cid, data in containers.items()}

    # Get a Container by Name
    @log_to_file(logger)
    def get_container_by_name(self, name: str) -> Optional[Dict[str, str]]:
        """Get container data by name.
        
        Args:
            name: Container name
            
        Returns:
            Container data dictionary if found, None otherwise
        """
        data = self.redis_client.hget("containers", name)
        return json.loads(data) if data else None

    @log_to_file(logger)
    def get_containers_node(self, node_name: str) -> Optional[List[str]]:
        """Get containers running on a specific node.
        
        Args:
            node_name: Node name to filter by
            
        Returns:
            List of container names if found, None otherwise
        """
        containers_on_node = []
        containers = self.redis_client.hgetall("containers")
        for name, data in containers.items():
            containers_data = json.loads(data)
            if containers_data["node"] == node_name:
                containers_on_node.append(name)
                return containers_on_node
        return None

    # Namespace to Node Mapping
    @log_to_file(logger)
    def save_namespace_mapping(self, namespace: str, node: str) -> None:
        """Save namespace to node mapping.
        
        Args:
            namespace: Namespace name
            node: Node name
        """
        self.redis_client.hset("namespace_mapping", namespace, node)

    @log_to_file(logger)
    def get_namespace_mappings(self) -> Dict[str, str]:
        """Get all namespace to node mappings.
        
        Returns:
            Dictionary mapping namespaces to nodes
        """
        return self.redis_client.hgetall("namespace_mapping")

    # Container to App Cluster Mapping
    @log_to_file(logger)
    def save_container_cluster(self, container_id: str, cluster: str) -> None:
        """Save container to cluster mapping.
        
        Args:
            container_id: Container ID
            cluster: Cluster name
        """
        self.redis_client.hset("container_clusters", container_id, cluster)

    @log_to_file(logger)
    def get_container_clusters(self) -> Dict[str, str]:
        """Get all container to cluster mappings.
        
        Returns:
            Dictionary mapping container IDs to clusters
        """
        return self.redis_client.hgetall("container_clusters")

    # Cluster Health Check Configuration
    @log_to_file(logger)
    def save_cluster_health(self, cluster: str, port: int, url: str, interval: int, checks: int) -> None:
        """Save cluster health check configuration.
        
        Args:
            cluster: Cluster name
            port: Health check port
            url: Health check URL
            interval: Check interval in seconds
            checks: Number of checks
        """
        self.redis_client.hset("cluster_health", cluster, json.dumps({
            "port": port,
            "url": url,
            "interval": interval,
            "checks": checks
        }))

    @log_to_file(logger)
    def get_cluster_health(self) -> Dict[str, Dict[str, Any]]:
        """Get all cluster health check configurations.
        
        Returns:
            Dictionary mapping cluster names to health check configs
        """
        clusters = self.redis_client.hgetall("cluster_health")
        return {cluster: json.loads(data) for cluster, data in clusters.items()}

    # Healthy Containers in a Cluster
    @log_to_file(logger)
    def add_healthy_container(self, cluster: str, container_id: str) -> None:
        """Add a container to the healthy containers set for a cluster.
        
        Args:
            cluster: Cluster name
            container_id: Container ID
        """
        self.redis_client.sadd(f"healthy_containers:{cluster}", container_id)

    @log_to_file(logger)
    def get_healthy_containers(self, cluster: str) -> set:
        """Get healthy containers for a cluster.
        
        Args:
            cluster: Cluster name
            
        Returns:
            Set of healthy container IDs
        """
        return self.redis_client.smembers(f"healthy_containers:{cluster}")

    @log_to_file(logger)
    def save_url_cluster(self, url: str, cluster: str) -> None:
        """Save URL to cluster mapping.
        
        Args:
            url: URL
            cluster: Cluster name
        """
        self.redis_client.hset("url_to_cluster", url, cluster)

    @log_to_file(logger)
    @handle_errors("get_url_cluster", "REDIS_ERROR")
    def get_url_cluster(self, cluster: str) -> List[str]:
        """Get URLs for a specific cluster.
        
        Args:
            cluster: Cluster name
            
        Returns:
            List of URLs associated with the cluster
        """
        url_list=[]
        try:
            url_data=self.redis_client.hgetall("url_to_cluster")
            url_list.extend(name for name, data in url_data.items() if data == cluster)
        except Exception as e:
            raise RedisError(
                message=f"Failed to get URL cluster from Redis: {str(e)}",
                error_code="REDIS_GET_URL_CLUSTER_ERROR",
                details={"cluster": cluster},
                cause=e
            ) from e
        return url_list

    # AWS Node Configuration Storage
    @log_to_file(logger)
    def save_aws_node_config(self, config: Dict[str, Any]) -> None:
        """Save AWS node configuration to Redis.
        
        Only stores the 4 requested fields:
            - ami_id: AMI ID
            - key_name: Key pair name
            - security_group_ids: List of security group IDs
            - subnet_id: Subnet ID
        
        Args:
            config: Dictionary containing AWS configuration (only the 4 fields above are stored)
        """
        # Only store the 4 requested fields
        allowed_fields = ['ami_id', 'key_name', 'security_group_ids', 'subnet_id']
        
        # Store each field individually for easier updates
        if 'ami_id' in config:
            self.redis_client.hset("aws_node_config", "ami_id", config['ami_id'])
        if 'key_name' in config:
            self.redis_client.hset("aws_node_config", "key_name", config['key_name'])
        if 'security_group_ids' in config:
            self.redis_client.hset("aws_node_config", "security_group_ids", json.dumps(config['security_group_ids']))
        if 'subnet_id' in config:
            self.redis_client.hset("aws_node_config", "subnet_id", config['subnet_id'])
        
        stored_fields = [k for k in allowed_fields if k in config]
        logger.info(f"Saved AWS node configuration to Redis: {stored_fields}")

    @log_to_file(logger)
    def get_aws_node_config(self) -> Optional[Dict[str, Any]]:
        """Get AWS node configuration from Redis.
        
        Only returns the 4 requested fields:
            - ami_id
            - key_name
            - security_group_ids
            - subnet_id
        
        Returns:
            Dictionary containing only the 4 requested AWS configuration fields, or None if not found
        """
        config_data = self.redis_client.hgetall("aws_node_config")
        if not config_data:
            return None
        
        # Only return the 4 requested fields
        config = {}
        if 'ami_id' in config_data:
            config['ami_id'] = config_data['ami_id']
        if 'key_name' in config_data:
            config['key_name'] = config_data['key_name']
        if 'security_group_ids' in config_data:
            try:
                config['security_group_ids'] = json.loads(config_data['security_group_ids'])
            except (json.JSONDecodeError, TypeError):
                # Handle legacy single-value or comma-separated format
                if isinstance(config_data['security_group_ids'], list):
                    config['security_group_ids'] = config_data['security_group_ids']
                else:
                    config['security_group_ids'] = [config_data['security_group_ids']]
        if 'subnet_id' in config_data:
            config['subnet_id'] = config_data['subnet_id']
        
        return config if config else None

    @log_to_file(logger)
    def delete_aws_node_config(self) -> None:
        """Delete AWS node configuration from Redis."""
        self.redis_client.delete("aws_node_config")
        logger.info("Deleted AWS node configuration from Redis")

