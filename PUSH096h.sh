#!/bin/bash
# FunnyPilot v0.9.6h Push Script
# This script pushes the 0.9.6h codebase to the device

set -e

DEVICE_IP="192.168.86.31"
DEVICE_USER="comma"
BRANCH="funnypilot-0.9.6"

echo "========================================="
echo "FunnyPilot v0.9.6h Push Script"
echo "========================================="
echo ""
echo "Target Device: $DEVICE_USER@$DEVICE_IP"
echo "Branch: $BRANCH"
echo ""

# Check if we're on the right branch
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
  echo "ERROR: Not on $BRANCH branch (currently on $CURRENT_BRANCH)"
  echo "Switch to the correct branch first: git checkout $BRANCH"
  exit 1
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD --; then
  echo "WARNING: You have uncommitted changes!"
  echo ""
  git status --short
  echo ""
  read -p "Continue anyway? (y/N) " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
  fi
fi

echo "Step 1: Testing SSH connection..."
if ! ssh -o ConnectTimeout=5 "$DEVICE_USER@$DEVICE_IP" "echo 'Connection successful'"; then
  echo "ERROR: Cannot connect to device at $DEVICE_IP"
  echo "Make sure the device is online and connected to the network."
  exit 1
fi

echo ""
echo "Step 2: Pushing code to device..."
ssh "$DEVICE_USER@$DEVICE_IP" "cd /data/openpilot && git fetch"
git push funnypilot "$BRANCH:$BRANCH" --force

echo ""
echo "Step 3: Updating device to $BRANCH..."
ssh "$DEVICE_USER@$DEVICE_IP" "cd /data/openpilot && git checkout $BRANCH && git reset --hard funnypilot/$BRANCH"

echo ""
echo "Step 4: Verifying FUNNYPILOT_VERSION..."
REMOTE_VERSION=$(ssh "$DEVICE_USER@$DEVICE_IP" "cat /data/openpilot/FUNNYPILOT_VERSION")
LOCAL_VERSION=$(cat FUNNYPILOT_VERSION)

if [ "$REMOTE_VERSION" = "$LOCAL_VERSION" ]; then
  echo "✓ Version matches: $REMOTE_VERSION"
else
  echo "WARNING: Version mismatch!"
  echo "  Local:  $LOCAL_VERSION"
  echo "  Remote: $REMOTE_VERSION"
fi

echo ""
echo "Step 5: Restarting openpilot services..."
ssh "$DEVICE_USER@$DEVICE_IP" "sudo systemctl restart comma"

echo ""
echo "========================================="
echo "✓ FunnyPilot v0.9.6h pushed successfully!"
echo "========================================="
echo ""
echo "The device will restart openpilot in a few seconds."
echo "Check the main screen for 'FunnyPilot 0.9.6h' to verify."
