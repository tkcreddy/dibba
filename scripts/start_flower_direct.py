#!/usr/bin/env python3
"""
Start Flower directly in this process (not via subprocess) to ensure SSL patches work.

This ensures all patches are applied before Flower starts.
"""
import os
import sys
import ssl

# CRITICAL: Patch Redis FIRST - before ANY imports
import redis

# Patch Redis classes directly at the lowest level
_original_redis_init = redis.Redis.__init__
_original_strict_init = redis.StrictRedis.__init__

def _patched_redis_init(self, *args, **kwargs):
    # Always disable SSL verification if SSL is enabled
    if kwargs.get('ssl') is True or 'ssl' in str(kwargs):
        kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
        kwargs['ssl_check_hostname'] = False
    return _original_redis_init(self, *args, **kwargs)

def _patched_strict_init(self, *args, **kwargs):
    if kwargs.get('ssl') is True or 'ssl' in str(kwargs):
        kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
        kwargs['ssl_check_hostname'] = False
    return _original_strict_init(self, *args, **kwargs)

redis.Redis.__init__ = _patched_redis_init
redis.StrictRedis.__init__ = _patched_strict_init

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Set environment variables
os.environ['KOMBU_SSL_VERIFY'] = 'false'
os.environ['CELERY_BROKER_USE_SSL'] = 'true'

# Import and apply all patches
try:
    import utils.flower  # Triggers all patches
except ImportError:
    try:
        from utils.flower.flower_broker_patch import patch_flower_broker
        from utils.flower.flower_ssl_patch import patch_redis_ssl
        patch_flower_broker()
        patch_redis_ssl()
    except ImportError:
        pass

# Import config
try:
    from utils.flower.flower_config import *
except ImportError:
    from utils.ReadConfig import ReadConfig as rc
    read_config = rc()
    redis_config = read_config.redis_queue_config
    if redis_config.get('ssl_ca_certs') or redis_config.get('ssl_certfile'):
        os.environ['CELERY_BROKER_USE_SSL'] = 'true'
        os.environ['CELERY_BROKER_SSL_VERIFY'] = 'false'

# Change to app directory
os.chdir(project_root)

# Now import and start Flower directly (not via subprocess)
from flower.command import FlowerCommand

# Parse arguments
argv = sys.argv[1:] if len(sys.argv) > 1 else []
default_args = [
    '--port=5555',
    '--url_prefix=flower',
    '--persistent=True',
    '--db=/var/log/dibba/flower_db',
    '--state_save_interval=10000',
]

flower_args = ['flower'] + default_args + argv

print(f"Starting Flower with SSL patches applied")
print(f"Arguments: {' '.join(flower_args)}")

# Start Flower
flower = FlowerCommand()
flower.run_from_argv(flower_args)

