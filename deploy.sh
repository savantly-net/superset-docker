#!/bin/bash

REPO_NAME=savantly/superset

IMAGE_LATEST=${REPO_NAME}:latest
#IMAGE_TAG=${REPO_NAME}:1.3.0

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR

docker build -t $IMAGE_LATEST .
#docker tag $IMAGE_LATEST $IMAGE_TAG

docker push $IMAGE_LATEST
#docker push $IMAGE_TAG