#!/bin/bash
# HF-256 Raspberry Pi Image Builder
# Version 0.1.0 — Multi-session, Direwolf AX.25, Mesh Sync
#
# Run on Pi 4 (or x86-64 with qemu-user-static) as root:
#   sudo bash build.sh
#
# Input:  ~/hf256-pi/bookworm-lite.img  (decompressed base image)
# Output: ~/hf256-pi/output/hf256-<date>.img.xz
#
# Prerequisites alongside build.sh (in image/ directory):
#   ardopcf_arm_Linux_64   — ARDOP modem binary
#                            https://github.com/pflarue/ardop/releases

set -euo pipefail

# ------------------------------------------------------------------ #
# Configuration
# ------------------------------------------------------------------ #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INPUT_IMG="$PROJECT_DIR/bookworm-lite.img"
OUTPUT_DIR="$PROJECT_DIR/output"
DATE_STR="$(date +%Y%m%d)"
OUTPUT_IMG="$OUTPUT_DIR/hf256-${DATE_STR}.img"
MOUNT_DIR="/mnt/hf256-build"
LOG="$OUTPUT_DIR/build-${DATE_STR}.log"

BOOT_MNT="$MOUNT_DIR/boot"
ROOT_MNT="$MOUNT_DIR/root"

LOOP_BOOT=""
LOOP_ROOT=""

# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #
mkdir -p "$OUTPUT_DIR"
exec > >(tee -a "$LOG") 2>&1

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] ✓ $*"; }
err()  { echo "[$(date '+%H:%M:%S')] ✗ $*"; exit 1; }
warn() { echo "[$(date '+%H:%M:%S')] ⚠ $*"; }

# ------------------------------------------------------------------ #
# Preflight checks
# ------------------------------------------------------------------ #
log "=================================================="
log " HF-256 Image Builder v0.1.0"
log "=================================================="
log " Project dir: $PROJECT_DIR"
log " Input image: $INPUT_IMG"
log " Output:      $OUTPUT_IMG.xz"
log "=================================================="

[ "$(id -u)" -eq 0 ] || err "Must run as root: sudo bash build.sh"

for cmd in losetup mount umount chroot rsync xz parted \
           e2fsck resize2fs truncate sha256sum; do
    command -v "$cmd" &>/dev/null || err "Missing required command: $cmd"
done

[ -f "$INPUT_IMG" ] || \
    err "Input image not found: $INPUT_IMG
    Run: cp ~/2024-11-19-raspios-bookworm-arm64-lite.img $INPUT_IMG"

ARDOPCF_BIN="$SCRIPT_DIR/ardopcf_arm_Linux_64"
[ -f "$ARDOPCF_BIN" ] || \
    err "ardopcf binary not found: $ARDOPCF_BIN
    Download ardopcf_arm_Linux_64 from https://github.com/pflarue/ardop/releases
    and place it in the image/ directory alongside build.sh"

ok "Preflight checks passed"

# ------------------------------------------------------------------ #
# Copy input image to working copy
# ------------------------------------------------------------------ #
log "Copying base image to working copy..."
WORK_IMG="$OUTPUT_DIR/hf256-work.img"
cp "$INPUT_IMG" "$WORK_IMG"
ok "Working image: $WORK_IMG ($(du -h "$WORK_IMG" | cut -f1))"

# ------------------------------------------------------------------ #
# Expand image by 2GB
# (Extra 512 MB vs v0.0.x covers Direwolf + mesh sync dependencies)
# ------------------------------------------------------------------ #
log "Expanding image by 2048 MB..."
truncate -s +2048M "$WORK_IMG"

LOOP_DEV=$(losetup --find --show "$WORK_IMG")
log "  Loop device: $LOOP_DEV"
partprobe "$LOOP_DEV" 2>/dev/null || true

PART2_START=$(parted -s "$LOOP_DEV" unit s print | \
    awk '/^ 2/{print $2}' | tr -d 's')
log "  Root partition start sector: $PART2_START"

parted -s "$LOOP_DEV" resizepart 2 100%
ok "Partition 2 expanded"
losetup -d "$LOOP_DEV"

# Mount with partition offsets
SECTOR_SIZE=512
BOOT_OFFSET=$(parted -s "$WORK_IMG" unit s print | \
    awk '/^ 1/{print $2}' | tr -d 's')
