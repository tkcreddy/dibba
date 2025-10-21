from utils.celery.celery_config import celery_app
from kombu import Queue,Exchange
from json import dumps
from socket import gethostname
from utils.ReadConfig import ReadConfig as rc
from utils.extensions.utilities_extention import UtilitiesExtension
#from utils.redis.hc_get_name_urls import get_urls_with_cluster
cluster_name='cluster_1'
url_list = [
    "https://www.google.com",
    "https://www.timesofindia.com",
    "https://www.yahoo.com",
    "https://www.cnn.com",
    "https://www.foxnews.com",
    "https://www.thehindu.com",
    "https://www.vzw.com",
    "https://www.verizon.com",
    "https://www.att.com",
    "https://www.greatandhra.com",
    "https://www.youtube.com",
    "https://www.nfl.com",
    "https://www.nba.com",
    "https://www.pgatour.com",
    "https://www.deccanchronicle.com/",
    "https://www.lll.com"
    # Add more unique host URLs here
]

read_config = rc()
key = read_config.encryption_config['key']
encode_util = UtilitiesExtension(key)
health_check_queue_name = encode_util.encode_hostname_with_key('health_check')
secure_exchange = Exchange('secure_exchange', type='direct')
celery_app.conf.update(
    beat_schedule={
        'run-health-check-every-5-seconds': {
            'task': 'utils.celery.tasks.health_check_tasks.health_check_task',
            'schedule': 5.0,
            'args': ['cluster_1'],
            'options': {
                'queue': health_check_queue_name,
                'exchange': secure_exchange,
                'routing_key': health_check_queue_name,
                'delivery_mode': 2,
                # ensure we don't accumulate a huge backlog of these if the workers are down
                'expires': 5
            }

        },

    'refresh-health-check-args-every-30-seconds': {
        'task': 'utils.celery.tasks.refresh_health_check_arguments',
        'schedule': 10.0,  # Refresh args every 30 seconds
    },
    },
)