#!/usr/bin/env python3
"""
HF-256 PiTFT 1.3" ST7789 Status Display Daemon
Hardware: Adafruit Mini PiTFT 1.3" 240x240 ST7789 SPI
GPIO CS: CE0, DC: 25, RST: 24, Backlight: 26
Buttons: GPIO 23 (A), GPIO 24 (B)
Gracefully runs in headless mode if display hardware is absent.
"""
import time
import json
import subprocess
import signal
import sys
import os
import logging
from pathlib import Path

log = logging.getLogger("hf256.display")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/var/log/hf256-display.log"),
        logging.StreamHandler()
    ]
)

SETTINGS_FILE = "/etc/hf256/settings.json"
CONFIG_ENV    = "/etc/hf256/config.env"
SETUP_FLAG    = "/etc/hf256/.setup_complete"

WIDTH  = 240
HEIGHT = 240

# Button GPIO pins (BCM)
BUTTON_A = 23
BUTTON_B = 24

# Colors (RGB)
BLACK      = (0,   0,   0)
WHITE      = (255, 255, 255)
DARK_BLUE  = (31,  56,  100)
MID_BLUE   = (46,  117, 182)
LIGHT_BLUE = (173, 216, 230)
GREEN      = (0,   200, 0)
RED        = (220, 50,  50)
ORANGE     = (255, 165, 0)
GRAY       = (150, 150, 150)
YELLOW     = (255, 220, 0)

REFRESH_INTERVAL = 2
AP_RESET_HOLD    = 5


def init_display():
    try:
        import board
        import busio
        import digitalio
        from adafruit_rgb_display import st7789

        bl = digitalio.DigitalInOut(board.D26)
        bl.direction = digitalio.Direction.OUTPUT
        bl.value = True

        spi = busio.SPI(clock=board.SCK, MOSI=board.MOSI)
        dc  = digitalio.DigitalInOut(board.D25)
        cs  = digitalio.DigitalInOut(board.CE0)
        rst = digitalio.DigitalInOut(board.D24)

        disp = st7789.ST7789(
            spi, dc=dc, cs=cs, rst=rst,
            baudrate=24000000,
            width=WIDTH, height=HEIGHT,
            y_offset=80, rotation=180
        )
        log.info("ST7789 display initialized")
        return disp
    except ImportError as e:
        log.warning("Display libraries not available: %s", e)
        return None
    except Exception as e:
        log.error("Display init failed: %s", e)
        return None


