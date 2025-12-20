"""
Unit tests for RedisInterface utility.
"""
import pytest
import json
from unittest.mock import MagicMock, patch
from utils.redis.redis_interface import RedisInterface


@pytest.mark.unit
class TestRedisInterface:
    """Test cases for RedisInterface class."""
    
    def test_save_user_pass(self, mock_redis_interface):
        """Test saving user password."""
        mock_redis_interface.save_user_pass("testuser", "hashed_password")
        mock_redis_interface.redis_client.hset.assert_called_once_with(
            "authentication", "testuser", "hashed_password"
        )
    
    def test_get_user_pass_exists(self, mock_redis_interface):
        """Test getting user password when it exists."""
        mock_redis_interface.redis_client.hget.return_value = "hashed_password"
        result = mock_redis_interface.get_user_pass("testuser")
        assert result == "hashed_password"
        mock_redis_interface.redis_client.hget.assert_called_once_with("authentication", "testuser")
    
    def test_get_user_pass_not_exists(self, mock_redis_interface):
        """Test getting user password when it doesn't exist."""
        mock_redis_interface.redis_client.hget.return_value = None
        result = mock_redis_interface.get_user_pass("testuser")
        assert result is None
    
    def test_save_node(self, mock_redis_interface):
        """Test saving node data."""
        node_data = {
            "hostname": "test-host",
            "ip": "192.168.1.1",
            "InstanceId": "i-123456"
        }
        mock_redis_interface.save_node("test-host", node_data)
        mock_redis_interface.redis_client.hset.assert_called_once_with(
            "nodes", "test-host", json.dumps(node_data)
        )
    
    def test_get_nodes(self, mock_redis_interface):
        """Test getting all nodes."""
        nodes_data = {
            "host1": json.dumps({"hostname": "host1", "ip": "192.168.1.1"}),
            "host2": json.dumps({"hostname": "host2", "ip": "192.168.1.2"})
        }
        mock_redis_interface.redis_client.hgetall.return_value = nodes_data
        result = mock_redis_interface.get_nodes()
        assert len(result) == 2
        assert "host1" in result
        assert "host2" in result
    
    def test_get_nodes_empty(self, mock_redis_interface):
        """Test getting nodes when none exist."""
        mock_redis_interface.redis_client.hgetall.return_value = {}
        result = mock_redis_interface.get_nodes()
        assert result == {}
    
    def test_get_instance_ids(self, mock_redis_interface):
        """Test getting instance IDs."""
        nodes_data = {
            "host1": json.dumps({"InstanceId": "i-123456"}),
            "host2": json.dumps({"InstanceId": "i-789012"}),
            "host3": json.dumps({"hostname": "host3"})  # No InstanceId
        }
        mock_redis_interface.redis_client.hgetall.return_value = nodes_data
        result = mock_redis_interface.get_instance_ids()
        assert "i-123456" in result
        assert "i-789012" in result
        assert len(result) == 2
    
    def test_get_instance_ids_empty(self, mock_redis_interface):
        """Test getting instance IDs when none exist."""
        mock_redis_interface.redis_client.hgetall.return_value = {}
        result = mock_redis_interface.get_instance_ids()
        assert result is None
    
    def test_get_instance_ids_namespace(self, mock_redis_interface):
        """Test getting instance IDs by namespace."""
        nodes_data = {
            "host1": json.dumps({"InstanceId": "i-123456", "NameSpace": "ns1"}),
            "host2": json.dumps({"InstanceId": "i-789012", "NameSpace": "ns1"}),
            "host3": json.dumps({"InstanceId": "i-345678", "NameSpace": "ns2"})
        }
        mock_redis_interface.redis_client.hgetall.return_value = nodes_data
        result = mock_redis_interface.get_instance_ids_namespace("ns1")
        assert "i-123456" in result
        assert "i-789012" in result
        assert "i-345678" not in result
    
    def test_get_instance_ids_namespace_not_found(self, mock_redis_interface):
        """Test getting instance IDs for non-existent namespace."""
        nodes_data = {
            "host1": json.dumps({"InstanceId": "i-123456", "NameSpace": "ns1"})
        }
        mock_redis_interface.redis_client.hgetall.return_value = nodes_data
        result = mock_redis_interface.get_instance_ids_namespace("ns2")
        assert result is None
    
    def test_delete_instance_ids(self, mock_redis_interface):
        """Test deleting instance IDs."""
        nodes_data = {
            "host1": json.dumps({"InstanceId": "i-123456"}),
            "host2": json.dumps({"InstanceId": "i-789012"})
        }
        mock_redis_interface.redis_client.hgetall.return_value = nodes_data
        result = mock_redis_interface.delete_instance_ids(["i-123456"])
        assert result is True
        mock_redis_interface.redis_client.hdel.assert_called()
    
    def test_get_node_by_name(self, mock_redis_interface):
        """Test getting node by name."""
        node_data = {"hostname": "test-host", "ip": "192.168.1.1"}
        mock_redis_interface.redis_client.hget.return_value = json.dumps(node_data)
        result = mock_redis_interface.get_node_by_name("test-host")
        assert result == node_data
    
    def test_get_node_by_name_not_found(self, mock_redis_interface):
        """Test getting node by name when it doesn't exist."""
        mock_redis_interface.redis_client.hget.return_value = None
        result = mock_redis_interface.get_node_by_name("nonexistent")
        assert result is None

