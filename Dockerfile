FROM apache/superset:2.1.0

# Switching to root to install the required packages
USER root

# Install ODBC libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    unixodbc \
    unixodbc-dev \
    alien \
    wget

# Using Postgres and Dremio
RUN pip install  --no-cache pyodbc psycopg2==2.8.5 redis==3.2.1 sqlalchemy_dremio=3.0.3

# Install Dremio ODBC driver
ARG DREMIO_ODBC_PACKAGE_NAME=arrow-flight-sql-odbc-driver-LATEST.x86_64.rpm
ENV DREMIO_ODBC_URL=https://download.dremio.com/arrow-flight-sql-odbc-driver/${DREMIO_ODBC_PACKAGE_NAME}
RUN wget ${DREMIO_ODBC_URL}
RUN alien -i ${DREMIO_ODBC_PACKAGE_NAME}
# Remove alien
RUN apt-get purge -y --auto-remove alien 

# Install dependencies for alerts/scheduling
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends libnss3 libdbus-glib-1-2 libgtk-3-0 libx11-xcb1

# Install GeckoDriver WebDriver
ARG GECKODRIVER_VERSION=v0.28.0
RUN wget https://github.com/mozilla/geckodriver/releases/download/${GECKODRIVER_VERSION}/geckodriver-${GECKODRIVER_VERSION}-linux64.tar.gz -O /tmp/geckodriver.tar.gz && \
    tar xvfz /tmp/geckodriver.tar.gz -C /tmp && \
    mv /tmp/geckodriver /usr/local/bin/geckodriver && \
    rm /tmp/geckodriver.tar.gz

# Install Firefox
ARG FIREFOX_VERSION=88.0
RUN wget https://download-installer.cdn.mozilla.net/pub/firefox/releases/${FIREFOX_VERSION}/linux-x86_64/en-US/firefox-${FIREFOX_VERSION}.tar.bz2 -O /opt/firefox.tar.bz2 && \
    tar xvf /opt/firefox.tar.bz2 -C /opt && \
    ln -s /opt/firefox/firefox /usr/local/bin/firefox
    
RUN pip install --no-cache gevent

# Switching back to using the `superset` user
USER superset

COPY config/superset_config.py /app/pythonpath/superset_config.py