from utils.celery.celery_config import celery_app
from utils.celery.tasks.worker_node_tasks import get_worker_node_info
from kombu import Queue,Exchange
from socket import gethostname
from utils.ReadConfig import ReadConfig as rc
from utils.extensions.utilities_extention import UtilitiesExtension
from kombu import Exchange
from logpkg.log_kcld import LogKCld, log_to_file
import re
logger = LogKCld()


# Read configuration
read_config = rc()
aws_config = read_config.aws_config
key_read = read_config.encryption_config
redis_db_config = read_config.redis_db_config
ue = UtilitiesExtension(key_read['key'])
def get_celery_nodes():
    """
    Retrieves a list of active Celery worker nodes in the cluster.
    """
    try:
        # Create an inspector instance for the Celery app
        inspector = celery_app.control.inspect()

        # Get statistics from all workers, which includes worker names (nodes)
        stats = inspector.stats()

        if stats:
            # The keys of the stats dictionary are the worker node names
            workers = list(stats.keys())
            nodes = [re.sub(r'^.*@', '', w) for w in workers]
            return nodes
        else:
            return []
    except Exception as e:
        print(f"Error retrieving Celery nodes: {e}")
        return []

if __name__ == "__main__":
    active_nodes = get_celery_nodes()
    print(f"Active node full info is {active_nodes}")
    async_results = []
    if active_nodes:
        print("Active Celery nodes in the cluster:")
        for node in active_nodes:
            print(f"{node}")
            host_queue_info = {
                'exchange': Exchange('secure_exchange', type='direct'),
                'queue': ue.encode_hostname_with_key(node),
                'routing_key': ue.encode_hostname_with_key(node),
                'delivery_mode': 2
            }
            r = get_worker_node_info.apply_async(
                args=(),
                **host_queue_info
                )
            print(f"Queued get_worker_node_info on {node} with task_id={r.id}")
            async_results.append((node, r))
    else:
        print("No active Celery nodes found or an error occurred.")
    for node, r in async_results:
        try:
            info = r.get(timeout=30)
            print(f"Result from {node} ({r.id}): {info}")
        except Exception as e:
            print(f"Error getting result from {node} ({r.id}): {e}")
        finally:
            try:
                # This removes it from the backend and pending registry
                r.forget()
            except Exception:
                pass
