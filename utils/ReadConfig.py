import json
from utils.singleton import Singleton
import os


# from logpkg.log_decorator import setup_logger,log_to_file
# logger = setup_logger("my_app", log_file="app.log")
class _ReadConfig:

    def __init__(self, base_dir=None) -> None:
        if base_dir is not None:
            print(f"Loading config from {base_dir}")
            self.base_dir = os.path.join(base_dir, 'config')
            print(f'base_dir: {self.base_dir}')
        else:
            self.base_dir = 'config/'

        try:
            self.file_path = os.path.join(self.base_dir, 'config.json')
            self._config_data = None
            self.load_config()
            print(f"Initializing ReadConfig once {self.file_path}")
        except Exception as e:
            print(f"Fail to load config: {e}")

    @property
    def set_config_dir(self) -> str:
        return self.base_dir

    def load_config(self) -> None:
        try:
            with open(self.file_path, 'r') as file:
                self._config_data = json.load(file)
        except Exception as e:
            print(f"file open error {e}")

    @property
    def logging_config(self):
        return self._config_data['logging']

    @property
    def kafka_config(self):
        return self._config_data['kafka']

    @property
    def kafka_ssl(self):
        return self.kafka_config['ssl_config']

    @property
    def encryption_config(self):
        return self._config_data['encryption']
    @property
    def aws_config(self):
        return self._config_data['aws']
    @property
    def celery_config(self):
        return self._config_data['celery']
    @property
    def redis_db_config(self):
        return self._config_data['redis_db']
    @property
    def redis_queue_config(self):
        return self._config_data['redis_queue']

class ReadConfig(_ReadConfig, metaclass=Singleton):
    pass