ROOT_OFFSET=$(parted -s "$WORK_IMG" unit s print | \
    awk '/^ 2/{print $2}' | tr -d 's')
BOOT_BYTES=$(( BOOT_OFFSET * SECTOR_SIZE ))
ROOT_BYTES=$(( ROOT_OFFSET * SECTOR_SIZE ))

log "  Boot offset: $BOOT_BYTES bytes"
log "  Root offset: $ROOT_BYTES bytes"

mkdir -p "$BOOT_MNT" "$ROOT_MNT"

LOOP_ROOT=$(losetup --find --show --offset "$ROOT_BYTES" "$WORK_IMG")
log "  Root loop: $LOOP_ROOT"

log "Checking root filesystem..."
e2fsck -f -y "$LOOP_ROOT" || warn "e2fsck reported issues (continuing)"
resize2fs "$LOOP_ROOT"
ok "Root filesystem resized"

mount "$LOOP_ROOT" "$ROOT_MNT"

LOOP_BOOT=$(losetup --find --show --offset "$BOOT_BYTES" "$WORK_IMG")
log "  Boot loop: $LOOP_BOOT"
mount "$LOOP_BOOT" "$BOOT_MNT"
ok "Image mounted at $MOUNT_DIR"

# ------------------------------------------------------------------ #
# Cleanup trap
# ------------------------------------------------------------------ #
cleanup() {
    log "Cleaning up mounts..."
    for dir in dev/pts dev proc sys run; do
        umount "$ROOT_MNT/$dir" 2>/dev/null || true
    done
    umount "$ROOT_MNT/boot/firmware" 2>/dev/null || true
    umount "$BOOT_MNT"              2>/dev/null || true
    umount "$ROOT_MNT"              2>/dev/null || true
    losetup -d "$LOOP_BOOT"         2>/dev/null || true
    losetup -d "$LOOP_ROOT"         2>/dev/null || true
    log "Cleanup complete"
}
trap cleanup EXIT

# ------------------------------------------------------------------ #
# Bind mounts for chroot
# ------------------------------------------------------------------ #
log "Setting up chroot bind mounts..."
mount --bind /dev     "$ROOT_MNT/dev"
mount --bind /dev/pts "$ROOT_MNT/dev/pts"
mount --bind /proc    "$ROOT_MNT/proc"
mount --bind /sys     "$ROOT_MNT/sys"
mount --bind /run     "$ROOT_MNT/run"

mkdir -p "$ROOT_MNT/boot/firmware"
mount --bind "$BOOT_MNT" "$ROOT_MNT/boot/firmware"
ok "Bind mounts ready"

# ------------------------------------------------------------------ #
# DNS for chroot apt
# ------------------------------------------------------------------ #
cp "$ROOT_MNT/etc/resolv.conf" "$ROOT_MNT/etc/resolv.conf.bak" 2>/dev/null || true
cp /etc/resolv.conf "$ROOT_MNT/etc/resolv.conf"

# ------------------------------------------------------------------ #
# chroot helper
# ------------------------------------------------------------------ #
run_chroot() {
    chroot "$ROOT_MNT" /bin/bash -c "$1"
}

# ------------------------------------------------------------------ #
# System configuration
# ------------------------------------------------------------------ #
log "Setting hostname to hf256..."
echo "hf256" > "$ROOT_MNT/etc/hostname"
sed -i 's/raspberrypi/hf256/g' "$ROOT_MNT/etc/hosts" 2>/dev/null || true
ok "Hostname set"

log "Enabling SSH..."
touch "$BOOT_MNT/ssh"
run_chroot "systemctl enable ssh" || true
ok "SSH enabled"

log "Enabling SPI in config.txt..."
BOOT_CFG="$BOOT_MNT/config.txt"
if ! grep -q "^dtparam=spi=on" "$BOOT_CFG"; then
    echo ""                                   >> "$BOOT_CFG"
    echo "# HF-256 — SPI for PiTFT display"  >> "$BOOT_CFG"
    echo "dtparam=spi=on"                     >> "$BOOT_CFG"
fi
ok "SPI enabled"

