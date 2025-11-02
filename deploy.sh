#!/bin/bash

BASE_TAG=5.0.0

REPO_NAME=savantly/superset

IMAGE_LATEST=${REPO_NAME}:latest
IMAGE_TAG=${REPO_NAME}:${BASE_TAG}

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR

docker buildx build --platform=linux/amd64 --load -t $IMAGE_LATEST -t $IMAGE_TAG .
docker push $IMAGE_LATEST 
docker push $IMAGE_TAG