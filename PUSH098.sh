#!/bin/bash
# FunnyPilot v0.9.8 Push Script

set -e

DEVICE_IP="192.168.86.31"
DEVICE_IP_TS="100.93.118.118"
DEVICE_USER="comma"
BRANCH="funnypilot-0.9.8"
TS_PROXY="/home/astro/bin/tailscale --socket=/home/astro/.local/share/tailscale/tailscaled.sock nc %h %p"

echo "========================================="
echo "FunnyPilot v0.9.8 Push Script"
echo "========================================="
echo ""
echo "Target Device: $DEVICE_USER@$DEVICE_IP (or $DEVICE_IP_TS via Tailscale)"
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
# Try home network first, fall back to Tailscale
SSH_TARGET="$DEVICE_USER@$DEVICE_IP"
SSH_OPTS="-o ConnectTimeout=5 -o StrictHostKeyChecking=no"
if ! ssh $SSH_OPTS "$SSH_TARGET" "echo 'Connection successful'" 2>/dev/null; then
  echo "Home network unreachable, trying Tailscale..."
  SSH_TARGET="$DEVICE_USER@$DEVICE_IP_TS"
  SSH_OPTS="$SSH_OPTS -o ProxyCommand=\"$TS_PROXY\""
  if ! eval "ssh $SSH_OPTS \"$SSH_TARGET\" \"echo 'Connection successful'\"" 2>/dev/null; then
    echo "ERROR: Cannot connect to device via home network or Tailscale."
    echo "  Home: $DEVICE_USER@$DEVICE_IP"
    echo "  Tailscale: $DEVICE_USER@$DEVICE_IP_TS"
    exit 1
  fi
  echo "Connected via Tailscale."
fi

echo ""
echo "Step 2: Pushing code to device..."
eval "ssh $SSH_OPTS \"$SSH_TARGET\" \"cd /data/openpilot && git fetch funnypilot\""
git push funnypilot "$BRANCH:$BRANCH" --force

echo ""
echo "Step 3: Updating device to $BRANCH..."
eval "ssh $SSH_OPTS \"$SSH_TARGET\" \"cd /data/openpilot && git checkout $BRANCH && git reset --hard funnypilot/$BRANCH\""

echo ""
echo "Step 4: Verifying FUNNYPILOT_VERSION..."
REMOTE_VERSION=$(eval "ssh $SSH_OPTS \"$SSH_TARGET\" \"cat /data/openpilot/FUNNYPILOT_VERSION\"")
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
eval "ssh $SSH_OPTS \"$SSH_TARGET\" \"sudo systemctl restart comma\""

echo ""
echo "========================================="
echo "✓ FunnyPilot v0.9.8 pushed successfully!"
echo "========================================="
echo ""
echo "The device will restart openpilot in a few seconds."
echo "Check the main screen for 'FunnyPilot 0.9.8' to verify."
