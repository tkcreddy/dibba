"""
ETCD interface for querying Calico pod IP addresses.

This module provides methods to:
- Connect to Calico's etcd database
- Query workload endpoints (pods) and their IP addresses
- Map pod names to IP addresses

NOTE: etcd3 requires protobuf 3.x, but Dibba uses protobuf 6.x for containerd.
To use etcd3, set environment variable PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
BEFORE starting the application (in your shell or systemd service file).
This uses pure-Python protobuf parsing which is slower but compatible.
"""
import os
# CRITICAL: Set environment variable BEFORE importing any protobuf-related modules
# This must happen before any imports that might trigger protobuf initialization
# Check if environment variable is already set (e.g., by user in shell/systemd)
# If not set and we haven't initialized protobuf yet, set it conditionally
# However, if protobuf is already imported (via containerd), this won't help
# So we make etcd3 optional and provide clear error messages

import json
import sys
import importlib
import re
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse
from logpkg.log_kcld import LogKCld, log_to_file
from utils.exceptions import RedisError
from utils.error_handlers import handle_errors

logger = LogKCld()

# Check if protobuf is already imported (would be imported by containerd modules)
# CRITICAL: Once protobuf is imported, PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION has no effect
PROTOBUF_ALREADY_IMPORTED = 'google.protobuf' in sys.modules or 'protobuf' in sys.modules

# Check if environment variable is already set (user set it before starting)
ENV_VAR_ALREADY_SET = os.environ.get('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION') == 'python'

# Try to import etcd3, handling protobuf compatibility issues
ETCD3_AVAILABLE = False
PROTOBUF_COMPATIBILITY_ISSUE = False
etcd3 = None  # Will be set if import succeeds

try:
    # If protobuf is already imported, the environment variable won't help
    # We can only try to import etcd3 if the environment variable was set before protobuf import
    import etcd3
    ETCD3_AVAILABLE = True
except ImportError:
    ETCD3_AVAILABLE = False
    logger.warning("etcd3 library not available. Install with: pip install etcd3")
except (TypeError, RuntimeError) as e:
    # Protobuf 6.x compatibility issue with etcd3's generated code
    error_str = str(e)
    if "Descriptors cannot be created directly" in error_str or "protobuf" in error_str.lower():
        ETCD3_AVAILABLE = False
        PROTOBUF_COMPATIBILITY_ISSUE = True
        
        # Once protobuf is imported, environment variable changes have no effect
        # However, if the environment variable was set BEFORE protobuf import, it should work
        if PROTOBUF_ALREADY_IMPORTED and not ENV_VAR_ALREADY_SET:
            logger.warning(
                "etcd3 cannot be used because protobuf was already imported by containerd modules "
                "and PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python was not set before starting. "
                "To use etcd3, you MUST set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python "
                "BEFORE starting the application (not at runtime).\n"
                "Example in shell:\n"
                "  export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python\n"
                "  python -m server.main_api\n"
                "Or in systemd service file, add:\n"
                "  Environment=\"PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python\"\n"
                "Note: This makes protobuf use pure-Python implementation (slower but compatible with etcd3)."
            )
        elif ENV_VAR_ALREADY_SET:
            # Environment variable is set but still failed - protobuf might have been imported before it was set
            logger.warning(
                "etcd3 import failed despite PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python being set. "
                "This may indicate protobuf was imported before the environment variable was set, "
                "or etcd3 installation issues. etcd3 is optional - core Dibba functionality works without it."
            )
        else:
            # Protobuf not yet imported, try setting environment variable and retry
            os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'
            
            # Clear any partially loaded modules
            modules_to_clear = [name for name in sys.modules.keys() if name.startswith('etcd3')]
            for module_name in modules_to_clear:
                del sys.modules[module_name]
            
            try:
                import etcd3
                ETCD3_AVAILABLE = True
                PROTOBUF_COMPATIBILITY_ISSUE = False
                logger.info(
                    "Successfully imported etcd3 using pure-Python protobuf implementation "
                    "(PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python). "
                    "Note: This is slower than the default C++ implementation but required for protobuf 6.x compatibility."
                )
            except Exception as retry_error:
                logger.error(
                    f"Failed to import etcd3 even with compatibility mode: {retry_error}. "
                    f"etcd3 requires protobuf 3.x but protobuf 6.x is needed for containerd communication. "
                    f"Set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python BEFORE starting the application."
                )
    else:
        raise
