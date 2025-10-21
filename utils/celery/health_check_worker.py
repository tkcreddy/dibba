from utils.celery.celery_config import celery_app
from kombu import Queue,Exchange
from socket import gethostname
from utils.ReadConfig import ReadConfig as rc
from utils.extensions.utilities_extention import UtilitiesExtension

secure_exchange = Exchange('secure_exchange', type='direct')
hostname = gethostname()

read_config = rc()
key = read_config.encryption_config['key']
encode_util = UtilitiesExtension(key)
health_check_queue_name = encode_util.encode_hostname_with_key('health_check')
# encode_hostname=encode_util.encode_hostname_with_key(hostname)
celery_app.conf.task_queues = [
            Queue(health_check_queue_name, exchange=secure_exchange, routing_key=health_check_queue_name),]


# Generate the dynamic queue name
# dynamic_queue_name = generate_queue_name(hostname)


celery_app.autodiscover_tasks(['utils.celery.tasks.health_check_tasks'])
