# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# This file is included in the final Docker image and can be overridden when
# deploying the image to prod. Settings configured here are intended for use in local
# development environments. Also note that superset_config_docker.py is imported
# as a final step as a means to override "defaults" configured here
#
import ast
import logging
import os
from datetime import timedelta
from typing import Optional

from cachelib.file import FileSystemCache
from celery.schedules import crontab

logger = logging.getLogger()



def get_env_variable(var_name: str, default: Optional[str] = None) -> str:
    """Get the environment variable or raise exception."""
    try:
        return os.environ[var_name]
    except KeyError:
        if default is not None:
            return default
        else:
            error_msg = "The environment variable {} was missing, abort...".format(
                var_name
            )
            raise EnvironmentError(error_msg)


import json
from superset.security import SupersetSecurityManager

enable_oauth = get_env_variable("OAUTH_ENABLED", False)

if enable_oauth:
    oauthBaseUrl = get_env_variable("OAUTH_ISSUER")
    oauth_name = get_env_variable("OAUTH_NAME")
    oauth_client_id = get_env_variable("OAUTH_CLIENT_ID")
    oauth_client_secret = get_env_variable("OAUTH_CLIENT_SECRET")
    oauth_token_key = get_env_variable("OAUTH_TOKEN_KEY", "access_token")
    oauth_token_endpoint = get_env_variable("OAUTH_TOKEN_ENDPOINT")
    oauth_authorization_endpoint = get_env_variable("OAUTH_AUTHORIZATION_ENDPOINT")
    userinfo_endpoint = get_env_variable("OAUTH_USERINFO_ENDPOINT", "")
    # Path to roles in OAuth user info response (supports dot notation for nested paths, e.g., "user.roles" or "attributes.roles")
    oauth_roles_path = get_env_variable("OAUTH_ROLES_PATH", "roles")
    # OAuth scope parameter (space-separated scopes, e.g., "profile email roles")
    oauth_scope = get_env_variable("OAUTH_SCOPE", "openid")
    # JWKS URI for token validation (required for OpenID Connect)
    oauth_jwks_uri = get_env_variable("OAUTH_JWKS_URI", "")
    # Server metadata URL for OpenID Connect discovery
    oauth_server_metadata_url = get_env_variable("OAUTH_SERVER_METADATA_URL", "")
    
    from flask_appbuilder.security.manager import AUTH_OAUTH
    AUTH_TYPE = AUTH_OAUTH

    # Build OAuth provider configuration
    oauth_provider_config = {
        'name': oauth_name,
        'token_key': oauth_token_key, 
        'icon':'fa-address-card',   # Icon for the provider
        'remote_app': {
            'api_base_url': oauthBaseUrl,
            'client_id': oauth_client_id, 
            'client_secret': oauth_client_secret,
            'client_kwargs':{
                'scope': oauth_scope
            },
            'request_token_url':None,
            'access_token_url': oauth_token_endpoint,
            'authorize_url': oauth_authorization_endpoint,
        }
    }
    
    # Add JWKS URI if provided (required for OpenID Connect)
    if oauth_jwks_uri:
        oauth_provider_config['remote_app']['jwks_uri'] = oauth_jwks_uri
    
    # Add server metadata URL if provided (for OpenID Connect discovery)
    if oauth_server_metadata_url:
        oauth_provider_config['remote_app']['server_metadata_url'] = oauth_server_metadata_url

    OAUTH_PROVIDERS = [oauth_provider_config]



def get_nested_value(data, path, default=None):
    """Get a nested value from a dictionary using a dot-separated path."""
    keys = path.split('.')
    current = data
    try:
        for key in keys:
            current = current[key]
        return current
    except (KeyError, TypeError):
        return default


class CustomSsoSecurityManager(SupersetSecurityManager):
    def oauth_user_info(self, provider, response=None):
        logging.debug("Oauth2 provider: {0}.".format(provider))
        logging.debug("Oauth2 oauth_remotes provider: {0}.".format(self.appbuilder.sm.oauth_remotes[provider]))
        # Assuming all oauth connections are from keycloak
        # Get the user info using the access token
        res = self.appbuilder.sm.oauth_remotes[provider].get(userinfo_endpoint)
        logger.info(f"userinfo response:")
        for attr, value in vars(res).items():
            print(attr, '=', value)
        if res.status_code != 200:
            logger.error('Failed to obtain user info: %s', res._content)
            return
        #dict_str = res._content.decode("UTF-8")
        me = json.loads(res._content)
        logger.debug(" user_data: %s", me)
        
        # Get roles using configurable path
        user_roles = get_nested_value(me, oauth_roles_path, [])
        
        return {
            'username' : me['preferred_username'],
            'name' : me['name'],
            'email' : me['email'],
            'first_name': me['given_name'],
            'last_name': me['family_name'],
            'roles': user_roles,
            'is_active': True,
        }

    def auth_user_oauth(self, userinfo):
        user = super(CustomSsoSecurityManager, self).auth_user_oauth(userinfo)
        roles = [self.find_role(x) for x in userinfo['roles']]
        roles = [x for x in roles if x is not None]
        user.roles = roles
        logger.debug(' Update <User: %s> role to %s', user.username, roles)
        self.update_user(user)  # update user roles
        return user


