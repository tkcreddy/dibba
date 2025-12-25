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
        poll_interval: float = POLL_INTERVAL
    ) -> None:
        """Initialize the consumer.
        
        Args:
            redis_interface: RedisInterface instance
            batch_size: Number of messages to process in each batch
            poll_interval: Seconds to wait between queue polls
        """
        self.redis = redis_interface.redis_client
        self.store = HostPodStore(redis_interface)
        self.integration = HostPodIntegration(redis_interface)
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        
        # Statistics
        self.stats = {
            "processed": 0,
            "errors": 0,
            "last_processed": None,
            "start_time": datetime.now(timezone.utc).isoformat()
        }
    
    @log_to_file(logger)
    def process_queue(self, max_messages: Optional[int] = None) -> Dict[str, Any]:
        """Process messages from the queue.
        
        Args:
            max_messages: Maximum number of messages to process (None for unlimited)
            
        Returns:
            Dictionary with processing statistics
        """
        processed_count = 0
        error_count = 0
        running = True
        
        try:
            while running:
                if max_messages and processed_count >= max_messages:
                    break
                
                # Get batch of messages
                messages = self._get_batch()
                
                if not messages:
                    # No messages, wait before next poll
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
                
                logger.info(
                    f"Processed batch: {batch_processed} messages, "
                    f"{batch_errors} errors (total processed: {processed_count}, "
                    f"total errors: {error_count})"
                )
        
        except KeyboardInterrupt:
            logger.info("Consumer stopped by user")
            running = False
        except Exception as e:
            logger.error(f"Consumer error: {e}", exc_info=True)
            running = False
            raise
        
        return {
            "processed": processed_count,
            "errors": error_count,
            "stats": self.stats
        }
    
    @log_to_file(logger)
    def _get_batch(self) -> List[Dict[str, Any]]:
        """Get a batch of messages from the queue.
        
        Returns:
            List of message dictionaries
        """
        messages: List[Dict[str, Any]] = []
        
        try:
            # Use RPOP to get messages (FIFO)
            for _ in range(self.batch_size):
                message_str = self.redis.rpop(INFO_QUEUE_NAME)
                if not message_str:
                    break
                
                try:
                    message = json.loads(message_str)
                    if isinstance(message, dict):
                        messages.append(message)
                    else:
                        logger.warning(f"Invalid message format: {type(message)}")
                        self._send_to_error_queue(message_str, "Invalid message format")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse message: {e}")
                    self._send_to_error_queue(message_str, f"JSON decode error: {e}")
        
        except Exception as e:
            logger.error(f"Failed to get batch from queue: {e}", exc_info=True)
        
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
                            self.integration.update_pod_from_list_result(
                                pods_list=pods_list,
                                hostname=hostname,
                                namespace=namespace
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to update pods for {hostname} "
                                f"in namespace {namespace}: {e}"
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

