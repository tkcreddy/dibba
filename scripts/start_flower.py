#!/usr/bin/env python3
"""
Start Flower with proper SSL configuration for Redis.

This script ensures that Flower can connect to Redis with SSL
even when using self-signed certificates.

Usage:
    python3 scripts/start_flower.py
    python3 scripts/start_flower.py --port=8080
"""
import os
import sys
import subprocess
import ssl

# CRITICAL: Patch Redis BEFORE anything else imports it
# This must happen before any Redis connections are created
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

# Set environment variables for kombu/celery
os.environ['KOMBU_SSL_VERIFY'] = 'false'
os.environ['CELERY_BROKER_USE_SSL'] = 'true'

# Import flower patches (which also patch Flower-specific code)
try:
    import utils.flower  # This triggers all patches via __init__.py
except ImportError:
    try:
        from utils.flower.flower_broker_patch import patch_flower_broker
        from utils.flower.flower_ssl_patch import patch_redis_ssl
        patch_flower_broker()
        patch_redis_ssl()
    except ImportError:
        pass

# Import configuration to set up SSL environment variables
try:
    from utils.flower.flower_config import *  # Sets up SSL env vars
except ImportError:
    # If flower_config doesn't exist, set basic SSL env vars
    from utils.ReadConfig import ReadConfig as rc
    read_config = rc()
    redis_config = read_config.redis_queue_config  # Use redis_queue_config for Celery broker
    
    if redis_config.get('ssl_ca_certs') or redis_config.get('ssl_certfile'):
        os.environ['CELERY_BROKER_USE_SSL'] = 'true'
        os.environ['CELERY_BROKER_SSL_VERIFY'] = 'false'

# Get script directory
script_dir = os.path.dirname(os.path.abspath(__file__))
app_dir = os.path.dirname(script_dir)

# Change to app directory
os.chdir(app_dir)

# Default Flower arguments
default_args = [
    '--port=5555',
    '--url_prefix=flower',
    '--persistent=True',
    '--db=/var/log/dibba/flower_db',
    '--state_save_interval=10000',
]

# Get user-provided arguments (if any)
user_args = sys.argv[1:] if len(sys.argv) > 1 else []

# Build command
cmd = ['celery', '-A', 'utils.celery.celery_app', 'flower'] + default_args + user_args

# Print command for debugging
print(f"Starting Flower with command: {' '.join(cmd)}")
print(f"Working directory: {app_dir}")
print("SSL patches applied - Redis connections will skip certificate verification")

# Start Flower
try:
    subprocess.run(cmd, check=True)
except KeyboardInterrupt:
    print("\nFlower stopped by user")
    sys.exit(0)
except subprocess.CalledProcessError as e:
    print(f"Error starting Flower: {e}")
    sys.exit(1)
