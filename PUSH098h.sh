#!/bin/bash
set -e

BRANCH=$(git branch --show-current)
echo "=== Pushing $BRANCH to funnypilot remote ==="
git push funnypilot "$BRANCH:$BRANCH" --force

echo "=== Deploying to device via home network ==="
ssh comma@192.168.86.31 "
  set -e
  echo '--- Deploying $BRANCH ---'
  cd /data/openpilot
  git fetch funnypilot
  git checkout $BRANCH
  git reset --hard funnypilot/$BRANCH

  echo '--- Removing Tailscale ---'
  sudo systemctl stop tailscaled 2>/dev/null || true
  sudo systemctl disable tailscaled 2>/dev/null || true
  sudo rm -f /etc/systemd/system/tailscaled.service
  sudo rm -rf /etc/systemd/system/tailscaled.service.d
  sudo systemctl daemon-reload
  sudo rm -rf /data/tailscale
  echo 'Tailscale removed.'

  echo '--- Restarting comma service ---'
  sudo systemctl restart comma
  echo 'Done!'
"
