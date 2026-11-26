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
git tag -a v7.0.0rc3 -m "Superset 7.0.0rc3 image"
git push origin v7.0.0rc3        # -> savantly/superset:7.0.0rc3 (built from apache/superset:7.0.0rc3)
```

`vX.Y.Z-N` publishes a rebuild on the same Superset version. Version tags are immutable. `latest` only moves
through the manual "Retag Docker image" workflow, after the version is proven in production, and never to a
release candidate. Pin deployments by digest (the publish run summary prints the pin string).

## Checks
CI runs nine checks on every event (pull request, push to `main`, tag, manual run) before any push to Docker Hub:

```
scripts/check-package-unmodified.sh apache/superset:7.0.0rc3 savantly/superset:ci-check
scripts/test-package-check.sh 7.0.0rc3
scripts/smoke-test.sh savantly/superset:ci-check
scripts/public-lockdown-check.sh savantly/superset:ci-check
scripts/test-lockdown-check.sh savantly/superset:ci-check
scripts/public-lockdown-check.sh savantly/superset:ci-check 240 "" scripts/deployed-shape.env
scripts/guest-login-check.sh savantly/superset:ci-check
scripts/test-guest-login-check.sh savantly/superset:ci-check
RIG_SKIP_BUILD=1 rig/rls-check.sh     # after: docker tag savantly/superset:ci-check franops-bi-rig/superset:local
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
- The second lockdown run passes `scripts/deployed-shape.env`: OAuth and embedding on, with placeholder values.
- `guest-login-check.sh` starts the image in the same shape and runs `scripts/guest_login_probe.py`. It sends
  the broker's own login body for six cases (spike T-4): the service account gets `200`; another password
  user, an OAuth user, a `GuestIssuer` user with Admin, a password user with the `ldap` provider, and the
  service account with a second role are refused. It also reads the embed settings below from the app and from the `/login/` response.
