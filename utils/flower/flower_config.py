"""
Flower configuration wrapper to handle SSL certificate issues.

This module provides a custom Flower configuration that properly handles
Redis SSL connections with self-signed certificates.
"""
import os
import ssl
from utils.ReadConfig import ReadConfig as rc

# Read Redis SSL configuration
read_config = rc()
redis_config = read_config.redis_queue_config

# Configure SSL context for Redis connections
# This is used by Flower when connecting to Redis broker
if redis_config.get('ssl_ca_certs') or redis_config.get('ssl_certfile'):
    # Create SSL context that doesn't verify certificates (for self-signed certs)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    # Set environment variables that kombu/redis will use
    # These are picked up by Celery's broker connection
    os.environ['CELERY_BROKER_USE_SSL'] = 'true'
    
    # Note: Flower uses the Celery app's broker connection, so the SSL
    # configuration in celery_config.py should handle this, but we set
    # these as a fallback
    if redis_config.get('ssl_ca_certs'):
        os.environ['CELERY_BROKER_SSL_CA_CERTS'] = redis_config['ssl_ca_certs']
    if redis_config.get('ssl_certfile'):
        os.environ['CELERY_BROKER_SSL_CERTFILE'] = redis_config['ssl_certfile']
    if redis_config.get('ssl_keyfile'):
        os.environ['CELERY_BROKER_SSL_KEYFILE'] = redis_config['ssl_keyfile']

