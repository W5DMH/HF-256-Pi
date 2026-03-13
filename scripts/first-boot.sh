#!/bin/bash
# HF-256 First Boot Script
# Runs once on first boot to initialize WiFi AP and start services
# Does NOT mark setup complete - that is done by the web portal after radio config

LOG="/var/log/hf256-firstboot.log"
SETUP_COMPLETE="/etc/hf256/.setup_complete"

touch "$LOG" 2>/dev/null || LOG="/tmp/hf256-firstboot.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

if [ -f "$SETUP_COMPLETE" ]; then
    log "Setup already complete, skipping first-boot"
    exit 0
fi

log "============================================"
log "HF-256 First Boot Starting"
log "============================================"

mkdir -p /etc/hf256
mkdir -p /etc/hf256/backups
mkdir -p /run/hf256

# Backup original configs
log "Backing up original configs..."
[ -f /etc/hostapd/hostapd.conf ] && \
    cp /etc/hostapd/hostapd.conf /etc/hf256/backups/hostapd.conf.default
[ -f /etc/dnsmasq.d/hf256.conf ] && \
    cp /etc/dnsmasq.d/hf256.conf /etc/hf256/backups/dnsmasq.conf.default

# Write default settings.json if not present
if [ ! -f /etc/hf256/settings.json ]; then
    log "Writing default settings.json..."
    cat > /etc/hf256/settings.json << 'SETTINGS'
{
  "callsign": "N0CALL",
  "role": "",
  "hub_address": "",
  "encryption_enabled": true,
  "network_key_set": false,
  "wifi_mode": "ap",
  "ap_ssid": "HF256-N0CALL",
  "ap_password": "hf256setup",
  "client_ssid": "",
  "client_password": ""
}
SETTINGS
    log "  [OK] Default settings.json written"
fi

log "Waiting for system initialization..."
sleep 5

# Unblock WiFi
log "Unblocking WiFi..."
rfkill unblock wlan 2>&1 || log "  rfkill not needed or failed"

# Wait for wlan0
log "Waiting for wlan0 interface..."
WLAN_FOUND=0
for i in $(seq 1 30); do
    if ip link show wlan0 &>/dev/null; then
        log "  wlan0 found after $i seconds"
        WLAN_FOUND=1
        break
    fi
    sleep 1
done

if [ "$WLAN_FOUND" -eq 0 ]; then
    log "  [ERROR] wlan0 not found after 30 seconds"
    log "  Available interfaces: $(ip link show | grep -E '^[0-9]+:' | cut -d: -f2 | tr -d ' ')"
    exit 1
fi

# Assign static IP
log "Assigning static IP to wlan0..."
ip link set wlan0 up
ip addr add 192.168.4.1/24 dev wlan0 2>/dev/null || \
    log "  IP already assigned or failed"

IP_ADDR=$(ip addr show wlan0 | grep 'inet ' | awk '{print $2}')
log "  wlan0 IP: ${IP_ADDR:-not assigned}"

# Start hostapd
log "Starting hostapd..."
systemctl unmask hostapd 2>/dev/null || true
systemctl start hostapd
sleep 3

if pgrep hostapd &>/dev/null; then
    log "  [OK] hostapd running"
    systemctl enable hostapd 2>/dev/null || true
else
    log "  [ERROR] hostapd failed to start"
    journalctl -u hostapd --no-pager -n 20 >> "$LOG" 2>&1
fi

# Start dnsmasq
log "Starting dnsmasq..."
systemctl start dnsmasq
sleep 2

if pgrep dnsmasq &>/dev/null; then
    log "  [OK] dnsmasq running"
    systemctl enable dnsmasq 2>/dev/null || true
else
    log "  [ERROR] dnsmasq failed to start"
    journalctl -u dnsmasq --no-pager -n 20 >> "$LOG" 2>&1
fi

# Start portal
log "Starting web portal..."
systemctl start hf256-portal
sleep 3

if systemctl is-active hf256-portal &>/dev/null; then
    log "  [OK] Portal running"
    if ss -tlnp | grep -q ':80'; then
        log "  [OK] Portal listening on port 80"
    else
        log "  [WARN] Portal not yet listening on port 80"
    fi
else
    log "  [ERROR] Portal failed to start"
    journalctl -u hf256-portal --no-pager -n 20 >> "$LOG" 2>&1
fi

log ""
log "============================================"
log "HF-256 First Boot Complete"
log "============================================"
log "  WiFi AP:  HF256-N0CALL"
log "  Password: hf256setup"
log "  Portal:   http://192.168.4.1"
log "  Log:      $LOG"
log "============================================"

exit 0
