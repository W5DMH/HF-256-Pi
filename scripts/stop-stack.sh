#!/bin/bash
# HF-256 Stop Stack
# Stops all services cleanly

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

log "Stopping HF-256 stack..."

systemctl stop hf256 2>/dev/null && log "  [OK] hf256 stopped" || log "  hf256 not running"
systemctl stop freedvtnc2 2>/dev/null && log "  [OK] freedvtnc2 stopped" || log "  freedvtnc2 not running"
systemctl stop rigctld 2>/dev/null && log "  [OK] rigctld stopped" || log "  rigctld not running"
systemctl stop hf256-portal 2>/dev/null && log "  [OK] portal stopped" || log "  portal not running"

log "HF-256 stack stopped"
