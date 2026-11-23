"""Rig override. config/superset_config.py imports this file last.

The rig has no Redis, so the caches are null and async queries are off. Everything else comes
from the shipped config and the environment in rig/compose.yml: embedding, the guest role, and
the feature flags (S5.8). Do not set them here, or the rig tests pass on an image that does not
set them.
"""

CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
FILTER_STATE_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
EXPLORE_FORM_DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
RATELIMIT_STORAGE_URI = "memory://"
GLOBAL_ASYNC_QUERIES = False
