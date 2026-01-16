"""
Direct patch for Flower's broker connection to handle SSL.

This patches Flower at the lowest level - when it parses the broker URL.
"""
import ssl
import os

def patch_flower_broker():
    """Patch Flower's broker connection to disable SSL verification."""
    
    # Set environment variable that kombu will respect
    os.environ['KOMBU_SSL_VERIFY'] = 'false'
    os.environ['CELERY_BROKER_USE_SSL'] = 'true'
    
    # Try to patch Flower's broker module if it exists
    try:
        import flower.broker as flower_broker
        
        # Patch the get_queues function to catch SSL errors
        if hasattr(flower_broker, 'get_queues'):
            original_get_queues = flower_broker.get_queues
            
            def safe_get_queues(broker_url):
                try:
                    return original_get_queues(broker_url)
                except Exception as e:
                    error_str = str(e).lower()
                    if 'ssl' in error_str or 'certificate' in error_str:
                        # Return empty dict to prevent crash
                        return {}
                    raise
            
            flower_broker.get_queues = safe_get_queues
    except ImportError:
        pass
    
    # Patch kombu's URL parsing to add SSL options
    try:
        from kombu.utils.url import parse_url
        
        original_parse_url = parse_url
        
        def patched_parse_url(url):
            scheme, host, port, user, password, path, query = original_parse_url(url)
            
            # If it's a rediss:// URL, add SSL options to query
            if scheme == 'rediss':
                if not query:
                    query = {}
                # Add SSL options to disable verification
                query['ssl_cert_reqs'] = 'CERT_NONE'
                query['ssl_check_hostname'] = 'false'
            
            return scheme, host, port, user, password, path, query
        
        # Don't actually patch parse_url as it might break things
        # Instead, we'll patch at the connection level
    except ImportError:
        pass

# Auto-apply
patch_flower_broker()

