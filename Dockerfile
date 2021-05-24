FROM apache/superset
# Switching to root to install the required packages
USER root

# Install ODBC libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    unixodbc \
    unixodbc-dev \
    alien

# Using Postgres and Dremio
RUN pip install pyodbc psycopg2==2.8.5 redis==3.2.1 sqlalchemy_dremio

# Install Dremio ODBC driver
RUN wget https://download.dremio.com/odbc-driver/dremio-odbc-LATEST.x86_64.rpm 
RUN alien -i dremio-odbc-LATEST.x86_64.rpm 

# Switching back to using the `superset` user
USER superset