log "Disabling first-run wizard..."
rm -f "$ROOT_MNT/etc/profile.d/sap*"                            2>/dev/null || true
rm -f "$ROOT_MNT/etc/xdg/autostart/piwiz.desktop"              2>/dev/null || true
rm -f "$ROOT_MNT/etc/init.d/resize2fs_once"                    2>/dev/null || true
touch "$ROOT_MNT/boot/firmware/firstboot_disabled"             2>/dev/null || true
sed -i 's| init=/usr/lib/raspberrypi-sys-mods/firstboot||g' \
    "$BOOT_MNT/cmdline.txt"
log "  Removed firstboot from cmdline.txt"

# Pre-create pi user with password 12345678
HASHED_PW=$(echo "12345678" | openssl passwd -6 -stdin)
echo "pi:${HASHED_PW}" > "$BOOT_MNT/userconf"
run_chroot "id pi 2>/dev/null || \
    useradd -m -s /bin/bash \
    -G sudo,dialout,audio,video,plugdev,gpio,i2c,spi,netdev pi"
run_chroot "echo 'pi:12345678' | chpasswd"

mkdir -p "$ROOT_MNT/etc/default"
cat > "$ROOT_MNT/etc/default/keyboard" << 'KBEOF'
XKBMODEL="pc105"
XKBLAYOUT="us"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
KBEOF

cat > "$ROOT_MNT/etc/default/locale" << 'LCEOF'
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
LANGUAGE=en_US.UTF-8
LCEOF

run_chroot "sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen"
run_chroot "grep -qxF 'en_US.UTF-8 UTF-8' /etc/locale.gen || echo 'en_US.UTF-8 UTF-8' >> /etc/locale.gen"
run_chroot "locale-gen"
run_chroot "update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LANGUAGE=en_US.UTF-8"
ok "First-run wizard disabled, pi user pre-configured"

# ------------------------------------------------------------------ #
# APT packages
# ------------------------------------------------------------------ #
log "Updating APT package lists..."
run_chroot "apt-get update -qq" || warn "apt-get update had issues"

log "Installing system packages..."
PACKAGES=(
    # Audio — ALSA base utils only. No PulseAudio — Direwolf accesses
    # the DigiRig USB audio directly via ALSA (plughw) for deterministic
    # low-latency TX without daemon scheduling overhead.
    alsa-utils
    # Serial / CAT
    minicom
    lrzsz
    # Networking
    hostapd
    dnsmasq
    iptables
    rfkill
    # Python
    python3-pip
    python3-venv
    python3-dev
    python3-flask
    python3-serial
    python3-pil
    python3-rpi.gpio
    python3-cryptography
    # Hamlib (for rigctld CAT control)
    libhamlib-utils
    libhamlib4
    # Build tools
    gcc
    python3-setuptools
    python3-wheel
    # System
    git
    curl
    # PiTFT display
    i2c-tools
    read-edid
    # ── NEW v0.1.0 ──────────────────────────────────────────────────
    # Direwolf AX.25 soundcard TNC (VHF 9600 baud + HF 300 baud)
    direwolf
    # AX.25 utilities (ax25-tools provides kissattach, axcall, etc.)
    ax25-tools
    ax25-apps
    # PortAudio — used by ARDOP and FreeDV (not Direwolf which uses raw ALSA)
    portaudio19-dev
    libportaudio2
    # FreeDV / codec2 build dependencies
    libasound2-dev
    libcodec2-dev
)

run_chroot "DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ${PACKAGES[*]}" \
    || err "APT package installation failed"
ok "System packages installed"

# ------------------------------------------------------------------ #
# pip packages
# ------------------------------------------------------------------ #
log "Installing Python packages via pip..."

PIP_PACKAGES=(
    cryptography
    pillow
    RPi.GPIO
    adafruit-blinka
    adafruit-circuitpython-st7789
    adafruit-circuitpython-rgb-display
    flask-sock          # WebSocket support for console
)

for pkg in "${PIP_PACKAGES[@]}"; do
    log "  pip install $pkg..."
    run_chroot "pip3 install --break-system-packages --quiet $pkg" \
        || warn "pip install $pkg failed (non-fatal)"
done
ok "Python packages installed"

