#!/bin/bash
# FunnyPilot v1.0.0 offline/local deployment script

set -euo pipefail

DEVICE_IP="192.168.86.31"
DEVICE_USER="comma"
BRANCH="funnypilot-1.0.0"
REMOTE_REPO="/data/openpilot"
REMOTE_BUNDLE="/tmp/${BRANCH}.bundle"

echo "========================================="
echo "FunnyPilot v1.0.0 Push Script (LAN)"
echo "========================================="
echo "Target Device: ${DEVICE_USER}@${DEVICE_IP}"
echo "Branch: ${BRANCH}"
echo

CURRENT_BRANCH="$(git branch --show-current)"
if [ "${CURRENT_BRANCH}" != "${BRANCH}" ]; then
  echo "ERROR: Not on ${BRANCH} (currently on ${CURRENT_BRANCH})"
  echo "Run: git checkout ${BRANCH}"
  exit 1
fi

if ! git diff-index --quiet HEAD --; then
  echo "WARNING: You have uncommitted changes. They will be included in local bundle source only if committed."
  git status --short
  echo "Please commit desired changes before deploying."
  exit 1
fi

echo "Step 1: Testing local SSH connection..."
ssh -o ConnectTimeout=5 "${DEVICE_USER}@${DEVICE_IP}" "echo 'Connection successful'"

BUNDLE_PATH="$(mktemp -t ${BRANCH}.XXXXXX.bundle)"
trap 'rm -f "${BUNDLE_PATH}"' EXIT

echo
echo "Step 2: Creating git bundle..."
git bundle create "${BUNDLE_PATH}" "${BRANCH}"

echo
echo "Step 3: Copying bundle to device..."
scp "${BUNDLE_PATH}" "${DEVICE_USER}@${DEVICE_IP}:${REMOTE_BUNDLE}"

echo
echo "Step 4: Updating device repository from bundle..."
ssh "${DEVICE_USER}@${DEVICE_IP}" "cd ${REMOTE_REPO} && git fetch ${REMOTE_BUNDLE} ${BRANCH}:${BRANCH} && git checkout ${BRANCH} && git reset --hard ${BRANCH}"

echo
echo "Step 5: Verifying deployed version..."
REMOTE_VERSION="$(ssh "${DEVICE_USER}@${DEVICE_IP}" "cat ${REMOTE_REPO}/FUNNYPILOT_VERSION")"
LOCAL_VERSION="$(cat FUNNYPILOT_VERSION)"
if [ "${REMOTE_VERSION}" = "${LOCAL_VERSION}" ]; then
  echo "OK: Version matches (${REMOTE_VERSION})"
else
  echo "WARNING: Version mismatch"
  echo "  Local:  ${LOCAL_VERSION}"
  echo "  Remote: ${REMOTE_VERSION}"
fi

echo
echo "Step 6: Restarting comma service..."
ssh "${DEVICE_USER}@${DEVICE_IP}" "sudo systemctl restart comma && rm -f ${REMOTE_BUNDLE}"

echo
echo "========================================="
echo "Deployment complete"
echo "========================================="
