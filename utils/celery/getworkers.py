from utils.celery.celery_config import celery_app
from kombu import Queue,Exchange
from socket import gethostname
from utils.ReadConfig import ReadConfig as rc
import re
from utils.celery.worker_discovery import discover_workers

workers = discover_workers(celery_app)

for name, info in workers.items():
    print(f"Worker: {name}")
    print(f"  Host          : {info.host}")
    print(f"  Online        : {info.online}")
    print(f"  PID           : {info.pid}")
    print(f"  Concurrency   : {info.concurrency}")
    print(f"  Platform      : {info.platform}")
    print(f"  Broker        : {info.broker}")
    print(f"  Queues        : {info.queues}")
    print(f"  Active tasks  : {info.active_tasks}")
    print(f"  Reserved tasks: {info.reserved_tasks}")
    print(f"  Registered    : {info.registered_tasks}")
    print()
