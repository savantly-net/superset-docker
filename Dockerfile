FROM savantly/superset-ci

ARG DREMIO_ODBC_FOLDER=1.5.3.1000_2
ARG DREMIO_ODBC_VERSION=1.5.3.1000-2
ARG FIREFOX_VERSION=88.0
ARG GECKODRIVER_VERSION=v0.28.0

# Switching to root to install the required packages
USER root

# Install ODBC libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    unixodbc \
    unixodbc-dev \
    alien

# Using Postgres and Dremio
RUN pip install  --no-cache pyodbc psycopg2==2.8.5 redis==3.2.1 sqlalchemy_dremio

# Install Dremio ODBC driver
RUN wget https://download.dremio.com/odbc-driver/${DREMIO_ODBC_FOLDER}/dremio-odbc-${DREMIO_ODBC_VERSION}.x86_64.rpm 
RUN alien -i dremio-odbc-${DREMIO_ODBC_VERSION}.x86_64.rpm 
# Remove alien
RUN apt-get purge -y --auto-remove alien 

# Install dependencies for alerts/scheduling
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends libnss3 libdbus-glib-1-2 libgtk-3-0 libx11-xcb1

# Install GeckoDriver WebDriver
RUN wget https://github.com/mozilla/geckodriver/releases/download/${GECKODRIVER_VERSION}/geckodriver-${GECKODRIVER_VERSION}-linux64.tar.gz -O /tmp/geckodriver.tar.gz && \
    tar xvfz /tmp/geckodriver.tar.gz -C /tmp && \
    mv /tmp/geckodriver /usr/local/bin/geckodriver && \
    rm /tmp/geckodriver.tar.gz

# Install Firefox
RUN wget https://download-installer.cdn.mozilla.net/pub/firefox/releases/${FIREFOX_VERSION}/linux-x86_64/en-US/firefox-${FIREFOX_VERSION}.tar.bz2 -O /opt/firefox.tar.bz2 && \
    tar xvf /opt/firefox.tar.bz2 -C /opt && \
    ln -s /opt/firefox/firefox /usr/local/bin/firefox
    
RUN pip install --no-cache gevent

# Switching back to using the `superset` user
USER superset

COPY config/superset_config.py /app/pythonpath/superset_config.py