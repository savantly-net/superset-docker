# Negative fixture for scripts/check-package-unmodified.sh.
# This Dockerfile writes one file into each copy of the superset package: the copy that
# resolves from the working directory (/app/superset, which the server imports), and the
# installed copy in site-packages. The check must fail on it. Never publish this image.
ARG BASE_TAG=7.0.0rc2
FROM apache/superset:${BASE_TAG}
USER root
RUN . /app/.venv/bin/activate && \
    python -c "import importlib.util, os; root = importlib.util.find_spec('superset').submodule_search_locations[0]; open(os.path.join(root, 'fixture_runtime_patch.py'), 'w').write('# negative fixture\n')" && \
    python -I -c "import importlib.util, os; root = importlib.util.find_spec('superset').submodule_search_locations[0]; open(os.path.join(root, 'fixture_installed_patch.py'), 'w').write('# negative fixture\n')"
USER superset
