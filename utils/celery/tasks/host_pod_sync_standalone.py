"""
Standalone script for running host/pod sync task in a loop.

This script can be run independently (not via Celery Beat) to collect
and send host/pod information every 30 seconds.
"""
import time
import signal
import sys
from typing import Optional
from logpkg.log_kcld import LogKCld
# Import the core function (not the Celery task decorator)
from utils.celery.tasks.host_pod_sync_tasks import (
    _collect_host_info,
    _collect_pod_info,
    _send_to_queue,
    INFO_QUEUE_NAME,
    DEFAULT_CONTAINERD_SOCKET,
    DEFAULT_NAMESPACE
)
from utils.redis.redis_interface import RedisInterface
from socket import gethostname
from datetime import datetime, timezone
import json

logger = LogKCld()

# Configuration
SYNC_INTERVAL = 30.0  # seconds
RUNNING = True


def _run_sync_cycle(
    containerd_socket: Optional[str] = None,
    namespace: Optional[str] = None
) -> dict:
    """Run a single sync cycle (same logic as Celery task but without decorator).
    
    Args:
        containerd_socket: Containerd socket path (optional)
        namespace: Containerd namespace (optional)
        
    Returns:
        Dictionary with collection status and message count
    """
    hostname = gethostname()
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    ns = namespace or DEFAULT_NAMESPACE
    
    try:
        # Initialize Redis interface
        redis_client = RedisInterface()
        
        # Collect host information
        host_info = _collect_host_info(hostname)
        
        # Collect pod information
        pod_info = _collect_pod_info(hostname, sock, ns)
        
        # Package the information
        info_package = {
            "hostname": hostname,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "host_info": host_info,
            "pod_info": pod_info,
            "metadata": {
                "containerd_socket": sock,
                "namespace": ns,
                "collection_version": "1.0"
            }
        }
        
        # Send to Redis queue
        message_count = _send_to_queue(redis_client, info_package)
        
        logger.info(
            f"Collected and sent host/pod info for {hostname} "
            f"(pods: {len(pod_info.get('pods', []))}, queue_size: {message_count})"
        )
        
        return {
            "status": "success",
            "hostname": hostname,
            "host_info_collected": host_info is not None,
            "pods_count": sum(len(pods) for pods in pod_info.get("pods", {}).values()),
            "namespaces_count": len(pod_info.get("namespaces", [])),
            "queue_size": message_count,
            "timestamp": info_package["timestamp"]
        }
        
    except Exception as e:
        logger.error(
            f"Failed to collect and send host/pod info for {hostname}: {e}",
            exc_info=True
        )
        return {
            "status": "error",
            "hostname": hostname,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


def signal_handler(signum, frame) -> None:
    """Handle shutdown signals gracefully."""
    global RUNNING
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    RUNNING = False
    sys.exit(0)


def run_sync_loop(
    containerd_socket: Optional[str] = None,
    namespace: Optional[str] = None,
    interval: float = SYNC_INTERVAL
) -> None:
    """Run the sync task in a continuous loop.
    
    Args:
        containerd_socket: Containerd socket path (optional)
        namespace: Containerd namespace (optional)
        interval: Time between syncs in seconds (default: 30.0)
    """
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info(
        f"Starting host/pod sync loop "
        f"(interval={interval}s, socket={containerd_socket or 'default'}, "
        f"namespace={namespace or 'default'})"
    )
    
    cycle_count = 0
    
    try:
        while RUNNING:
            cycle_count += 1
            logger.info(f"Starting sync cycle #{cycle_count}")
            
            try:
                # Call the sync function directly (not as Celery task)
                result = _run_sync_cycle(
                    containerd_socket=containerd_socket,
                    namespace=namespace
                )
                
                if result.get("status") == "success":
                    logger.info(
                        f"Sync cycle #{cycle_count} completed successfully: "
                        f"hostname={result.get('hostname')}, "
                        f"pods={result.get('pods_count')}, "
                        f"queue_size={result.get('queue_size')}"
                    )
                else:
                    logger.warning(
                        f"Sync cycle #{cycle_count} completed with errors: "
                        f"{result.get('error', 'Unknown error')}"
                    )
            
            except Exception as e:
                logger.error(
                    f"Sync cycle #{cycle_count} failed: {e}",
                    exc_info=True
                )
            
            # Wait for next cycle (unless shutting down)
            if RUNNING:
                logger.debug(f"Waiting {interval} seconds before next sync...")
                time.sleep(interval)
    
    except KeyboardInterrupt:
        logger.info("Sync loop interrupted by user")
    except Exception as e:
        logger.error(f"Sync loop error: {e}", exc_info=True)
        raise
    
    logger.info(f"Sync loop stopped after {cycle_count} cycles")


def main() -> None:
    """Main entry point for standalone sync script."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Standalone host/pod sync service"
    )
    parser.add_argument(
        "--socket",
        type=str,
        default=None,
        help="Containerd socket path (default: unix:///run/containerd/containerd.sock)"
    )
    parser.add_argument(
        "--namespace",
        type=str,
        default=None,
        help="Containerd namespace (default: k8s.io)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=SYNC_INTERVAL,
        help=f"Sync interval in seconds (default: {SYNC_INTERVAL})"
    )
    
    args = parser.parse_args()
    
    run_sync_loop(
        containerd_socket=args.socket,
        namespace=args.namespace,
        interval=args.interval
    )


if __name__ == "__main__":
    main()

