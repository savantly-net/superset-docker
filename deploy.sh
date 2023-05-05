#!/bin/bash

REPO_NAME=savantly/superset

IMAGE_LATEST=${REPO_NAME}:latest
IMAGE_TAG=${REPO_NAME}:2.1.0

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR

docker buildx build --platform=linux/amd64 --push -t $IMAGE_LATEST -t $IMAGE_TAG .