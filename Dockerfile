ARG BASE_TAG

# Since 7.0 the default apache/superset image is batteries-included: Postgres driver, Authlib (SSO),
# fastmcp (MCP server), Playwright + Chromium (reports/thumbnails), openpyxl, Pillow and the Google Sheets
# adapter are all in the base. This image only adds our config.
FROM apache/superset:${BASE_TAG:-7.0.0rc1}

COPY --chown=superset config/superset_config.py /app/
ENV SUPERSET_CONFIG_PATH=/app/superset_config.py

CMD ["/app/docker/entrypoints/run-server.sh"]
