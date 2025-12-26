from celery import Celery
from utils.ReadConfig import ReadConfig as rc
class CeleryAppConfig:
    def __init__(self, name='utils.celery.tasks', broker_url='redis://localhost:6379/0',
                 backend_url='redis://localhost:6379/0') -> None:
        """
        Initializes the Celery application with broker and backend configurations.
        """
        read_config=rc()
        celery_config=read_config.celery_config
        self.app = Celery(name, broker=celery_config['broker_url'], backend=celery_config['backend_url'])
        self.configure()

    def configure(self):
        """
        Configures the Celery application with dynamic routing and task queues.
        """

        self.app.conf.update(
            task_serializer='json',
            accept_content=['json'],
            result_serializer='json',
            timezone='UTC',
            enable_utc=True)
        self.app.conf.broker_transport_options = {
            "socket_connect_timeout": 3,
            "socket_timeout": 3,
            "retry_on_timeout": False,
        }
        self.app.conf.redis_socket_connect_timeout = 3
        self.app.conf.redis_socket_timeout = 3
        self.app.conf.broker_connection_timeout = 3

# Initialize Celery app
celery_app = CeleryAppConfig().app


