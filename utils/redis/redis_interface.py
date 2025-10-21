import redis
import json
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld, log_to_file
logger = LogKCld()

read_conf = rc('/Users/krishnareddy/PycharmProjects/baks/')
redis_config = read_conf.redis_db_config


class RedisInterface:
    @log_to_file(logger)
    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 1):
        print(f"{redis_config['ssl_ca_certs'], redis_config['ssl_certfile'], redis_config['ssl_keyfile']}")
        self.redis_client = redis.Redis(host=redis_config['redis_host'], port=redis_config['redis_port'], db=redis_config['redis_db'], decode_responses=True, ssl=True,
                                        ssl_ca_certs=redis_config['ssl_ca_certs'],
                                        ssl_certfile=redis_config['ssl_certfile'],
                                        ssl_keyfile=redis_config['ssl_keyfile'])
        #self.redis_kkk= redis.StrictRedis

    @log_to_file(logger)
    def save_user_pass(self, user, password):
        self.redis_client.hset("authentication", user, password)

    @log_to_file(logger)
    def get_user_pass(self, user):
        password = self.redis_client.hget("authentication", user)
        return password or None

    # Nodes Storage
    @log_to_file(logger)
    def save_node(self, name, data: dict):
        logger.info(f"save_node {name} {data}")
        self.redis_client.hset("nodes", name, json.dumps(data))

    @log_to_file(logger)
    def get_nodes(self):
        nodes = self.redis_client.hgetall("nodes")
        return {name: json.loads(data) for name, data in nodes.items()}

    @log_to_file(logger)
    def get_instance_ids(self):
        """Retrieves a list of instance IDs from Redis-stored node data."""
        if nodes := self.redis_client.hgetall("nodes"):
            return [json.loads(data).get("InstanceId") for data in nodes.values() if "InstanceId" in json.loads(data)]
        else:
            return None

    @log_to_file(logger)
    def get_instance_ids_namespace(self, namespace):
        """Retrieves a list of instance IDs from Redis-stored node data."""
        nodes = self.redis_client.hgetall("nodes")
        print(nodes)
        instance_ids = []
        if not nodes:
            return None
        for data in nodes.values():
            node_data = json.loads(data)
            if node_data.get("NameSpace") == namespace:
                instance_ids.append(node_data.get("InstanceId"))
        return instance_ids or None

    @log_to_file(logger)
    def delete_instance_ids(self, instance_ids: list):
        """Retrieves a list of instance IDs from Redis-stored node data and deletes the corresponding keys."""
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
    def get_node_by_name(self, name):
        data = self.redis_client.hget("nodes", name)
        return json.loads(data) if data else None

    # Get a Node by IP Address (Search All Nodes)
    import json
    @log_to_file(logger)
    def get_node_by_ip(self, ip_address):
        """Retrieves node details by IP address from Redis-stored node data."""
        nodes = self.redis_client.hgetall("nodes")

        for name, data in nodes.items():
            node_data = json.loads(data)
            if node_data.get("IpAddress") == ip_address:
                return {"name": name, "IpAddress": node_data["IpAddress"]}

        return None



    @log_to_file(logger)
    def save_node_config(self, name, cpu, memory):
        self.redis_client.hset("node_config", name, json.dumps({"cpu": cpu, "memory": memory}))

    @log_to_file(logger)
    def get_node_config_more_cpu(self, cpu):
        return self._extracted_from_get_node_config_more_mem_2("cpu", cpu)

    @log_to_file(logger)
    def get_node_config_more_mem(self, memory):
        return self._extracted_from_get_node_config_more_mem_2("memory", memory)



    # TODO Rename this here and in `get_node_config_more_cpu` and `get_node_config_more_mem`
    @log_to_file(logger)
    def _extracted_from_get_node_config_more_mem_2(self, arg0, arg1):
        nodes = self.redis_client.hgetall("node_config")
        for name, data in nodes.items():
            node_data = json.loads(data)
            if node_data[arg0] > arg1:
                return {"name": name, arg0: node_data[arg0]}
        return None

    # Container Storage
    @log_to_file(logger)
    def save_container(self, container_name, ipaddress, node):
        self.redis_client.hset("containers", container_name, json.dumps({"ipaddress": ipaddress, "node": node}))

    @log_to_file(logger)
    def get_containers(self):
        containers = self.redis_client.hgetall("containers")
        return {cid: json.loads(data) for cid, data in containers.items()}

    # Get a Container by Name
    @log_to_file(logger)
    def get_container_by_name(self, name):
        data = self.redis_client.hget("containers", name)
        return json.loads(data) if data else None

    @log_to_file(logger)
    def get_containers_node(self, node_name):
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
    def save_namespace_mapping(self, namespace, node):
        self.redis_client.hset("namespace_mapping", namespace, node)

    @log_to_file(logger)
    def get_namespace_mappings(self):
        return self.redis_client.hgetall("namespace_mapping")

    # Container to App Cluster Mapping
    @log_to_file(logger)
    def save_container_cluster(self, container_id, cluster):
        self.redis_client.hset("container_clusters", container_id, cluster)

    @log_to_file(logger)
    def get_container_clusters(self):
        return self.redis_client.hgetall("container_clusters")

    # Cluster Health Check Configuration
    @log_to_file(logger)
    def save_cluster_health(self, cluster, port, url, interval, checks):
        self.redis_client.hset("cluster_health", cluster, json.dumps({
            "port": port,
            "url": url,
            "interval": interval,
            "checks": checks
        }))

    @log_to_file(logger)
    def get_cluster_health(self):
        clusters = self.redis_client.hgetall("cluster_health")
        return {cluster: json.loads(data) for cluster, data in clusters.items()}

    # Healthy Containers in a Cluster
    @log_to_file(logger)
    def add_healthy_container(self, cluster, container_id):
        self.redis_client.sadd(f"healthy_containers:{cluster}", container_id)

    @log_to_file(logger)
    def get_healthy_containers(self, cluster):
        return self.redis_client.smembers(f"healthy_containers:{cluster}")

    @log_to_file(logger)
    def save_url_cluster(self, url, cluster):
        self.redis_client.hset("url_to_cluster", url, cluster)

    @log_to_file(logger)
    def get_url_cluster(self,cluster):
        url_list=[]
        try:
            url_data=self.redis_client.hgetall("url_to_cluster")
            url_list.extend(name for name, data in url_data.items() if data == cluster)
        except Exception as e:
            print(f"Error: {e}")
        return url_list

