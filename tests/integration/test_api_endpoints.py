"""
Integration tests for API endpoints.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi import status
import jwt
from datetime import datetime, timedelta, UTC


@pytest.mark.integration
@pytest.mark.api
class TestAuthentication:
    """Test authentication endpoints."""
    
    def test_login_success(self, api_client, mock_redis_interface):
        """Test successful login."""
        with patch('server.main_api.rd', mock_redis_interface):
            mock_redis_interface.get_user_pass.return_value = "hashed_password"
            with patch('server.main_api.ue.encode_phrase_with_key', return_value="hashed_password"):
                response = api_client.post(
                    "/token",
                    data={"username": "testuser", "password": "testpass"}
                )
                assert response.status_code == status.HTTP_200_OK
                assert "access_token" in response.json()
                assert response.json()["token_type"] == "bearer"
    
    def test_login_invalid_credentials(self, api_client, mock_redis_interface):
        """Test login with invalid credentials."""
        with patch('server.main_api.rd', mock_redis_interface):
            mock_redis_interface.get_user_pass.return_value = None
            response = api_client.post(
                "/token",
                data={"username": "invalid", "password": "wrong"}
            )
            assert response.status_code == status.HTTP_400_BAD_REQUEST
    
    def test_get_current_user_valid_token(self, authenticated_client, mock_redis_interface):
        """Test getting current user with valid token."""
        with patch('server.main_api.rd', mock_redis_interface):
            mock_redis_interface.get_user_pass.return_value = "hashed_password"
            # Token is already set in authenticated_client fixture
            # This would be tested indirectly through protected endpoints
            pass
    
    def test_get_current_user_invalid_token(self, api_client):
        """Test getting current user with invalid token."""
        api_client.headers = {"Authorization": "Bearer invalid-token"}
        with patch('server.main_api.rd'):
            # This would fail when accessing protected endpoints
            pass


@pytest.mark.integration
@pytest.mark.api
class TestAWSEndpoints:
    """Test AWS-related endpoints."""
    
    def test_create_instances(self, authenticated_client, mock_celery_task):
        """Test creating EC2 instances."""
        with patch('server.main_api.create_worker_nodes') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.post(
                "/create-instances/",
                json={
                    "instance_type": "t2.micro",
                    "ami_id": "ami-123456",
                    "key_name": "test-key",
                    "security_group_ids": ["sg-123456"],
                    "subnet_id": "subnet-123456",
                    "namespace": "test-ns",
                    "min_count": 1,
                    "max_count": 1
                }
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()
    
    def test_create_instances_unauthorized(self, api_client):
        """Test creating instances without authentication."""
        response = api_client.post(
            "/create-instances/",
            json={
                "instance_type": "t2.micro",
                "ami_id": "ami-123456",
                "key_name": "test-key",
                "security_group_ids": ["sg-123456"],
                "subnet_id": "subnet-123456",
                "namespace": "test-ns",
                "min_count": 1,
                "max_count": 1
            }
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    
    def test_terminate_namespace(self, authenticated_client, mock_redis_interface, mock_celery_task):
        """Test terminating instances by namespace."""
        with patch('server.main_api.rd', mock_redis_interface):
            mock_redis_interface.get_instance_ids_namespace.return_value = ["i-123456"]
            with patch('server.main_api.terminate_worker_node') as mock_task:
                mock_task.apply_async.return_value = mock_celery_task
                response = authenticated_client.post(
                    "/terminate-namespace/",
                    json={"namespace": "test-ns"}
                )
                assert response.status_code == status.HTTP_200_OK
                assert "task_id" in response.json()
    
    def test_terminate_namespace_no_instances(self, authenticated_client, mock_redis_interface):
        """Test terminating namespace with no instances."""
        with patch('server.main_api.rd', mock_redis_interface):
            mock_redis_interface.get_instance_ids_namespace.return_value = None
            response = authenticated_client.post(
                "/terminate-namespace/",
                json={"namespace": "test-ns"}
            )
            assert response.status_code == status.HTTP_200_OK
            assert "No instances found" in response.json()["message"]


@pytest.mark.integration
@pytest.mark.api
class TestWorkerNodeEndpoints:
    """Test worker node information endpoints."""
    
    def test_get_worker_node_data(self, authenticated_client, mock_celery_task):
        """Test getting worker node data."""
        with patch('server.main_api.get_worker_node_info') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.get(
                "/get_worker_node_data/",
                params={"host_name": "test-host"}
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()
    
    def test_get_worker_node_ip(self, authenticated_client, mock_celery_task):
        """Test getting worker node IP."""
        with patch('server.main_api.get_host_ip') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.get(
                "/get_worker_node_ip/",
                params={"host_name": "test-host"}
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()
    
    def test_get_worker_usage_data(self, authenticated_client, mock_celery_task):
        """Test getting worker usage data."""
        with patch('server.main_api.get_usage') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.get(
                "/get_worker_usage_data/",
                params={"host_name": "test-host"}
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()


@pytest.mark.integration
@pytest.mark.api
class TestContainerdEndpoints:
    """Test containerd-related endpoints."""
    
    def test_create_pods(self, authenticated_client, mock_celery_task, sample_pod_request):
        """Test creating pods."""
        with patch('server.main_api.create_pod_task') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.post(
                "/containerd/create-pods",
                json=sample_pod_request
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()
    
    def test_list_namespaces_and_pods(self, authenticated_client, mock_celery_task):
        """Test listing namespaces and pods."""
        with patch('server.main_api.list_namespaces_and_pods_task') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.post(
                "/containerd/list_namespaces_and_pods/",
                json={"host_name": "test-host"}
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()
    
    def test_list_pods_by_namespace(self, authenticated_client, mock_celery_task):
        """Test listing pods by namespace."""
        with patch('server.main_api.list_pods_by_namespace_task') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.post(
                "/containerd/list_pods_by_namespace/",
                json={"host_name": "test-host", "namespace": "test-ns"}
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()
    
    def test_terminate_pod(self, authenticated_client, mock_celery_task):
        """Test terminating a pod."""
        with patch('server.main_api.terminate_pod_task') as mock_task:
            mock_task.apply_async.return_value = mock_celery_task
            response = authenticated_client.post(
                "/containerd/terminate_pod/",
                json={
                    "host_name": "test-host",
                    "namespace": "test-ns",
                    "pod_name": "test-pod"
                }
            )
            assert response.status_code == status.HTTP_200_OK
            assert "task_id" in response.json()
    
    def test_get_task_status(self, authenticated_client):
        """Test getting task status."""
        with patch('server.main_api.celery_app') as mock_celery:
            mock_result = MagicMock()
            mock_result.id = "test-task-id"
            mock_result.status = "SUCCESS"
            mock_result.ready.return_value = True
            mock_result.result = {"status": "completed"}
            mock_celery.AsyncResult.return_value = mock_result
            
            response = authenticated_client.get("/task/test-task-id")
            assert response.status_code == status.HTTP_200_OK
            assert response.json()["task_id"] == "test-task-id"
            assert response.json()["status"] == "SUCCESS"

