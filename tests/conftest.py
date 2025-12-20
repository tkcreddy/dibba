"""
Pytest configuration and shared fixtures.
"""
import pytest
import json
import os
import tempfile
from unittest.mock import Mock, MagicMock, patch
from typing import Generator
import redis
from fastapi.testclient import TestClient
from celery import Celery

# Import application components
from server.main_api import app
from utils.ReadConfig import ReadConfig
from utils.redis.redis_interface import RedisInterface
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.celery.celery_config import celery_app


@pytest.fixture(scope="session")
def test_config_dir(tmp_path_factory):
    """Create a temporary config directory with test config.json."""
    config_dir = tmp_path_factory.mktemp("config")
    config_file = config_dir / "config.json"
    
    test_config = {
        "logging": {
            "level": "DEBUG",
            "file": "test.log"
        },
        "kafka": {
            "bootstrap_servers": "localhost:9092",
            "ssl_config": {}
        },
        "encryption": {
            "key": "test-secret-key-for-encryption-12345"
        },
        "aws": {
            "aws_access_key_id": "test-key",
            "aws_secret_access_key": "test-secret",
            "region": "us-east-1"
        },
        "celery": {
            "broker_url": "redis://localhost:6379/0",
            "backend_url": "redis://localhost:6379/0"
        },
        "redis_db": {
            "redis_host": "localhost",
            "redis_port": 6379,
            "redis_db": 0,
            "ssl_ca_certs": None,
            "ssl_certfile": None,
            "ssl_keyfile": None
        },
        "redis_queue": {
            "host": "localhost",
            "port": 6379
        }
    }
    
    with open(config_file, 'w') as f:
        json.dump(test_config, f)
    
    return str(config_dir)


@pytest.fixture(scope="function")
def mock_redis_client():
    """Mock Redis client for testing."""
    mock_client = MagicMock(spec=redis.Redis)
    
    # Setup common mock behaviors
    mock_client.hget.return_value = None
    mock_client.hset.return_value = True
    mock_client.hgetall.return_value = {}
    mock_client.ping.return_value = True
    
    return mock_client


@pytest.fixture(scope="function")
def mock_redis_interface(mock_redis_client):
    """Mock RedisInterface with mocked Redis client."""
    with patch('utils.redis.redis_interface.redis.Redis', return_value=mock_redis_client):
        # Create instance with test config
        interface = RedisInterface('localhost', 6379, 0)
        interface.redis_client = mock_redis_client
        yield interface


@pytest.fixture(scope="function")
def utilities_extension():
    """Create UtilitiesExtension instance with test key."""
    key = "test-secret-key-for-encryption-12345"
    return UtilitiesExtension(key)


@pytest.fixture(scope="function")
def mock_celery_app():
    """Mock Celery app for testing."""
    mock_app = MagicMock(spec=Celery)
    mock_app.conf = MagicMock()
    return mock_app


@pytest.fixture(scope="function")
def mock_celery_task():
    """Mock Celery task result."""
    mock_result = MagicMock()
    mock_result.id = "test-task-id-123"
    mock_result.status = "PENDING"
    mock_result.ready.return_value = False
    mock_result.result = None
    return mock_result


@pytest.fixture(scope="function")
def api_client():
    """Create test client for FastAPI app."""
    return TestClient(app)


@pytest.fixture(scope="function")
def mock_jwt_token():
    """Generate a mock JWT token for testing."""
    import jwt
    from datetime import datetime, timedelta, UTC
    
    secret = "test-secret-key-for-encryption-12345"
    payload = {
        "sub": "testuser",
        "exp": datetime.now(UTC) + timedelta(minutes=30)
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture(scope="function")
def authenticated_client(api_client, mock_jwt_token):
    """Create authenticated test client."""
    api_client.headers = {"Authorization": f"Bearer {mock_jwt_token}"}
    return api_client


@pytest.fixture(scope="function")
def mock_read_config(test_config_dir):
    """Mock ReadConfig to use test config."""
    with patch('utils.ReadConfig.ReadConfig') as mock_config:
        # Reset singleton instance
        if hasattr(ReadConfig, '_instances'):
            ReadConfig._instances.clear()
        
        # Create new instance with test config
        config_instance = ReadConfig(test_config_dir)
        yield config_instance


@pytest.fixture(scope="function")
def mock_containerd_client():
    """Mock ContainerdClient for testing."""
    mock_client = MagicMock()
    mock_client.pull_image.return_value = {"ok": True, "image_ref": "test-image:latest"}
    mock_client.create_container.return_value = {"ok": True, "cid": "test-container-id"}
    return mock_client


@pytest.fixture(scope="function")
def mock_pod_manager(mock_containerd_client):
    """Mock PodManager for testing."""
    mock_manager = MagicMock()
    mock_manager.create_pod.return_value = {
        "ok": True,
        "pod": {
            "name": "test-pod",
            "namespace": "test-namespace",
            "pause_cid": "pause-container-id"
        }
    }
    return mock_manager


@pytest.fixture(scope="function")
def sample_container_spec():
    """Sample container specification for testing."""
    return {
        "image": "nginx:latest",
        "name": "test-container",
        "resources": {
            "cpu": "100m",
            "memory": "128Mi"
        },
        "env": {"ENV_VAR": "value"},
        "args": ["nginx", "-g", "daemon off;"]
    }


@pytest.fixture(scope="function")
def sample_pod_request(sample_container_spec):
    """Sample pod creation request."""
    return {
        "host_name": "test-host",
        "namespace": "test-namespace",
        "containers": [sample_container_spec]
    }


@pytest.fixture(scope="function", autouse=True)
def reset_singletons():
    """Reset singleton instances before each test."""
    # Clear singleton instances
    if hasattr(ReadConfig, '_instances'):
        ReadConfig._instances.clear()
    if hasattr(UtilitiesExtension, '_instances'):
        UtilitiesExtension._instances.clear()
    yield
    # Cleanup after test
    if hasattr(ReadConfig, '_instances'):
        ReadConfig._instances.clear()
    if hasattr(UtilitiesExtension, '_instances'):
        UtilitiesExtension._instances.clear()


@pytest.fixture(scope="function")
def mock_aws_ec2():
    """Mock AWS EC2 client."""
    mock_ec2 = MagicMock()
    mock_ec2.run_instances.return_value = {
        'Instances': [
            {
                'InstanceId': 'i-1234567890abcdef0',
                'PrivateDnsName': 'ip-10-0-0-1.ec2.internal',
                'PrivateIpAddress': '10.0.0.1',
                'InstanceType': 't2.micro',
                'State': {'Name': 'running'}
            }
        ]
    }
    mock_ec2.terminate_instances.return_value = {
        'TerminatingInstances': [
            {'InstanceId': 'i-1234567890abcdef0', 'CurrentState': {'Name': 'terminating'}}
        ]
    }
    return mock_ec2


@pytest.fixture(scope="function")
def mock_os_interface():
    """Mock OS interface functions."""
    return {
        'get_system_info': MagicMock(return_value={'hostname': 'test-host', 'os': 'Linux'}),
        'host_ip': MagicMock(return_value='192.168.1.1'),
        'get_system_usage': MagicMock(return_value={'cpu': 50.0, 'memory': 60.0})
    }

