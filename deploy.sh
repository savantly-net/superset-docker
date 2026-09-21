#!/bin/bash

BASE_TAG=6.1.0
# During the 5.0.0 -> 6.1.0 upgrade only the version tag is pushed; `latest` stays on 5.0.0 until prod cutover.
# Revert to unconditional pushes after the cutover (PUSH_LATEST=true pushes it now).
PUSH_LATEST=${PUSH_LATEST:-false}

REPO_NAME=savantly/superset

IMAGE_LATEST=${REPO_NAME}:latest
IMAGE_TAG=${REPO_NAME}:${BASE_TAG}

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR

docker buildx build --platform=linux/amd64 --build-arg BASE_TAG=$BASE_TAG --load -t $IMAGE_LATEST -t $IMAGE_TAG .
if [ "$PUSH_LATEST" = "true" ]; then docker push $IMAGE_LATEST; fi
docker push $IMAGE_TAG
