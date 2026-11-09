#!/usr/bin/env bash
# Runs ON the EC2 instance. The GitHub Actions workflow sends this script
# through AWS Systems Manager Run Command; it is not meant to be run by hand
# (though it works if you export the variables below first).
#
# Required environment:
#   IMAGE       full image reference to deploy, e.g. <registry>/devleap-ecr-alexk:<tag>
#   REGISTRY    the ECR registry host, e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com
#   AWS_REGION  AWS region of the registry and of Bedrock
# Optional:
#   HOST_PORT       port published on the instance (default 8001)
#   HEALTH_TIMEOUT  seconds to wait for the new version to become healthy (default 60)
#
# What it does: pull the image, replace the container, wait for /healthz, and
# if the new version isn't healthy put the previous one back and fail. The
# data volume (accounts, conversations, secret key) is never touched.

set -euo pipefail

: "${IMAGE:?IMAGE is required}" "${REGISTRY:?REGISTRY is required}" "${AWS_REGION:?AWS_REGION is required}"
NAME=bedrock-chat
HOST_PORT="${HOST_PORT:-8001}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"

echo "Deploying $IMAGE"

# Pull first: if this fails, the running container has not been touched.
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker pull "$IMAGE"

# Remember what is running now (by image ID) so we can put it back.
PREVIOUS_ID="$(docker inspect --format '{{.Image}}' "$NAME" 2>/dev/null || true)"

start() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "$HOST_PORT:8000" \
    -e AWS_REGION="$AWS_REGION" \
    -v bedrock-chat-data:/data \
    "$1" >/dev/null
}

healthy() {
  local i
  for ((i = 0; i < HEALTH_TIMEOUT; i++)); do
    if curl -fsS "http://127.0.0.1:$HOST_PORT/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start "$IMAGE"
if healthy; then
  echo "New version is healthy."
  # Remove unused images older than 3 days (keeps the 8 GiB disk from filling up).
  docker image prune -af --filter "until=72h" >/dev/null || true
  docker ps --filter "name=$NAME" --format 'Running: {{.Names}}  {{.Image}}  {{.Status}}'
  exit 0
fi

echo "New version did NOT become healthy within ${HEALTH_TIMEOUT}s. Last log lines:"
docker logs --tail 30 "$NAME" 2>&1 || true

if [ -n "$PREVIOUS_ID" ]; then
  echo "Rolling back to the previous image ($PREVIOUS_ID)."
  start "$PREVIOUS_ID"
  if healthy; then
    echo "Rolled back: the previous version is running again."
  else
    echo "WARNING: the previous version is not healthy either. Check the instance."
  fi
else
  echo "There was no previous container to roll back to."
fi
exit 1
