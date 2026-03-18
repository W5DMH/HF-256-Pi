#!/bin/bash
# HF-256 WiFi Boot Mode Setup
# Called by hf256-wlan.service at boot.
# Reads settings.json and configures wlan0 for AP or client mode.
# Has extra patience for boot-time hardware readiness.

SETTINGS="/etc/hf256/settings.json"
LOG="/var/log/hf256-wifi.log"
WIFI_MODE_SH="/opt/hf256/scripts/wifi-mode.sh"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] BOOT: $1" | tee -a "$LOG"; }

get_setting() {
    python3 -c "
import json, sys
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('$1', ''))
except:
    print('')
" 2>/dev/null
}

log "WiFi boot setup starting"

# ── Wait for wlan0 hardware to be present ────────────────────────────────────
WLAN_READY=0
for i in $(seq 1 15); do
    if ip link show wlan0 &>/dev/null; then
        WLAN_READY=1
        log "wlan0 ready after ${i}s"
        break
    fi
    sleep 1
done

if [ "$WLAN_READY" = "0" ]; then
    log "ERROR: wlan0 not found after 15s — cannot configure WiFi"
    exit 1
fi

# ── Unblock WiFi rfkill if blocked ───────────────────────────────────────────
rfkill unblock wifi 2>/dev/null || true
sleep 1

# ── Read settings ─────────────────────────────────────────────────────────────
MODE=$(get_setting wifi_mode)
[ -z "$MODE" ] && MODE="ap"

log "Configured mode: $MODE"

if [ "$MODE" = "client" ]; then
    SSID=$(get_setting client_ssid)
    PASS=$(get_setting client_password)

    if [ -z "$SSID" ]; then
        log "Client mode set but no SSID configured — falling back to AP"
        MODE="ap"
    else
        log "Starting client mode: SSID=$SSID"
        "$WIFI_MODE_SH" client "$SSID" "$PASS"
        EXIT=$?
        if [ $EXIT -eq 0 ]; then
            log "Client mode started successfully"
            exit 0
        else
            log "Client mode failed (exit $EXIT) — AP fallback already handled by wifi-mode.sh"
            exit 0
        fi
    fi
fi

if [ "$MODE" = "ap" ]; then
    SSID=$(get_setting ap_ssid)
    PASS=$(get_setting ap_password)
    [ -z "$SSID" ] && SSID="HF256-N0CALL"
    [ -z "$PASS" ] && PASS="hf256setup"

    log "Starting AP mode: SSID=$SSID"
    "$WIFI_MODE_SH" ap "$SSID" "$PASS"
    EXIT=$?
    if [ $EXIT -eq 0 ]; then
        log "AP mode started successfully"
    else
        log "ERROR: AP mode failed (exit $EXIT)"
    fi
    exit $EXIT
fi

log "Unknown mode: $MODE"
exit 1
