#!/bin/bash
# HF-256 WiFi Mode Manager
# Switches wlan0 between AP mode (hostapd) and client mode (wpa_supplicant)
# Called by the web portal settings page

SETTINGS="/etc/hf256/settings.json"
LOG="/var/log/hf256-wifi.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

get_setting() {
    python3 -c "
import json, sys
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('$1', ''))
except:
    print('')
"
}

# ── Completely stop AP stack ─────────────────────────────────────────────────
stop_ap() {
    systemctl stop hostapd   2>/dev/null || true
    systemctl stop dnsmasq   2>/dev/null || true
    pkill -f dnsmasq         2>/dev/null || true
    sleep 1
}

# ── Completely stop client stack ─────────────────────────────────────────────
stop_client() {
    systemctl stop    wpa_supplicant@wlan0 2>/dev/null || true
    systemctl disable wpa_supplicant@wlan0 2>/dev/null || true
    # NOTE: do NOT mask wpa_supplicant@wlan0 — masking persists across
    # reboots and causes start_client to fail on next boot if it runs
    # before the unmask completes.
    pkill -f "wpa_supplicant.*wlan0"       2>/dev/null || true
    pkill -f "dhclient.*wlan0"             2>/dev/null || true
    dhclient -r wlan0 2>/dev/null || true
    sleep 1
}

# ── Revert settings.json wifi_mode to ap ─────────────────────────────────────
revert_to_ap_settings() {
    python3 -c "
import json
f='$SETTINGS'
try:
    d=json.load(open(f))
    d['wifi_mode']='ap'
    json.dump(d,open(f,'w'),indent=2)
except:
    pass
"
}

start_ap() {
    local ssid="$1"
    local password="$2"
    log "Starting AP mode: SSID=$ssid"

    stop_client

    ip addr flush dev wlan0 2>/dev/null || true
    ip link set wlan0 up
    ip addr add 192.168.4.1/24 dev wlan0 2>/dev/null || true

    if [ -n "$password" ]; then
        cat > /etc/hostapd/hostapd.conf << EOF
interface=wlan0
driver=nl80211
ssid=$ssid
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
country_code=US
ieee80211n=1
wpa=2
wpa_passphrase=$password
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF
    else
        cat > /etc/hostapd/hostapd.conf << EOF
interface=wlan0
driver=nl80211
ssid=$ssid
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
country_code=US
ieee80211n=1
wpa=0
EOF
    fi

    systemctl unmask hostapd 2>/dev/null || true

    # Retry hostapd up to 3 times — at boot the WiFi hardware may not
    # be ready immediately (rfkill, firmware load delay)
    local STARTED=0
    for attempt in 1 2 3; do
        systemctl restart hostapd 2>/dev/null
        sleep 2
        if pgrep hostapd &>/dev/null; then
            STARTED=1
            break
        fi
        log "  hostapd start attempt $attempt failed — retrying..."
        sleep 2
    done

    systemctl restart dnsmasq 2>/dev/null || true

    if [ "$STARTED" = "1" ]; then
        log "  [OK] AP mode active: $ssid"
        return 0
    else
        log "  [ERROR] hostapd failed to start after 3 attempts"
        return 1
    fi
}

start_client() {
    local ssid="$1"
    local password="$2"
    log "Starting client mode: SSID=$ssid"

    # CRITICAL: Stop AP stack completely FIRST.
    # dnsmasq must be dead before we return — if it stays running it
    # intercepts port 80 and the portal becomes completely unreachable.
    stop_ap

    ip addr flush dev wlan0 2>/dev/null || true
    ip link set wlan0 up

    cat > /etc/wpa_supplicant/wpa_supplicant-wlan0.conf << EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US
network={
    ssid="$ssid"
    psk="$password"
    key_mgmt=WPA-PSK
}
EOF
    chmod 600 /etc/wpa_supplicant/wpa_supplicant-wlan0.conf

    # Unmask in case a previous invocation left it masked
    systemctl unmask  wpa_supplicant@wlan0 2>/dev/null || true
    systemctl enable  wpa_supplicant@wlan0
    # Brief delay to ensure unmask is fully committed before start
    sleep 1
    systemctl restart wpa_supplicant@wlan0

    # Wait for association — max 20s
    log "  Waiting for association..."
    ASSOCIATED=0
    for i in $(seq 1 20); do
        STATE=$(wpa_cli -i wlan0 status 2>/dev/null | grep 'wpa_state' | cut -d= -f2)
        if [ "$STATE" = "COMPLETED" ]; then
            ASSOCIATED=1
            break
        fi
        sleep 1
    done

    if [ "$ASSOCIATED" = "0" ]; then
        log "  [WARN] Association failed — wrong password or SSID not found"
        log "  Falling back to AP mode..."
        revert_to_ap_settings
        APSSID=$(get_setting ap_ssid)
        APPASS=$(get_setting ap_password)
        [ -z "$APSSID" ] && APSSID="HF256-N0CALL"
        [ -z "$APPASS" ] && APPASS="hf256setup"
        start_ap "$APSSID" "$APPASS"
        return 1
    fi

    # Get DHCP lease — background dhclient, poll for IP max 15s
    log "  Associated — requesting DHCP..."
    dhclient wlan0 2>/dev/null &
    DHCP_PID=$!

    IP=""
    for i in $(seq 1 15); do
        sleep 1
        IP=$(ip addr show wlan0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
        [ -n "$IP" ] && break
    done

    if [ -z "$IP" ]; then
        log "  [WARN] No IP assigned — DHCP failed"
        log "  Falling back to AP mode..."
        kill "$DHCP_PID" 2>/dev/null || true
        revert_to_ap_settings
        APSSID=$(get_setting ap_ssid)
        APPASS=$(get_setting ap_password)
        [ -z "$APSSID" ] && APSSID="HF256-N0CALL"
        [ -z "$APPASS" ] && APPASS="hf256setup"
        start_ap "$APSSID" "$APPASS"
        return 1
    fi

    log "  [OK] Client mode active, IP: $IP"
    return 0
}

reset_to_ap() {
    log "Emergency reset to AP mode"
    revert_to_ap_settings
    SSID=$(get_setting ap_ssid)
    PASS=$(get_setting ap_password)
    [ -z "$SSID" ] && SSID="HF256-N0CALL"
    [ -z "$PASS" ] && PASS="hf256setup"
    start_ap "$SSID" "$PASS"
}

case "$1" in
    ap)     start_ap     "$2" "$3" ;;
    client) start_client "$2" "$3" ;;
    reset)  reset_to_ap          ;;
    *)
        echo "Usage: $0 {ap <ssid> <password>|client <ssid> <password>|reset}"
        exit 1
        ;;
esac