# ------------------------------------------------------------------ #
# Install ardopcf (pre-built arm64 binary)
# ------------------------------------------------------------------ #
log "Installing ardopcf pre-built binary..."
install -m 755 "$ARDOPCF_BIN" "$ROOT_MNT/usr/local/bin/ardopc"
[ -f "$ROOT_MNT/usr/local/bin/ardopc" ] || err "ardopc install failed"
ok "ardopc installed to /usr/local/bin/ardopc"

# ------------------------------------------------------------------ #
# Directory structure
# ------------------------------------------------------------------ #
log "Creating HF-256 directory structure..."
run_chroot "mkdir -p \
    /opt/hf256/hf256 \
    /opt/hf256/portal/templates \
    /opt/hf256/portal/static \
    /opt/hf256/scripts \
    /opt/hf256/configs \
    /etc/hf256/backups \
    /etc/direwolf \
    /var/log \
    /home/pi/.hf256/downloads \
    /home/pi/.hf256/hub_messages \
    /home/pi/.hf256/hub_files"

# Direwolf log dir
run_chroot "mkdir -p /var/log/direwolf"
run_chroot "chown pi:pi /var/log/direwolf"

run_chroot "chown -R pi:pi /home/pi/.hf256"
ok "Directory structure created"

# ------------------------------------------------------------------ #
# Copy application files
# ------------------------------------------------------------------ #
log "Copying HF-256 application files..."

# hf256 Python package (ardop.py, chat.py, session_manager.py, etc.)
rsync -a --quiet "$PROJECT_DIR/hf256/" \
    "$ROOT_MNT/opt/hf256/hf256/"

# Portal (app.py, templates/, static/)
rsync -a --quiet "$PROJECT_DIR/portal/" \
    "$ROOT_MNT/opt/hf256/portal/"

# Scripts
rsync -a --quiet "$PROJECT_DIR/scripts/" \
    "$ROOT_MNT/opt/hf256/scripts/"
chmod +x "$ROOT_MNT/opt/hf256/scripts/"*.sh \
          "$ROOT_MNT/opt/hf256/scripts/"*.py 2>/dev/null || true

# Configs (asound.conf, hostapd.conf, dnsmasq.conf, etc.)
rsync -a --quiet "$PROJECT_DIR/configs/" \
    "$ROOT_MNT/opt/hf256/configs/"

ok "Application files copied"

# Verify the new multi-session modules are present
REQUIRED_MODULES=(
    session_manager.py
    tcp_transport.py
    direwolf_transport.py
    hub_core.py
    mesh_sync.py
    direwolf_config.py
)
MISSING_MODS=0
for mod in "${REQUIRED_MODULES[@]}"; do
    if [ ! -f "$ROOT_MNT/opt/hf256/hf256/$mod" ]; then
        warn "MISSING module: $mod — add to hf256/ before building"
        MISSING_MODS=$(( MISSING_MODS + 1 ))
    fi
done
[ "$MISSING_MODS" -eq 0 ] && ok "All multi-session modules present" || \
    warn "$MISSING_MODS module(s) missing — image may be incomplete"

# ------------------------------------------------------------------ #
# Install system configs
# ------------------------------------------------------------------ #
log "Installing system configs..."

# ALSA default
cp "$PROJECT_DIR/configs/asound.conf" \
   "$ROOT_MNT/etc/asound.conf"

# hostapd
mkdir -p "$ROOT_MNT/etc/hostapd"
cp "$PROJECT_DIR/configs/hostapd.conf" \
   "$ROOT_MNT/etc/hostapd/hostapd.conf"
sed -i 's|^#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' \
    "$ROOT_MNT/etc/default/hostapd" 2>/dev/null || \
    echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' \
    >> "$ROOT_MNT/etc/default/hostapd"

# dnsmasq
mkdir -p "$ROOT_MNT/etc/dnsmasq.d"
cp "$PROJECT_DIR/configs/dnsmasq.conf" \
   "$ROOT_MNT/etc/dnsmasq.d/hf256.conf"
echo "port=0" > "$ROOT_MNT/etc/dnsmasq.d/00-disable-default.conf"

# hostapd rfkill override
mkdir -p "$ROOT_MNT/etc/systemd/system/hostapd.service.d"
cp "$PROJECT_DIR/configs/hostapd-rfkill.conf" \
   "$ROOT_MNT/etc/systemd/system/hostapd.service.d/rfkill.conf"