except Exception as e:
    ETCD3_AVAILABLE = False
    logger.warning(f"etcd3 library import failed: {e}")


def _enable_etcd3_compatibility() -> bool:
    """Enable etcd3 compatibility with protobuf 6.x.
    
    NOTE: This function cannot work if protobuf has already been imported.
    The environment variable PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION must be
    set BEFORE starting the Python process (e.g., in shell or systemd service file).
    
    Returns:
        True if etcd3 can now be imported, False otherwise
    """
    global ETCD3_AVAILABLE, etcd3, PROTOBUF_COMPATIBILITY_ISSUE
    
    if ETCD3_AVAILABLE:
        return True  # Already available
    
    # CRITICAL: Check at runtime if protobuf is already imported (not just at module load time)
    # Once protobuf is imported, environment variable changes have no effect
    protobuf_now_imported = 'google.protobuf' in sys.modules or 'protobuf' in sys.modules
    env_var_set = os.environ.get('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION') == 'python'
    
    if protobuf_now_imported and not env_var_set:
        # Protobuf is already imported and environment variable was not set before import
        logger.error(
            "Cannot enable etcd3 compatibility: protobuf was already imported by containerd modules "
            "and PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python was not set before starting the application. "
            "Once protobuf is imported, the environment variable has no effect.\n\n"
            "SOLUTION: Set the environment variable BEFORE starting the application:\n"
            "  export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python\n"
            "  python -m server.main_api\n\n"
            "Or in systemd service file, add:\n"
            "  Environment=\"PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python\"\n\n"
            "See ETCD3_SETUP.md for detailed instructions.\n"
            "Note: etcd3 is optional - Dibba works without it."
        )
        return False
    elif protobuf_now_imported and env_var_set:
        # Protobuf is imported but environment variable is set - it should have worked, but didn't
        # This might indicate protobuf was imported before the variable was set, or etcd3 installation issue
        logger.warning(
            "etcd3 import failed despite PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python being set. "
            "This may indicate protobuf was imported before the environment variable was set, "
            "or etcd3 installation issues. etcd3 is optional - Dibba works without it."
        )
        return False
    
    # Set environment variable before importing (only works if protobuf not yet imported)
    os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'
    
    try:
        # Clear any cached import attempts by reloading if already attempted
        modules_to_remove = []
        for module_name in list(sys.modules.keys()):
            if module_name == 'etcd3' or module_name.startswith('etcd3.'):
                modules_to_remove.append(module_name)
        
        for module_name in modules_to_remove:
            del sys.modules[module_name]
        
        # Now try importing with the environment variable set
        import etcd3
        ETCD3_AVAILABLE = True
        PROTOBUF_COMPATIBILITY_ISSUE = False
        logger.info("Successfully imported etcd3 using pure-Python protobuf implementation (PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python)")
        return True
    except Exception as e:
        logger.error(
            f"Failed to import etcd3 even with compatibility mode: {e}. "
            f"etcd3 requires protobuf 3.x but protobuf 6.x is needed for containerd. "
            f"Set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python BEFORE starting the application.",
            exc_info=True
        )
        return False


def _try_import_etcd3_with_compatibility() -> bool:
    """Try to import etcd3 with protobuf compatibility enabled.
    
    This is called automatically when etcd3 is needed but not available due to protobuf issues.
    
    Returns:
        True if etcd3 is now available, False otherwise
    """
    if PROTOBUF_COMPATIBILITY_ISSUE:
        return _enable_etcd3_compatibility()
    return ETCD3_AVAILABLE


