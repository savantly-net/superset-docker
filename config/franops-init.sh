#!/usr/bin/env sh
# The one-off init task of a BI instance (S5.9): the migrations, Superset's own roles, then the
# FranOps roles and the service account. Each step is safe to run again. Run it as the task's
# command, with the service's environment (BI_SERVICE_PASSWORD sets the service account).
set -eu
superset db upgrade
superset init
franops-bi roles apply