ok "System configs installed"

# ------------------------------------------------------------------ #
# Write default /etc/direwolf/direwolf.conf (placeholder)
# Real config is written by the web portal when the operator
# configures Direwolf in Settings.
# ------------------------------------------------------------------ #
log "Writing placeholder direwolf.conf..."
cat > "$ROOT_MNT/etc/direwolf/direwolf.conf" << 'DWCONF'
# HF-256 Direwolf Configuration
# This file is managed by the HF-256 web portal.
# Configure via Settings -> Direwolf in the web UI.
# Do not edit manually — it will be overwritten.
#
# Direwolf is disabled by default.
# Enable by configuring audio card(s) in the HF-256 Settings page.

MYCALL N0CALL
AGWPORT 8000
KISSPORT 8001
LOGLEVEL 0
DWCONF

chmod 644 "$ROOT_MNT/etc/direwolf/direwolf.conf"
ok "Placeholder direwolf.conf written"

# ------------------------------------------------------------------ #
# Write default /etc/hf256/settings.json
# ------------------------------------------------------------------ #
log "Writing default settings.json..."
mkdir -p "$ROOT_MNT/etc/hf256"
cat > "$ROOT_MNT/etc/hf256/settings.json" << 'SETTINGS'
{
  "callsign":              "N0CALL",
  "role":                  "",
  "hub_address":           "",
  "encryption_enabled":    true,
  "network_key_set":       false,
  "wifi_mode":             "ap",
  "ap_ssid":               "HF256-N0CALL",
  "ap_password":           "hf256setup",
  "client_ssid":           "",
  "client_password":       "",

  "direwolf_vhf_card":          null,
  "direwolf_vhf_serial":        "",
  "direwolf_vhf_ptt":           "RTS",
  "direwolf_vhf_baud":          1200,
  "direwolf_hf_card":           null,
  "direwolf_hf_serial":         "",
  "direwolf_hf_ptt":            "RTS",
  "direwolf_hf_hamlib_model":   null,
  "direwolf_hf_alsa_device":    "",

  "mesh_peers":            [],
  "mesh_sync_interval":    300,

  "max_sessions":          10,
  "session_idle_timeout":  300,
  "session_auth_timeout":  120
}
SETTINGS
chmod 644 "$ROOT_MNT/etc/hf256/settings.json"
ok "Default settings.json written"

# sudoers for pi (poweroff + shutdown only)
echo "pi ALL=(ALL) NOPASSWD: /sbin/poweroff, /sbin/shutdown, /sbin/reboot" \
    > "$ROOT_MNT/etc/sudoers.d/hf256"
chmod 440 "$ROOT_MNT/etc/sudoers.d/hf256"

# ------------------------------------------------------------------ #
# Install systemd services
# ------------------------------------------------------------------ #
log "Installing systemd services..."

for svc in "$PROJECT_DIR/services/"*.service; do
    svc_name=$(basename "$svc")
    cp "$svc" "$ROOT_MNT/etc/systemd/system/$svc_name"
    log "  Installed: $svc_name"
done

# Enable core HF-256 services
run_chroot "systemctl enable \
    hf256-firstboot.service \
    hf256-portal.service \
    hf256-display.service \
    hf256-wlan.service" \
    || warn "Some services failed to enable"

# Direwolf: installed but NOT enabled by default.
# The web portal enables it via 'systemctl enable direwolf'
# after the operator configures the audio card(s).
# We mask it here so it can never start before being configured —
# an unconfigured Direwolf crashes immediately on first boot.
run_chroot "systemctl disable direwolf 2>/dev/null || true"
run_chroot "systemctl mask    direwolf 2>/dev/null || true"
log "  Direwolf service installed (masked until configured via web UI)"

# Disable services that conflict with AP mode
run_chroot "systemctl disable \
    wpa_supplicant.service \
    dhcpcd.service \
    NetworkManager.service 2>/dev/null || true"

run_chroot "systemctl mask NetworkManager                2>/dev/null || true"
run_chroot "systemctl mask wpa_supplicant@wlan0.service  2>/dev/null || true"

