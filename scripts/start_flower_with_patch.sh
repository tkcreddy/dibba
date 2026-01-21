#!/bin/bash
# Start Celery Flower with SSL patch applied

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="/opt/dibba"
APP_ENV="$APP_DIR/.venv/bin/activate"

cd "$APP_DIR" || exit 1

# Activate virtual environment
[ -f "$APP_ENV" ] && source "$APP_ENV"

# Apply SSL patch BEFORE importing anything else
python3 << 'PYTHON_PATCH'
import sys
import os

# Add project to path
sys.path.insert(0, '/opt/dibba')

# Patch Redis BEFORE anything else imports it
import ssl
import redis

# Store original
_original_redis_init = redis.Redis.__init__
_original_strict_init = redis.StrictRedis.__init__

def patched_redis_init(self, *args, **kwargs):
    if kwargs.get('ssl') or 'ssl' in str(kwargs):
        kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
        kwargs['ssl_check_hostname'] = False
    return _original_redis_init(self, *args, **kwargs)

def patched_strict_init(self, *args, **kwargs):
    if kwargs.get('ssl') or 'ssl' in str(kwargs):
        kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
        kwargs['ssl_check_hostname'] = False
    return _original_strict_init(self, *args, **kwargs)

redis.Redis.__init__ = patched_redis_init
redis.StrictRedis.__init__ = patched_strict_init

# Now import and apply flower patch
try:
    from utils.flower.flower_ssl_patch import patch_redis_ssl
    patch_redis_ssl()
except Exception as e:
    print(f"Warning: Could not apply flower patch: {e}")

# Import flower config
try:
    from utils.flower.flower_config import *
except Exception:
    pass

# Now start Flower
os.chdir('/opt/dibba')
os.system('celery -A utils.celery.celery_app flower --port=5555 --url_prefix=flower --persistent=True --db=/var/log/dibba/flower_db --state_save_interval=10000')
PYTHON_PATCH

