#!/usr/bin/env bash
# Archive SheetTwin's data volume (uploaded workbooks + SheetIR databases + LLM logs) to ~/backups.
# Download the .tgz from JupyterLab's file browser (right-click > Download) to keep a copy off the VM.
set -euo pipefail
mkdir -p "$HOME/backups"
out="sheettwin-data-$(date +%Y%m%d-%H%M).tgz"
docker run --rm -v sheettwin_sheettwin-data:/data:ro -v "$HOME/backups:/out" busybox:1.37.0 tar czf "/out/$out" -C /data .
echo "$HOME/backups/$out"
# Restore on a new VM:  docker volume create sheettwin_sheettwin-data &&
#   docker run --rm -v sheettwin_sheettwin-data:/data -v "$HOME/backups:/in" busybox:1.37.0 tar xzf /in/<file> -C /data
