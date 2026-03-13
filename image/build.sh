#!/bin/bash
# HF-256 Raspberry Pi Image Builder
# Run on Pi 4 as root: sudo bash build.sh
# Input:  ~/hf256-pi/bookworm-lite.img (decompressed base image)
# Output: ~/hf256-pi/output/hf256-<date>.img.xz

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
log " HF-256 Image Builder"
log "=================================================="
log " Project dir: $PROJECT_DIR"
log " Input image: $INPUT_IMG"
log " Output:      $OUTPUT_IMG.xz"
log "=================================================="

[ "$(id -u)" -eq 0 ] || err "Must run as root: sudo bash build.sh"

for cmd in losetup mount umount chroot rsync xz parted \
           e2fsck resize2fs truncate; do
    command -v "$cmd" &>/dev/null || err "Missing command: $cmd"
done

[ -f "$INPUT_IMG" ] || \
    err "Input image not found: $INPUT_IMG
    Run: cp ~/2024-11-19-raspios-bookworm-arm64-lite.img $INPUT_IMG"

ok "Preflight checks passed"

# ------------------------------------------------------------------ #
# Copy input image to working copy
# ------------------------------------------------------------------ #
log "Copying base image to working copy..."
WORK_IMG="$OUTPUT_DIR/hf256-work.img"
cp "$INPUT_IMG" "$WORK_IMG"
ok "Working image: $WORK_IMG ($(du -h "$WORK_IMG" | cut -f1))"

# ------------------------------------------------------------------ #
# Expand image by 1.5GB for packages and app
# ------------------------------------------------------------------ #
log "Expanding image by 1536MB..."
truncate -s +1536M "$WORK_IMG"

# Expand the root partition
LOOP_DEV=$(losetup --find --show "$WORK_IMG")
log "  Loop device: $LOOP_DEV"
partprobe "$LOOP_DEV" 2>/dev/null || true

# Get partition info
PART2_START=$(parted -s "$LOOP_DEV" unit s print | \
    awk '/^ 2/{print $2}' | tr -d 's')
log "  Root partition start sector: $PART2_START"

# Resize partition 2 to fill disk
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

# Mount partitions
mkdir -p "$BOOT_MNT" "$ROOT_MNT"

LOOP_ROOT=$(losetup --find --show --offset "$ROOT_BYTES" "$WORK_IMG")
log "  Root loop: $LOOP_ROOT"

# Check and resize filesystem
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
# Cleanup function
# ------------------------------------------------------------------ #
cleanup() {
    log "Cleaning up mounts..."
    for dir in dev/pts dev proc sys run; do
        umount "$ROOT_MNT/$dir" 2>/dev/null || true
    done
    umount "$ROOT_MNT/boot/firmware" 2>/dev/null || true
    umount "$BOOT_MNT" 2>/dev/null || true
    umount "$ROOT_MNT" 2>/dev/null || true
    losetup -d "$LOOP_BOOT" 2>/dev/null || true
    losetup -d "$LOOP_ROOT" 2>/dev/null || true
    log "Cleanup complete"
}
trap cleanup EXIT

# ------------------------------------------------------------------ #
# Bind mount for chroot
# ------------------------------------------------------------------ #
log "Setting up chroot bind mounts..."
mount --bind /dev     "$ROOT_MNT/dev"
mount --bind /dev/pts "$ROOT_MNT/dev/pts"
mount --bind /proc    "$ROOT_MNT/proc"
mount --bind /sys     "$ROOT_MNT/sys"
mount --bind /run     "$ROOT_MNT/run"

# Link boot partition inside root
mkdir -p "$ROOT_MNT/boot/firmware"
mount --bind "$BOOT_MNT" "$ROOT_MNT/boot/firmware"

ok "Bind mounts ready"

# ------------------------------------------------------------------ #
# DNS for chroot apt
# ------------------------------------------------------------------ #
cp "$ROOT_MNT/etc/resolv.conf" "$ROOT_MNT/etc/resolv.conf.bak" 2>/dev/null || true
cp /etc/resolv.conf "$ROOT_MNT/etc/resolv.conf"

