from utils.celery.celery_config import celery_app
from kombu import Queue,Exchange
from socket import gethostname
from utils.ReadConfig import ReadConfig as rc
import re
from utils.celery.worker_discovery import discover_workers
from logpkg.log_kcld import LogKCld

logger = LogKCld()

workers = discover_workers(celery_app)

for name, info in workers.items():
    logger.info(f"Worker: {name}")
    logger.info(f"  Host          : {info.host}")
    logger.info(f"  Online        : {info.online}")
    logger.info(f"  PID           : {info.pid}")
    logger.info(f"  Concurrency   : {info.concurrency}")
    logger.info(f"  Platform      : {info.platform}")
    logger.info(f"  Broker        : {info.broker}")
    logger.info(f"  Queues        : {info.queues}")
    logger.info(f"  Active tasks  : {info.active_tasks}")
    logger.info(f"  Reserved tasks: {info.reserved_tasks}")
    logger.info(f"  Registered    : {info.registered_tasks}")
    logger.info("")
