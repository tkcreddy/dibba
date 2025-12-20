"""
Tests for worker node Celery tasks.
"""
import pytest
from unittest.mock import patch, MagicMock
from utils.celery.tasks.worker_node_tasks import (
    get_worker_node_info,
    get_host_ip,
    get_usage
)


@pytest.mark.celery
class TestWorkerNodeTasks:
    """Test cases for worker node tasks."""
    
    @patch('utils.celery.tasks.worker_node_tasks.get_system_info')
    def test_get_worker_node_info_success(self, mock_get_system_info):
        """Test successful worker node info retrieval."""
        mock_get_system_info.return_value = {
            "hostname": "test-host",
            "os": "Linux",
            "kernel": "5.4.0"
        }
        result = get_worker_node_info()
        assert result == {
            "hostname": "test-host",
            "os": "Linux",
            "kernel": "5.4.0"
        }
        mock_get_system_info.assert_called_once()
    
    @patch('utils.celery.tasks.worker_node_tasks.get_system_info')
    def test_get_worker_node_info_error(self, mock_get_system_info):
        """Test worker node info retrieval with error."""
        mock_get_system_info.side_effect = Exception("System error")
        result = get_worker_node_info()
        assert result == ""  # Returns empty string on error
    
    @patch('utils.celery.tasks.worker_node_tasks.host_ip')
    def test_get_host_ip_success(self, mock_host_ip):
        """Test successful host IP retrieval."""
        mock_host_ip.return_value = "192.168.1.1"
        result = get_host_ip()
        assert result == "192.168.1.1"
        mock_host_ip.assert_called_once()
    
    @patch('utils.celery.tasks.worker_node_tasks.host_ip')
    def test_get_host_ip_error(self, mock_host_ip):
        """Test host IP retrieval with error."""
        mock_host_ip.side_effect = Exception("Network error")
        result = get_host_ip()
        assert result == ""  # Returns empty string on error
    
    @patch('utils.celery.tasks.worker_node_tasks.get_system_usage')
    def test_get_usage_success(self, mock_get_system_usage):
        """Test successful system usage retrieval."""
        mock_get_system_usage.return_value = {
            "cpu": 45.5,
            "memory": 60.2,
            "disk": 75.0
        }
        result = get_usage()
        assert result == {
            "cpu": 45.5,
            "memory": 60.2,
            "disk": 75.0
        }
        mock_get_system_usage.assert_called_once()
    
    @patch('utils.celery.tasks.worker_node_tasks.get_system_usage')
    def test_get_usage_error(self, mock_get_system_usage):
        """Test system usage retrieval with error."""
        mock_get_system_usage.side_effect = Exception("Usage error")
        result = get_usage()
        assert result == ""  # Returns empty string on error

