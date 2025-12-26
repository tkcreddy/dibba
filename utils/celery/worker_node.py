"""
Celery worker node configuration with integrated host/pod sync task.

This module:
1. Configures Celery worker with task queues
2. Includes worker_node_tasks and containerd_tasks
3. Runs host/pod sync task in a separate thread every 30 seconds
"""
import threading
import time
from typing import Optional
from utils.celery.celery_config import celery_app
from kombu import Queue, Exchange
from socket import gethostname
from utils.ReadConfig import ReadConfig as rc
from utils.extensions.utilities_extention import UtilitiesExtension
from logpkg.log_kcld import LogKCld

logger = LogKCld()

# Configuration
read_config = rc()
secure_exchange = Exchange('secure_exchange', type='direct')
hostname = gethostname()
key = read_config.encryption_config['key']
encode_util = UtilitiesExtension(key)
hostname_queue_name = encode_util.encode_hostname_with_key(hostname)

# Sync task configuration
SYNC_INTERVAL = 30.0  # seconds
SYNC_RUNNING = False
SYNC_THREAD = None

logger.info(f"Worker node hostname: {hostname}")

# Configure Celery task queues
celery_app.conf.task_queues = [
    Queue(hostname_queue_name, exchange=secure_exchange, routing_key=hostname_queue_name),
]

# Include task modules
celery_app.autodiscover_tasks(['utils.celery.tasks.worker_node_tasks'])
celery_app.conf.include = [
    "utils.celery.tasks.containerd_tasks",
    "utils.celery.tasks.host_pod_sync_tasks",
    "utils.celery.tasks.scheduler_tasks"
]


def _run_sync_loop(
    containerd_socket: Optional[str] = None,
    namespace: Optional[str] = None,
    interval: float = SYNC_INTERVAL
) -> None:
    """Run the host/pod sync task in a continuous loop.
    
    This function runs in a separate thread and periodically collects
    host and pod information, sending it to the Redis queue.
    
    Args:
        containerd_socket: Containerd socket path (optional)
        namespace: Containerd namespace (optional)
        interval: Time between syncs in seconds (default: 30.0)
    """
    global SYNC_RUNNING
    
    # Import here to avoid circular dependencies
    from utils.celery.tasks.host_pod_sync_tasks import collect_and_send_host_pod_info
    
    logger.info(
        f"Starting host/pod sync thread "
        f"(interval={interval}s, socket={containerd_socket or 'default'}, "
        f"namespace={namespace or 'default'})"
    )
    
    cycle_count = 0
    SYNC_RUNNING = True
    
    try:
        while SYNC_RUNNING:
            cycle_count += 1
            logger.debug(f"Starting sync cycle #{cycle_count}")
            
            try:
                # Call the sync function directly (not as Celery task)
                result = collect_and_send_host_pod_info(
                    containerd_socket=containerd_socket,
                    namespace=namespace
                )
                
                if result and result.get("status") == "success":
                    logger.info(
                        f"Sync cycle #{cycle_count} completed: "
                        f"hostname={result.get('hostname')}, "
                        f"pods={result.get('pods_count')}, "
                        f"queue_size={result.get('queue_size')}"
                    )
                else:
                    error_msg = result.get('error', 'Unknown error') if result else 'No result'
                    logger.warning(
                        f"Sync cycle #{cycle_count} completed with errors: {error_msg}"
                    )
            
            except Exception as e:
                logger.error(
                    f"Sync cycle #{cycle_count} failed: {e}",
                    exc_info=True
                )
            
            # Wait for next cycle (unless shutting down)
            if SYNC_RUNNING:
                # Sleep in small increments to allow quick shutdown
                sleep_time = 0
                while sleep_time < interval and SYNC_RUNNING:
                    time.sleep(min(1.0, interval - sleep_time))
                    sleep_time += 1.0
    
    except Exception as e:
        logger.error(f"Sync loop error: {e}", exc_info=True)
        SYNC_RUNNING = False
    
    logger.info(f"Host/pod sync thread stopped after {cycle_count} cycles")


def start_sync_thread(
    containerd_socket: Optional[str] = None,
    namespace: Optional[str] = None,
    interval: float = SYNC_INTERVAL
) -> threading.Thread:
    """Start the host/pod sync task in a separate thread.
    
    Args:
        containerd_socket: Containerd socket path (optional)
        namespace: Containerd namespace (optional)
        interval: Sync interval in seconds (default: 30.0)
        
    Returns:
        The thread object
    """
    global SYNC_THREAD, SYNC_RUNNING
    
    if SYNC_THREAD and SYNC_THREAD.is_alive():
        logger.warning("Sync thread is already running")
        return SYNC_THREAD
    
    SYNC_RUNNING = True
    SYNC_THREAD = threading.Thread(
        target=_run_sync_loop,
        args=(containerd_socket, namespace, interval),
        name="host-pod-sync",
        daemon=True  # Daemon thread so it doesn't prevent shutdown
    )
    SYNC_THREAD.start()
    logger.info(f"Started host/pod sync thread (PID: {SYNC_THREAD.ident})")
    
    return SYNC_THREAD


def stop_sync_thread() -> None:
    """Stop the host/pod sync thread gracefully."""
    global SYNC_RUNNING, SYNC_THREAD
    
    if not SYNC_THREAD or not SYNC_THREAD.is_alive():
        return
    
    logger.info("Stopping host/pod sync thread...")
    SYNC_RUNNING = False
    
    if SYNC_THREAD.is_alive():
        SYNC_THREAD.join(timeout=5.0)
        if SYNC_THREAD.is_alive():
            logger.warning("Sync thread did not stop gracefully within timeout")
        else:
            logger.info("Sync thread stopped successfully")


# Export celery_app for Celery CLI
# This allows: celery -A utils.celery.worker_node worker
app = celery_app

# Celery worker signals to start/stop sync thread
# Use standard signal imports to avoid attribute errors during app discovery
try:
    from celery.signals import worker_process_init, worker_process_shutdown
    
    @worker_process_init.connect
    def worker_process_init_handler(sender=None, **kwargs):
        """Called when a worker process starts."""
        logger.info("Worker process initialized, starting host/pod sync thread...")
        start_sync_thread()

    @worker_process_shutdown.connect
    def worker_process_shutdown_handler(sender=None, **kwargs):
        """Called when a worker process shuts down."""
        logger.info("Worker process shutting down, stopping host/pod sync thread...")
        stop_sync_thread()
except (ImportError, AttributeError) as e:
    # If signals can't be imported, log a warning but don't fail
    # The sync thread can be started manually if needed
    logger.warning(f"Could not register Celery signals: {e}")
    logger.info("Sync thread will need to be started manually if signals are unavailable.")