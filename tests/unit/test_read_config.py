"""
Unit tests for ReadConfig utility.
"""
import pytest
import json
import os
import tempfile
from unittest.mock import patch, mock_open
from utils.ReadConfig import ReadConfig


@pytest.mark.unit
class TestReadConfig:
    """Test cases for ReadConfig class."""
    
    def test_init_with_base_dir(self, test_config_dir):
        """Test ReadConfig initialization with base directory."""
        config = ReadConfig(test_config_dir)
        assert config.base_dir == os.path.join(test_config_dir, 'config')
        assert config._config_data is not None
    
    def test_init_without_base_dir(self, test_config_dir):
        """Test ReadConfig initialization without base directory."""
        with patch('utils.ReadConfig.os.getcwd', return_value=test_config_dir):
            config = ReadConfig()
            assert config.base_dir == os.path.join(test_config_dir, 'config')
    
    def test_set_config_dir_property(self, test_config_dir):
        """Test set_config_dir property."""
        config = ReadConfig(test_config_dir)
        assert config.set_config_dir == os.path.join(test_config_dir, 'config')
    
    def test_load_config_success(self, test_config_dir):
        """Test successful config loading."""
        config = ReadConfig(test_config_dir)
        assert config._config_data is not None
        assert 'encryption' in config._config_data
        assert 'aws' in config._config_data
    
    def test_load_config_file_not_found(self):
        """Test config loading when file doesn't exist."""
        with patch('utils.ReadConfig.os.path.join', return_value='/nonexistent/config.json'):
            with pytest.raises(Exception):
                ReadConfig('/nonexistent')
    
    def test_logging_config_property(self, test_config_dir):
        """Test logging_config property."""
        config = ReadConfig(test_config_dir)
        logging_config = config.logging_config
        assert isinstance(logging_config, dict)
        assert 'level' in logging_config or 'file' in logging_config
    
    def test_kafka_config_property(self, test_config_dir):
        """Test kafka_config property."""
        config = ReadConfig(test_config_dir)
        kafka_config = config.kafka_config
        assert isinstance(kafka_config, dict)
    
    def test_kafka_ssl_property(self, test_config_dir):
        """Test kafka_ssl property."""
        config = ReadConfig(test_config_dir)
        ssl_config = config.kafka_ssl
        assert isinstance(ssl_config, dict)
    
    def test_encryption_config_property(self, test_config_dir):
        """Test encryption_config property."""
        config = ReadConfig(test_config_dir)
        encryption_config = config.encryption_config
        assert isinstance(encryption_config, dict)
        assert 'key' in encryption_config
    
    def test_aws_config_property(self, test_config_dir):
        """Test aws_config property."""
        config = ReadConfig(test_config_dir)
        aws_config = config.aws_config
        assert isinstance(aws_config, dict)
        assert 'region' in aws_config
    
    def test_celery_config_property(self, test_config_dir):
        """Test celery_config property."""
        config = ReadConfig(test_config_dir)
        celery_config = config.celery_config
        assert isinstance(celery_config, dict)
        assert 'broker_url' in celery_config
    
    def test_redis_db_config_property(self, test_config_dir):
        """Test redis_db_config property."""
        config = ReadConfig(test_config_dir)
        redis_config = config.redis_db_config
        assert isinstance(redis_config, dict)
        assert 'redis_host' in redis_config
    
    def test_redis_queue_config_property(self, test_config_dir):
        """Test redis_queue_config property."""
        config = ReadConfig(test_config_dir)
        queue_config = config.redis_queue_config
        assert isinstance(queue_config, dict)
    
    def test_singleton_pattern(self, test_config_dir):
        """Test that ReadConfig follows singleton pattern."""
        config1 = ReadConfig(test_config_dir)
        config2 = ReadConfig(test_config_dir)
        assert config1 is config2

