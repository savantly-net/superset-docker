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

## Quickstart

```
docker compose up
```
