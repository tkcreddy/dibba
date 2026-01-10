"""
Celery Beat scheduler configuration for Dibba.

This module configures periodic tasks for:
- Health checks (pod liveness and readiness probes)
- Host/pod information collection
- Deployment recovery (auto-scaling and replica management)
"""
from utils.celery.celery_config import celery_app
from kombu import Exchange
from utils.ReadConfig import ReadConfig as rc
from utils.extensions.utilities_extention import UtilitiesExtension

read_config = rc()
key = read_config.encryption_config['key']
encode_util = UtilitiesExtension(key)
health_check_queue_name = encode_util.encode_hostname_with_key('health_check')
scheduler_queue_name = encode_util.encode_hostname_with_key('scheduler')
secure_exchange = Exchange('secure_exchange', type='direct')
celery_app.conf.update(
    beat_schedule={
    'collect-host-pod-info-every-30-seconds': {
        'task': 'utils.celery.tasks.host_pod_sync_tasks.collect_and_send_host_pod_info',
        'schedule': 30.0,  # Run every 30 seconds
        'options': {
            'queue': 'host_pod_sync',  # Dedicated queue for sync tasks
            'exchange': secure_exchange,
            'routing_key': 'host_pod_sync',
            'delivery_mode': 2,
            'expires': 60,  # Expire if not processed within 60 seconds
        }
    },
    'recover-missing-replicas-every-60-seconds': {
        'task': 'deployment.recover_missing_replicas',
        'schedule': 60.0,  # Run every 60 seconds
        'options': {
            'queue': scheduler_queue_name,
            'exchange': secure_exchange,
            'routing_key': scheduler_queue_name,
            'delivery_mode': 2,
            'expires': 120,  # Expire if not processed within 120 seconds
        }
    },
    'refresh-health-check-workers-every-60-seconds': {
        'task': 'health.refresh_worker_list',
        'schedule': 60.0,  # Run every 60 seconds to update worker list
        # ASYNC WORKER DISCOVERY:
        # - Updates worker list asynchronously without blocking health checks
        # - Runs in background, health check tasks just read from cache
        # - Non-blocking - health checks are never delayed by worker discovery
        'options': {
            'queue': health_check_queue_name,  # Use dedicated health_check queue
            'exchange': secure_exchange,
            'routing_key': health_check_queue_name,
            'delivery_mode': 2,
            'expires': 120,  # Expire if not processed within 120 seconds
        }
    },
    'check-all-pods-health-every-5-seconds': {
        'task': 'health.check_all_pods_health',
        'schedule': 5.0,  # Run every 5 seconds for fool-proof checking
        # FOOL-PROOF DESIGN:
        # - Beat schedule at 5s ensures we can catch up quickly if tasks are delayed
        # - should_check_probe() intelligently handles:
        #   1. Normal case: Skip if < periodSeconds (e.g., 10s) since last check
        #   2. Normal case: Check if >= periodSeconds since last check
        #   3. Catch-up case: ALWAYS check if > 1.5x periodSeconds (overdue by >5s for 10s period)
        # - This ensures we never miss more than 1 check period, even if tasks queue up
        # - With beat at 5s, we can detect and catch up on overdue checks within 5 seconds
        # - Multiple tasks can run concurrently - should_check_probe() enforces periodSeconds per probe
        # - Tasks taking 15-25 seconds are fine - next beat will catch up within 5s
        # - Worker list is read from cache (non-blocking) - updated every 60s by separate beat task
        'options': {
            'queue': health_check_queue_name,  # Use dedicated health_check queue
            'exchange': secure_exchange,
            'routing_key': health_check_queue_name,
            'delivery_mode': 2,
            'expires': 60,  # Expire if not processed within 60 seconds (allows for task execution time and catch-up)
        }
    },
    },
)