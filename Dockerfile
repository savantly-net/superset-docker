ARG BASE_TAG

# Since 7.0 the default apache/superset image is batteries-included: Postgres driver, Authlib (SSO),
# fastmcp (MCP server), Playwright + Chromium (reports/thumbnails), openpyxl, Pillow and the Google Sheets
# adapter are all in the base. This image only adds our config.
FROM apache/superset:${BASE_TAG:-7.0.0rc3}

COPY --chown=superset config/superset_config.py config/draft_hook.py config/franops_api.py /app/
COPY --chown=superset --chmod=755 config/franops-init.sh /app/franops-init.sh
ENV SUPERSET_CONFIG_PATH=/app/superset_config.py

# franops-bi, for `franops-bi roles apply` in the init task and the roles endpoint (S5.9).
# PyYAML is in the base, so the tool installs with no new dependency.
USER root
COPY tools/franops_bi /tmp/franops_bi
RUN uv pip install --python /app/.venv/bin/python --no-deps --no-cache /tmp/franops_bi \
    && rm -rf /tmp/franops_bi
USER superset

CMD ["/app/docker/entrypoints/run-server.sh"]
