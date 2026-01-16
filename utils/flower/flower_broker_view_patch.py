"""
Direct patch for Flower's BrokerView to fix UnboundLocalError.

This patches the BrokerView.get method to ensure 'queues' is always defined,
even when get_queues() fails with SSL errors.
"""
import ssl

def patch_broker_view():
    """Patch Flower's BrokerView to handle SSL errors gracefully."""
    
    try:
        from flower.views.broker import BrokerView
        
        # Get the original get method
        if not hasattr(BrokerView, '_dibba_patched_view'):
            original_get = BrokerView.get
            
            def safe_get(self):
                """Safe version of get that always defines queues."""
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
            BrokerView._dibba_patched_view = True
            
    except ImportError:
        pass

# Auto-apply
patch_broker_view()