# ------------------------------------------------------------------ #
# chroot install function
# ------------------------------------------------------------------ #
run_chroot() {
    chroot "$ROOT_MNT" /bin/bash -c "$1"
}

# ------------------------------------------------------------------ #
# System configuration
# ------------------------------------------------------------------ #
log "Setting hostname to hf256..."
echo "hf256" > "$ROOT_MNT/etc/hostname"
# Enable SSH
log "Enabling SSH..."
touch "$BOOT_MNT/ssh"
run_chroot "systemctl enable ssh" || true
ok "SSH enabled"
sed -i 's/raspberrypi/hf256/g' "$ROOT_MNT/etc/hosts" 2>/dev/null || true
ok "Hostname set"

log "Enabling SPI in config.txt..."
BOOT_CFG="$BOOT_MNT/config.txt"
if ! grep -q "^dtparam=spi=on" "$BOOT_CFG"; then
    echo "" >> "$BOOT_CFG"
    echo "# HF-256 - SPI for PiTFT display" >> "$BOOT_CFG"
    echo "dtparam=spi=on" >> "$BOOT_CFG"
fi
ok "SPI enabled"

log "Disabling first-run wizard..."
rm -f "$ROOT_MNT/etc/profile.d/sap*" 2>/dev/null || true
# Disable piwiz if present
rm -f "$ROOT_MNT/etc/xdg/autostart/piwiz.desktop" 2>/dev/null || true
# Disable Raspberry Pi OS first-boot user creation prompt
rm -f "$ROOT_MNT/etc/init.d/resize2fs_once" 2>/dev/null || true
touch "$ROOT_MNT/boot/firmware/firstboot_disabled" 2>/dev/null || true
# Remove firstboot init from cmdline.txt - base image has never been booted
sed -i 's| init=/usr/lib/raspberrypi-sys-mods/firstboot||g' "$BOOT_MNT/cmdline.txt"
log "✓ Removed firstboot from cmdline.txt"
# Pre-create pi user with password 12345678
HASHED_PW=$(echo "12345678" | openssl passwd -6 -stdin)
# Write userconf to boot partition (Bookworm mechanism)
echo "pi:${HASHED_PW}" > "$BOOT_MNT/userconf"
# Also ensure pi user exists in the image
run_chroot "id pi 2>/dev/null || useradd -m -s /bin/bash -G sudo,dialout,audio,video,plugdev,gpio,i2c,spi pi"
run_chroot "echo 'pi:12345678' | chpasswd"
# Set keyboard layout to US
mkdir -p "$ROOT_MNT/etc/default"
cat > "$ROOT_MNT/etc/default/keyboard" << 'KBEOF'
XKBMODEL="pc105"
XKBLAYOUT="us"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
KBEOF
# Set locale to en_US.UTF-8
cat > "$ROOT_MNT/etc/default/locale" << 'LCEOF'
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
LANGUAGE=en_US.UTF-8
LCEOF
run_chroot "locale-gen en_US.UTF-8" || true
run_chroot "update-locale LANG=en_US.UTF-8" || true
ok "Raspbian first-run wizard disabled, pi user pre-configured"

# ------------------------------------------------------------------ #
# APT packages
# ------------------------------------------------------------------ #
log "Updating APT package lists..."
run_chroot "apt-get update -qq" || warn "apt-get update had issues"

log "Installing system packages..."
PACKAGES=(
    # Audio
    alsa-utils
    # Serial
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
    # Hamlib
    libhamlib-utils
    # Build tools
    gcc
    cmake
    python3-setuptools
    python3-wheel
    # System
    git
    curl
    # PiTFT display
    i2c-tools
    # Audio for freedv
    portaudio19-dev
    libportaudio2
    libasound2-dev
    libcodec2-dev
    read-edid
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
    audioop-lts
)

for pkg in "${PIP_PACKAGES[@]}"; do
    log "  pip install $pkg..."
    run_chroot "pip3 install --break-system-packages --quiet $pkg" \
        || warn "pip install $pkg failed (non-fatal)"
done
ok "Python packages installed"

