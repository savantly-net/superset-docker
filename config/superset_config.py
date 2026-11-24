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
import urllib.request
from datetime import timedelta
from typing import Optional
from pathlib import Path

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


def download_logo_from_url(logo_url: str) -> str:
    """Download logo from URL and return local path."""
    try:
        # Create logos directory if it doesn't exist
        logos_dir = Path("/app/superset/static/assets/images/")
        logos_dir.mkdir(parents=True, exist_ok=True)

        # Extract filename from URL or use default
        filename = logo_url.split("/")[-1]
        if not filename or "." not in filename:
            filename = "app_logo.png"

        local_path = logos_dir / filename

        # Download the image with custom User-Agent
        # This allows users to whitelist this agent in Cloudflare/WAF rules
        request = urllib.request.Request(
            logo_url,
            headers={"User-Agent": "Superset-Logo-Fetcher/1.0"}
        )
        with urllib.request.urlopen(request) as response:
            with open(local_path, "wb") as f:
                f.write(response.read())

        logger.info(f"Downloaded logo from {logo_url} to {local_path}")

        return str(local_path)
    except Exception as e:
        logger.error(f"Failed to download logo from {logo_url}: {e}")
        return None


# Handle APP_ICON environment variable
# Only set APP_ICON if we successfully download the custom logo.
# If download fails, don't set APP_ICON at all so Superset uses its built-in default.
# Setting APP_ICON to None causes a crash in Superset's "show_watermark" check.
app_logo_url = get_env_variable("APP_ICON", "")

if app_logo_url:
    downloaded_logo_path = download_logo_from_url(app_logo_url)
    if downloaded_logo_path:
        # Strip /app/superset prefix to make path relative to web server root
        relative_path = downloaded_logo_path.replace("/app/superset", "")
        APP_ICON = relative_path
        logger.info(f"APP_ICON set to: {APP_ICON}")
    else:
        logger.warning("Failed to download APP_ICON, using default logo")


import json
import urllib.error
import urllib.parse
from superset.security import SupersetSecurityManager

# Env values are strings: "false" was truthy before this check existed.
enable_oauth = str(get_env_variable("OAUTH_ENABLED", "false")).strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------------------------
# Web authentication (S5.2, design/bi-image.md section 1): one generic OpenID Connect provider.
# The image reads the provider from its discovery document, so it holds no code for one
# provider. The provider gives the identity only. The BI role comes from the FranOps `userinfo`
# endpoint at login (spike T-5 check 2: the provider's tokens carry no role).
# ---------------------------------------------------------------------------------------------
if enable_oauth:
    # OIDC_ISSUER is the name in design/bi-image.md; OAUTH_ISSUER is the name of earlier deploys.
    oauth_issuer = (os.environ.get("OIDC_ISSUER") or get_env_variable("OAUTH_ISSUER")).rstrip("/")
    oauth_name = get_env_variable("OAUTH_NAME", "oidc")
    oauth_client_id = os.environ.get("OIDC_CLIENT_ID") or get_env_variable("OAUTH_CLIENT_ID")
    oauth_client_secret = get_env_variable("OAUTH_CLIENT_SECRET")
    oauth_token_key = get_env_variable("OAUTH_TOKEN_KEY", "access_token")
    # `openid` must stay in the scope: without it, the provider sends no ID token and Authlib
    # verifies none.
    oauth_scope = get_env_variable("OAUTH_SCOPE", "openid email profile")
    # The discovery document gives the authorize, token, userinfo, and JWKS endpoints. Authlib
    # reads it at the first login, not at start.
    oauth_server_metadata_url = get_env_variable(
        "OAUTH_SERVER_METADATA_URL", f"{oauth_issuer}/.well-known/openid-configuration"
    )

    from flask_appbuilder.security.manager import AUTH_OAUTH
    AUTH_TYPE = AUTH_OAUTH

    oauth_remote_app = {
        "api_base_url": oauth_issuer,
        "client_id": oauth_client_id,
        "client_secret": oauth_client_secret,
        "client_kwargs": {"scope": oauth_scope},
        "server_metadata_url": oauth_server_metadata_url,
    }
    # Optional overrides of the discovery document. Leave them unset for a provider whose
    # discovery document is correct.
    for _env, _key in (
        ("OAUTH_AUTHORIZATION_ENDPOINT", "authorize_url"),
        ("OAUTH_TOKEN_ENDPOINT", "access_token_url"),
        ("OAUTH_JWKS_URI", "jwks_uri"),
    ):
        if os.environ.get(_env):
            oauth_remote_app[_key] = os.environ[_env]

    OAUTH_PROVIDERS = [
        {
            "name": oauth_name,
            "token_key": oauth_token_key,
            "icon": "fa-address-card",
            "remote_app": oauth_remote_app,
        }
    ]


