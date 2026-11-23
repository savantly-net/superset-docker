"""Rig override. config/superset_config.py imports this file last.

This dict replaces FEATURE_FLAGS of config/superset_config.py, so it repeats TAGGING_SYSTEM,
which the shipped config also turns on (S5.5). The rig has no
Redis, so the caches are null and async queries are off.
"""

FEATURE_FLAGS = {
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "ALERT_REPORTS": False,
    "EMBEDDED_SUPERSET": True,
    "TAGGING_SYSTEM": True,
}
CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
FILTER_STATE_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
EXPLORE_FORM_DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
RATELIMIT_STORAGE_URI = "memory://"
GLOBAL_ASYNC_QUERIES = False

# The rig turns on EMBEDDED_SUPERSET, and Superset refuses to start with the default secret.
GUEST_TOKEN_JWT_SECRET = "rig-guest-secret-not-for-production-0123456789"
