#!/bin/bash
# HF-256 WiFi Mode Restore
# Reads wifi_mode from settings.json and restores correct mode on boot
SETTINGS="/etc/hf256/settings.json"
LOG="/var/log/hf256-wifi.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

log "WiFi restore starting"

MODE=$(python3 -c "
import json
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('wifi_mode', 'ap'))
except:
    print('ap')
")

log "Restoring WiFi mode: $MODE"

if [ "$MODE" = "client" ]; then
    SSID=$(python3 -c "
import json
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('client_ssid', ''))
except:
    print('')
")
    PASS=$(python3 -c "
import json
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('client_password', ''))
except:
    print('')
")
    if [ -z "$SSID" ]; then
        log "No client SSID configured - falling back to AP mode"
        /opt/hf256/scripts/wifi-mode.sh reset
    else
        log "Restoring client mode: SSID=$SSID"
        /opt/hf256/scripts/wifi-mode.sh client "$SSID" "$PASS"
    fi
else
    AP_SSID=$(python3 -c "
import json
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('ap_ssid', 'HF256-N0CALL'))
except:
    print('HF256-N0CALL')
")
    AP_PASS=$(python3 -c "
import json
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('ap_password', 'hf256setup'))
except:
    print('hf256setup')
")
    log "Restoring AP mode: SSID=$AP_SSID"
    /opt/hf256/scripts/wifi-mode.sh ap "$AP_SSID" "$AP_PASS"
fi

log "WiFi restore complete"
