"""Flower configuration utilities for Dibba."""

# Import ALL patches on module import to ensure they're applied before Flower starts
# Order matters - patches must be applied in this order

# 1. Patch Redis first (lowest level)
try:
    from utils.flower.flower_ssl_patch import patch_redis_ssl
    patch_redis_ssl()
except ImportError:
    pass

# 2. Patch Flower's broker module
try:
    from utils.flower.flower_broker_patch import patch_flower_broker
    patch_flower_broker()
except ImportError:
    pass

# 3. Patch Flower's BrokerView (fixes UnboundLocalError)
try:
    from utils.flower.flower_broker_view_patch import patch_broker_view
    patch_broker_view()
except ImportError:
    pass
