"""
ETCD interface for querying Calico pod IP addresses.

This module provides methods to:
- Connect to Calico's etcd database
- Query workload endpoints (pods) and their IP addresses
- Map pod names to IP addresses
"""
import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from logpkg.log_kcld import LogKCld, log_to_file
from utils.exceptions import RedisError
from utils.error_handlers import handle_errors

logger = LogKCld()

try:
    import etcd3
    ETCD3_AVAILABLE = True
except ImportError:
    ETCD3_AVAILABLE = False
    logger.warning("etcd3 library not available. Install with: pip install etcd3")


class EtcdInterface:
    """Interface for querying Calico's etcd database."""
    
    # Calico etcd key prefix for workload endpoints (pods)
    CALICO_WORKLOAD_ENDPOINT_PREFIX = "/calico/resources/v3/projectcalico.org/workloadendpoints/"
    
    def __init__(
        self,
        etcd_endpoints: Optional[str] = None,
        timeout: int = 5,
        ca_cert: Optional[str] = None,
        cert_key: Optional[str] = None,
        cert_cert: Optional[str] = None
    ) -> None:
        """Initialize etcd interface.
        
        Args:
            etcd_endpoints: etcd endpoints (e.g., "http://172.31.17.19:2379" or "172.31.17.19:2379")
            timeout: Connection timeout in seconds
            ca_cert: Path to CA certificate (optional, for TLS)
            cert_key: Path to client key (optional, for TLS)
            cert_cert: Path to client certificate (optional, for TLS)
        """
        if not ETCD3_AVAILABLE:
            raise ImportError("etcd3 library is not available. Install with: pip install etcd3")
        
        self.etcd_endpoints = etcd_endpoints or "http://localhost:2379"
        self.timeout = timeout
        self.etcd_client = None
        self._parse_endpoints()
        
        # Initialize etcd client
        try:
            if ca_cert or cert_key or cert_cert:
                # TLS connection
                self.etcd_client = etcd3.client(
                    host=self.host,
                    port=self.port,
                    timeout=self.timeout,
                    ca_cert=ca_cert,
                    cert_key=cert_key,
                    cert_cert=cert_cert
                )
            else:
                # Non-TLS connection
                self.etcd_client = etcd3.client(
                    host=self.host,
                    port=self.port,
                    timeout=self.timeout
                )
            logger.info(f"Initialized etcd interface: {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to initialize etcd client: {e}", exc_info=True)
            raise
    
    def _parse_endpoints(self) -> None:
        """Parse etcd endpoints to extract host and port.
        
        Supports formats:
        - http://172.31.17.19:2379
        - https://172.31.17.19:2379
        - 172.31.17.19:2379
        - 172.31.17.19 (defaults to port 2379)
        """
        endpoint = self.etcd_endpoints.strip()
        
        # Remove http:// or https:// prefix if present
        if endpoint.startswith("http://"):
            endpoint = endpoint[7:]
            self.use_tls = False
        elif endpoint.startswith("https://"):
            endpoint = endpoint[8:]
            self.use_tls = True
        else:
            self.use_tls = False
        
        # Split host and port
        if ":" in endpoint:
            self.host, port_str = endpoint.split(":", 1)
            try:
                self.port = int(port_str)
            except ValueError:
                logger.warning(f"Invalid port in etcd endpoint: {port_str}, using default 2379")
                self.port = 2379
        else:
            self.host = endpoint
            self.port = 2379
    
    @log_to_file(logger)
    @handle_errors("get_all_pod_ips", "ETCD_ERROR")
    def get_all_pod_ips(self) -> Dict[str, Dict[str, any]]:
        """Get all pod IP addresses from Calico etcd.
        
        Returns:
            Dictionary mapping pod identifiers to pod info:
            {
                "pod_name": {
                    "ip_addresses": ["10.244.1.5"],
                    "node": "ip-172-31-19-101",
                    "namespace": "default",
                    "workload_endpoint": "k8s-pod-name-..."
                }
            }
        """
        if not self.etcd_client:
            raise RuntimeError("etcd client not initialized")
        
        pod_ips = {}
        
        try:
            # Query all workload endpoints from Calico
            for value, meta in self.etcd_client.get_prefix(self.CALICO_WORKLOAD_ENDPOINT_PREFIX):
                try:
                    data = json.loads(value)
                    metadata = data.get("metadata", {})
                    spec = data.get("spec", {})
                    
                    # Extract pod information
                    workload_endpoint_name = metadata.get("name", "")
                    node = spec.get("node", "unknown")
                    ip_networks = spec.get("ipNetworks", [])
                    namespace = metadata.get("namespace", "default")
                    
                    # Extract pod name from workload endpoint name
                    # Calico format: k8s-{namespace}-{pod_name}-{container_id}
                    # Or: {namespace}-{pod_name}-{container_id}
                    pod_name = self._extract_pod_name(workload_endpoint_name, namespace)
                    
                    if pod_name and ip_networks:
                        # Use first IP address (primary IP)
                        primary_ip = ip_networks[0] if ip_networks else None
                        
                        if primary_ip:
                            pod_ips[pod_name] = {
                                "ip_addresses": ip_networks,
                                "primary_ip": primary_ip,
                                "node": node,
                                "namespace": namespace,
                                "workload_endpoint": workload_endpoint_name
                            }
                            logger.debug(f"Found pod {pod_name} (namespace: {namespace}) on node {node} with IPs: {ip_networks}")
                
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse workload endpoint data: {e}")
                    continue
        
        except Exception as e:
            logger.error(f"Failed to query etcd for pod IPs: {e}", exc_info=True)
            raise
        
        logger.info(f"Retrieved {len(pod_ips)} pod IP addresses from Calico etcd")
        return pod_ips
    
    def _extract_pod_name(self, workload_endpoint_name: str, namespace: str) -> Optional[str]:
        """Extract pod name from Calico workload endpoint name.
        
        Calico workload endpoint names can be in formats:
        - k8s-{namespace}-{pod_name}-{container_id}
        - {namespace}-{pod_name}-{container_id}
        - {pod_name}-{container_id}
        
        Args:
            workload_endpoint_name: Full workload endpoint name from Calico
            namespace: Namespace name
            
        Returns:
            Pod name if extractable, None otherwise
        """
        if not workload_endpoint_name:
            return None
        
        # Remove k8s- prefix if present
        name = workload_endpoint_name
        if name.startswith("k8s-"):
            name = name[4:]
        
        # Try to extract pod name by removing namespace and container ID
        # Pattern: {namespace}-{pod_name}-{container_id}
        parts = name.split("-")
        
        if len(parts) >= 2:
            # If namespace matches, pod name is likely after it
            if parts[0] == namespace and len(parts) > 2:
                # Skip namespace and container ID (last part), join the rest as pod name
                pod_name = "-".join(parts[1:-1])
                return pod_name
            elif len(parts) > 1:
                # Assume last part is container ID, rest is pod name
                pod_name = "-".join(parts[:-1])
                return pod_name
        
        # If we can't parse, return the original name (might be the pod name itself)
        return name
    
    @log_to_file(logger)
    @handle_errors("get_pod_ip", "ETCD_ERROR")
    def get_pod_ip(self, pod_name: str, namespace: Optional[str] = None) -> Optional[str]:
        """Get IP address for a specific pod.
        
        Args:
            pod_name: Pod name
            namespace: Optional namespace to filter
            
        Returns:
            Primary IP address if found, None otherwise
        """
        all_pods = self.get_all_pod_ips()
        
        # Try exact match first
        if pod_name in all_pods:
            pod_info = all_pods[pod_name]
            if not namespace or pod_info.get("namespace") == namespace:
                return pod_info.get("primary_ip")
        
        # Try partial match (pod name might be part of the key)
        for key, pod_info in all_pods.items():
            if pod_name in key:
                if not namespace or pod_info.get("namespace") == namespace:
                    return pod_info.get("primary_ip")
        
        return None
    
    @log_to_file(logger)
    @handle_errors("get_pods_by_node", "ETCD_ERROR")
    def get_pods_by_node(self, node_hostname: str) -> Dict[str, str]:
        """Get all pod IPs for a specific node.
        
        Args:
            node_hostname: Node hostname
            
        Returns:
            Dictionary mapping pod names to IP addresses
        """
        all_pods = self.get_all_pod_ips()
        node_pods = {}
        
        for pod_name, pod_info in all_pods.items():
            if pod_info.get("node") == node_hostname:
                node_pods[pod_name] = pod_info.get("primary_ip")
        
        return node_pods
    
    def close(self) -> None:
        """Close etcd connection."""
        if self.etcd_client:
            try:
                self.etcd_client.close()
                logger.info("Closed etcd connection")
            except Exception as e:
                logger.warning(f"Error closing etcd connection: {e}")
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def get_etcd_interface_from_config() -> Optional[EtcdInterface]:
    """Create etcd interface from configuration.
    
    Reads etcd configuration from ReadConfig or environment variables.
    
    Returns:
        EtcdInterface instance if configured, None otherwise
    """
    if not ETCD3_AVAILABLE:
        logger.warning("etcd3 library not available. Install with: pip install etcd3")
        return None
    
    try:
        from utils.ReadConfig import ReadConfig as rc
        
        read_config = rc()
        
        # Try to get etcd config from ReadConfig
        try:
            etcd_config = read_config.etcd_config
            etcd_endpoints = etcd_config.get("endpoints")
            timeout = etcd_config.get("timeout", 5)
            ca_cert = etcd_config.get("ca_cert")
            cert_key = etcd_config.get("cert_key")
            cert_cert = etcd_config.get("cert_cert")
        except (AttributeError, KeyError):
            # Fallback to environment variable
            import os
            etcd_endpoints = os.getenv("ETCD_ENDPOINTS")
            timeout = 5
            ca_cert = None
            cert_key = None
            cert_cert = None
        
        if not etcd_endpoints:
            logger.warning("No etcd endpoints configured, skipping etcd interface initialization")
            return None
        
        return EtcdInterface(
            etcd_endpoints=etcd_endpoints,
            timeout=timeout,
            ca_cert=ca_cert,
            cert_key=cert_key,
            cert_cert=cert_cert
        )
    
    except Exception as e:
        logger.error(f"Failed to create etcd interface from config: {e}", exc_info=True)
        return None