# hf256.service (old standalone main.py) superseded by portal
run_chroot "systemctl mask hf256.service 2>/dev/null || true"

ok "Systemd services installed"

# ------------------------------------------------------------------ #
# User groups
# ------------------------------------------------------------------ #
log "Setting pi user group memberships..."
# gpio  — Direwolf GPIO PTT
# dialout — serial ports (DigiRig, CAT)
# audio   — ALSA soundcard access for Direwolf + FreeDV
# netdev  — wpa_supplicant wifi management
run_chroot "usermod -a -G dialout,audio,spi,gpio,netdev pi"
ok "Group memberships updated"

# ------------------------------------------------------------------ #
# USB Audio configuration
# ------------------------------------------------------------------ #
log "Configuring USB audio..."

# Disable Pi built-in audio so USB audio devices enumerate as card 0.
# This is required for Direwolf to open plughw:0,0 reliably.
# USB audio devices vary — built-in audio as card 0 pushes USB to card 1+
# and some chipsets reject ALSA's default period sizes at higher card indices.
# Disable built-in audio so USB audio device is always card 0.
# The base image has 'dtparam=audio=on' — replace it with 'off'.
# Then ensure 'dtparam=audio=off' is present (covers edge cases where
# the base image doesn't have the line at all).
BOOT_CFG="$BOOT_MNT/config.txt"
sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$BOOT_CFG"
if ! grep -q "dtparam=audio=off" "$BOOT_CFG"; then
    echo ""                                                       >> "$BOOT_CFG"
    echo "# HF-256 — Disable built-in audio; USB audio = card 0" >> "$BOOT_CFG"
    echo "dtparam=audio=off"                                      >> "$BOOT_CFG"
fi
log "  Built-in audio disabled (USB audio will be card 0)"

# Disable USB autosuspend — USB audio devices go to sleep after 2 seconds
# by default. When Direwolf tries to open the device it gets ETIMEDOUT
# because the device doesn't wake fast enough for ALSA's open sequence.
# usbcore.autosuspend=-1 disables this globally.
CMDLINE_FILE="$BOOT_MNT/cmdline.txt"
if ! grep -q "usbcore.autosuspend" "$CMDLINE_FILE"; then
    # cmdline.txt is a single line — append parameter, no newline
    sed -i 's/$/ usbcore.autosuspend=-1/' "$CMDLINE_FILE"
    log "  Added usbcore.autosuspend=-1 to cmdline.txt"
fi

# Device-specific udev rule for C-Media CM108 (0d8c:0012) — keeps the USB
# audio hardware permanently powered even if the global autosuspend setting
# doesn't fully apply to this device. The CM108 TX audio path takes 50-200ms
# to re-initialize after hardware sleep, causing the first frame of every
# transmission to be carrier-only with no AFSK modulation. Setting
# power/control=on forces the device to stay active at all times.
cat > "$ROOT_MNT/etc/udev/rules.d/90-hf256-usb-audio.rules" << 'UDEVEOF'
# HF-256 USB Audio Power Management
# Keep C-Media CM108 USB audio permanently powered to prevent TX audio
# path re-initialization delay (carrier-only first frame symptom).
ACTION=="add", SUBSYSTEM=="usb", \
    ATTRS{idVendor}=="0d8c", ATTRS{idProduct}=="0012", \
    ATTR{power/autosuspend_delay_ms}="-1", \
    ATTR{power/control}="on"
# Also covers CM108B variant used in DigiRig
ACTION=="add", SUBSYSTEM=="usb", \
    ATTRS{idVendor}=="0d8c", ATTRS{idProduct}=="013c", \
    ATTR{power/autosuspend_delay_ms}="-1", \
    ATTR{power/control}="on"
UDEVEOF
chmod 644 "$ROOT_MNT/etc/udev/rules.d/90-hf256-usb-audio.rules"
log "  CM108 USB audio power management rule installed"

ok "USB audio configured"

# ------------------------------------------------------------------ #
# ALSA — direct hardware access, no PulseAudio
# ------------------------------------------------------------------ #
# PulseAudio is NOT installed. Direwolf accesses the DigiRig USB audio
# directly via ALSA (ADEVICE plughw:N,0). This gives deterministic,
# low-latency audio with no daemon scheduling issues.
# The X6100 (CM108 chip, requires MMAP access) is not supported in
# this build — it needs PulseAudio which conflicts with reliable AX.25.
log "Configuring ALSA for direct hardware access..."

