from celery import Celery
import ssl
from utils.ReadConfig import ReadConfig as rc
class CeleryAppConfig:
    def __init__(self, name='utils.celery.tasks', broker_url='redis://localhost:6379/0',
                 backend_url='redis://localhost:6379/0') -> None:
        """
        Initializes the Celery application with broker and backend configurations.
        """
        read_config=rc()
        celery_config=read_config.celery_config
        # Use redis_queue_config for Celery broker (it's for Celery queues and has SSL settings)
        redis_config = read_config.redis_queue_config
        self.app = Celery(name, broker=celery_config['broker_url'], backend=celery_config['backend_url'])
        self.redis_config = redis_config
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
        
        # Configure broker transport options with SSL support
        broker_transport_options = {
            "socket_connect_timeout": 3,
            "socket_timeout": 3,
            "retry_on_timeout": False,
        }
        
        # Add SSL configuration if Redis is using SSL
        # Note: For Redis with SSL, we need to configure SSL in broker_transport_options
        # This is especially important for Flower which uses the same broker connection
        if self.redis_config.get('ssl_ca_certs') or self.redis_config.get('ssl_certfile'):
            # For self-signed certificates, disable verification to avoid certificate errors
            # This allows Flower and other tools to connect without certificate verification errors
            # In production, you should use proper certificates and set ssl_cert_reqs to CERT_REQUIRED
            broker_transport_options['ssl_cert_reqs'] = ssl.CERT_NONE
            if self.redis_config.get('ssl_ca_certs'):
                broker_transport_options['ssl_ca_certs'] = self.redis_config['ssl_ca_certs']
            if self.redis_config.get('ssl_certfile'):
                broker_transport_options['ssl_certfile'] = self.redis_config['ssl_certfile']
            if self.redis_config.get('ssl_keyfile'):
                broker_transport_options['ssl_keyfile'] = self.redis_config['ssl_keyfile']
        
        self.app.conf.broker_transport_options = broker_transport_options
        
        # Also configure result backend SSL if using Redis backend with SSL
        # The backend URL might be different from broker URL, but we use the same SSL config
        if self.redis_config.get('ssl_ca_certs') or self.redis_config.get('ssl_certfile'):
            result_backend_transport_options = {
                "socket_connect_timeout": 3,
                "socket_timeout": 3,
                "retry_on_timeout": False,
                "ssl_cert_reqs": ssl.CERT_NONE,
            }
            if self.redis_config.get('ssl_ca_certs'):
                result_backend_transport_options['ssl_ca_certs'] = self.redis_config['ssl_ca_certs']
            if self.redis_config.get('ssl_certfile'):
                result_backend_transport_options['ssl_certfile'] = self.redis_config['ssl_certfile']
            if self.redis_config.get('ssl_keyfile'):
                result_backend_transport_options['ssl_keyfile'] = self.redis_config['ssl_keyfile']
            self.app.conf.result_backend_transport_options = result_backend_transport_options
        self.app.conf.redis_socket_connect_timeout = 3
        self.app.conf.redis_socket_timeout = 3
        self.app.conf.broker_connection_timeout = 3

# Initialize Celery app
celery_app = CeleryAppConfig().app


