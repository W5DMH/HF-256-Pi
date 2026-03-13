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
start_ap() {
    local ssid="$1"
    local password="$2"
    log "Starting AP mode: SSID=$ssid"
    # Stop and mask client mode to prevent it starting on reboot
    systemctl stop wpa_supplicant@wlan0 2>/dev/null || true
    systemctl disable wpa_supplicant@wlan0 2>/dev/null || true
    systemctl mask wpa_supplicant@wlan0 2>/dev/null || true
    # Release any DHCP lease
    # old line dhclient -r wlan0 2>/dev/null || true
    # Kill any running dhclient and release lease
    pkill -f "dhclient.*wlan0" 2>/dev/null || true
    sleep 1
    dhclient -r wlan0 2>/dev/null || true
    # Flush wlan0
    ip addr flush dev wlan0 2>/dev/null || true
    ip link set wlan0 up
    # Assign static IP
    ip addr add 192.168.4.1/24 dev wlan0 2>/dev/null || true
    # Write hostapd config
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
    systemctl --no-block restart hostapd
    systemctl --no-block restart dnsmasq
    sleep 2
    if pgrep hostapd &>/dev/null; then
        log "  [OK] AP mode active: $ssid"
        return 0
    else
        log "  [ERROR] hostapd failed to start"
        return 1
    fi
}
start_client() {
    local ssid="$1"
    local password="$2"
    log "Starting client mode: SSID=$ssid"
    # Stop AP mode
    systemctl stop hostapd 2>/dev/null || true
    systemctl stop dnsmasq 2>/dev/null || true
    # Flush wlan0
    ip addr flush dev wlan0 2>/dev/null || true
    ip link set wlan0 up
    # Write wpa_supplicant config
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
    systemctl unmask wpa_supplicant@wlan0 2>/dev/null || true
    systemctl enable wpa_supplicant@wlan0
    systemctl --no-block restart wpa_supplicant@wlan0

    # Wait for wpa_supplicant to associate
    log "  Waiting for association..."
    for i in $(seq 1 30); do
        STATE=$(wpa_cli -i wlan0 status 2>/dev/null | grep 'wpa_state' | cut -d= -f2)
        if [ "$STATE" = "COMPLETED" ]; then
            log "  Associated, requesting DHCP..."
            dhclient -v wlan0 2>/dev/null
            sleep 2
            break
        fi
        sleep 1
    done

    # Wait for IP assignment
    log "  Waiting for IP..."
    for i in $(seq 1 15); do
        IP=$(ip addr show wlan0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
        if [ -n "$IP" ]; then
            log "  [OK] Client mode active, IP: $IP"
            return 0
        fi
        sleep 1
    done

    # No IP obtained - fall back to AP mode
    log "  [WARN] No IP assigned - falling back to AP mode"
    SSID=$(get_setting ap_ssid)
    PASS=$(get_setting ap_password)
    [ -z "$SSID" ] && SSID="HF256-N0CALL"
    [ -z "$PASS" ] && PASS="hf256setup"
    start_ap "$SSID" "$PASS"
    return 1
}
reset_to_ap() {
    log "Emergency reset to AP mode"
    SSID=$(get_setting ap_ssid)
    PASS=$(get_setting ap_password)
    [ -z "$SSID" ] && SSID="HF256-N0CALL"
    [ -z "$PASS" ] && PASS="hf256setup"
    start_ap "$SSID" "$PASS"
}
case "$1" in
    ap)
        start_ap "$2" "$3"
        ;;
    client)
        start_client "$2" "$3"
        ;;
    reset)
        reset_to_ap
        ;;
    *)
        echo "Usage: $0 {ap <ssid> <password>|client <ssid> <password>|reset}"
        exit 1
        ;;
esac
