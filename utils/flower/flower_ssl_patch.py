"""
Flower SSL patch to handle Redis SSL connections with self-signed certificates.

This module patches Flower's Redis connection to disable SSL certificate verification.
It should be imported before starting Flower.
"""
import ssl
import redis
from kombu import Connection
from kombu.transport.redis import Transport

# Store original methods
_original_redis_connection = None
_original_redis_strict_connection = None
_original_connection_connect = None
_original_connection_pool = None
_patched = False


def patch_redis_ssl():
    """Patch Redis connection to disable SSL certificate verification."""
    global _original_redis_connection, _original_redis_strict_connection, _original_connection_connect, _original_connection_pool, _patched
    
    if _patched:
        return
    
    # Patch redis.Redis to disable SSL verification
    if not _original_redis_connection:
        _original_redis_connection = redis.Redis.__init__
        
        def patched_redis_init(self, *args, **kwargs):
            # If SSL is enabled, disable certificate verification for self-signed certs
            if kwargs.get('ssl') is True or 'ssl' in kwargs:
                kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
                kwargs['ssl_check_hostname'] = False
            return _original_redis_connection(self, *args, **kwargs)
        
        redis.Redis.__init__ = patched_redis_init
    
    # Patch redis.StrictRedis
    if not _original_redis_strict_connection:
        _original_redis_strict_connection = redis.StrictRedis.__init__
        
        def patched_strict_redis_init(self, *args, **kwargs):
            # If SSL is enabled, disable certificate verification
            if kwargs.get('ssl') is True or 'ssl' in kwargs:
                kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
                kwargs['ssl_check_hostname'] = False
            return _original_redis_strict_connection(self, *args, **kwargs)
        
        redis.StrictRedis.__init__ = patched_strict_redis_init
    
    # Patch redis.ConnectionPool to disable SSL verification at pool level
    if not _original_connection_pool:
        try:
            _original_connection_pool = redis.ConnectionPool.__init__
            
            def patched_pool_init(self, *args, **kwargs):
                # If SSL is enabled, disable certificate verification
                if kwargs.get('ssl') is True or 'ssl' in kwargs:
                    kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
                    kwargs['ssl_check_hostname'] = False
                return _original_connection_pool(self, *args, **kwargs)
            
            redis.ConnectionPool.__init__ = patched_pool_init
        except AttributeError:
            pass
    
    # Patch kombu's Redis transport channel
    try:
        from kombu.transport.redis import Channel
        
        if not hasattr(Channel, '_original_client'):
            Channel._original_client = Channel._create_client
            
            def patched_create_client(self):
                client = Channel._original_client(self)
                # Patch the client's connection pool if it exists
                if hasattr(client, 'connection_pool') and hasattr(client.connection_pool, 'connection_kwargs'):
                    conn_kwargs = client.connection_pool.connection_kwargs
                    if conn_kwargs.get('ssl') is True or 'ssl' in conn_kwargs:
                        conn_kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
                        conn_kwargs['ssl_check_hostname'] = False
                return client
            
            Channel._create_client = patched_create_client
    except ImportError:
        pass
    
    # Patch Flower's broker view to handle connection errors gracefully
    # This fixes the UnboundLocalError when get_queues() fails
    try:
        from flower.views.broker import BrokerView
        
        if not hasattr(BrokerView, '_original_get'):
            BrokerView._original_get = BrokerView.get
            
            def patched_get(self):
                try:
                    return BrokerView._original_get(self)
                except Exception as e:
                    # If there's a connection error, return empty queues instead of crashing
                    import tornado.web
                    error_msg = str(e).lower()
                    if 'ssl' in error_msg or 'certificate' in error_msg or 'certificate_verify_failed' in error_msg:
                        # Return empty queues on SSL error - this prevents UnboundLocalError
                        return self.render('broker.html', queues={})
                    raise
            
            BrokerView.get = patched_get
        
        # Also patch the get_queues call directly in BrokerView
        # This ensures queues is always defined even if get_queues() fails
        if hasattr(BrokerView, 'get') and not hasattr(BrokerView, '_dibba_patched_broker'):
            original_get_method = BrokerView.get
            
            def safe_get(self):
                # Initialize queues to empty dict to prevent UnboundLocalError
                queues = {}
                try:
                    # Try to get queues from broker
                    from flower import broker
                    broker_url = self.application.options.broker
                    queues = broker.get_queues(broker_url) or {}
                except Exception as e:
                    error_msg = str(e).lower()
                    if 'ssl' in error_msg or 'certificate' in error_msg or 'certificate_verify_failed' in error_msg:
                        # SSL error - use empty queues
                        queues = {}
                    else:
                        # Other error - still use empty queues to prevent crash
                        queues = {}
                
                # Render with queues (always defined now)
                return self.render('broker.html', queues=queues)
            
            BrokerView.get = safe_get
            BrokerView._dibba_patched_broker = True
    except ImportError:
        pass
    
    # Patch Flower's broker module directly - this is the key fix
    try:
        import flower.broker as flower_broker
        
        # Patch get_queues function
        if hasattr(flower_broker, 'get_queues') and not hasattr(flower_broker, '_original_get_queues'):
            flower_broker._original_get_queues = flower_broker.get_queues
            
            def patched_get_queues(broker_url):
                try:
                    return flower_broker._original_get_queues(broker_url)
                except Exception as e:
                    error_msg = str(e).lower()
                    if 'ssl' in error_msg or 'certificate' in error_msg or 'certificate_verify_failed' in error_msg:
                        # Return empty dict on SSL error to prevent crash and UnboundLocalError
                        return {}
                    # For any other error, also return empty dict to prevent UnboundLocalError
                    return {}
            
            flower_broker.get_queues = patched_get_queues
        
        # Also patch the broker connection creation
        if hasattr(flower_broker, 'get_broker') and not hasattr(flower_broker, '_original_get_broker'):
            flower_broker._original_get_broker = flower_broker.get_broker
            
            def patched_get_broker(app):
                try:
                    broker = flower_broker._original_get_broker(app)
                    # If it's a Redis broker, patch its connection
                    if hasattr(broker, 'connection_pool') and hasattr(broker.connection_pool, 'connection_kwargs'):
                        conn_kwargs = broker.connection_pool.connection_kwargs
                        if conn_kwargs.get('ssl') or 'ssl' in str(conn_kwargs):
                            conn_kwargs['ssl_cert_reqs'] = ssl.CERT_NONE
                            conn_kwargs['ssl_check_hostname'] = False
                    return broker
                except Exception:
                    return flower_broker._original_get_broker(app)
            
            flower_broker.get_broker = patched_get_broker
    except ImportError:
        pass
    
    _patched = True


# Auto-patch on import
patch_redis_ssl()
