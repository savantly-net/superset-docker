# superset-docker
Superset docker image for Savantly projects: `savantly/superset` on Docker Hub.

## What the image adds
Since Superset 7.0 the upstream default image is batteries-included (Postgres driver, Authlib for SSO, the
fastmcp MCP server, Playwright + Chromium for reports and thumbnails, Google Sheets). This image adds only
our `config/superset_config.py`; extra database drivers (e.g. `pymssql` for SQL Server) would go in a
`RUN . /app/.venv/bin/activate && uv pip install ...` step between `USER root` and `USER superset`.
Rebuilding from a different Superset version is `--build-arg BASE_TAG=<apache/superset tag>`.

## Publishing
Images are published by CI from git tags, never from merges:

```
git tag -a v7.0.0rc2 -m "Superset 7.0.0rc2 image"
git push origin v7.0.0rc2        # -> savantly/superset:7.0.0rc2 (built from apache/superset:7.0.0rc2)
```

`vX.Y.Z-N` publishes a rebuild on the same Superset version. Version tags are immutable. `latest` only moves
through the manual "Retag Docker image" workflow, after the version is proven in production, and never to a
release candidate. Pin deployments by digest (the publish run summary prints the pin string).

## Checks
CI runs five checks on every event (pull request, push to `main`, tag, manual run) before any push to Docker Hub:

```
scripts/check-package-unmodified.sh apache/superset:7.0.0rc2 savantly/superset:ci-check
scripts/test-package-check.sh 7.0.0rc2
scripts/smoke-test.sh savantly/superset:ci-check
scripts/public-lockdown-check.sh savantly/superset:ci-check
scripts/test-lockdown-check.sh savantly/superset:ci-check
```

- `check-package-unmodified.sh` compares both copies of the `superset` package (site-packages and
  `/app/superset`) in the built image with the base image, file by file, and fails on any change. At build
  time the image may add config, plugins and Python dependencies, but never write into the package. The check
  does not see runtime writes: with `APP_ICON` set, `superset_config.py` downloads the logo into
  `/app/superset/static/assets/images/`.
- `test-package-check.sh` builds `tests/fixtures/writes-into-package`, which writes into the package, and
  fails unless the check fails on it.
- `smoke-test.sh` starts the image with a throwaway Postgres and expects `200` from `/health`.
- `public-lockdown-check.sh` runs `superset init`, then sends one request with no credentials to each method
  of each `/api/v1` route that the app's URL map holds (305 routes in 27 groups on 7.0.0rc2). A route passes
  with `401` or `403`. A write that the CSRF check refuses first (`400`, or a redirect to `/login/`) passes
  too, and is counted apart. `scripts/lockdown_probe.py` names each other exception with its reason: the
  login, the database OAuth2 callback, and the routes that answer `404` while `THUMBNAILS` or
  `EMBEDDED_SUPERSET` is off. It also checks that the guest-token route answers `401` (the CSRF exemption)
  that the guest-token validator refuses its bad cases, that only an explicit false value of
  `GUEST_TOKEN_REQUIRE_RLS` turns the validator off, and that the `Public` role holds no permission. The
  last check reads the role itself: an anonymous write never reaches the permission check, so a `Public`
  write permission does not show in the requests.
- `test-lockdown-check.sh` breaks the lockdown three ways (`PUBLIC_ROLE_LIKE = "Gamma"`, no CSRF exemption,
  no validator hook) and fails unless the lockdown check fails on each.

## Lockdown
`superset_config.py` keeps the public endpoint to sessions and guest tokens:
- `Public` holds no permission (`AUTH_ROLE_PUBLIC = "Public"`, no `PUBLIC_ROLE_LIKE`).
- `superset.security.api.guest_token` is CSRF-exempt, so a service account can mint a guest token with a
  Bearer access token alone. The route still needs `can_grant_guest_token`.
- `GUEST_TOKEN_VALIDATOR_HOOK` refuses a guest token with no RLS rule, a rule that names a `dataset`, or a
  clause that is empty or breaks out of its parentheses. Each of these returns every row. The clause check
  skips single-quoted strings and double-quoted identifiers, and refuses any `\` or `$`, because
  PostgreSQL's `E'…'` and `$$…$$` strings can end a quote where the check does not see it. Set
  `GUEST_TOKEN_REQUIRE_RLS=false` only on an instance whose guest tokens may see all rows. Any other value,
  an empty one included, keeps the check on.

## Quickstart

```
docker compose up
```