# Persist ALSA mixer state across reboots via alsa-restore.service.
# alsa-utils installs alsa-restore.service which runs 'alsactl restore'
# at boot. Initial state file will be written by the operator after
# setting levels via Settings → Direwolf → Set Audio Levels.
# Ensure alsa-restore.service is enabled:
run_chroot "systemctl enable alsa-restore.service 2>/dev/null || true"

ok "ALSA configured for direct hardware access"


# ------------------------------------------------------------------ #
# rigctld — enable for CAT PTT (Xiegu, Icom, and other CI-V radios)
# ------------------------------------------------------------------ #
log "Configuring rigctld..."

# rigctld is enabled but starts only when RIGCTLD_CMD is set in
# /etc/hf256/config.env. The web portal writes this when the operator
# selects CAT PTT in Settings -> Direwolf.
# Masked by default like Direwolf — the portal unmasks it on first use.
run_chroot "systemctl disable rigctld 2>/dev/null || true"
run_chroot "systemctl mask    rigctld 2>/dev/null || true"
log "  rigctld installed (masked until CAT PTT is configured in web UI)"

# Write empty config.env so the file exists and rigctld.service doesn't
# fail its ConditionPathExists check on first boot.
mkdir -p "$ROOT_MNT/etc/hf256"
if [ ! -f "$ROOT_MNT/etc/hf256/config.env" ]; then
    cat > "$ROOT_MNT/etc/hf256/config.env" << 'ENVEOF'
# HF-256 radio CAT control configuration
# Written by the HF-256 web portal when CAT PTT is configured.
# Do not edit manually.
RIGCTLD_CMD=""
ENVEOF
    chmod 644 "$ROOT_MNT/etc/hf256/config.env"
fi

ok "rigctld configured"
log "Disabling Bluetooth..."
BOOT_CFG="$BOOT_MNT/config.txt"
if ! grep -q "dtoverlay=disable-bt" "$BOOT_CFG"; then
    echo ""                                          >> "$BOOT_CFG"
    echo "# HF-256 — Disable Bluetooth to free UART" >> "$BOOT_CFG"
    echo "dtoverlay=disable-bt"                      >> "$BOOT_CFG"
fi
run_chroot "systemctl disable hciuart 2>/dev/null || true"
ok "Bluetooth disabled"

# ------------------------------------------------------------------ #
# wpa_supplicant base config
# ------------------------------------------------------------------ #
log "Writing base wpa_supplicant config..."
cat > "$ROOT_MNT/etc/wpa_supplicant/wpa_supplicant-wlan0.conf" << 'WPA'
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US
WPA
chmod 600 "$ROOT_MNT/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
ok "wpa_supplicant config written"

# ------------------------------------------------------------------ #
# Disable swap (reduces SD card wear)
# ------------------------------------------------------------------ #
log "Disabling swap..."
run_chroot "systemctl disable dphys-swapfile 2>/dev/null || true"
run_chroot "apt-get remove -y dphys-swapfile 2>/dev/null || true"
ok "Swap disabled"

# ------------------------------------------------------------------ #
# Verify Direwolf binary is installed correctly
# ------------------------------------------------------------------ #
log "Verifying Direwolf installation..."
if run_chroot "command -v direwolf > /dev/null 2>&1"; then
    DW_VER=$(run_chroot "dpkg -s direwolf 2>/dev/null | grep '^Version'" || echo "unknown")
    ok "Direwolf installed: $DW_VER"
else
    warn "Direwolf binary not found — check apt install direwolf"
fi

# Verify ax25-tools
if run_chroot "command -v kissattach > /dev/null 2>&1"; then
    ok "ax25-tools (kissattach) installed"
else
    warn "ax25-tools not installed — AX.25 kernel mode unavailable"
fi