SECRET_KEY = get_env_variable("SECRET_KEY")

# This will make sure the redirect_uri is properly computed, even with SSL offloading
ENABLE_PROXY_FIX = get_env_variable("ENABLE_PROXY_FIX", False)

DB_DIALECT = get_env_variable("DB_DIALECT")
DB_USER = get_env_variable("DB_USER")
DB_PASSWORD = get_env_variable("DB_PASSWORD")
DB_HOST = get_env_variable("DB_HOST")
DB_PORT = get_env_variable("DB_PORT")
DB_NAME = get_env_variable("DB_NAME")

# The SQLAlchemy connection string.
SQLALCHEMY_DATABASE_URI = "%s://%s:%s@%s:%s/%s" % (
    DB_DIALECT,
    DB_USER,
    DB_PASSWORD,
    DB_HOST,
    DB_PORT,
    DB_NAME,
)

REDIS_PROTOCOL = get_env_variable("REDIS_PROTOCOL", "redis")
REDIS_HOST = get_env_variable("REDIS_HOST")
REDIS_PORT = get_env_variable("REDIS_PORT")
REDIS_CELERY_DB = get_env_variable("REDIS_CELERY_DB", "0")
REDIS_RESULTS_DB = get_env_variable("REDIS_RESULTS_DB", "1")

RESULTS_BACKEND = FileSystemCache("/app/superset_home/sqllab")


class CeleryConfig(object):
    BROKER_URL = f"{REDIS_PROTOCOL}://{REDIS_HOST}:{REDIS_PORT}/{REDIS_CELERY_DB}"
    CELERY_IMPORTS = ("superset.sql_lab", "superset.tasks")
    CELERY_RESULT_BACKEND = f"{REDIS_PROTOCOL}://{REDIS_HOST}:{REDIS_PORT}/{REDIS_RESULTS_DB}"
    CELERYD_LOG_LEVEL = "DEBUG"
    CELERYD_PREFETCH_MULTIPLIER = 1
    CELERY_ACKS_LATE = False
    CELERYBEAT_SCHEDULE = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=10, hour=0),
        },
    }


CELERY_CONFIG = CeleryConfig

FEATURE_FLAGS = {
    "ENABLE_TEMPLATE_PROCESSING": get_env_variable('ENABLE_TEMPLATE_PROCESSING', True),
    "DASHBOARD_NATIVE_FILTERS": get_env_variable('DASHBOARD_NATIVE_FILTERS', True),
    "ALERT_REPORTS": True
}
ALERT_REPORTS_NOTIFICATION_DRY_RUN = True
WEBDRIVER_BASEURL = "http://superset:8088/"
# The base URL for the email report hyperlinks.
WEBDRIVER_BASEURL_USER_FRIENDLY = WEBDRIVER_BASEURL

SQLLAB_CTAS_NO_LIMIT = True

# Will allow user self registration, allowing to create Flask users from Authorized User
AUTH_USER_REGISTRATION = get_env_variable("AUTH_USER_REGISTRATION", True)

# The default user self registration role
AUTH_USER_REGISTRATION_ROLE = get_env_variable("AUTH_USER_REGISTRATION_ROLE", "Public")

if enable_oauth:
    from flask_appbuilder.security.manager import AUTH_OAUTH
    CUSTOM_SECURITY_MANAGER = CustomSsoSecurityManager


# Email configuration
EMAIL_NOTIFICATIONS = True
SMTP_HOST = os.getenv("SMTP_HOST","smtp.gmail.com")
SMTP_STARTTLS = ast.literal_eval(os.getenv("SMTP_STARTTLS", "True"))
SMTP_SSL = ast.literal_eval(os.getenv("SMTP_SSL", "False"))
SMTP_USER = os.getenv("SMTP_USER","superset")
SMTP_PORT = os.getenv("SMTP_PORT",587)
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD","superset")
SMTP_MAIL_FROM = os.getenv("SMTP_MAIL_FROM","noreply@savantly.cloud")

#
# Optionally import superset_config_docker.py (which will have been included on
# the PYTHONPATH) in order to allow for local settings to be overridden
#
try:
    import superset_config_docker
    from superset_config_docker import *  # noqa

    logger.info(
        f"Loaded your Docker configuration at " f"[{superset_config_docker.__file__}]"
    )
except ImportError:
    logger.info("Using default Docker config...")
