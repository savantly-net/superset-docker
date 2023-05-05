# superset-docker
Superset docker image for Savantly projects

Preconfigured to support Postgres, Dremio, and Alerting


available in Dockerhub

```
docker pull savantly/superset
```

## Quickstart 

```
docker compose up
```

The Dremio connection URL should look something like this  -  

```
dremio+flight://superset_app:XXXXXXXXXX@dremio-client:32010/dremio?UseEncryption=false
```