def _env_flag(name: str, default: bool = False) -> bool:
    """A boolean from the environment. Only 1, true, yes, or on (any case) is true."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# The service account of the guest-token broker (S5.8, T-4, decided 2026-11-16). Under OAuth,
# the API login with the `db` provider accepts this account only, and only while its one role
# is GUEST_ISSUER_ROLE. FranOps stores the same username in its BI connection.
BI_SERVICE_USERNAME = get_env_variable("BI_SERVICE_USERNAME", "franops_guest_issuer")
GUEST_ISSUER_ROLE = get_env_variable("GUEST_ISSUER_ROLE", "GuestIssuer")


def service_login_allowed(user, service_username: str, issuer_role: str) -> bool:
    """True when `user` may log in through the API with a password under OAuth.

    The user must be the service account, and its roles must be the issuer role and nothing
    else. A second role (Admin, for example) would make the password login a way around OAuth
    for that role, so it refuses the account.
    """
    if user is None or user.username != service_username:
        return False
    return [role.name for role in user.roles] == [issuer_role]


class LoginRefused(Exception):
    """The OAuth login is refused. FAB catches it, logs its text, and shows the login page.

    The text names the reason only. It never holds an email, a token, or the API key.
    """


# The BI role that the FranOps `userinfo` endpoint returns, mapped to the Superset role key.
# AUTH_ROLES_MAPPING maps the key to the Superset role. Each other value refuses the login.
FRANOPS_BI_ROLES = {"HQ": "HQ"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib sends the Authorization header on to a redirect target, so a redirect is refused:
    FRANOPS_URL must name the host that answers."""

    def redirect_request(self, *args, **kwargs):
        return None


_franops_opener = urllib.request.build_opener(_NoRedirect)


