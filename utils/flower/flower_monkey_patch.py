"""
Monkey patch for Flower to fix SSL certificate verification issues.

This is a more aggressive patch that runs at import time before anything else.
Place this at the very top of your Flower startup script.
"""
import sys
import ssl

# Patch SSL module to disable verification globally (last resort)
_original_ssl_wrap_socket = None
_original_create_default_context = None


def patch_ssl_module():
    """Patch SSL module to disable certificate verification."""
    global _original_ssl_wrap_socket, _original_create_default_context
    
    # This is a very aggressive patch - only use if other methods fail
    # It patches the SSL module itself to always disable verification
    
    try:
        import ssl as ssl_module
        
        if not _original_ssl_wrap_socket:
            _original_ssl_wrap_socket = ssl_module.wrap_socket
            
            def patched_wrap_socket(*args, **kwargs):
                # Force disable certificate verification
                kwargs['cert_reqs'] = ssl_module.CERT_NONE
                kwargs['check_hostname'] = False
                return _original_ssl_wrap_socket(*args, **kwargs)
            
            # Don't actually patch wrap_socket as it's deprecated
            # Instead, patch create_default_context
        
        if not _original_create_default_context:
            _original_create_default_context = ssl_module.create_default_context
            
            def patched_create_default_context(*args, **kwargs):
                ctx = _original_create_default_context(*args, **kwargs)
                # Disable verification in the context
                ctx.check_hostname = False
                ctx.verify_mode = ssl_module.CERT_NONE
                return ctx
            
            ssl_module.create_default_context = patched_create_default_context
    except Exception:
        pass


# Don't auto-patch SSL module - too aggressive
# Only patch Redis connections via flower_ssl_patch