def init_buttons():
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(BUTTON_A, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(BUTTON_B, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        log.info("Buttons initialized on GPIO %d and %d", BUTTON_A, BUTTON_B)
        return GPIO
    except ImportError:
        log.warning("RPi.GPIO not available - buttons disabled")
        return None
    except Exception as e:
        log.error("Button init failed: %s", e)
        return None


def get_ip_address():
    for iface in ["wlan0", "eth0"]:
        try:
            result = subprocess.run(
                ["ip", "addr", "show", iface],
                capture_output=True, text=True, timeout=3
            )
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("inet ") and "127." not in line:
                    return line.split()[1].split("/")[0]
        except Exception:
            pass
    return "No IP"


def get_uptime():
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days  = int(secs // 86400)
        hours = int((secs % 86400) // 3600)
        mins  = int((secs % 3600) // 60)
        if days > 0:
            return f"{days}d {hours}h {mins}m"
        elif hours > 0:
            return f"{hours}h {mins}m"
        else:
            return f"{mins}m"
    except Exception:
        return "unknown"


def get_hostname():
    try:
        return subprocess.run(
            ["hostname"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except Exception:
        return "hf256"


def load_settings():
    defaults = {
        "callsign": "N0CALL",
        "role": "",
        "wifi_mode": "ap",
        "ap_ssid": "HF256-N0CALL",
        "encryption_enabled": True
    }
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        defaults.update(data)
    except Exception:
        pass
    return defaults


def load_config_env():
    config = {}
    try:
        with open(CONFIG_ENV) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip().strip('"')
    except Exception:
        pass
    return config


def svc_active(name):
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def get_connection_state():
    setup_done = Path(SETUP_FLAG).exists()
    if not setup_done:
        return "SETUP NEEDED", YELLOW
    hf256_ok    = svc_active("hf256")
    freedv_ok   = svc_active("freedvtnc2")
    if hf256_ok and freedv_ok:
        return "RUNNING", GREEN
    elif hf256_ok or freedv_ok:
        return "PARTIAL", ORANGE
    else:
        return "STOPPED", RED


def build_frame(settings, config, ip, uptime):
    from PIL import Image, ImageDraw, ImageFont

    img  = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
    draw = ImageDraw.Draw(img)

    try:
        font_lg   = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        font_md   = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        font_sm   = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        font_xs   = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        font_bold = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    except Exception:
        font_lg = font_md = font_sm = font_xs = font_bold = ImageFont.load_default()

    callsign = settings.get("callsign", "N0CALL")
    role     = settings.get("role", "").upper() or "?"
    wifi     = settings.get("wifi_mode", "ap").upper()
    ssid     = settings.get("ap_ssid", "HF256-N0CALL")
    enc      = "ENC" if settings.get("encryption_enabled", True) else "OPEN"
    mode     = config.get("FREEDV_MODE", "DATAC1")

    state, state_color = get_connection_state()

    # Header bar
    draw.rectangle([(0, 0), (WIDTH, 34)], fill=DARK_BLUE)
    draw.text((8, 6), "HF-256", font=font_lg, fill=WHITE)
    draw.text((110, 8), callsign, font=font_md, fill=LIGHT_BLUE)

    # Divider
    draw.line([(0, 35), (WIDTH, 35)], fill=MID_BLUE, width=1)

    # Info rows
    rows = [
        ("IP",    ip,           WHITE),
        ("Role",  role,         LIGHT_BLUE),
        ("WiFi",  wifi,         LIGHT_BLUE),
        ("Mode",  mode,         LIGHT_BLUE),
        ("Sec",   enc,          GREEN if enc == "ENC" else ORANGE),
    ]

    y = 42
    for label, value, color in rows:
        draw.text((8,   y), label + ":", font=font_sm, fill=GRAY)
        draw.text((85,  y), value,       font=font_sm, fill=color)
        y += 26

    # Status bar
    draw.line([(0, y), (WIDTH, y)], fill=MID_BLUE, width=1)
    y += 6
    draw.text((8, y), "Status:", font=font_bold, fill=GRAY)
    draw.text((90, y), state,    font=font_bold, fill=state_color)

    # Footer
    draw.line([(0, 218), (WIDTH, 218)], fill=DARK_BLUE, width=1)
    draw.text((8, 222),   f"Up: {uptime}", font=font_xs, fill=GRAY)
    draw.text((150, 222), "hf256.local",   font=font_xs, fill=GRAY)

    return img


def check_buttons(gpio, hold_starts):
    """
    Check button states and return action string or None.
    Button A + B held 5s = AP reset
    Button B alone held 5s = graceful shutdown
    """
    if gpio is None:
        return None
    try:
        a = not gpio.input(BUTTON_A)
        b = not gpio.input(BUTTON_B)
        now = time.time()
        if a and b:
            hold_starts['shutdown'] = None
            if hold_starts['ap_reset'] is None:
                hold_starts['ap_reset'] = now
            elif now - hold_starts['ap_reset'] >= AP_RESET_HOLD:
                hold_starts['ap_reset'] = None
                return 'ap_reset'
        elif b and not a:
            hold_starts['ap_reset'] = None
            if hold_starts['shutdown'] is None:
                hold_starts['shutdown'] = now
            elif now - hold_starts['shutdown'] >= AP_RESET_HOLD:
                hold_starts['shutdown'] = None
                return 'shutdown'
        else:
            hold_starts['ap_reset'] = None
            hold_starts['shutdown'] = None
    except Exception:
        pass
    return None


def trigger_ap_reset():
    log.info("AP reset triggered by button hold")
    try:
        settings = load_settings()
        ssid = settings.get("ap_ssid", "HF256-N0CALL")
        password = settings.get("ap_password", "hf256setup")
        subprocess.run(
            ["/opt/hf256/scripts/wifi-mode.sh", "ap", ssid, password],
            capture_output=True, timeout=60
        )
    except Exception as e:
        log.error("AP reset failed: %s", e)


def trigger_shutdown(disp):
    log.info("Graceful shutdown triggered by button hold")
    try:
        if disp is not None:
            from PIL import Image, ImageDraw, ImageFont
            img  = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
            except Exception:
                font = ImageFont.load_default()
            draw.rectangle([(0, 0), (WIDTH, HEIGHT)], fill=DARK_BLUE)
            draw.text((50, 90),  "Shutting",  font=font, fill=WHITE)
            draw.text((70, 120), "Down...",   font=font, fill=ORANGE)
            disp.image(img)
            time.sleep(2)
        subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True)
    except Exception as e:
        log.error("Shutdown failed: %s", e)


def run_display_loop(disp):
    gpio        = init_buttons()
    hold_starts = {'ap_reset': None, 'shutdown': None}
    running     = [True]

    def handle_signal(sig, frame):
        log.info("Display daemon shutting down")
        running[0] = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log.info("Display loop started (hardware=%s)", disp is not None)

    while running[0]:
        try:
            action = check_buttons(gpio, hold_starts)
            if action == 'ap_reset':
                trigger_ap_reset()
            elif action == 'shutdown':
                trigger_shutdown(disp)
                break
            settings = load_settings()
            config   = load_config_env()
            ip       = get_ip_address()
            uptime   = get_uptime()
            if disp is not None:
                img = build_frame(settings, config, ip, uptime)
                disp.image(img)
            else:
                state, _ = get_connection_state()
                log.debug("Headless state: IP=%s status=%s uptime=%s",
                          ip, state, uptime)
        except Exception as e:
            log.error("Display loop error: %s", e, exc_info=True)
        time.sleep(REFRESH_INTERVAL)
    log.info("Display loop exited")


def main():
    log.info("HF-256 display daemon starting")
    disp = init_display()
    if disp is None:
        log.warning("No display hardware - running in headless mode")
    run_display_loop(disp)


if __name__ == "__main__":
    main()
