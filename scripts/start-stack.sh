#!/bin/bash
# HF-256 Start Stack
# Starts all services in the correct order

LOG="/var/log/hf256-stack.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

SETUP_COMPLETE="/etc/hf256/.setup_complete"
CONFIG_ENV="/etc/hf256/config.env"

log "Starting HF-256 stack..."

if [ ! -f "$SETUP_COMPLETE" ]; then
    log "Setup not complete - starting portal only"
    systemctl start hf256-portal
    exit 0
fi

if [ ! -f "$CONFIG_ENV" ]; then
    log "[ERROR] config.env not found - cannot start radio services"
    systemctl start hf256-portal
    exit 1
fi

# Load config
source "$CONFIG_ENV"

# Start rigctld if serial port is configured
if [ -n "$SERIAL_PORT" ] && [ "$SERIAL_PORT" != "" ]; then
    log "Starting rigctld on $SERIAL_PORT..."
    systemctl start rigctld
    sleep 2
    if pgrep rigctld &>/dev/null; then
        log "  [OK] rigctld running"
    else
        log "  [WARN] rigctld failed - continuing without CAT control"
    fi
fi

# Start freedvtnc2
log "Starting freedvtnc2..."
systemctl start freedvtnc2
sleep 3

# Wait for freedvtnc2 KISS port
log "Waiting for freedvtnc2 KISS port 8001..."
for i in $(seq 1 30); do
    if ss -tln | grep -q ':8001'; then
        log "  [OK] freedvtnc2 listening on port 8001 after ${i}s"
        break
    fi
    sleep 1
done

if ! ss -tln | grep -q ':8001'; then
    log "  [ERROR] freedvtnc2 not listening after 30s"
    exit 1
fi

# Start HF-256 application
log "Starting HF-256 application..."
systemctl start hf256
sleep 2

if systemctl is-active hf256 &>/dev/null; then
    log "  [OK] HF-256 application running"
else
    log "  [ERROR] HF-256 application failed to start"
    journalctl -u hf256 --no-pager -n 20 >> "$LOG" 2>&1
fi

# Start portal
log "Starting web portal..."
systemctl start hf256-portal

log "HF-256 stack started"
