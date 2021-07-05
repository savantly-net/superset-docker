FROM apache/superset:latest
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
RUN wget https://download.dremio.com/odbc-driver/dremio-odbc-LATEST.x86_64.rpm 
RUN alien -i dremio-odbc-LATEST.x86_64.rpm 

# Install dependencies for alerts/scheduling
RUN apt-get update && \
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && \
    apt-get install -y --no-install-recommends ./google-chrome-stable_current_amd64.deb && \
    rm -f google-chrome-stable_current_amd64.deb

RUN export CHROMEDRIVER_VERSION=$(curl --silent https://chromedriver.storage.googleapis.com/LATEST_RELEASE_88) && \
    wget -q https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip && \
    unzip chromedriver_linux64.zip -d /usr/bin && \
    chmod 755 /usr/bin/chromedriver && \
    rm -f chromedriver_linux64.zip
    
RUN pip install --no-cache gevent


# Switching back to using the `superset` user
USER superset