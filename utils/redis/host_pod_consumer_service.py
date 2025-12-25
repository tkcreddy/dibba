"""
Standalone consumer service for processing host and pod information.

This service can be run as a separate process to continuously consume
messages from the Redis queue and update the database.
"""
import signal
import sys
from typing import Optional
from logpkg.log_kcld import LogKCld
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_consumer import HostPodConsumer

logger = LogKCld()

# Configuration
BATCH_SIZE = 10
POLL_INTERVAL = 1.0
SHUTDOWN_GRACEFUL = True


class ConsumerService:
    """Service wrapper for the host/pod consumer."""
    
    def __init__(self) -> None:
        """Initialize the consumer service."""
        self.consumer: Optional[HostPodConsumer] = None
        self.running = False
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False
        if self.consumer:
            stats = self.consumer.get_stats()
            logger.info(f"Final statistics: {stats}")
        sys.exit(0)
    
    def run(self) -> None:
        """Run the consumer service."""
        try:
            # Initialize Redis interface
            redis_interface = RedisInterface()
            
            # Initialize consumer
            from utils.redis.host_pod_consumer import HostPodConsumer
            self.consumer = HostPodConsumer(
                redis_interface=redis_interface,
                batch_size=BATCH_SIZE,
                poll_interval=POLL_INTERVAL
            )
            
            logger.info("Starting host/pod info consumer service...")
            logger.info(f"Configuration: batch_size={BATCH_SIZE}, poll_interval={POLL_INTERVAL}")
            
            self.running = True
            
            # Run consumer
            stats = self.consumer.process_queue(max_messages=None)  # Run indefinitely
            
            logger.info(f"Consumer service stopped. Final stats: {stats}")
        
        except Exception as e:
            logger.error(f"Consumer service error: {e}", exc_info=True)
            sys.exit(1)


def main() -> None:
    """Main entry point for the consumer service."""
    service = ConsumerService()
    service.run()


if __name__ == "__main__":
    main()

