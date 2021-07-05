import os

def get_env_variable(var_name, default=None):
    """Get the environment variable or raise exception."""
    try:
        return os.environ[var_name]
    except KeyError:
        if default is not None:
            return default
        else:
            error_msg = 'The environment variable {} was missing, abort...' \
                .format(var_name)
            raise EnvironmentError(error_msg)

SUPERSET_DB_URI = get_env_variable('SUPERSET_DB_URI')

SQLALCHEMY_DATABASE_URI = SUPERSET_DB_URI