import redis
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld, log_to_file
logger = LogKCld()

read_config = rc('/Users/krishnareddy/PycharmProjects/baks/')
redis_config = read_config.redis_queue_config

class HcTrack:
    @log_to_file(logger)
    def __init__(self):
        # Connect to Redis
        self.redis_client = redis.StrictRedis(host=redis_config['redis_host'], port=redis_config['redis_port'], db=redis_config['redis_db'], decode_responses=True, ssl=True,
                                        ssl_ca_certs=redis_config['ssl_ca_certs'],
                                        ssl_certfile=redis_config['ssl_certfile'],
                                        ssl_keyfile=redis_config['ssl_keyfile'])
    @log_to_file(logger)
    def track_consecutive_failures(self, key:str,status:str,cluster_name:str,time:int=60):
        failure_count_key = f"{cluster_name}:::{key}:::failure_count"
        if status == "healthy":
            print("Health check passed.")
            self.redis_client.hset(cluster_name,key, 0)  # Reset failure count on success
        elif status == "unhealthy":
            # Increment failure count on failure
            current_count = self.redis_client.hincrby(cluster_name,key,1)
            print(f"Health check failed. Consecutive failures: {current_count}")
        self.redis_client.expire(cluster_name,60)

    @log_to_file(logger)
    def lb_update(self, url:str,status:str,time:int=60,cluster_name:str =None):
        failure_count_key = f"{url}:::failure_count"
        if status == "healthy":
            print("Health check passed.")
            self.redis_client.set(failure_count_key, 0)  # Reset failure count on success
        elif status == "unhealthy":
            # Increment failure count on failure
            current_count = self.redis_client.incr(failure_count_key)
            print(f"Health check failed. Consecutive failures: {current_count}")
        self.redis_client.expire(failure_count_key,60)

