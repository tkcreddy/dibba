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
aws_interface_queue_name = encode_util.encode_hostname_with_key('aws_interface')
scheduler_queue_name = encode_util.encode_hostname_with_key('scheduler')

celery_app.conf.task_queues = [
    Queue(aws_interface_queue_name, exchange=secure_exchange, routing_key=aws_interface_queue_name),
    Queue(scheduler_queue_name, exchange=secure_exchange, routing_key=scheduler_queue_name),
]
celery_app.autodiscover_tasks(['utils.celery.tasks.aws_tasks'])

celery_app.conf.include = [
    "utils.celery.tasks.scheduler_tasks",
    "utils.celery.tasks.deployment_recovery_tasks",
    "utils.celery.tasks.health_check_tasks",  # Include health check tasks
]