- `test-guest-login-check.sh` breaks it three ways (Superset's own security manager, `GUEST_ROLE_NAME =
  "Public"`, the default Talisman settings) and fails unless the check fails on each.
- `rig/rls-check.sh` is the automated RLS check (S5.6, NFR-8). It starts the rig, deploys
  `tests/fixtures/bi` with `franops-bi`, and `rig/rig.sh rls-plan` finds each chart tagged `certified`.
  `scripts/rls_check.py` logs in as the service account, mints a guest token for franchisee A
  (`location_key IN ('loc-001')`) on each dashboard that holds a certified chart, and calls
  `POST /api/v1/chart/data`. It fails if a row has a key of another franchisee (exit 3), or if it cannot
  judge (exit 1): no certified chart, a refused login or mint, a chart that answers other than `200`, an
  empty answer, or rows with no `location_key`. It runs with OAuth off, then with OAuth on, on the same
  metadata database. Its negative test turns the validator off (`GUEST_TOKEN_REQUIRE_RLS=false`), mints a
  token with no `rls` rule, and fails unless the check exits 3. `scripts/rls_check.py` takes the URL, the
  plan, the clause and the allowed keys as inputs. Its only planner is the rig's `rls-plan`, which also
  rewrites the `GuestIssuer` and `Embed` roles and the service account, so a plan for another BI is
  hand-made for now.

## Base image watch
`.github/workflows/base-image-watch.yml` runs `scripts/base_image_watch.py` each day (S5.7, NFR-7: each
security patch within 14 days). When Docker Hub has a newer `apache/superset` tag, it pushes
`base-image/<tag>`, moves every pin (`Dockerfile`, `rig/compose.yml`, `deploy.sh`, the package-check
fixture), and opens a pull request. The candidate is a final `7.x.y` release newer than the pin, or, while
the pin is a release candidate, a later candidate of the same version. A `-dev`, `-lean` or `-pyNNN` variant
never counts. An existing branch for the tag stops a second pull request.

A pull request that `GITHUB_TOKEN` opens starts no `pull_request` run. The watch therefore dispatches
`docker-publish.yml` on the branch with `check_only=true`: every check above runs, the RLS check included,
and nothing is published. Merging the pull request publishes nothing either. Tests:
`python3 scripts/test_base_image_watch.py`.

## Embedding and the service login
`EMBEDDED_SUPERSET=true` turns embedding on (S5.8). Off, the image behaves as before. On, it needs:
- `GUEST_TOKEN_JWT_SECRET`, the key of the guest tokens. The start stops with the variable's name when it is
  missing.
- `FRANOPS_ORIGIN`, and optionally `EMBED_ORIGINS` (spaces or commas): the origins that may frame BI. Each is a
  scheme and a host only. They go into `frame-ancestors` and into the CORS origins. The image sends no
  `X-Frame-Options` header while embedding is on, because `SAMEORIGIN` would block FranOps.

The guest role is `Embed` and a guest token lives 300 seconds, on and off.

With `OAUTH_ENABLED=true`, web login is OAuth only, and the API login with the `db` provider is open to one
account: `BI_SERVICE_USERNAME` (default `franops_guest_issuer`), while its only role is `GUEST_ISSUER_ROLE`
(default `GuestIssuer`). The FranOps broker logs in this way to mint guest tokens. Each other user gets `401`.
Give the service account a password, the `GuestIssuer` role, and nothing else. A second role closes its login.
`ALERT_REPORTS=false` turns the reports scheduler off, for an instance with no Redis.

## Lockdown
`superset_config.py` keeps the public endpoint to sessions and guest tokens:
- `Public` holds no permission (`AUTH_ROLE_PUBLIC = "Public"`, no `PUBLIC_ROLE_LIKE`).
- `superset.security.api.guest_token` is CSRF-exempt, so a service account can mint a guest token with a
  Bearer access token alone. The route still needs `can_grant_guest_token`.
- `GUEST_TOKEN_VALIDATOR_HOOK` refuses a guest token with no RLS rule, a rule that names a `dataset`, or a
  clause that is empty or breaks out of its parentheses. Each of these returns every row. The clause check
  skips single-quoted strings and double-quoted identifiers, and refuses any `\` or `$`, because
  PostgreSQL's `E'…'` and `$$…$$` strings can end a quote where the check does not see it. It also refuses
  each Jinja delimiter (`{{`, `{%`, `{#`), because Superset renders the clause as a template first.
- `GUEST_TOKEN_REQUIRE_RLS` turns the check off only for `false`, `0`, `no`, or `off` (any case, spaces
  trimmed). Use one of these only on an instance whose guest tokens may see all rows. Each other value,
  an empty one included, keeps the check on.
- Before you deploy this image onto an existing database, read the `Public` role in the UI and remove each
  permission it holds. `PUBLIC_ROLE_LIKE = None` stops `superset init` from adding permissions, but it does
  not remove the ones already stored. The lockdown check runs on a new database, so it cannot see them.

## Certified assets: `franops-bi`
A brand repository keeps its certified datasets, charts, and dashboards in `bi/`, in Superset's export
format: the exploded ZIP of `GET /api/v1/assets/export/` without its root folder (`metadata.yaml`,
`databases/`, `datasets/`, `charts/`, `dashboards/`). `tests/fixtures/bi/` is an example, exported from the
rig by `rig/make_fixture.py`.

```
pip install -e tools/franops_bi
franops-bi validate --dir bi/
SUPERSET_PASSWORD=... FRANOPS_BI_DB_PASSWORDS='{"databases/warehouse.yaml": "..."}' \
  franops-bi deploy --dir bi/ --url https://bi.example.com --username deployer
```

- `validate` parses each YAML and exits 1 on a defect: a file that does not parse, a bad `metadata.yaml`,
  a missing required key, a duplicate `uuid`, a reference to an asset that is not in the folder, or `tags`
  on a database or a dataset.
- `deploy` runs the same check, then rewrites a copy of each asset YAML and imports it with
  `POST /api/v1/assets/import/` and `overwrite=true`. The source folder does not change. Each asset gets
  `is_managed_externally: true`. Each chart and dashboard gets `tags: [certified]`, which replaces its tag
  list: the import sets the object's tags to exactly that list.
- Superset 7.0.0rc2 cannot import a tag on a dataset or a database: their import schemas refuse the
  `tags` key, and the whole import fails. So `deploy` tags each dataset of the folder `certified` through
  the tag API after the import (`--no-tag-datasets` skips it). A later import keeps that tag. ⚠️ A deploy
  never removes a dataset tag, and the deploy account needs write on Tag.
- After the import, `deploy` reads the tags back and fails when a chart or a dashboard is not tagged
  `certified`. On 7.0.0rc2 the API cannot read a dataset's tags, so for a dataset the check is the `201`
  of the tag call.
- The export masks each database password. Give it with `FRANOPS_BI_DB_PASSWORDS` (a JSON map from
  `databases/<file>.yaml` to the password) or `--db-password databases/<file>.yaml=ENV_VAR`. An import onto
  an existing database keeps its stored password.
- Tags are written only with the `TAGGING_SYSTEM` feature flag on. `config/superset_config.py` turns it on.

### The rig
`rig/compose.yml` runs the image that this repository's Dockerfile builds (local tag
`franops-bi-rig/superset:local`, never pushed), a metadata Postgres, and a warehouse Postgres with the
fixture tables `marts.calls_daily` (S5.5) and `embed.scorecard_fixture` (cuid location keys, for the
fran-ops guest-token rig tests). Superset listens on `127.0.0.1:18388` (admin / `admin-rig-pass`). The
rig turns embedding on through the image's own variables (`EMBEDDED_SUPERSET`, `FRANOPS_ORIGIN`). Its
override sets only the caches, so the rig tests run on the image's embed settings (S5.8).

The fran-ops guest-token broker (S6.2) has its rig tests in fran-ops. On a rig that the S5.5 tests do
not need:

```
rig/rig.sh up && rig/rig.sh embed > /tmp/rig-embed.json
# in fran-ops:
FRANOPS_BI_RIG_EMBED=/tmp/rig-embed.json pnpm exec vitest run tests/integration/data-embed/guest-token-broker.rig.test.ts
```

`rig/embed/setup_embed.py` adds the dataset, an embedded dashboard, the `GuestIssuer` and `Embed` roles,
and the service account `franops_guest_issuer`.

```
pip install -e 'tools/franops_bi[test]'
python -m pytest tools/franops_bi/tests     # unit tests; the rig tests skip
rig/test-franops-bi.sh                      # an empty rig, the rig tests, then `down --volumes`
```

The rig tests deploy `tests/fixtures/bi` into an empty instance and compare the count of each asset type
with the folder (AC1). They deploy again and assert that nothing changed: the same id and uuid of each
asset, the same tag rows, the same export content, the same `changed_on`, and no new row in the
`*_version` history tables (AC2). A control deploy of a changed YAML shows that the import does write a
change. They assert the `certified` tag on each chart, dashboard, and dataset (AC3). `.github/workflows/franops-bi.yml`
runs the unit tests and `validate` on each pull request, and the rig tests after them.

## Quickstart

```
docker compose up
```