# ------------------------------------------------------------------ #
# Build codec2 from source (required for freedvtnc2)
# ------------------------------------------------------------------ #
log "Building codec2 from source (required for freedvtnc2)..."
run_chroot "
cd /tmp
rm -rf codec2
git clone --depth 1 --branch 1.2.0 https://github.com/drowe67/codec2.git codec2
mkdir -p codec2/build
cd codec2/build
cmake .. -DCMAKE_INSTALL_PREFIX=/usr/local -DINSTALL_EXAMPLES=OFF
make -j\$(nproc)
make install
ldconfig
echo 'codec2 1.2.0 installed OK'
grep -r 'FREEDV_MODE_DATAC4' /usr/local/include/codec2/freedv_api.h | head -1 \
    && echo 'DATAC4 confirmed in codec2 headers'
"

# ------------------------------------------------------------------ #
# Install freedvtnc2
# ------------------------------------------------------------------ #
# Force compiler to use our codec2 1.2.0, not system 1.0.5
run_chroot "apt-get remove -y libcodec2-dev libcodec2-1.0 2>/dev/null || true"
run_chroot "ldconfig"
log "Installing freedvtnc2 (LFManifesto fork)..."
run_chroot "pip3 install --break-system-packages \
    git+https://github.com/LFManifesto/freedvtnc2.git" \
    || warn "freedvtnc2 install failed - may need manual install"

# Verify installation
if run_chroot "python3 -m freedvtnc2 --help > /dev/null 2>&1"; then
    ok "freedvtnc2 installed as Python module"
else
    warn "freedvtnc2 binary not found - will need manual install"
fi

# ------------------------------------------------------------------ #
# Create directory structure
# ------------------------------------------------------------------ #
log "Creating HF-256 directory structure..."
run_chroot "mkdir -p \
    /opt/hf256/hf256 \
    /opt/hf256/portal/templates \
    /opt/hf256/portal/static \
    /opt/hf256/scripts \
    /opt/hf256/configs \
    /etc/hf256 \
    /var/log \
    /home/pi/.hf256/downloads \
    /home/pi/.hf256/hub_messages \
    /home/pi/.hf256/hub_files"
run_chroot "chown -R pi:pi /home/pi/.hf256"
ok "Directory structure created"

# ------------------------------------------------------------------ #
# Copy application files
# ------------------------------------------------------------------ #
log "Copying HF-256 application files..."

# hf256 Python package
rsync -a --quiet "$PROJECT_DIR/hf256/" \
    "$ROOT_MNT/opt/hf256/hf256/"

# Portal
rsync -a --quiet "$PROJECT_DIR/portal/" \
    "$ROOT_MNT/opt/hf256/portal/"

# Scripts
rsync -a --quiet "$PROJECT_DIR/scripts/" \
    "$ROOT_MNT/opt/hf256/scripts/"
chmod +x "$ROOT_MNT/opt/hf256/scripts/"*.sh \
          "$ROOT_MNT/opt/hf256/scripts/"*.py 2>/dev/null || true

# Configs
rsync -a --quiet "$PROJECT_DIR/configs/" \
    "$ROOT_MNT/opt/hf256/configs/"

ok "Application files copied"

# ------------------------------------------------------------------ #
# Install configs to system locations
# ------------------------------------------------------------------ #
log "Installing system configs..."

# ALSA
cp "$PROJECT_DIR/configs/asound.conf" \
   "$ROOT_MNT/etc/asound.conf"

# hostapd
mkdir -p "$ROOT_MNT/etc/hostapd"
cp "$PROJECT_DIR/configs/hostapd.conf" \
   "$ROOT_MNT/etc/hostapd/hostapd.conf"

# Point hostapd to config
sed -i 's|^#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' \
    "$ROOT_MNT/etc/default/hostapd" 2>/dev/null || \
    echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' \
    >> "$ROOT_MNT/etc/default/hostapd"

# dnsmasq
mkdir -p "$ROOT_MNT/etc/dnsmasq.d"
cp "$PROJECT_DIR/configs/dnsmasq.conf" \
   "$ROOT_MNT/etc/dnsmasq.d/hf256.conf"

# Disable dnsmasq default config to avoid conflicts
echo "port=0" > "$ROOT_MNT/etc/dnsmasq.d/00-disable-default.conf"

