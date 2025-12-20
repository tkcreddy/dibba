"""
Tests for containerd Celery tasks.
"""
import pytest
import json
from unittest.mock import patch, MagicMock
from utils.celery.tasks.containerd_tasks import (
    _rehydrate_containers,
    _extract_ipv4_from_cni_result,
    _ipv4_from_netns,
    _safe_json
)


@pytest.mark.celery
class TestContainerdTaskHelpers:
    """Test helper functions for containerd tasks."""
    
    def test_rehydrate_containers_valid(self):
        """Test rehydrating valid container specs."""
        containers_json = [
            {
                "image": "nginx:latest",
                "name": "test-container",
                "resources": {"cpu": "100m", "memory": "128Mi"}
            }
        ]
        result = _rehydrate_containers(containers_json)
        assert len(result) == 1
        assert result[0].image == "nginx:latest"
        assert result[0].name == "test-container"
    
    def test_rehydrate_containers_invalid_type(self):
        """Test rehydrating with invalid type."""
        with pytest.raises(TypeError):
            _rehydrate_containers("not-a-list")
    
    def test_rehydrate_containers_invalid_item(self):
        """Test rehydrating with invalid item type."""
        with pytest.raises(TypeError):
            _rehydrate_containers(["not-a-dict"])
    
    def test_extract_ipv4_from_cni_result_with_ips(self):
        """Test extracting IPv4 from CNI result with ips field."""
        cni_result = {
            "ips": [
                {"address": "10.0.0.1/24", "version": "4"}
            ]
        }
        result = _extract_ipv4_from_cni_result(cni_result)
        assert result == "10.0.0.1"
    
    def test_extract_ipv4_from_cni_result_with_interfaces(self):
        """Test extracting IPv4 from CNI result with interfaces field."""
        cni_result = {
            "interfaces": [
                {
                    "name": "eth0",
                    "addresses": ["10.0.0.2/24"]
                }
            ]
        }
        result = _extract_ipv4_from_cni_result(cni_result, "eth0")
        assert result == "10.0.0.2"
    
    def test_extract_ipv4_from_cni_result_none(self):
        """Test extracting IPv4 from invalid CNI result."""
        result = _extract_ipv4_from_cni_result(None)
        assert result is None
    
    def test_extract_ipv4_from_cni_result_no_match(self):
        """Test extracting IPv4 when no match found."""
        cni_result = {
            "ips": [
                {"address": "2001:db8::1/64", "version": "6"}
            ]
        }
        result = _extract_ipv4_from_cni_result(cni_result)
        assert result is None
    
    @patch('utils.celery.tasks.containerd_tasks.subprocess.check_output')
    def test_ipv4_from_netns_success(self, mock_subprocess):
        """Test extracting IPv4 from network namespace."""
        mock_subprocess.return_value = json.dumps([
            {
                "addr_info": [
                    {"family": "inet", "local": "10.0.0.3"}
                ]
            }
        ])
        result = _ipv4_from_netns(12345, "eth0")
        assert result == "10.0.0.3"
    
    @patch('utils.celery.tasks.containerd_tasks.subprocess.check_output')
    def test_ipv4_from_netns_error(self, mock_subprocess):
        """Test extracting IPv4 from network namespace with error."""
        mock_subprocess.side_effect = Exception("Command failed")
        result = _ipv4_from_netns(12345, "eth0")
        assert result is None
    
    def test_safe_json_string(self):
        """Test safe_json with string."""
        result = _safe_json("test-string")
        assert result == "test-string"
    
    def test_safe_json_int(self):
        """Test safe_json with integer."""
        result = _safe_json(42)
        assert result == 42
    
    def test_safe_json_none(self):
        """Test safe_json with None."""
        result = _safe_json(None)
        assert result is None
    
    def test_safe_json_bytes(self):
        """Test safe_json with bytes."""
        result = _safe_json(b"test-bytes")
        assert result == "test-bytes"
    
    def test_safe_json_dict(self):
        """Test safe_json with dictionary."""
        data = {"key": "value", "number": 42}
        result = _safe_json(data)
        assert result == {"key": "value", "number": 42}
    
    def test_safe_json_nested_dict(self):
        """Test safe_json with nested dictionary."""
        data = {"outer": {"inner": "value"}}
        result = _safe_json(data)
        assert result == {"outer": {"inner": "value"}}