# ------------------------------------------------------------------ #
# Post-install verification of multi-session Python modules
# ------------------------------------------------------------------ #
log "Verifying multi-session Python modules..."
run_chroot "python3 -c '
import sys
sys.path.insert(0, \"/opt/hf256\")
modules = [
    \"hf256.session_manager\",
    \"hf256.tcp_transport\",
    \"hf256.direwolf_transport\",
    \"hf256.hub_core\",
    \"hf256.mesh_sync\",
    \"hf256.direwolf_config\",
    \"hf256.chat\",
    \"hf256.crypto\",
    \"hf256.ardop\",
]
failed = []
for m in modules:
    try:
        __import__(m)
        print(\"  ✓ \" + m)
    except Exception as e:
        print(\"  ✗ \" + m + \": \" + str(e))
        failed.append(m)
if failed:
    print(\"WARN: \" + str(len(failed)) + \" module(s) failed to import\")
    sys.exit(1)
else:
    print(\"All modules OK\")
'" || warn "Some Python modules failed import check — review log"

ok "Module verification complete"

# ------------------------------------------------------------------ #
# APT cleanup
# ------------------------------------------------------------------ #
log "Cleaning APT cache..."
run_chroot "apt-get clean"
run_chroot "rm -rf /var/lib/apt/lists/*"
ok "APT cache cleaned"

# Restore resolv.conf
mv "$ROOT_MNT/etc/resolv.conf.bak" \
   "$ROOT_MNT/etc/resolv.conf" 2>/dev/null || \
    echo "nameserver 1.1.1.1" > "$ROOT_MNT/etc/resolv.conf"

# ------------------------------------------------------------------ #
# Unmount
# ------------------------------------------------------------------ #
log "Unmounting image..."
umount "$ROOT_MNT/boot/firmware" 2>/dev/null || true
for dir in dev/pts dev proc sys run; do
    umount "$ROOT_MNT/$dir" 2>/dev/null || true
done
umount "$BOOT_MNT" 2>/dev/null || true
sync
umount "$ROOT_MNT"
sync
losetup -d "$LOOP_BOOT" 2>/dev/null || true
losetup -d "$LOOP_ROOT" 2>/dev/null || true
ok "Image unmounted cleanly"

# Disable trap — manual cleanup already done
trap - EXIT

# ------------------------------------------------------------------ #
# Finalize image
# ------------------------------------------------------------------ #
log "Moving working image to output..."
mv "$WORK_IMG" "$OUTPUT_IMG"

# SHA-256 checksum for integrity verification
log "Generating SHA-256 checksum..."
sha256sum "$OUTPUT_IMG" > "${OUTPUT_IMG}.sha256"
ok "Checksum: $(cat "${OUTPUT_IMG}.sha256" | awk '{print $1}')"

log "Compressing image with xz (10-20 minutes)..."
xz -T0 -v "$OUTPUT_IMG"
sha256sum "${OUTPUT_IMG}.xz" > "${OUTPUT_IMG}.xz.sha256"
ok "Compressed: ${OUTPUT_IMG}.xz ($(du -h "${OUTPUT_IMG}.xz" | cut -f1))"

# ------------------------------------------------------------------ #
# Done
# ------------------------------------------------------------------ #
log "=================================================="
log " HF-256 Image Build Complete  v0.1.0"
log "=================================================="
log " Output:    ${OUTPUT_IMG}.xz"
log " Checksum:  ${OUTPUT_IMG}.xz.sha256"
log " Log:       $LOG"
log ""
log " Flash with:"
log "   xz -dk ${OUTPUT_IMG}.xz"
log "   sudo dd if=${OUTPUT_IMG} of=/dev/sdX bs=4M status=progress conv=fsync"
log "   # or use Raspberry Pi Imager"
log ""
log " What's new in v0.1.0:"
log "   + Multi-session hub (up to 10 simultaneous spokes)"
log "   + Direwolf AX.25 VHF 1200 baud + HF 300 baud"
log "   + Hub-to-hub mesh sync (TCP port 14257)"
log "   + Dual soundcard support (separate VHF + HF radios)"
log "   + Hub session panel in web console"
log "   + PulseAudio for CM108 USB audio chip compatibility"
log "   + CAT PTT via rigctld for Xiegu/Icom CI-V radios"
log "   + USB audio as card 0 (built-in audio disabled)"
log "   + USB autosuspend disabled for stable audio"
log "   + Downloads page and persistent recent connections"
log "=================================================="
