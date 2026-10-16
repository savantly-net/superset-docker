ARG BASE_TAG

# Since 7.0 the default apache/superset image is batteries-included: Postgres driver, Authlib (SSO),
# fastmcp (MCP server), Playwright + Chromium (reports/thumbnails), openpyxl, Pillow and the Google Sheets
# adapter are all in the base. Only the SQL Server driver is added here.
FROM apache/superset:${BASE_TAG:-7.0.0rc1}

USER root
# Same range as the upstream `mssql` extra; the wheel bundles FreeTDS, so no apt packages are needed.
RUN . /app/.venv/bin/activate && uv pip install "pymssql>=2.3.13,<3"
USER superset

COPY --chown=superset config/superset_config.py /app/
ENV SUPERSET_CONFIG_PATH=/app/superset_config.py

CMD ["/app/docker/entrypoints/run-server.sh"]