def franops_bi_role(sub: str) -> dict:
    """Ask FranOps for the BI role of the provider's user `sub`.

    GET {FRANOPS_URL}/api/data/bi/userinfo?sub=<sub>, with FRANOPS_BI_API_KEY as the Bearer
    (a key whose only scope is `data_bi:userinfo`). Returns the answer as a dict. Raises
    LoginRefused when the configuration is missing, when FranOps does not answer 200 in
    FRANOPS_TIMEOUT_SECONDS (a redirect included), or when the answer is not a JSON object.
    """
    base = os.environ.get("FRANOPS_URL", "").strip().rstrip("/")
    key = os.environ.get("FRANOPS_BI_API_KEY", "").strip()
    if not base or not key:
        raise LoginRefused("FRANOPS_URL or FRANOPS_BI_API_KEY is not set")
    try:
        timeout = float(os.environ.get("FRANOPS_TIMEOUT_SECONDS", "5"))
    except ValueError:
        timeout = 5.0
    url = f"{base}/api/data/bi/userinfo?{urllib.parse.urlencode({'sub': sub})}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            # Some edges refuse the default Python-urllib agent (spike T-5).
            "User-Agent": "franops-bi-login/1",
        },
    )
    try:
        with _franops_opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise LoginRefused(f"FranOps userinfo answered {response.status}")
            body = json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        raise LoginRefused(f"FranOps userinfo answered {err.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise LoginRefused(f"FranOps userinfo did not answer: {type(reason).__name__}") from None
    except ValueError:
        raise LoginRefused("FranOps userinfo answered with no JSON") from None
    if not isinstance(body, dict):
        raise LoginRefused("FranOps userinfo answered with no JSON object")
    return body


def oidc_user_info(claims: dict, franops: dict) -> dict:
    """The FAB userinfo of one login, from the provider's claims and the FranOps answer.

    The claims are those of the verified ID token (or of the provider's userinfo). They need
    `sub`. The username is `oidc:<sub>`. `email`, `given_name`, and `family_name` are used when
    present; `name` fills the first name when `given_name` is absent. `preferred_username` is
    never read. A FranOps role that is not in FRANOPS_BI_ROLES (`NONE`, for a franchisee)
    refuses the login.
    """
    sub = claims.get("sub")
    role_key = FRANOPS_BI_ROLES.get(str(franops.get("biRole", "")))
    if role_key is None:
        raise LoginRefused(f"FranOps biRole {str(franops.get('biRole'))[:16]!r} has no BI access")
    email = claims.get("email") or franops.get("email") or ""
    first_name = claims.get("given_name") or claims.get("name") or ""
    return {
        "username": f"oidc:{sub}",
        "email": email,
        "first_name": first_name,
        "last_name": claims.get("family_name") or "",
        "role_keys": [role_key],
    }


class CustomSsoSecurityManager(SupersetSecurityManager):
    def auth_user_db(self, username, password):
        """Under OAuth, the API password login is open to the service account only (S5.8).

        AUTH_API_LOGIN_ALLOW_MULTIPLE_PROVIDERS opens the `db` login to each user with a
        password (T-4). This check closes it again for each user except the service account.
        The password check of the parent still runs for the service account.
        """
        user = super().auth_user_db(username, password)
        if user is not None and not service_login_allowed(user, BI_SERVICE_USERNAME, GUEST_ISSUER_ROLE):
            logger.warning("api db login refused under oauth: %s is not the service account", user.username)
            return None
        return user

    def oauth_user_info(self, provider, response=None):
        """The user of one OAuth login (S5.2). Raises LoginRefused to refuse it.

        FAB passes the user info that this returns to auth_user_oauth outside its error handler,
        so a refusal raises: a None here would be a server error. FAB catches the exception in
        the callback, logs its text, and shows the login page with a message.

        Nothing here logs the claims, the email, a token, or the API key.
        """
        # Authlib verifies the ID token (signature from the provider's JWKS, issuer, audience,
        # nonce, expiry) and stores its claims in response["userinfo"].
        claims = (response or {}).get("userinfo")
        if not claims:
            # No ID token: the provider's userinfo endpoint, called with the access token.
            claims = self.appbuilder.sm.oauth_remotes[provider].userinfo()
        sub = str((claims or {}).get("sub") or "").strip()
        if not sub:
            raise LoginRefused("the provider's claims have no sub")
        try:
            userinfo = oidc_user_info(dict(claims), franops_bi_role(sub))
            # ab_user.email is unique. FAB's insert of a second user with the same email fails,
            # and FAB logs the database error, which holds the email. Refuse first.
            if userinfo["email"]:
                holder = self.find_user(email=userinfo["email"])
                if holder is not None and holder.username != userinfo["username"]:
                    raise LoginRefused("another Superset user has the same email")
        except LoginRefused as err:
            logger.warning("oauth login refused: provider=%s sub=%s: %s", provider, sub, err)
            raise
        logger.info("oauth login: provider=%s sub=%s role_keys=%s", provider, sub, userinfo["role_keys"])
        return userinfo


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
    "ALERT_REPORTS": True,
    # The `draft` and `certified` tags (bi-image.md, S5.5). Off, an import writes no tag.
    "TAGGING_SYSTEM": True,
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
# Superset 6.x reads this on /login/ whenever registration is on and auth is not OAuth (apache/superset#39364);
# the default config does not define it, so DB-auth mode 500s without this. Empty = no captcha.
RECAPTCHA_PUBLIC_KEY = ""

if enable_oauth:
    CUSTOM_SECURITY_MANAGER = CustomSsoSecurityManager
    # S5.2: an OAuth user is created at the first login, and its roles are set again at each
    # login from the FranOps answer (`role_keys`). These are fixed under OAuth: the env value
    # of AUTH_USER_REGISTRATION is a string, and "false" would be truthy.
    AUTH_USER_REGISTRATION = True
    AUTH_ROLES_MAPPING = {"HQ": ["HQ"]}
    AUTH_ROLES_SYNC_AT_LOGIN = True
    # FAB adds the registration role to each OAuth user. `Public` holds no permission (the
    # lockdown check reads it), so the registration role grants nothing.
    AUTH_USER_REGISTRATION_ROLE = "Public"
    # The broker logs in with the `db` provider while web login is OAuth (T-4). The flag and
    # the auth_user_db limit above are set together, and only here: without the limit, the
    # flag opens the API login to each user with a password.
    AUTH_API_LOGIN_ALLOW_MULTIPLE_PROVIDERS = True

# ---------------------------------------------------------------------------------------------
# Embedding (S5.8, design/bi-image.md section 3). EMBEDDED_SUPERSET=true turns it on, and it
# then needs GUEST_TOKEN_JWT_SECRET and FRANOPS_ORIGIN. Off, the image behaves as before.
# ---------------------------------------------------------------------------------------------
# The guest role. `can_read` on Chart and Dashboard, and `datasource_access` on the scorecard
# datasets (T-4). Superset's default is Public, which holds no permission here.
GUEST_ROLE_NAME = "Embed"
GUEST_TOKEN_JWT_EXP_SECONDS = 300

embedding_enabled = _env_flag("EMBEDDED_SUPERSET")
FEATURE_FLAGS["EMBEDDED_SUPERSET"] = embedding_enabled
# No Redis on the rig: ALERT_REPORTS=false turns the reports scheduler off.
FEATURE_FLAGS["ALERT_REPORTS"] = _env_flag("ALERT_REPORTS", True)

if embedding_enabled:
    import copy

    from superset.config import CORS_OPTIONS as _DEFAULT_CORS_OPTIONS  # noqa: E402
    from superset.config import TALISMAN_CONFIG as _DEFAULT_TALISMAN_CONFIG  # noqa: E402

    # Superset refuses to start with its default secret while embedding is on. A missing
    # variable stops the start here, with its name.
    GUEST_TOKEN_JWT_SECRET = get_env_variable("GUEST_TOKEN_JWT_SECRET")

    # The origins that may frame BI: FranOps, and each external host (FR-6.5), separated by
    # spaces or commas.
    embed_origins = [get_env_variable("FRANOPS_ORIGIN").strip()] + [
        origin for origin in get_env_variable("EMBED_ORIGINS", "").replace(",", " ").split() if origin
    ]
    for origin in embed_origins:
        if not origin.startswith(("https://", "http://")) or origin.count("/") != 2:
            raise EnvironmentError(f"embed origin {origin!r} must be a scheme and a host only")

    TALISMAN_CONFIG = copy.deepcopy(_DEFAULT_TALISMAN_CONFIG)
    TALISMAN_CONFIG["content_security_policy"]["frame-ancestors"] = ["'self'", *embed_origins]
    # frame-ancestors decides who may frame BI. X-Frame-Options SAMEORIGIN would block each
    # other origin in a browser that reads it first.
    TALISMAN_CONFIG["frame_options"] = None

    CORS_OPTIONS = copy.deepcopy(_DEFAULT_CORS_OPTIONS)
    CORS_OPTIONS["origins"] = [*CORS_OPTIONS.get("origins", []), *embed_origins]


# Email configuration
EMAIL_NOTIFICATIONS = True
SMTP_HOST = os.getenv("SMTP_HOST","smtp.gmail.com")
SMTP_STARTTLS = ast.literal_eval(os.getenv("SMTP_STARTTLS", "True"))
SMTP_SSL = ast.literal_eval(os.getenv("SMTP_SSL", "False"))
SMTP_USER = os.getenv("SMTP_USER","superset")
SMTP_PORT = os.getenv("SMTP_PORT",587)
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD","superset")
SMTP_MAIL_FROM = os.getenv("SMTP_MAIL_FROM","noreply@savantly.cloud")

# ---------------------------------------------------------------------------------------------
# Lockdown (S5.4, NFR-2): the public endpoint accepts only a session or a guest token.
# scripts/public-lockdown-check.sh proves it on each build.
# ---------------------------------------------------------------------------------------------
# An anonymous request runs as `Public`. `Public` copies no other role, so it holds no
# permission. Do not set PUBLIC_ROLE_LIKE: it opens each copied permission to the internet.
AUTH_ROLE_PUBLIC = "Public"
PUBLIC_ROLE_LIKE = None

# The guest-token route takes a Bearer access token, not a session, so it cannot carry a CSRF
# token. Without this exemption, a Bearer-only caller gets 400 (spike T-4). The route still
# needs `can_grant_guest_token`, so an anonymous call gets 401.
from superset.config import WTF_CSRF_EXEMPT_LIST as _DEFAULT_CSRF_EXEMPT  # noqa: E402

WTF_CSRF_EXEMPT_LIST = [*_DEFAULT_CSRF_EXEMPT, "superset.security.api.guest_token"]


def _clause_is_contained(clause: str) -> bool:
    """True when the clause cannot break out of the parentheses that Superset puts around it.

    Superset writes each RLS clause into the query as `(<clause>)` and does not check it
    (security/api.py, a TODO). `a = 'A') OR (1=1` balances its parentheses but closes the outer
    one early, so the depth goes below zero. Text inside single quotes (a string) and double
    quotes (an identifier) is skipped; a doubled quote escapes it. A statement separator or a
    comment outside quotes is refused too.

    The scanner models only those two quote forms, so it refuses each character that starts
    another PostgreSQL form: a backslash (`E'\''` ends the string at a place the scanner does not
    see) and a dollar sign (`$$'$$` is a string that holds a quote). This also refuses a clause
    that uses them for a good reason, which is the safe direction.

    Superset renders the clause as a Jinja template before it wraps it (connectors/sqla/models.py,
    ENABLE_TEMPLATE_PROCESSING is on here), so `{{ 'a = 0) OR (1=1' }}` is quoted text to this
    scanner and SQL after the render. Each Jinja delimiter is refused, inside quotes too.
    """
    if "\\" in clause or "$" in clause:
        return False
    if "{{" in clause or "{%" in clause or "{#" in clause:
        return False
    depth = 0
    i = 0
    quote = None  # the open quote character, ' or "
    while i < len(clause):
        ch = clause[i]
        if quote:
            if ch == quote:
                if i + 1 < len(clause) and clause[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
        elif ch == ";" or clause.startswith("--", i) or clause.startswith("/*", i):
            return False
        i += 1
    return depth == 0 and quote is None


def guest_token_validator(body: dict) -> bool:
    """GUEST_TOKEN_VALIDATOR_HOOK: refuse a guest token that would not filter its rows.

    Spike T-4 measured each of these refusals as a token that returns every row:
    - an empty `rls` list;
    - a rule with a `dataset` ID (a wrong ID makes BI drop the rule with no error);
    - a clause that is empty, or that breaks out of its parentheses.
    Set GUEST_TOKEN_REQUIRE_RLS=false only for an instance whose guest tokens may see all rows.
    """
    rules = body.get("rls") or []
    if not rules:
        logger.warning("guest token refused: no rls rule")
        return False
    for rule in rules:
        if rule.get("dataset") is not None:
            logger.warning("guest token refused: an rls rule names a dataset")
            return False
        clause = str(rule.get("clause") or "").strip()
        if not clause or not _clause_is_contained(clause):
            logger.warning("guest token refused: an rls clause is empty or not contained")
            return False
    return True


def _rls_check_is_off(value) -> bool:
    """Only an explicit false value turns the check off. An empty value, a typo, or any other
    value keeps it on, so a bad template cannot open every row with no warning."""
    return str(value).strip().lower() in ("0", "false", "no", "off")


if not _rls_check_is_off(get_env_variable("GUEST_TOKEN_REQUIRE_RLS", "true")):
    GUEST_TOKEN_VALIDATOR_HOOK = guest_token_validator

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
