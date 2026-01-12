"""
Consumer service for processing host and pod information from Redis queue.

This module provides a consumer that listens to the Redis queue and updates
the database efficiently using batch processing and error handling.
"""
import json
import time
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from logpkg.log_kcld import LogKCld, log_to_file
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from utils.redis.host_pod_integration import HostPodIntegration
from utils.exceptions import RedisError

logger = LogKCld()

# Queue name
INFO_QUEUE_NAME = "host_pod_info_queue"

# Consumer configuration
BATCH_SIZE = 10  # Process messages in batches
POLL_INTERVAL = 1.0  # Seconds between queue polls
MAX_RETRIES = 3  # Maximum retries for failed messages
ERROR_QUEUE_NAME = "host_pod_info_queue_errors"  # Dead letter queue


class HostPodConsumer:
    """Consumer service for processing host and pod information from Redis queue.
    
    This consumer:
    - Listens to the Redis queue for host/pod information
    - Processes messages in batches for efficiency
    - Updates the database using HostPodStore
    - Handles errors gracefully with retry logic
    - Maintains statistics on processing
    """
    
    def __init__(
        self,
        redis_interface: RedisInterface,
        batch_size: int = BATCH_SIZE,
        poll_interval: float = POLL_INTERVAL,
        enable_etcd: bool = True
    ) -> None:
        """Initialize the consumer.
        
        Args:
            redis_interface: RedisInterface instance
            batch_size: Number of messages to process in each batch
            poll_interval: Seconds to wait between queue polls
            enable_etcd: Whether to enable etcd integration for pod IP fetching
        """
        self.redis = redis_interface.redis_client
        self.store = HostPodStore(redis_interface)
        self.integration = HostPodIntegration(redis_interface)
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        
        # Initialize etcd interface if enabled (lazy import to avoid protobuf compatibility issues)
        self.etcd_interface = None
        if enable_etcd:
            try:
                # Lazy import to avoid protobuf compatibility issues if etcd3 is not properly installed
                from utils.etcd.etcd_interface import get_etcd_interface_from_config
                self.etcd_interface = get_etcd_interface_from_config()
                if self.etcd_interface:
                    logger.info("ETCD interface initialized for pod IP fetching")
                else:
                    logger.warning("ETCD interface not available (not configured or etcd3 not installed)")
            except ImportError as import_err:
                logger.warning(f"ETCD interface not available (etcd3 library not installed or incompatible): {import_err}")
                self.etcd_interface = None
            except Exception as e:
                logger.warning(f"Failed to initialize etcd interface: {e}. Continuing without etcd.")
                self.etcd_interface = None
        else:
            self.etcd_interface = None
        
        # Statistics
        self.stats = {
            "processed": 0,
            "errors": 0,
            "last_processed": None,
            "start_time": datetime.now(timezone.utc).isoformat(),
            "etcd_ips_fetched": 0
        }
    
    @log_to_file(logger)
    def process_queue(self, max_messages: Optional[int] = None) -> Dict[str, Any]:
        """Process messages from the queue.
        
        This consumer is COMPLETELY INDEPENDENT of producers:
        - Reads from Redis queue using RPOP (non-blocking)
        - Processes messages that were queued by producers (even while consumer was down)
        - Producers don't need to be restarted when consumer restarts
        - Messages accumulate in queue when consumer is offline and are processed on restart
        
        Args:
            max_messages: Maximum number of messages to process (None for unlimited)
            
        Returns:
            Dictionary with processing statistics
        """
        processed_count = 0
        error_count = 0
        running = True
        
        # Get initial queue size for logging
        initial_queue_size = self.get_queue_size()
        if initial_queue_size > 0:
            logger.info(
                f"Starting consumer - {initial_queue_size} messages already in queue "
                f"(producers queued messages independently while consumer was offline)"
            )
        
        try:
            while running:
                if max_messages and processed_count >= max_messages:
                    break
                
                # Get batch of messages from Redis queue (non-blocking, independent of producers)
                messages = self._get_batch()
                
                if not messages:
                    # No messages, wait before next poll
                    # This is normal - producers queue messages independently
                    time.sleep(self.poll_interval)
                    continue
                
                # Process batch
                batch_processed, batch_errors = self._process_batch(messages)
                processed_count += batch_processed
                error_count += batch_errors
                
                # Update statistics
                self.stats["processed"] += batch_processed
                self.stats["errors"] += batch_errors
                self.stats["last_processed"] = datetime.now(timezone.utc).isoformat()
                
                # Get current queue size for logging
                remaining_queue_size = self.get_queue_size()
                
                logger.info(
                    f"Processed batch: {batch_processed} messages, "
                    f"{batch_errors} errors (total processed: {processed_count}, "
                    f"total errors: {error_count}, queue remaining: {remaining_queue_size})"
                )
        
        except KeyboardInterrupt:
            logger.info("Consumer stopped by user")
            running = False
        except Exception as e:
            logger.error(f"Consumer error: {e}", exc_info=True)
            running = False
            raise
        
        final_queue_size = self.get_queue_size()
        logger.info(
            f"Consumer stopped - processed: {processed_count}, errors: {error_count}, "
            f"queue remaining: {final_queue_size} (producers continue queuing independently)"
        )
        
        return {
            "processed": processed_count,
            "errors": error_count,
            "queue_remaining": final_queue_size,
            "stats": self.stats
        }
    
    @log_to_file(logger)
    def _get_batch(self) -> List[Dict[str, Any]]:
        """Get a batch of messages from the queue.
        
        This method reads from Redis queue independently - it doesn't require
        producers to be running. Messages can accumulate in the queue while
        the consumer is down and will be processed when consumer restarts.
        
        Returns:
            List of message dictionaries (empty list if no messages available)
        """
        messages: List[Dict[str, Any]] = []
        
        try:
            # Use RPOPLPUSH for atomic operation (if we want reliability)
            # Or use RPOP for simple FIFO processing
            # RPOP is fine here since we process in batches and handle errors
            for _ in range(self.batch_size):
                # RPOP removes and returns the rightmost (oldest) element
                # This is non-blocking - returns None if queue is empty
                message_str = self.redis.rpop(INFO_QUEUE_NAME)
                if not message_str:
                    # No more messages in queue - this is normal, not an error
                    break
                
                try:
                    # Parse message - handle both string and bytes
                    if isinstance(message_str, bytes):
                        message_str = message_str.decode('utf-8')
                    
                    message = json.loads(message_str)
                    if isinstance(message, dict):
                        messages.append(message)
                    else:
                        logger.warning(f"Invalid message format: {type(message)}, expected dict")
                        self._send_to_error_queue(message_str, f"Invalid message format: {type(message)}")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse message: {e}, message_str length: {len(message_str) if message_str else 0}")
                    self._send_to_error_queue(message_str, f"JSON decode error: {e}")
                except UnicodeDecodeError as e:
                    logger.error(f"Failed to decode message: {e}")
                    self._send_to_error_queue(message_str if isinstance(message_str, str) else message_str.decode('utf-8', errors='replace'), f"Unicode decode error: {e}")
        
        except Exception as e:
            logger.error(f"Failed to get batch from queue: {e}", exc_info=True)
        
        if messages:
            logger.debug(f"Retrieved {len(messages)} messages from queue (queue remaining: {self.redis.llen(INFO_QUEUE_NAME)})")
        
        return messages
    
    @log_to_file(logger)
    def _process_batch(self, messages: List[Dict[str, Any]]) -> tuple[int, int]:
        """Process a batch of messages.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Tuple of (processed_count, error_count)
        """
        processed = 0
        errors = 0
        
        for message in messages:
            try:
                self._process_message(message)
                processed += 1
            except Exception as e:
                logger.error(f"Failed to process message: {e}", exc_info=True)
                errors += 1
                # Send to error queue for later analysis
                self._send_to_error_queue(json.dumps(message), str(e))
        
        return processed, errors
    
    @log_to_file(logger)
    def _process_message(self, message: Dict[str, Any]) -> None:
        """Process a single message.
        
        Args:
            message: Message dictionary with host and pod information
            
        Raises:
            ValueError: If message is invalid
            RedisError: If database update fails
        """
        if not isinstance(message, dict):
            raise ValueError(f"Message must be a dictionary, got {type(message)}")
        
        hostname = message.get("hostname")
        if not hostname or not isinstance(hostname, str):
            raise ValueError(f"Invalid hostname in message: {hostname}")
        
        host_info = message.get("host_info")
        pod_info = message.get("pod_info")
        
        # Update host information
        if host_info and isinstance(host_info, dict):
            try:
                self.store.save_host_info(
                    hostname=hostname,
                    ip_address=host_info.get("ip_address"),
                    system_info=host_info.get("system_info"),
                    usage_metrics=host_info.get("usage_metrics"),
                    status="online"
                )
            except Exception as e:
                logger.warning(f"Failed to update host info for {hostname}: {e}")
        
        # Update pod information
        if pod_info and isinstance(pod_info, dict):
            pods_by_namespace = pod_info.get("pods", {})
            if isinstance(pods_by_namespace, dict):
                for namespace, pods_list in pods_by_namespace.items():
                    if isinstance(pods_list, list):
                        try:
                            # Get current pod IDs from the sync message
                            current_pod_ids = set()
                            for pod_data in pods_list:
                                if isinstance(pod_data, dict):
                                    pod_id = pod_data.get("pod_id")
                                    if pod_id and isinstance(pod_id, str):
                                        current_pod_ids.add(pod_id)
                            
                            # Get existing pod IDs from Redis for this host/namespace
                            existing_pods = self.store.get_pods_by_host_and_namespace(hostname, namespace)
                            existing_pod_ids = {p.get("pod_id") for p in existing_pods if p.get("pod_id")}
                            
                            # Find pods that are in Redis but not in current sync (terminated pods)
                            terminated_pod_ids = existing_pod_ids - current_pod_ids
                            
                            # Remove terminated pods from Redis
                            for pod_id in terminated_pod_ids:
                                try:
                                    self.integration.remove_pod(pod_id)
                                    logger.info(
                                        f"Removed terminated pod {pod_id} from Redis "
                                        f"(host: {hostname}, namespace: {namespace})"
                                    )
                                except Exception as remove_err:
                                    logger.warning(
                                        f"Failed to remove terminated pod {pod_id}: {remove_err}",
                                        exc_info=True
                                    )
                            
                            # Update/add current pods
                            # Check each pod's status and set startup_time if it just became running
                            for pod_data in pods_list:
                                if isinstance(pod_data, dict):
                                    pod_id = pod_data.get("pod_id")
                                    if pod_id and isinstance(pod_id, str):
                                        # Get existing pod data to check if startup_time needs to be set
                                        existing_pod = self.store.get_pod(pod_id)
                                        pod_status = pod_data.get("pause", {}).get("status") or pod_data.get("status", "unknown")
                                        
                                        # If pod is running and startup_time is not set, add it to pod_data
                                        if pod_status in ["running", "RUNNING"] and existing_pod:
                                            if not existing_pod.get("startup_time"):
                                                pod_data["startup_time"] = datetime.now(timezone.utc).isoformat()
                            
                            # Enrich pods with IP addresses from etcd if available
                            if self.etcd_interface:
                                pods_list = self._enrich_pods_with_etcd_ips(
                                    pods_list, hostname, namespace
                                )
                            
                            # Update/add current pods (normal update - will preserve startup_time if set above)
                            self.integration.update_pod_from_list_result(
                                pods_list=pods_list,
                                hostname=hostname,
                                namespace=namespace
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to update pods for {hostname} "
                                f"in namespace {namespace}: {e}",
                                exc_info=True
                            )
        
        logger.debug(f"Successfully processed message for host {hostname}")
    
    @log_to_file(logger)
    def _send_to_error_queue(self, message: str, error: str) -> None:
        """Send failed message to error queue for analysis.
        
        Args:
            message: Original message string
            error: Error message
        """
        try:
            error_entry = {
                "message": message,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            self.redis.lpush(ERROR_QUEUE_NAME, json.dumps(error_entry))
            self.redis.expire(ERROR_QUEUE_NAME, 86400)  # 24 hours
        except Exception as e:
            logger.error(f"Failed to send message to error queue: {e}", exc_info=True)
    
    @log_to_file(logger)
    def _enrich_pods_with_etcd_ips(
        self,
        pods_list: List[Dict[str, Any]],
        hostname: str,
        namespace: str
    ) -> List[Dict[str, Any]]:
        """Enrich pod list with IP addresses from Calico etcd.
        
        Args:
            pods_list: List of pod dictionaries
            hostname: Host where pods are running
            namespace: Namespace name
            
        Returns:
            Updated pods list with IP addresses
        """
        if not self.etcd_interface:
            return pods_list
        
        try:
            # Get all pod IPs from etcd for this node
            etcd_pod_ips = self.etcd_interface.get_pods_by_node(hostname)
            
            if not etcd_pod_ips:
                logger.debug(f"No pod IPs found in etcd for node {hostname}")
                return pods_list
            
            # Map pod names to IPs
            enriched_count = 0
            for pod_data in pods_list:
                if not isinstance(pod_data, dict):
                    continue
                
                pod_id = pod_data.get("pod_id")
                if not pod_id:
                    continue
                
                # Try to find IP for this pod
                # Calico workload endpoint names might differ from pod_id
                # Try exact match first, then partial match
                pod_ip = None
                
                # Try exact match
                if pod_id in etcd_pod_ips:
                    pod_ip = etcd_pod_ips[pod_id]
                else:
                    # Try partial match (pod_id might be part of Calico workload endpoint name)
                    for etcd_pod_name, etcd_ip in etcd_pod_ips.items():
                        if pod_id in etcd_pod_name or etcd_pod_name in pod_id:
                            pod_ip = etcd_ip
                            break
                
                # If found, strip CIDR notation and add IP to pod data
                # etcd/Calico stores IPs with CIDR notation (e.g., "192.168.1.1/32")
                # We need clean IPs for health checks and network operations
                if pod_ip:
                    # Strip CIDR notation if present (e.g., "192.168.1.1/32" -> "192.168.1.1")
                    if '/' in pod_ip:
                        original_ip = pod_ip
                        pod_ip = pod_ip.split('/')[0]
                        logger.debug(f"Stripped CIDR notation from etcd IP: {original_ip} -> {pod_ip}")
                    
                    pod_data["ip_address"] = pod_ip
                    enriched_count += 1
                    logger.debug(f"Enriched pod {pod_id} with IP {pod_ip} from etcd")
            
            if enriched_count > 0:
                self.stats["etcd_ips_fetched"] += enriched_count
                logger.info(
                    f"Enriched {enriched_count}/{len(pods_list)} pods with IP addresses "
                    f"from etcd for host {hostname} in namespace {namespace}"
                )
        
        except Exception as e:
            logger.warning(
                f"Failed to enrich pods with etcd IPs for {hostname}: {e}",
                exc_info=True
            )
            # Return original list on error
        
        return pods_list
    
    @log_to_file(logger)
    def get_queue_size(self) -> int:
        """Get current queue size.
        
        Returns:
            Number of messages in queue
        """
        try:
            return self.redis.llen(INFO_QUEUE_NAME)
        except Exception as e:
            logger.error(f"Failed to get queue size: {e}", exc_info=True)
            return 0
    
    @log_to_file(logger)
    def get_stats(self) -> Dict[str, Any]:
        """Get consumer statistics.
        
        Returns:
            Dictionary with consumer statistics
        """
        return {
            **self.stats,
            "queue_size": self.get_queue_size(),
            "error_queue_size": self.redis.llen(ERROR_QUEUE_NAME) if ERROR_QUEUE_NAME else 0
        }


def run_consumer(
    redis_interface: RedisInterface,
    batch_size: int = BATCH_SIZE,
    poll_interval: float = POLL_INTERVAL,
    max_messages: Optional[int] = None
) -> Dict[str, Any]:
    """Run the consumer service.
    
    This is the main entry point for running the consumer as a service.
    
    Args:
        redis_interface: RedisInterface instance
        batch_size: Number of messages to process in each batch
        poll_interval: Seconds to wait between queue polls
        max_messages: Maximum number of messages to process (None for unlimited)
        
    Returns:
        Dictionary with processing statistics
    """
    consumer = HostPodConsumer(
        redis_interface=redis_interface,
        batch_size=batch_size,
        poll_interval=poll_interval
    )
    
    logger.info(
        f"Starting host/pod info consumer "
        f"(batch_size={batch_size}, poll_interval={poll_interval})"
    )
    
    return consumer.process_queue(max_messages=max_messages)

