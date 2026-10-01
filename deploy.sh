#!/bin/bash

BASE_TAG=6.1.0
# Set PUSH_LATEST=false to publish only the version tag (e.g. while a new version is proven on dev first).
PUSH_LATEST=${PUSH_LATEST:-true}

REPO_NAME=savantly/superset

IMAGE_LATEST=${REPO_NAME}:latest
IMAGE_TAG=${REPO_NAME}:${BASE_TAG}

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR

docker buildx build --platform=linux/amd64 --build-arg BASE_TAG=$BASE_TAG --load -t $IMAGE_LATEST -t $IMAGE_TAG .
if [ "$PUSH_LATEST" = "true" ]; then docker push $IMAGE_LATEST; fi
docker push $IMAGE_TAG
