import json
from typing import Optional, Dict, Any
from utils.singleton import Singleton
import os
from logpkg.log_kcld import LogKCld

logger = LogKCld()

class _ReadConfig:

    def __init__(self, base_dir: Optional[str] = None) -> None:
        if base_dir is not None:
            logger.debug(f"Loading config from {base_dir}")
            self.base_dir = os.path.join(base_dir, 'config')
            logger.debug(f'base_dir: {self.base_dir}')
        else:
            PATH = os.getcwd()
            logger.debug("Loading config from default path")
            self.base_dir = os.path.join(PATH, 'config')
            #self.base_dir = 'config/'
            logger.debug(f'base_dir: {self.base_dir}')


        try:
            self.file_path = os.path.join(self.base_dir, 'config.json')
            self._config_data = None
            self.load_config()
            logger.info(f"Initializing ReadConfig once: {self.file_path}")
        except Exception as e:
            logger.error(f"Failed to load config: {e}", exc_info=True)

    @property
    def set_config_dir(self) -> str:
        return self.base_dir

    def load_config(self) -> None:
        try:
            with open(self.file_path, 'r') as file:
                self._config_data = json.load(file)
        except Exception as e:
            logger.error(f"File open error: {e}", exc_info=True)
            raise

    @property
    def logging_config(self) -> Dict[str, Any]:
        """Get logging configuration."""
        return self._config_data['logging']

    @property
    def kafka_config(self) -> Dict[str, Any]:
        """Get Kafka configuration."""
        return self._config_data['kafka']

    @property
    def kafka_ssl(self) -> Dict[str, Any]:
        """Get Kafka SSL configuration."""
        return self.kafka_config['ssl_config']

    @property
    def encryption_config(self) -> Dict[str, Any]:
        """Get encryption configuration."""
        return self._config_data['encryption']
    
    @property
    def aws_config(self) -> Dict[str, Any]:
        """Get AWS configuration."""
        return self._config_data['aws']
    
    @property
    def celery_config(self) -> Dict[str, Any]:
        """Get Celery configuration."""
        return self._config_data['celery']
    
    @property
    def redis_db_config(self) -> Dict[str, Any]:
        """Get Redis database configuration."""
        return self._config_data['redis_db']
    
    @property
    def redis_queue_config(self) -> Dict[str, Any]:
        """Get Redis queue configuration."""
        return self._config_data['redis_queue']

class ReadConfig(_ReadConfig, metaclass=Singleton):
    pass