class EtcdInterface:
    """Interface for querying Calico's etcd database."""
    
    # Calico etcd key prefix for workload endpoints (pods)
    CALICO_WORKLOAD_ENDPOINT_PREFIX = "/calico/resources/v3/projectcalico.org/workloadendpoints/"
    # Calico etcd key prefix for nodes
    CALICO_NODE_PREFIX = "/calico/resources/v3/projectcalico.org/nodes/"
    # Calico etcd key prefix for IPAM blocks (IPv4)
    CALICO_IPAM_BLOCK_PREFIX = "/calico/ipam/v2/assignment/ipv4/block/"
    
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
        # Try to enable compatibility mode if needed
        if not ETCD3_AVAILABLE:
            if PROTOBUF_COMPATIBILITY_ISSUE:
                if not _try_import_etcd3_with_compatibility():
                    # Protobuf was already imported, so environment variable can't help
                    # Make etcd3 optional - log clear instructions instead of failing
                    logger.warning(
                        "etcd3 is not available due to protobuf compatibility issues. "
                        "To use etcd3, you MUST set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python "
                        "BEFORE starting the application (not at runtime).\n"
                        "Example in shell:\n"
                        "  export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python\n"
                        "  python -m server.main_api\n"
                        "Or in systemd service file:\n"
                        "  Environment=\"PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python\"\n"
                        "Note: etcd3 is optional - core Dibba functionality works without it."
                    )
                    raise ImportError(
                        "etcd3 library is not available due to protobuf compatibility issues. "
                        "Set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python BEFORE starting the application. "
                        "Note: etcd3 is optional - Dibba works without it."
                    )
            else:
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
                        # etcd/Calico stores IPs with CIDR notation (e.g., "192.168.1.1/32")
                        # Strip CIDR notation to get clean IP addresses for health checks and network operations
                        raw_primary_ip = ip_networks[0] if ip_networks else None
                        primary_ip = raw_primary_ip.split('/')[0] if raw_primary_ip and '/' in raw_primary_ip else raw_primary_ip
                        
                        # Strip CIDR notation from all IP addresses
                        clean_ip_addresses = []
                        for ip in ip_networks:
                            if ip and '/' in ip:
                                clean_ip = ip.split('/')[0]
                                clean_ip_addresses.append(clean_ip)
                            else:
                                clean_ip_addresses.append(ip)
                        
                        if primary_ip:
                            pod_ips[pod_name] = {
                                "ip_addresses": clean_ip_addresses,  # Store clean IPs without CIDR
                                "primary_ip": primary_ip,  # Clean primary IP without CIDR
                                "node": node,
                                "namespace": namespace,
                                "workload_endpoint": workload_endpoint_name
                            }
                            logger.debug(f"Found pod {pod_name} (namespace: {namespace}) on node {node} with IPs: {clean_ip_addresses} (stripped CIDR from etcd: {ip_networks})")
                
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
    
    @log_to_file(logger)
    @handle_errors("get_all_calico_nodes", "ETCD_ERROR")
    def get_all_calico_nodes(self) -> Dict[str, Dict[str, Any]]:
        """Get all Calico nodes from etcd.
        
        Returns:
            Dictionary mapping node names to node info:
            {
                "ip-172-31-16-125": {
                    "name": "ip-172-31-16-125",
                    "ipv4": "172.31.16.125/20",
                    "asn": "64512",
                    "key": "/calico/resources/v3/projectcalico.org/nodes/ip-172-31-16-125"
                }
            }
        """
        if not self.etcd_client:
            raise RuntimeError("etcd client not initialized")
        
        nodes = {}
        
        try:
            # Query all nodes from Calico
            for value, meta in self.etcd_client.get_prefix(self.CALICO_NODE_PREFIX):
                try:
                    data = json.loads(value)
                    metadata = data.get("metadata", {})
                    spec = data.get("spec", {})
                    
                    # Extract node information
                    node_name = metadata.get("name", "")
                    if not node_name:
                        # Try to extract from key
                        key = meta.key if hasattr(meta, 'key') else str(meta)
                        if key.startswith(self.CALICO_NODE_PREFIX):
                            node_name = key[len(self.CALICO_NODE_PREFIX):]
                    
                    # Extract IP addresses and ASN
                    ipv4 = spec.get("bgp", {}).get("ipv4Address", "")
                    asn = spec.get("bgp", {}).get("asNumber", "")
                    
                    if node_name:
                        nodes[node_name] = {
                            "name": node_name,
                            "ipv4": ipv4,
                            "asn": str(asn) if asn else "",
                            "key": f"{self.CALICO_NODE_PREFIX}{node_name}"
                        }
                        logger.debug(f"Found Calico node: {node_name} (IPv4: {ipv4}, ASN: {asn})")
                
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse Calico node data: {e}")
                    continue
        
        except Exception as e:
            logger.error(f"Failed to query etcd for Calico nodes: {e}", exc_info=True)
            raise
        
        logger.info(f"Retrieved {len(nodes)} Calico nodes from etcd")
        return nodes
    
    @log_to_file(logger)
    @handle_errors("delete_calico_node", "ETCD_ERROR")
    def delete_calico_node(self, node_name: str) -> bool:
        """Delete a Calico node from etcd.
        
        Args:
            node_name: Name of the node to delete (e.g., "ip-172-31-16-125")
            
        Returns:
            True if node was deleted, False otherwise
        """
        if not self.etcd_client:
            raise RuntimeError("etcd client not initialized")
        
        node_key = f"{self.CALICO_NODE_PREFIX}{node_name}"
        
        try:
            # Check if node exists
            try:
                self.etcd_client.get(node_key)
            except Exception:
                logger.warning(f"Calico node {node_name} does not exist in etcd")
                return False
            
            # Delete the node
            self.etcd_client.delete(node_key)
            logger.info(f"Successfully deleted Calico node {node_name} from etcd")
            return True
        
        except Exception as e:
            logger.error(f"Failed to delete Calico node {node_name}: {e}", exc_info=True)
            return False
    
    @log_to_file(logger)
    @handle_errors("get_ipam_blocks_for_node", "ETCD_ERROR")
    def get_ipam_blocks_for_node(self, node_name: str) -> List[str]:
        """Get all IPAM blocks associated with a specific node.
        
        Args:
            node_name: Name of the node (e.g., "ip-172-31-16-125")
            
        Returns:
            List of block CIDRs (e.g., ["192.168.12.192/26", "192.168.128.192/26"])
        """
        if not self.etcd_client:
            raise RuntimeError("etcd client not initialized")
        
        blocks = []
        
        try:
            # Query all IPAM blocks from Calico
            for value, meta in self.etcd_client.get_prefix(self.CALICO_IPAM_BLOCK_PREFIX):
                try:
                    data = json.loads(value)
                    # IPAM blocks have affinity to nodes
                    # Check if this block is associated with the node
                    affinities = data.get("affinity", {})
                    block_node = affinities.get("node") or affinities.get("hostname")
                    
                    if block_node == node_name:
                        # Extract block CIDR from key
                        key = meta.key if hasattr(meta, 'key') else str(meta)
                        if key.startswith(self.CALICO_IPAM_BLOCK_PREFIX):
                            block_cidr = key[len(self.CALICO_IPAM_BLOCK_PREFIX):]
                            blocks.append(block_cidr)
                            logger.debug(f"Found IPAM block {block_cidr} for node {node_name}")
                
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse IPAM block data: {e}")
                    continue
        
        except Exception as e:
            logger.error(f"Failed to query etcd for IPAM blocks: {e}", exc_info=True)
            raise
        
        logger.info(f"Retrieved {len(blocks)} IPAM blocks for node {node_name}: {blocks}")
        return blocks
    
    @log_to_file(logger)
    @handle_errors("get_all_ipam_blocks", "ETCD_ERROR")
    def get_all_ipam_blocks(self) -> Dict[str, Dict[str, Any]]:
        """Get all IPAM blocks from etcd.
        
        Returns:
            Dictionary mapping block CIDRs to block info:
            {
                "192.168.12.192/26": {
                    "cidr": "192.168.12.192/26",
                    "node": "ip-172-31-16-125",
                    "key": "/calico/ipam/v2/assignment/ipv4/block/192.168.12.192/26"
                }
            }
        """
        if not self.etcd_client:
            raise RuntimeError("etcd client not initialized")
        
        blocks = {}
        
        try:
            # Query all IPAM blocks from Calico
            for value, meta in self.etcd_client.get_prefix(self.CALICO_IPAM_BLOCK_PREFIX):
                try:
                    data = json.loads(value)
                    
                    # Extract block CIDR from key
                    key = meta.key if hasattr(meta, 'key') else str(meta)
                    if key.startswith(self.CALICO_IPAM_BLOCK_PREFIX):
                        block_cidr = key[len(self.CALICO_IPAM_BLOCK_PREFIX):]
                        
                        # Get node affinity
                        affinities = data.get("affinity", {})
                        block_node = affinities.get("node") or affinities.get("hostname") or ""
                        
                        blocks[block_cidr] = {
                            "cidr": block_cidr,
                            "node": block_node,
                            "key": key
                        }
                        logger.debug(f"Found IPAM block {block_cidr} (node: {block_node})")
                
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse IPAM block data: {e}")
                    continue
        
        except Exception as e:
            logger.error(f"Failed to query etcd for IPAM blocks: {e}", exc_info=True)
            raise
        
        logger.info(f"Retrieved {len(blocks)} IPAM blocks from etcd")
        return blocks
    
    @log_to_file(logger)
    @handle_errors("delete_ipam_block", "ETCD_ERROR")
    def delete_ipam_block(self, block_cidr: str) -> bool:
        """Delete an IPAM block from etcd.
        
        Args:
            block_cidr: Block CIDR (e.g., "192.168.12.192/26")
            
        Returns:
            True if block was deleted, False otherwise
        """
        if not self.etcd_client:
            raise RuntimeError("etcd client not initialized")
        
        block_key = f"{self.CALICO_IPAM_BLOCK_PREFIX}{block_cidr}"
        
        try:
            # Check if block exists
            try:
                self.etcd_client.get(block_key)
            except Exception:
                logger.warning(f"IPAM block {block_cidr} does not exist in etcd")
                return False
            
            # Delete the block
            self.etcd_client.delete(block_key)
            logger.info(f"Successfully deleted IPAM block {block_cidr} from etcd")
            return True
        
        except Exception as e:
            logger.error(f"Failed to delete IPAM block {block_cidr}: {e}", exc_info=True)
            return False


def get_etcd_interface_from_config() -> Optional[EtcdInterface]:
    """Create etcd interface from configuration.
    
    Reads etcd configuration from ReadConfig or environment variables.
    Automatically enables protobuf compatibility mode if needed.
    
    Returns:
        EtcdInterface instance if configured, None otherwise
    """
    # Try to enable compatibility mode if needed
    if not ETCD3_AVAILABLE:
        if PROTOBUF_COMPATIBILITY_ISSUE:
            if not _try_import_etcd3_with_compatibility():
                # Protobuf was already imported, so environment variable can't help
                # Make etcd3 optional - log clear instructions instead of failing
                logger.warning(
                    "etcd3 is not available due to protobuf compatibility issues. "
                    "To use etcd3, you MUST set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python "
                    "BEFORE starting the application (not at runtime).\n"
                    "Example: export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python && python -m server.main_api\n"
                    "Note: etcd3 is optional - core Dibba functionality works without it."
                )
                return None
        else:
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