# hostapd rfkill override
mkdir -p "$ROOT_MNT/etc/systemd/system/hostapd.service.d"
cp "$PROJECT_DIR/configs/hostapd-rfkill.conf" \
   "$ROOT_MNT/etc/systemd/system/hostapd.service.d/rfkill.conf"

ok "System configs installed"

# ------------------------------------------------------------------ #
# Write default /etc/hf256/settings.json
# ------------------------------------------------------------------ #
log "Writing default settings.json..."
mkdir -p "$ROOT_MNT/etc/hf256"
cat > "$ROOT_MNT/etc/hf256/settings.json" << 'SETTINGS'
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
chmod 644 "$ROOT_MNT/etc/hf256/settings.json"
echo "pi ALL=(ALL) NOPASSWD: /sbin/poweroff, /sbin/shutdown, /sbin/reboot" \
    > "$ROOT_MNT/etc/sudoers.d/hf256"
ok "Default settings.json written"

# ------------------------------------------------------------------ #
# Install systemd services
# ------------------------------------------------------------------ #
log "Installing systemd services..."

for svc in "$PROJECT_DIR/services/"*.service; do
    svc_name=$(basename "$svc")
    cp "$svc" "$ROOT_MNT/etc/systemd/system/$svc_name"
    log "  Installed: $svc_name"
done

# Enable services
run_chroot "systemctl enable \
    hf256-firstboot.service \
    hf256-portal.service \
    hf256-display.service \
    hf256.service \
    hf256-wlan.service \
    freedvtnc2.service" \
    || warn "Some services failed to enable"

# Disable services that conflict with AP mode
run_chroot "systemctl disable \
    wpa_supplicant.service \
    dhcpcd.service \
    NetworkManager.service 2>/dev/null || true"

# Mask services to prevent interference
run_chroot "systemctl mask NetworkManager 2>/dev/null || true"
run_chroot "systemctl mask wpa_supplicant@wlan0.service 2>/dev/null || true"

ok "Systemd services installed and enabled"

# ------------------------------------------------------------------ #
# Configure serial port access for pi user
# ------------------------------------------------------------------ #
log "Adding pi to dialout and audio groups..."
run_chroot "usermod -a -G dialout,audio,spi,gpio pi"
ok "Group memberships updated"

# ------------------------------------------------------------------ #
# Disable Bluetooth to free UART
# ------------------------------------------------------------------ #
log "Disabling Bluetooth..."
BOOT_CFG="$BOOT_MNT/config.txt"
if ! grep -q "dtoverlay=disable-bt" "$BOOT_CFG"; then
    echo "" >> "$BOOT_CFG"
    echo "# HF-256 - Disable Bluetooth to free UART" >> "$BOOT_CFG"
    echo "dtoverlay=disable-bt" >> "$BOOT_CFG"
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
# Remove swap (not needed, reduces SD wear)
# ------------------------------------------------------------------ #
log "Disabling swap..."
run_chroot "systemctl disable dphys-swapfile 2>/dev/null || true"
run_chroot "apt-get remove -y dphys-swapfile 2>/dev/null || true"
ok "Swap disabled"

# ------------------------------------------------------------------ #
# Clean up chroot
# ------------------------------------------------------------------ #
log "Cleaning APT cache in image..."
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

# Disable trap now that we've cleaned up manually
trap - EXIT

# ------------------------------------------------------------------ #
# Compress output
# ------------------------------------------------------------------ #
log "Moving working image to output..."
mv "$WORK_IMG" "$OUTPUT_IMG"

log "Compressing image with xz (this takes 10-20 minutes)..."
xz -T0 -v "$OUTPUT_IMG"
ok "Compressed: ${OUTPUT_IMG}.xz ($(du -h "${OUTPUT_IMG}.xz" | cut -f1))"

# ------------------------------------------------------------------ #
# Done
# ------------------------------------------------------------------ #
log "=================================================="
log " HF-256 Image Build Complete"
log "=================================================="
log " Output: ${OUTPUT_IMG}.xz"
log " Log:    $LOG"
log ""
log " Flash with:"
log "   xz -dk ${OUTPUT_IMG}.xz"
log "   sudo dd if=${OUTPUT_IMG} of=/dev/sdX bs=4M status=progress"
log "   # or use Raspberry Pi Imager"
log "=================================================="
