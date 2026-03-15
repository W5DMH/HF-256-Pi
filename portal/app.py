#!/usr/bin/env python3
"""
HF-256 Web Configuration Portal
Flask app serving setup wizard, status dashboard, settings page,
and the HF-256 Console terminal.
Adapted from ReticulumHF setup-portal/app.py.
Runs as root on port 80 via hf256-portal.service.
"""

import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for
)
from flask_sock import Sock

from hardware import (
    load_radios, detect_serial_ports, detect_audio_devices,
    find_digirig, find_x6100, test_cat_connection, test_ptt,
    release_ptt, set_audio_levels, get_audio_controls,
    get_system_info, get_audio_levels
)

# ------------------------------------------------------------------ #
# Paths
# ------------------------------------------------------------------ #
CONFIG_DIR        = Path("/etc/hf256")
SETUP_FLAG        = CONFIG_DIR / ".setup_complete"
CONFIG_ENV        = CONFIG_DIR / "config.env"
SETTINGS_FILE     = CONFIG_DIR / "settings.json"
KEY_FILE          = CONFIG_DIR / "network.key"
BACKUPS_DIR       = CONFIG_DIR / "backups"
HOSTAPD_CONF      = Path("/etc/hostapd/hostapd.conf")
ASOUND_CONF       = Path("/etc/asound.conf")
FREEDVTNC2_BIN    = Path("/usr/bin/python3")
ARDOPC_BIN        = Path("/usr/local/bin/ardopc")

# ------------------------------------------------------------------ #
# App
# ------------------------------------------------------------------ #
app  = Flask(__name__)
sock = Sock(app)


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = \
        "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]  = "no-cache"
    response.headers["Expires"] = "-1"
    response.headers.pop("ETag", None)
    return response


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def is_setup_complete() -> bool:
    return SETUP_FLAG.exists()


def load_settings() -> dict:
    defaults = {
        "callsign":           "N0CALL",
        "role":               "",
        "hub_address":        "",
        "encryption_enabled": True,
        "network_key_set":    False,
        "wifi_mode":          "ap",
        "ap_ssid":            "HF256-N0CALL",
        "ap_password":        "hf256setup",
        "client_ssid":        "",
        "client_password":    ""
    }
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        defaults.update(data)
    except Exception:
        pass
    return defaults


def save_settings(data: dict) -> bool:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(SETTINGS_FILE, 0o644)
        return True
    except Exception as e:
        app.logger.error("save_settings error: %s", e)
        return False


def load_config_env() -> dict:
    """Load /etc/hf256/config.env key=value pairs."""
    config = {}
    try:
        with open(CONFIG_ENV) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    config[key.strip()] = val.strip().strip('"')
    except Exception:
        pass
    return config


def get_radio_by_id(radio_id: str) -> Optional[dict]:
    radios = load_radios()
    return next((r for r in radios if r["id"] == radio_id), None)


def generate_freedvtnc2_command(radio_id: str, serial_port: str,
                                 audio_card: int,
                                 freedv_mode: str = "DATAC1",
                                 tx_volume: int = 0) -> str:
    radio = get_radio_by_id(radio_id)
    if not radio:
        raise ValueError(f"Unknown radio: {radio_id}")

    valid_modes = ["DATAC0", "DATAC1", "DATAC3", "DATAC4"]
    if freedv_mode not in valid_modes:
        freedv_mode = "DATAC1"

    tx_volume = max(-20, min(0, tx_volume))

    ptt_method   = radio.get("ptt_method", "RTS")
    use_vox      = (not serial_port or
                    ptt_method.upper() == "VOX")
    rigctld_port = "0" if use_vox else "4532"

    ptt_on  = radio.get("ptt_on_delay_ms", 300)
    ptt_off = radio.get("ptt_off_delay_ms", 200)

    parts = [
        str(FREEDVTNC2_BIN),
        "-m freedvtnc2",
        "--no-cli",
        f"--input-device {audio_card}",
        f"--output-device {audio_card}",
        f"--mode {freedv_mode}",
        f"--rigctld-port {rigctld_port}",
        "--kiss-tcp-port 8001",
        "--kiss-tcp-address 0.0.0.0",
        "--cmd-port 8002",
        "--cmd-address 0.0.0.0",
        f"--ptt-on-delay-ms {ptt_on}",
        f"--ptt-off-delay-ms {ptt_off}",
        f"--output-volume {tx_volume}"
    ]
    return " ".join(parts)


def generate_rigctld_command(radio_id: str,
                              serial_port: str) -> str:
    radio = get_radio_by_id(radio_id)
    if not radio:
        raise ValueError(f"Unknown radio: {radio_id}")

    hamlib_id  = radio["hamlib_id"]
    ptt_method = radio.get("ptt_method", "RTS")
    baud_rate  = radio.get("baud_rate", 9600)

    # Hamlib model 1 (dummy) is used for DigiRig-style interfaces where
    # there is no CAT control — only RTS/DTR PTT on the serial port.
    # For model 1:  -p <port> -P RTS  (PTT port, NOT -r which is CAT port)
    # For real rigs: -r <port> -s <baud> -P <method> (CAT + PTT)
    if hamlib_id == 1:
        parts = [
            "rigctld",
            "-m 1",
            f"-p {serial_port}",
            f"-P {ptt_method}",
            "-t 4532"
        ]
    else:
        parts = [
            "rigctld",
            f"-m {hamlib_id}",
            f"-r {serial_port}",
            f"-s {baud_rate}",
            "-t 4532"
        ]
        if ptt_method and ptt_method.upper() not in ("VOX", ""):
            parts.append(f"-P {ptt_method}")
    return " ".join(parts)


def update_alsa_config(audio_card: int):
    """Write /etc/asound.conf for the configured audio card."""
    content = f"""# HF-256 ALSA Configuration
# Generated by setup wizard for audio card {audio_card}

pcm.!modem {{
    type null
}}
ctl.!modem {{
    type null
}}

pcm.usbaudio {{
    type hw
    card {audio_card}
    device 0
}}
ctl.usbaudio {{
    type hw
    card {audio_card}
}}

defaults.pcm.card 0
defaults.ctl.card 0
"""
    with open(ASOUND_CONF, "w") as f:
        f.write(content)


def freedvtnc2_command(command: str,
                        timeout: float = 5.0) -> Tuple[bool, str]:
    """Send command to freedvtnc2 command port 8002."""
    try:
        sock_cmd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock_cmd.settimeout(timeout)
        sock_cmd.connect(("127.0.0.1", 8002))
        sock_cmd.send(f"{command}\n".encode())
        response = sock_cmd.recv(1024).decode().strip()
        sock_cmd.close()
        return response.startswith("OK"), response
    except ConnectionRefusedError:
        return False, "ERROR freedvtnc2 not running"
    except Exception as e:
        return False, f"ERROR {e}"


def validate_callsign(call: str) -> bool:
    """Validate amateur callsign format."""
    if not call:
        return False
    pattern = r"^[A-Z]{1,2}\d[A-Z0-9]{1,4}$"
    return bool(re.match(pattern, call.upper().strip()))


# ------------------------------------------------------------------ #
# Page routes
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    if is_setup_complete():
        return redirect(url_for("status"))
    radios = load_radios()
    manufacturers = {}
    for r in radios:
        mfr = r["manufacturer"]
        if mfr not in manufacturers:
            manufacturers[mfr] = []
        manufacturers[mfr].append(r)
    return render_template("setup.html",
                           manufacturers=manufacturers,
                           system_info=get_system_info())


@app.route("/status")
def status():
    return render_template("status.html",
                           system_info=get_system_info(),
                           settings=load_settings())


@app.route("/settings")
def settings_page():
    return render_template("settings.html",
                           system_info=get_system_info(),
                           settings=load_settings(),
                           key_configured=KEY_FILE.exists())


@app.route("/console")
def console():
    """Serve the HF-256 Console terminal page."""
    return render_template("console.html",
                           system_info=get_system_info(),
                           settings=load_settings())


# ------------------------------------------------------------------ #
# Setup API
# ------------------------------------------------------------------ #

@app.route("/api/detect-hardware")
def api_detect_hardware():
    serial_ports  = detect_serial_ports()
    audio_devices = detect_audio_devices()
    digirig       = find_digirig()
    x6100         = find_x6100()

    recommended_port  = None
    recommended_audio = None

    if digirig.get("found"):
        recommended_port  = digirig.get("serial_port")
        recommended_audio = digirig.get("audio_card")
    elif x6100.get("found"):
        recommended_port  = x6100.get("serial_port")
        recommended_audio = x6100.get("audio_card")
    elif serial_ports:
        recommended_port = serial_ports[0]["port"]
    if recommended_audio is None:
        recommended_audio = next(
            (d["card"] for d in audio_devices
             if d.get("type") == "usb"), None
        )

    return jsonify({
        "serial_ports":  serial_ports,
        "audio_devices": audio_devices,
        "digirig":       digirig,
        "x6100":         x6100,
        "recommended": {
            "serial_port": recommended_port,
            "audio_card":  recommended_audio
        }
    })


@app.route("/api/test-cat", methods=["POST"])
def api_test_cat():
    data     = request.json or {}
    port     = data.get("port")
    radio_id = data.get("radio_id")
    if not port or not radio_id:
        return jsonify({"success": False,
                        "error": "Missing port or radio_id"}), 400
    return jsonify(test_cat_connection(port, radio_id))


@app.route("/api/test-ptt", methods=["POST"])
def api_test_ptt():
    data     = request.json or {}
    port     = data.get("port")
    radio_id = data.get("radio_id")
    if not port or not radio_id:
        return jsonify({"success": False,
                        "error": "Missing port or radio_id"}), 400
    return jsonify(test_ptt(port, radio_id))


@app.route("/api/release-ptt", methods=["POST"])
def api_release_ptt():
    data     = request.json or {}
    port     = data.get("port")
    radio_id = data.get("radio_id")
    if not port or not radio_id:
        return jsonify({"success": False,
                        "error": "Missing port or radio_id"}), 400
    return jsonify(release_ptt(port, radio_id))


@app.route("/api/set-audio", methods=["POST"])
def api_set_audio():
    data    = request.json or {}
    card    = data.get("card")
    speaker = data.get("speaker", 80)
    mic     = data.get("mic", 75)
    if card is None:
        return jsonify({"success": False, "error": "Missing card"}), 400
    return jsonify(set_audio_levels(card, speaker, mic))


@app.route("/api/audio-level/check/<int:card>")
def api_audio_level(card):
    return jsonify(get_audio_levels(card))


@app.route("/api/complete-setup", methods=["POST"])
def api_complete_setup():
    data        = request.json or {}
    radio_id    = data.get("radio_id")
    serial_port = data.get("serial_port", "")
    audio_card  = data.get("audio_card")
    freedv_mode = data.get("freedv_mode", "DATAC1")

    if not radio_id or audio_card is None:
        return jsonify({"success": False,
                        "error": "Missing radio_id or audio_card"}), 400

    radio = get_radio_by_id(radio_id)
    if not radio:
        return jsonify({"success": False,
                        "error": f"Unknown radio: {radio_id}"}), 400

    try:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

        # Backup existing config if present
        if CONFIG_ENV.exists():
            ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(CONFIG_ENV, BACKUPS_DIR / f"config.env.{ts}")

        # Update ALSA config
        update_alsa_config(audio_card)
        set_audio_levels(audio_card, speaker_pct=80, mic_pct=75)

        # Generate commands
        freedvtnc2_cmd = generate_freedvtnc2_command(
            radio_id, serial_port, audio_card, freedv_mode
        )
        rigctld_cmd = generate_rigctld_command(radio_id, serial_port) \
            if serial_port else ""

        # Write config.env
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        env_content = f"""# HF-256 Hardware Configuration
# Generated by setup wizard {datetime.now().strftime('%Y-%m-%d %H:%M')}

RADIO_ID={radio_id}
SERIAL_PORT={serial_port}
AUDIO_CARD={audio_card}
FREEDV_MODE={freedv_mode}
TX_OUTPUT_VOLUME=0

RIGCTLD_CMD="{rigctld_cmd}"
FREEDVTNC2_CMD="{freedvtnc2_cmd}"
"""
        with open(CONFIG_ENV, "w") as f:
            f.write(env_content)
        os.chmod(CONFIG_ENV, 0o644)

        # Mark setup complete
        SETUP_FLAG.touch()
        os.chmod(SETUP_FLAG, 0o644)

        # Start services
        subprocess.run(["systemctl", "daemon-reload"],
                       capture_output=True)
        if serial_port:
            subprocess.run(["systemctl", "enable", "--now", "rigctld"],
                           capture_output=True)
        subprocess.run(["systemctl", "enable", "--now", "freedvtnc2"],
                       capture_output=True)

        # Wait for freedvtnc2 KISS port
        for _ in range(30):
            result = subprocess.run(
                ["ss", "-tln"], capture_output=True, text=True
            )
            if ":8001" in result.stdout:
                break
            time.sleep(1)

        subprocess.run(["systemctl", "restart", "hf256"],
                       capture_output=True)

        return jsonify({
            "success": True,
            "message": "Setup complete - go to Settings to configure "
                       "callsign, encryption key, and station role"
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------ #
# Settings API
# ------------------------------------------------------------------ #

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    settings = load_settings()
    settings["network_key_set"] = KEY_FILE.exists()
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data    = request.json or {}
    current = load_settings()

    # Validate callsign if provided
    callsign = data.get("callsign", current["callsign"]).upper().strip()
    if not validate_callsign(callsign):
        return jsonify({"success": False,
                        "error": f"Invalid callsign: {callsign}"}), 400

    # Validate role
    role = data.get("role", current["role"]).lower()
    if role and role not in ("hub", "spoke"):
        return jsonify({"success": False,
                        "error": "Role must be hub or spoke"}), 400

    # Update settings
    current.update({
        "callsign":           callsign,
        "role":               role,
        "hub_address":        data.get("hub_address",
                                       current["hub_address"]),
        "encryption_enabled": data.get("encryption_enabled",
                                       current["encryption_enabled"]),
        "wifi_mode":          data.get("wifi_mode",
                                       current["wifi_mode"]),
        "ap_ssid":            data.get("ap_ssid",
                                       current["ap_ssid"]),
        "ap_password":        data.get("ap_password",
                                       current["ap_password"]),
        "client_ssid":        data.get("client_ssid",
                                       current["client_ssid"]),
        "client_password":    data.get("client_password",
                                       current["client_password"]),
    })
    current["network_key_set"] = KEY_FILE.exists()

    if save_settings(current):
        if callsign and callsign != "N0CALL" and role:
            SETUP_FLAG.touch()
            os.chmod(SETUP_FLAG, 0o644)
        return jsonify({"success": True,
                        "message": "Settings saved",
                        "restart_required": True})
    return jsonify({"success": False,
                    "error": "Failed to save settings"}), 500


@app.route("/api/key/generate", methods=["POST"])
def api_generate_key():
    try:
        import base64
        key = os.urandom(32)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)
        key_b64 = base64.b64encode(key).decode()

        settings = load_settings()
        settings["network_key_set"] = True
        save_settings(settings)

        return jsonify({
            "success":  True,
            "key_b64":  key_b64,
            "message":  "New 256-bit key generated. "
                        "Copy this key to all stations on your network."
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/key/import", methods=["POST"])
def api_import_key():
    try:
        import base64
        data    = request.json or {}
        key_b64 = data.get("key_b64", "").strip()
        if not key_b64:
            return jsonify({"success": False,
                            "error": "No key provided"}), 400
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            return jsonify({"success": False,
                            "error": f"Key must be 32 bytes, "
                                     f"got {len(key)}"}), 400

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)

        settings = load_settings()
        settings["network_key_set"] = True
        save_settings(settings)

        return jsonify({"success": True,
                        "message": "Key imported successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/key/export", methods=["GET"])
def api_export_key():
    try:
        import base64
        if not KEY_FILE.exists():
            return jsonify({"success": False,
                            "error": "No key configured"}), 404
        with open(KEY_FILE, "rb") as f:
            key = f.read()
        return jsonify({
            "success": True,
            "key_b64": base64.b64encode(key).decode()
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/wifi-mode", methods=["POST"])
def api_wifi_mode():
    data = request.json or {}
    mode = data.get("mode", "ap")

    settings = load_settings()

    if mode == "ap":
        ssid     = data.get("ssid",     settings["ap_ssid"])
        password = data.get("password", settings["ap_password"])
        settings["wifi_mode"]   = "ap"
        settings["ap_ssid"]     = ssid
        settings["ap_password"] = password
        save_settings(settings)

        result = subprocess.run(
            ["/opt/hf256/scripts/wifi-mode.sh", "ap", ssid, password],
            capture_output=True, text=True, timeout=30
        )
        return jsonify({
            "success": result.returncode == 0,
            "message": f"AP mode: {ssid}",
            "output":  result.stdout[-500:]
        })

    elif mode == "client":
        ssid     = data.get("ssid", "")
        password = data.get("password", "")
        if not ssid:
            return jsonify({"success": False,
                            "error": "SSID required"}), 400

        settings["wifi_mode"]       = "client"
        settings["client_ssid"]     = ssid
        settings["client_password"] = password
        save_settings(settings)

        result = subprocess.run(
            ["/opt/hf256/scripts/wifi-mode.sh",
             "client", ssid, password],
            capture_output=True, text=True, timeout=45
        )
        # Get new IP
        new_ip = None
        try:
            r2 = subprocess.run(
                ["ip", "addr", "show", "wlan0"],
                capture_output=True, text=True, timeout=5
            )
            for line in r2.stdout.split("\n"):
                line = line.strip()
                if line.startswith("inet ") and "127." not in line:
                    new_ip = line.split()[1].split("/")[0]
                    break
        except Exception:
            pass

        return jsonify({
            "success":  result.returncode == 0,
            "message":  f"Client mode: {ssid}",
            "new_ip":   new_ip,
            "warning":  "Portal now at http://" + (new_ip or "IP-on-display")
        })

    return jsonify({"success": False,
                    "error": f"Unknown mode: {mode}"}), 400


# ------------------------------------------------------------------ #
# Status and service control API
# ------------------------------------------------------------------ #

@app.route("/api/service-status")
def api_service_status():
    def svc_active(name):
        try:
            r = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True, text=True, timeout=5
            )
            return r.stdout.strip() == "active"
        except Exception:
            return False

    settings  = load_settings()
    wifi_mode = settings.get("wifi_mode", "ap")
    ap_ssid   = settings.get("ap_ssid", "HF256-N0CALL")
    cl_ssid   = settings.get("client_ssid", "")
    ssid_disp = ap_ssid if wifi_mode == "ap" else cl_ssid

    return jsonify({
        "setup_complete": is_setup_complete(),
        "callsign": settings.get("callsign", "N0CALL"),
        "role":     settings.get("role", ""),
        "hf256":      {"running": svc_active("hf256")},
        "freedvtnc2": {"running": svc_active("freedvtnc2")},
        "rigctld":    {"running": svc_active("rigctld")},
        "portal":     {"running": svc_active("hf256-portal")},
        "ardopc":     {"running": _modem_manager.ardop_running()},
        "wifi": {
            "mode":       wifi_mode,
            "ssid":       ssid_disp,
            "ap_running": svc_active("hostapd")
        }
    })


@app.route("/api/system-health")
def api_system_health():
    health = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            health["cpu_temp"] = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        health["cpu_temp"] = None

    try:
        with open("/proc/uptime") as f:
            secs  = float(f.read().split()[0])
            days  = int(secs // 86400)
            hours = int((secs % 86400) // 3600)
            mins  = int((secs % 3600) // 60)
            health["uptime"] = (f"{days}d {hours}h {mins}m" if days
                                else f"{hours}h {mins}m")
    except Exception:
        health["uptime"] = None

    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        if total:
            used = total - avail
            health["memory_percent"] = round(used / total * 100, 1)
            health["memory_used"]    = f"{used // 1024}MB"
            health["memory_total"]   = f"{total // 1024}MB"
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["df", "-h", "/"], capture_output=True,
            text=True, timeout=5
        )
        lines = r.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            health["disk_percent"] = int(parts[4].rstrip("%"))
            health["disk_used"]    = parts[2]
            health["disk_total"]   = parts[1]
    except Exception:
        pass

    return jsonify(health)


@app.route("/api/service/<service>/<action>", methods=["POST"])
def api_service_control(service, action):
    allowed = ["hf256", "freedvtnc2", "rigctld",
               "hostapd", "dnsmasq", "hf256-portal"]
    if service not in allowed:
        return jsonify({"success": False,
                        "error": f"Unknown service: {service}"}), 400
    if action not in ("start", "stop", "restart"):
        return jsonify({"success": False,
                        "error": f"Invalid action: {action}"}), 400
    try:
        r = subprocess.run(
            ["systemctl", action, service],
            capture_output=True, text=True, timeout=30
        )
        return jsonify({"success": r.returncode == 0,
                        "output": r.stderr.strip()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/restart-services", methods=["POST"])
def api_restart_services():
    try:
        subprocess.run(["systemctl", "restart", "freedvtnc2"],
                       capture_output=True, timeout=15)
        for _ in range(30):
            r = subprocess.run(
                ["ss", "-tln"], capture_output=True, text=True
            )
            if ":8001" in r.stdout:
                break
            time.sleep(1)
        subprocess.run(["systemctl", "restart", "hf256"],
                       capture_output=True, timeout=15)
        return jsonify({"success": True,
                        "message": "Services restarted"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reset-setup", methods=["POST"])
def api_reset_setup():
    try:
        subprocess.run(["systemctl", "stop", "hf256", "freedvtnc2"],
                       capture_output=True)
        if SETUP_FLAG.exists():
            SETUP_FLAG.unlink()
        if CONFIG_ENV.exists():
            CONFIG_ENV.unlink()
        return jsonify({"success": True,
                        "message": "Setup reset - redirecting to wizard"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/logs/<service>")
def api_logs(service):
    allowed = ["hf256", "freedvtnc2", "rigctld",
               "hf256-portal", "hf256-firstboot",
               "hostapd", "dnsmasq"]
    if service not in allowed:
        return jsonify({"success": False,
                        "error": f"Unknown service: {service}"}), 400
    lines = min(request.args.get("lines", 50, type=int), 200)
    try:
        r = subprocess.run(
            ["journalctl", "-u", service,
             "--no-pager", "-n", str(lines)],
            capture_output=True, text=True, timeout=10
        )
        return jsonify({"success": True,
                        "service": service,
                        "logs":    r.stdout or "No logs"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/modem-status")
def api_modem_status():
    ok, response = freedvtnc2_command("STATUS")
    if not ok:
        return jsonify({"success": False, "online": False,
                        "error": response})
    status = {"success": True, "online": True}
    if response.startswith("OK STATUS "):
        for part in response[10:].split():
            if "=" in part:
                k, v = part.split("=", 1)
                status[k.lower()] = v
    return jsonify(status)


@app.route("/api/set-freedv-mode", methods=["POST"])
def api_set_freedv_mode():
    data = request.json or {}
    mode = data.get("mode", "DATAC1")
    valid = ["DATAC1", "DATAC3", "DATAC4"]
    if mode not in valid:
        return jsonify({"success": False,
                        "error": f"Mode must be one of {valid}"}), 400
    ok, response = freedvtnc2_command(f"MODE {mode}")
    return jsonify({"success": ok, "mode": mode,
                    "response": response})


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    subprocess.Popen("sleep 2 && reboot", shell=True)
    return jsonify({"success": True,
                    "message": "Rebooting in 2 seconds..."})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    subprocess.Popen("sleep 2 && poweroff", shell=True)
    return jsonify({"success": True,
                    "message": "Shutting down in 2 seconds..."})


# Captive portal detection
@app.route("/generate_204")
@app.route("/gen_204")
@app.route("/hotspot-detect.html")
@app.route("/ncsi.txt")
@app.route("/connecttest.txt")
def captive_portal():
    return redirect(url_for("index"))


# ------------------------------------------------------------------ #
# Console — WebSocket session
# ------------------------------------------------------------------ #

# ── Hub wire format constants (from project files chat.py) ────────
# Wire: [version:1][flags:1][msg_type:1][timestamp:4][iv:12][payload]
# These must match the hub's chat.py exactly.
_HUB_VERSION       = 0x01
_HUB_FLAG_ENC      = 0x01
_HUB_TYPE_CHAT     = 0x01
_HUB_TYPE_AUTH_REQ = 0x10
_HUB_TYPE_AUTH_RSP = 0x11
_HUB_TYPE_FL_REQ   = 0x02
_HUB_TYPE_FL_RSP   = 0x03
_HUB_TYPE_FILE_DATA= 0x04
_HUB_TYPE_STORE    = 0x12
_HUB_TYPE_RETRIEVE = 0x13
_HUB_TYPE_DL_REQ   = 0x06
_HUB_TYPE_COMPLETE = 0x07
_HUB_TYPE_ERROR    = 0x08


def _chat_payload(sender: str, text: str) -> bytes:
    """Binary chat payload: [sender_len:2][sender][text]"""
    import struct
    sender_bytes = sender.encode('utf-8')
    return struct.pack(">H", len(sender_bytes)) + sender_bytes + text.encode('utf-8')



    """
    Return (aesgcm, key) using the portal's network.key file, or None.
    Uses hub wire format: encrypt returns (nonce, ciphertext) separately.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if KEY_FILE.exists():
            key = KEY_FILE.read_bytes()
            if len(key) == 32:
                return AESGCM(key), key
    except Exception as e:
        app.logger.info("Console: crypto load error: %s", e)
    return None, None


def _hub_crypto():
    """
    Return (aesgcm, key) using the portal's network.key file, or (None, None).
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if KEY_FILE.exists():
            key = KEY_FILE.read_bytes()
            if len(key) == 32:
                return AESGCM(key), key
    except Exception as e:
        pass
    return None, None


def _hub_pack(msg_type: int, payload: bytes, encrypt: bool = True) -> bytes:
    """
    Pack a message in the hub's wire format:
    [version:1][flags:1][msg_type:1][timestamp:4][iv:12][payload+tag]

    AAD = first 7 bytes (version+flags+type+timestamp).
    """
    import struct, os, time
    aesgcm, key = _hub_crypto() if encrypt else (None, None)

    flags     = _HUB_FLAG_ENC if (aesgcm and encrypt) else 0x00
    timestamp = int(time.time())
    aad       = struct.pack(">BBBI", _HUB_VERSION, flags, msg_type, timestamp)

    if aesgcm and encrypt:
        iv         = os.urandom(12)
        ciphertext = aesgcm.encrypt(iv, payload, aad)
        return aad + iv + ciphertext
    else:
        dummy_iv = b"\x00" * 12
        return aad + dummy_iv + payload


def _hub_unpack(data: bytes) -> tuple:
    """
    Unpack a hub wire format message.
    Returns (msg_type, payload, encrypted) or raises ValueError.
    Wire: [version:1][flags:1][msg_type:1][timestamp:4][iv:12][payload]
    """
    import struct
    if len(data) < 19:
        raise ValueError(f"Message too short: {len(data)} bytes")

    version  = data[0]
    flags    = data[1]
    msg_type = data[2]
    iv       = data[7:19]
    body     = data[19:]
    encrypted = bool(flags & _HUB_FLAG_ENC)

    if encrypted:
        aesgcm, key = _hub_crypto()
        if aesgcm:
            aad = data[0:7]
            try:
                payload = aesgcm.decrypt(iv, body, aad)
            except Exception as e:
                raise ValueError(f"Decryption failed: {e}")
        else:
            # No key — cannot decrypt
            raise ValueError("Encrypted message but no key configured")
    else:
        payload = body

    return msg_type, payload, encrypted


# ------------------------------------------------------------------ #
# Modem process manager
# ------------------------------------------------------------------ #

class ModemManager:
    """
    Manages exclusive ownership of the audio card and PTT port across
    the three software modems: freedvtnc2 (FreeDV), ardopc HF, ardopc FM.

    Only one modem may run at a time because all three share the same
    USB audio device and serial PTT port.

    The portal runs as root so subprocess.Popen and systemctl work
    without sudo.

    Usage:
        mm = ModemManager()
        ok, msg = mm.switch_to("ardop-hf")   # stops others, starts ardopc
        ok, msg = mm.switch_to("freedv")      # stops ardopc, starts freedvtnc2
        ok, msg = mm.switch_to("tcp")         # stops all modems
        mm.stop_all()                          # clean shutdown
    """

    # How long to wait for a modem port to become available (seconds)
    _READY_TIMEOUT = 15

    def __init__(self):
        self._ardop_proc  = None   # subprocess.Popen handle for ardopc
        self._lock        = threading.Lock()

    # ── Public API ────────────────────────────────────────────────

    def switch_to(self, transport: str) -> tuple:
        """
        Stop the currently running modem(s) and start the one needed
        for `transport`.  Returns (success: bool, message: str).

        transport values: "tcp", "freedv", "ardop-hf", "ardop-fm"
        """
        with self._lock:
            config = load_config_env()
            audio_card  = config.get("AUDIO_CARD", "")
            serial_port = config.get("SERIAL_PORT", "")
            radio_id    = config.get("RADIO_ID", "")

            if transport == "tcp":
                self._stop_all_locked()
                return True, "TCP selected — all modems stopped"

            if not audio_card:
                return False, "No audio card configured — run setup wizard first"

            if transport == "freedv":
                return self._start_freedv_locked(
                    audio_card, serial_port, radio_id, config)

            if transport in ("ardop-hf", "ardop-fm"):
                return self._start_ardop_locked(
                    transport, audio_card, serial_port, radio_id)

            return False, f"Unknown transport: {transport}"

    def stop_all(self):
        """Stop all modems unconditionally."""
        with self._lock:
            self._stop_all_locked()

    def ardop_running(self) -> bool:
        """True if ardopc subprocess is alive."""
        with self._lock:
            return (self._ardop_proc is not None and
                    self._ardop_proc.poll() is None)

    # ── Internal helpers (call only while holding self._lock) ─────

    def _stop_all_locked(self):
        """Kill ardopc subprocess and stop freedvtnc2 service."""
        self._kill_ardop_locked()
        try:
            subprocess.run(
                ["systemctl", "stop", "freedvtnc2"],
                capture_output=True, timeout=10
            )
            app.logger.info("ModemManager: freedvtnc2 stopped")
        except Exception as e:
            app.logger.warning("ModemManager: freedvtnc2 stop error: %s", e)

    def _kill_ardop_locked(self):
        """Terminate the ardopc subprocess if running."""
        if self._ardop_proc and self._ardop_proc.poll() is None:
            try:
                self._ardop_proc.terminate()
                self._ardop_proc.wait(timeout=5)
                app.logger.info("ModemManager: ardopc terminated")
            except subprocess.TimeoutExpired:
                self._ardop_proc.kill()
                app.logger.warning("ModemManager: ardopc killed (SIGKILL)")
            except Exception as e:
                app.logger.warning("ModemManager: ardopc kill error: %s", e)
        self._ardop_proc = None

    # CI-V PTT strings for radios that use CAT PTT via ardopcf -c/-k/-u.
    # ardopcf sends the key string bytes verbatim to the radio serial port
    # to key TX, and unkey string to release.
    # Format: hex string, no spaces (ardopcf parses it directly).
    # Address byte 0x88 = IC-7300, 0x70 = X6100, 0x6E = G90 (typical defaults).
    # Users may have customised their CI-V address — these are factory defaults.
    _CIV_PTT = {
        # radio_id : (key_hex, unkey_hex, baud)
        "xiegu_x6100": ("FEFE70E01C0001FD", "FEFE70E01C0000FD", 19200),
        "xiegu_g90":   ("FEFE6EE01C0001FD", "FEFE6EE01C0000FD", 19200),
        # IC-7300 included for reference — not a Xiegu but uses same protocol
        "icom_ic7300":  ("FEFE94E01C0001FD", "FEFE94E01C0000FD", 19200),
    }

    def _start_ardop_locked(self, transport: str,
                             audio_card: str,
                             serial_port: str,
                             radio_id: str) -> tuple:
        """
        Stop rigctld + freedvtnc2, then launch ardopc.

        PTT strategy (in priority order):
          1. If radio_id is in _CIV_PTT: use ardopcf -c/-k/-u CI-V CAT PTT.
             rigctld must be stopped first to release the serial port.
          2. If serial_port set but no CI-V entry: use ardopcf -p RTS PTT.
          3. No serial_port: VOX (no PTT flags).
        """
        # Stop freedvtnc2 AND rigctld — both hold audio/serial resources
        for svc in ("freedvtnc2", "rigctld"):
            try:
                subprocess.run(
                    ["systemctl", "stop", svc],
                    capture_output=True, timeout=10
                )
                app.logger.info("ModemManager: stopped %s", svc)
            except Exception as e:
                app.logger.warning("ModemManager: stop %s error: %s", svc, e)

        # Brief pause to let the OS release the serial port
        time.sleep(0.5)

        # Kill any existing ardopc
        self._kill_ardop_locked()

        if not ARDOPC_BIN.exists():
            return False, f"ardopc binary not found at {ARDOPC_BIN}"

        # ardopcf requires ALSA plughw:N,0 device names, NOT integer
        # card indices (which freedvtnc2 uses).
        # config.env stores AUDIO_CARD as an integer (e.g. "1").
        # Convert: "1" -> "plughw:1,0"
        # plughw is required because ardopcf uses 12 kHz sample rate
        # which most cards do not natively support — the ALSA plug
        # layer handles resampling automatically.
        try:
            card_int = int(audio_card)
            alsa_dev = f"plughw:{card_int},0"
        except ValueError:
            # Already a device string (e.g. "plughw:1,0") — use as-is
            alsa_dev = audio_card

        # Build ardopcf command
        # Positional args order: [options] <port> <capture_dev> <playback_dev>
        cmd = [str(ARDOPC_BIN)]

        civ = self._CIV_PTT.get(radio_id.lower() if radio_id else "")
        if civ and serial_port:
            # CI-V CAT PTT — radio handles TX keying via serial CI-V commands
            key_str, unkey_str, baud = civ
            cmd += ["-c", serial_port,
                    "-k", key_str,
                    "-u", unkey_str]
            app.logger.info("ModemManager: using CI-V CAT PTT for %s "
                            "on %s (key=%s)", radio_id, serial_port, key_str)
        elif serial_port:
            # RTS hardware PTT — toggle RTS line on serial port
            cmd += ["-p", serial_port]
            app.logger.info("ModemManager: using RTS PTT on %s", serial_port)
        else:
            app.logger.info("ModemManager: no serial port — VOX mode")

        cmd += ["8515", alsa_dev, alsa_dev]

        app.logger.info("ModemManager: launching ardopc: %s", " ".join(cmd))

        try:
            self._ardop_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # merge stderr into stdout
                close_fds=True
            )
        except Exception as e:
            return False, f"Failed to launch ardopc: {e}"

        # Give ardopc 2 seconds to either crash or open its port.
        # If it crashes immediately we'll know right away.
        time.sleep(2)
        exit_code = self._ardop_proc.poll()
        if exit_code is not None:
            # Process already exited — read whatever it printed
            try:
                out = self._ardop_proc.stdout.read(2048).decode(
                    "utf-8", errors="replace").strip()
            except Exception:
                out = "(no output)"
            self._ardop_proc = None
            app.logger.error("ModemManager: ardopc exited immediately "
                             "(code %d): %s", exit_code, out)
            return False, (
                f"ardopc exited immediately (code {exit_code}). "
                f"Output: {out[:200] if out else '(none)'}"
            )

        # Process is alive — now wait for port 8515 to be ready
        label = "ARDOP HF" if transport == "ardop-hf" else "ARDOP VHF-FM"
        ready = self._wait_for_port(8515, self._READY_TIMEOUT)
        if not ready:
            # Read any output before killing it
            try:
                out = self._ardop_proc.stdout.read(2048).decode(
                    "utf-8", errors="replace").strip()
            except Exception:
                out = "(no output)"
            app.logger.error("ModemManager: ardopc port 8515 timeout. "
                             "Output: %s", out)
            self._kill_ardop_locked()
            return False, (
                f"ardopc running but port 8515 not ready after "
                f"{self._READY_TIMEOUT}s. Output: {out[:200] if out else '(none)'}"
            )

        app.logger.info("ModemManager: ardopc ready on port 8515")
        return True, f"{label} modem ready"

    def _start_freedv_locked(self, audio_card: str, serial_port: str,
                              radio_id: str, config: dict) -> tuple:
        """Kill ardopc and (re)start freedvtnc2 + rigctld services."""
        self._kill_ardop_locked()

        # Start rigctld BEFORE freedvtnc2 so the CAT/PTT interface is
        # ready when freedvtnc2 connects to it on port 4532.
        if serial_port:
            try:
                subprocess.run(
                    ["systemctl", "restart", "rigctld"],
                    capture_output=True, timeout=10
                )
                app.logger.info("ModemManager: rigctld restarted")
                # Brief pause to let rigctld bind port 4532 before freedvtnc2 starts
                time.sleep(1.5)
            except Exception as e:
                app.logger.warning("ModemManager: rigctld restart error: %s", e)

        # Now restart freedvtnc2 (it will find rigctld ready on port 4532)
        try:
            subprocess.run(
                ["systemctl", "restart", "freedvtnc2"],
                capture_output=True, timeout=15
            )
        except Exception as e:
            return False, f"freedvtnc2 restart failed: {e}"

        # Wait for KISS port 8001 first (data path)
        ready = self._wait_for_port(8001, self._READY_TIMEOUT)
        if not ready:
            return False, f"freedvtnc2 did not open port 8001 within {self._READY_TIMEOUT}s"

        # Also wait for command port 8002 — mode changes go here.
        # It comes up slightly after the KISS port. Give it 10s extra.
        cmd_ready = self._wait_for_port(8002, 10)
        if cmd_ready:
            app.logger.info("ModemManager: freedvtnc2 fully ready (8001+8002)")
        else:
            # Not fatal — KISS works, mode changes may fail briefly
            app.logger.warning("ModemManager: port 8002 not ready yet "
                               "(mode changes may fail initially)")

        return True, "FreeDV modem ready"

    @staticmethod
    def _wait_for_port(port: int, timeout: float) -> bool:
        """Poll until a TCP port is listening or timeout expires."""
        import socket as _socket
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with _socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.5)
        return False


# Module-level singleton — shared across all ConsoleSession instances
# (only one modem can run at a time regardless of browser tabs)
_modem_manager = ModemManager()


class ConsoleSession:
    """
    Manages one browser WebSocket connection for the Console page.
    Bridges browser commands to the HF-256 hub via TCP transport.
    Uses the hub's wire format directly (project files chat.py format).

    Thread safety: background TCP threads call send() which enqueues
    messages on _send_q. A dedicated sender thread (started by
    console_ws) drains the queue and calls ws.send(). The main thread
    only calls ws.receive(). simple-websocket 1.1.0 supports concurrent
    send/receive from separate threads.
    """

    def __init__(self, ws):
        self.ws             = ws
        self.settings       = load_settings()
        self.mycall         = self.settings.get("callsign", "N0CALL").upper()
        self.enc_enabled    = self.settings.get("encryption_enabled", True)
        self.transport      = None
        self.transport_mode = "tcp"
        self._lock          = threading.Lock()   # protects self.transport
        self._send_q        = queue.Queue()      # outbound messages for sender thread

    # ── Modem switching ──────────────────────────────────────────

    def _switch_modem(self, transport: str):
        """
        Called in a daemon thread when the user clicks a transport button.
        Stops the current modem and starts the new one, reporting status
        to the browser via sys_msg.
        """
        labels = {
            "tcp":      "TCP/Internet",
            "freedv":   "FreeDV HF",
            "ardop-hf": "ARDOP HF",
            "ardop-fm": "ARDOP VHF-FM",
        }
        label = labels.get(transport, transport)

        if transport == "tcp":
            # TCP needs no modem — stop everything
            self.sys_msg("Stopping modems for TCP mode...")
            _modem_manager.stop_all()
            self.sys_msg(f"✓ {label} ready — use /connect <IP> [port]")
            return

        self.sys_msg(f"Switching to {label} — stopping other modems...")
        ok, msg = _modem_manager.switch_to(transport)

        if ok:
            self.sys_msg(f"✓ {msg}")
            if transport in ("ardop-hf", "ardop-fm"):
                self.sys_msg("Use /connect <CALLSIGN> to call a station")
            elif transport == "freedv":
                self.sys_msg("Use /connect <CALLSIGN> to call a station")
        else:
            self.sys_msg(f"✗ {msg}")
            self.sys_msg("Transport mode unchanged — fix the issue and retry,")
            self.sys_msg("or select a different transport.")
            # Do NOT revert transport_mode to tcp — if we did, a subsequent
            # /connect <CALLSIGN> would be misrouted as a TCP hostname.
            self.send({"type": "modem_error", "text": msg})

    # ── Browser helpers ───────────────────────────────────────────

    def send(self, obj: dict):
        """Queue a JSON message for delivery to the browser."""
        self._send_q.put(json.dumps(obj))

    def sys_msg(self, text: str):
        self.send({"type": "system", "text": text})

    # ── Transport ─────────────────────────────────────────────────

    def _connect_tcp(self, host: str, port: int):
        """Connect to HF-256 hub via TCP. Runs in a daemon thread."""
        try:
            from hf256.tcp_transport import TCPTransport

            with self._lock:
                if self.transport:
                    self.transport.close()
                    self.transport = None

            t = TCPTransport(mycall=self.mycall, mode="client",
                             host=host, port=port)
            t.on_state_change     = self._on_state_change
            t.on_message_received = self._on_message_received
            t.on_ptt_change       = lambda x: None

            with self._lock:
                self.transport = t

            ok = t.connect()
            if not ok:
                self.send({"type": "error",
                           "text": f"Connection to {host}:{port} failed"})
                self.send({"type": "disconnected"})

        except Exception as e:
            self.send({"type": "error", "text": f"Connect error: {e}"})
            self.send({"type": "disconnected"})

    def _connect_ardop(self, target_call: str, fm_mode: bool = False):
        """
        Initiate an ARQ call to target_call via the ardopc modem.
        Runs in a daemon thread.

        _switch_modem() has already started the ardopc process and
        confirmed port 8515 is open before this is called.  We just
        open the Python socket connection and issue ARQCALL.
        """
        try:
            from hf256.ardop import ARDOPConnection

            # Guard: confirm ardopc is actually running
            if not _modem_manager.ardop_running():
                self.send({"type": "error",
                           "text": "ARDOP modem is not running — "
                                   "select ARDOP transport first to start it"})
                self.send({"type": "disconnected"})
                return

            with self._lock:
                if self.transport:
                    try:
                        self.transport.vara_disconnect()
                    except Exception:
                        pass
                    self.transport.close()
                    self.transport = None

            t = ARDOPConnection(
                mycall=self.mycall,
                ardop_host="127.0.0.1",
                ardop_cmd_port=8515,
                ardop_data_port=8516
            )
            t.on_state_change     = self._on_state_change
            t.on_message_received = self._on_ardop_message
            t.on_ptt_change       = lambda x: None

            # Add a 2-byte length prefix wrapper to match main.py send_message
            # ARDOP mode on the target expects: [2-byte len BE][wire data]
            # as the payload delivered to ardop.send_data(), which then
            # wraps the whole thing in a 2-byte gARIM frame for over-air.
            # On receive the target strips the gARIM frame and reads the
            # 2-byte prefix to reassemble complete messages from fragments.
            import struct as _struct
            _ardop_send_raw = t.send_data
            def _ardop_send_framed(data: bytes, _raw=_ardop_send_raw) -> bool:
                framed = _struct.pack(">H", len(data)) + data
                return _raw(framed)
            t.send_data = _ardop_send_framed

            with self._lock:
                self.transport = t
                self._ardop_rx_buf = b""   # reassembly buffer

            # Connect Python sockets to the already-running ardopc process
            ok = t.connect()
            if not ok:
                self.send({"type": "error",
                           "text": "Could not connect to ardopc on port 8515 — "
                                   "check journalctl for ardopc errors"})
                self.send({"type": "disconnected"})
                return

            # For FM mode set narrower bandwidth
            if fm_mode:
                t._send_cmd("ARQBW 500MAX")

            # Initiate the ARQ call to the target station
            label = "ARDOP VHF-FM" if fm_mode else "ARDOP HF"
            app.logger.info("ARDOP: calling %s via %s", target_call, label)
            self.sys_msg(f"Calling {target_call.upper()} via {label} ...")
            self.sys_msg("(This may take 30-120 seconds on HF)")
            t.vara_connect(target_call.upper())

        except Exception as e:
            self.send({"type": "error", "text": f"ARDOP connect error: {e}"})
            self.send({"type": "disconnected"})

    def _connect_freedv(self, target_call: str):
        """
        Initiate a P2P FreeDV session to target_call.
        Runs in a daemon thread.
        freedvtnc2 must already be running (started by _switch_modem).
        """
        try:
            from hf256.freedv_transport import FreeDVTransport

            with self._lock:
                if self.transport:
                    try:
                        self.transport.vara_disconnect()
                    except Exception:
                        pass
                    self.transport.close()
                    self.transport = None

            t = FreeDVTransport(
                mycall=self.mycall,
                kiss_host="127.0.0.1",
                kiss_port=8001
            )
            t.on_state_change      = self._on_state_change
            t.on_message_received  = self._on_freedv_message
            t.on_announce_received = self._on_announce_received
            t.on_ptt_change        = lambda x: None

            # Add 2-byte length prefix wrapper — matches main.py send_message
            # ARDOP framing (both use same 2-byte prefix convention).
            import struct as _struct
            _fdv_send_raw = t.send_data
            def _fdv_send_framed(data: bytes, _raw=_fdv_send_raw) -> bool:
                framed = _struct.pack(">H", len(data)) + data
                return _raw(framed)
            t.send_data = _fdv_send_framed

            with self._lock:
                self.transport        = t
                self._freedv_rx_buf   = b""

            ok = t.connect()
            if not ok:
                self.send({"type": "error",
                           "text": "Could not connect to freedvtnc2 on "
                                   "port 8001 — check journalctl"})
                self.send({"type": "disconnected"})
                return

            self.sys_msg(f"Calling {target_call.upper()} via FreeDV ...")
            self.sys_msg("(Waiting for remote station to answer)")
            t.vara_connect(target_call.upper())

        except Exception as e:
            self.send({"type": "error",
                       "text": f"FreeDV connect error: {e}"})
            self.send({"type": "disconnected"})

    def _on_freedv_message(self, data: bytes):
        """
        Received payload from FreeDVTransport.on_message_received.
        FreeDVTransport delivers complete payloads (ARQ handles reassembly
        at the transport layer).  The payload is [2-byte prefix][wire]
        matching the same framing used by ARDOP (main.py send_message ARDOP
        mode).  Strip the 2-byte prefix and hand wire to _on_message_received.
        """
        import struct as _struct
        with self._lock:
            self._freedv_rx_buf = getattr(self, "_freedv_rx_buf", b"") + data

        app.logger.info("FreeDV fragment +%d bytes, buf=%d",
                        len(data), len(self._freedv_rx_buf))

        while True:
            with self._lock:
                buf = self._freedv_rx_buf
            if len(buf) < 2:
                break
            msg_len = _struct.unpack(">H", buf[:2])[0]
            if msg_len == 0 or msg_len > 256 * 1024:
                app.logger.error("FreeDV: bad 2-byte prefix %d — "
                                 "discarding buffer", msg_len)
                with self._lock:
                    self._freedv_rx_buf = b""
                break
            if len(buf) < 2 + msg_len:
                break
            wire = buf[2: 2 + msg_len]
            with self._lock:
                self._freedv_rx_buf = buf[2 + msg_len:]
            app.logger.info("FreeDV: complete %d-byte message extracted",
                            len(wire))
            self._on_message_received(wire)

    def _on_announce_received(self, src: str, text: str):
        """Handle received ANNOUNCE broadcast from another station."""
        app.logger.info("FreeDV ANNOUNCE from %s: %s", src, text[:80])
        self.send({"type": "announce", "src": src, "text": text})

    def _announce_direct(self, text: str) -> bool:
        """
        Send an ANNOUNCE packet directly to freedvtnc2 KISS port 8001
        without needing an active FreeDVTransport session.
        Used when /announce is issued before /connect.
        Opens a fresh socket, sends one KISS frame, closes immediately.
        """
        try:
            from hf256.freedv_transport import _pack, _kiss_encode, PKT_ANNOUNCE, BROADCAST
            import socket as _sock
            pkt   = _pack(PKT_ANNOUNCE, self.mycall, BROADCAST,
                          text.encode("utf-8", errors="replace"))
            frame = _kiss_encode(pkt)
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(3)
            s.connect(("127.0.0.1", 8001))
            s.sendall(frame)
            s.close()
            app.logger.info("FreeDV direct ANNOUNCE sent (%d bytes)", len(frame))
            return True
        except Exception as e:
            app.logger.error("FreeDV direct ANNOUNCE failed: %s", e)
            return False

    def _on_ardop_message(self, data: bytes):
        """
        Called by ARDOPConnection._data_reader with the payload of each
        received gARIM frame — the 2-byte length + mode header has already
        been stripped by _data_reader before calling on_message_received.

        The target Pi's send_message() for ARDOP sends raw wire data with
        a 2-byte length prefix (added by ardop.send_data gARIM framing).
        After _data_reader strips that header, we receive complete HF-256
        wire messages directly — no additional reassembly prefix needed.

        However ARDOP may fragment large messages across multiple ARQ frames
        so we still buffer and use the 2-byte prefix that main.py's
        send_message adds for ARDOP mode to know when we have a full message.
        """
        import struct as _struct

        with self._lock:
            self._ardop_rx_buf = getattr(self, "_ardop_rx_buf", b"") + data

        app.logger.info("ARDOP fragment +%d bytes, buf=%d",
                        len(data), len(self._ardop_rx_buf))

        while True:
            with self._lock:
                buf = self._ardop_rx_buf

            # main.py send_message for ARDOP uses a 2-byte length prefix
            if len(buf) < 2:
                break

            msg_len = _struct.unpack(">H", buf[:2])[0]
            if msg_len == 0 or msg_len > 256 * 1024:
                app.logger.error("ARDOP: bad 2-byte length prefix %d — "
                                 "discarding buffer", msg_len)
                with self._lock:
                    self._ardop_rx_buf = b""
                break

            if len(buf) < 2 + msg_len:
                break   # need more data

            wire = buf[2: 2 + msg_len]
            with self._lock:
                self._ardop_rx_buf = buf[2 + msg_len:]

            app.logger.info("ARDOP: complete %d-byte message extracted",
                            len(wire))
            # Hand complete wire message to the normal receive handler
            self._on_message_received(wire)

    def _on_state_change(self, old_state, new_state, trigger=None):
        if new_state == 2:
            remote = getattr(self.transport, "remote_call", None)
            self.send({"type": "connected", "remote_call": remote or "HUB"})
        elif new_state == 1:
            self.send({"type": "connecting"})
        elif new_state == 0:
            self.send({"type": "disconnected"})

    def _on_message_received(self, data: bytes):
        """
        Data from hub after Pi's TCPTransport._read_loop strips the
        4-byte length prefix. Hub send_message builds [prefix][wire]
        and calls send_data which sends as-is (no extra prefix).
        So we receive bare wire bytes ready for HF256Message.unpack.
        """
        import json as _json

        app.logger.info("Console RX: %d bytes", len(data))

        try:
            msg_type, payload, encrypted = _hub_unpack(data)
        except Exception as e:
            app.logger.info("Console RX unpack error: %s | hex=%s",
                            e, data[:32].hex())
            return

        app.logger.info("Console RX: type=0x%02x encrypted=%s payload=%d bytes",
                        msg_type, encrypted, len(payload))

        if msg_type == _HUB_TYPE_CHAT:
            # Binary format: [sender_len:2][sender][text]
            try:
                import struct as _struct
                sender_len = _struct.unpack(">H", payload[0:2])[0]
                sender = payload[2:2+sender_len].decode('utf-8', errors='replace')
                text   = payload[2+sender_len:].decode('utf-8', errors='replace')
                self.send({"type": "chat", "sender": sender, "text": text})
            except Exception as e:
                app.logger.info("Console RX chat parse error: %s", e)

        elif msg_type == _HUB_TYPE_AUTH_RSP:
            # JSON: {"success": bool, "message": str}
            try:
                import json as _json
                obj = _json.loads(payload.decode())
                if obj.get("success"):
                    self.send({"type": "auth_ok"})
                    if obj.get("message"):
                        self.sys_msg(obj["message"])
                else:
                    self.send({"type": "auth_fail",
                               "reason": obj.get("message", "rejected")})
            except Exception as e:
                app.logger.info("Console RX auth_rsp parse error: %s", e)

        elif msg_type == _HUB_TYPE_FL_RSP:
            # JSON: the files dict directly (no wrapping key)
            try:
                import json as _json
                files = _json.loads(payload.decode())
                self.send({"type": "file_list", "files": files})
            except Exception as e:
                app.logger.info("Console RX file_list parse error: %s", e)

        elif msg_type == _HUB_TYPE_FILE_DATA:
            # Binary: [filename_len:2][filename][chunk_num:4][total_chunks:4][hash_len:2][hash][data]
            try:
                import struct as _struct
                offset = 0
                fn_len = _struct.unpack(">H", payload[offset:offset+2])[0]; offset += 2
                filename = payload[offset:offset+fn_len].decode('utf-8'); offset += fn_len
                chunk_num, total_chunks = _struct.unpack(">II", payload[offset:offset+8]); offset += 8
                hash_len = _struct.unpack(">H", payload[offset:offset+2])[0]; offset += 2
                offset += hash_len  # skip hash
                chunk_data = payload[offset:]
                self.send({
                    "type":     "download_progress",
                    "filename": filename,
                    "progress": round((chunk_num + 1) / total_chunks, 3),
                    "done":     chunk_num + 1,
                    "total":    total_chunks,
                    "data":     chunk_data.hex()
                })
            except Exception as e:
                app.logger.info("Console RX file_data parse error: %s", e)

        elif msg_type == _HUB_TYPE_COMPLETE:
            # JSON: {"filename": str, "success": bool, "message": str}
            try:
                import json as _json
                obj = _json.loads(payload.decode())
                self.send({"type":     "download_complete",
                           "filename": obj.get("filename", ""),
                           "success":  obj.get("success", False)})
            except Exception as e:
                app.logger.info("Console RX complete parse error: %s", e)

        elif msg_type == _HUB_TYPE_ERROR:
            # JSON: {"filename": str, "error": str}
            try:
                import json as _json
                obj = _json.loads(payload.decode())
                self.send({"type": "error",
                           "text": f"File error: {obj.get('error', '?')}"})
            except Exception as e:
                app.logger.info("Console RX error parse error: %s", e)

        else:
            app.logger.info("Console RX: unhandled type 0x%02x", msg_type)

    # ── Send ──────────────────────────────────────────────────────

    def _send_hub(self, msg_type: int, payload: bytes) -> bool:
        """
        Pack in hub wire format and call transport.send_data(wire).

        Framing is transport-specific and handled by send_data():
          TCP:  TCPTransport.send_data() prepends a 4-byte length prefix.
          ARDOP: _ardop_send_framed wrapper prepends a 2-byte prefix to match
                 main.py send_message() ARDOP framing, then ardop.send_data()
                 wraps everything in a 2-byte gARIM frame for over-air delivery.
        """
        with self._lock:
            transport = self.transport

        if transport is None or transport.state != 2:
            self.sys_msg("✗ Not connected to hub")
            return False

        wire = _hub_pack(msg_type, payload, encrypt=self.enc_enabled)
        app.logger.info("Console TX: type=0x%02x wire=%d bytes transport=%s",
                        msg_type, len(wire), type(transport).__name__)
        return transport.send_data(wire)

    # ── Browser command dispatcher ────────────────────────────────

    def dispatch(self, msg: dict):
        mtype = msg.get("type", "")
        app.logger.info("Console dispatch: %s", mtype)

        if mtype == "hello":
            call = msg.get("call", "").strip().upper()
            if call:
                self.mycall = call
            self.enc_enabled = bool(msg.get("encrypt", self.enc_enabled))
            _, key = _hub_crypto()
            status = "key loaded" if key else "NO KEY — plaintext only"
            self.sys_msg(f"Session ready — {self.mycall} — {status}")

        elif mtype == "set_transport":
            new_mode = msg.get("transport", "tcp")
            self.transport_mode = new_mode
            # Switch modems in a background thread — port polling can take
            # up to _READY_TIMEOUT seconds and must not block the WS loop
            threading.Thread(
                target=self._switch_modem,
                args=(new_mode,),
                daemon=True
            ).start()

        elif mtype == "set_encrypt":
            self.enc_enabled = bool(msg.get("enabled", True))

        elif mtype == "connect":
            tmode = self.transport_mode
            if tmode in ("ardop-hf", "ardop-fm"):
                target = msg.get("target_call", "").strip().upper()
                if not target:
                    self.sys_msg("\u2717 Usage: /connect <CALLSIGN>  (ARDOP mode)")
                    return
                self.send({"type": "connecting"})
                fm = (tmode == "ardop-fm")
                threading.Thread(target=self._connect_ardop,
                                 args=(target, fm), daemon=True).start()
            elif tmode == "freedv":
                target = msg.get("target_call", "").strip().upper()
                if not target:
                    self.sys_msg("\u2717 Usage: /connect <CALLSIGN>  (FreeDV mode)")
                    return
                self.send({"type": "connecting"})
                threading.Thread(target=self._connect_freedv,
                                 args=(target,), daemon=True).start()
            else:
                host = msg.get("host", "127.0.0.1")
                port = int(msg.get("port", 14256))
                self.send({"type": "connecting"})
                threading.Thread(target=self._connect_tcp,
                                 args=(host, port), daemon=True).start()

        elif mtype == "disconnect":
            with self._lock:
                if self.transport:
                    if self.transport_mode in ("ardop-hf", "ardop-fm"):
                        # ARDOP: send DISCONNECT command, wait briefly
                        try:
                            self.transport.vara_disconnect()
                        except Exception:
                            pass
                        import time as _time
                        _time.sleep(0.3)
                    elif self.transport_mode == "freedv":
                        # FreeDV: send DISC packet (vara_disconnect does this)
                        try:
                            self.transport.vara_disconnect()
                        except Exception:
                            pass
                    else:
                        # TCP: shutdown() sends FIN immediately
                        import socket as _socket
                        try:
                            if self.transport.client_socket:
                                self.transport.client_socket.shutdown(
                                    _socket.SHUT_RDWR)
                        except Exception:
                            pass
                    self.transport.close()
                    self.transport = None
            self.send({"type": "disconnected"})

        elif mtype == "chat":
            text = msg.get("text", "").strip()
            if text:
                self._send_hub(_HUB_TYPE_CHAT, _chat_payload(self.mycall, text))

        elif mtype == "auth":
            password = msg.get("password", "")
            payload = json.dumps(
                {"callsign": self.mycall, "password": password}
            ).encode()
            self._send_hub(_HUB_TYPE_AUTH_REQ, payload)

        elif mtype == "send":
            to   = msg.get("to", "").upper()
            text = msg.get("text", "")
            if to and text:
                import struct as _struct
                # Inner chat wire (hub binary format)
                inner_wire = _hub_pack(_HUB_TYPE_CHAT,
                                       _chat_payload(self.mycall, text),
                                       encrypt=self.enc_enabled)
                # StoreMessage binary: [recipient_len:2][recipient][inner_wire]
                recip_bytes = to.encode('utf-8')
                store_payload = _struct.pack(">H", len(recip_bytes)) + recip_bytes + inner_wire
                self._send_hub(_HUB_TYPE_STORE, store_payload)

        elif mtype == "retrieve":
            self._send_hub(_HUB_TYPE_RETRIEVE, b"{}")

        elif mtype == "files":
            self._send_hub(_HUB_TYPE_FL_REQ, b"{}")

        elif mtype == "download":
            filename = msg.get("filename", "")
            if filename:
                payload = json.dumps({"filename": filename}).encode()
                self._send_hub(_HUB_TYPE_DL_REQ, payload)

        elif mtype == "cancel":
            self.sys_msg("Cancel requested — transfer will time out")

        elif mtype == "announce":
            text = msg.get("text", "").strip()
            if not text:
                self.sys_msg("\u2717 Usage: /announce <message>")
            elif self.transport_mode != "freedv":
                self.sys_msg("\u2717 /announce is only available in FreeDV mode")
            else:
                # Try the active transport first (if connected)
                with self._lock:
                    t = self.transport
                if t and hasattr(t, "send_announce"):
                    ok = t.send_announce(f"{self.mycall}: {text}")
                else:
                    # No active session — send directly via KISS to port 8001.
                    # Announce does not require a P2P connection.
                    ok = self._announce_direct(f"{self.mycall}: {text}")
                if ok:
                    self.sys_msg("\u2713 Announce sent: " + text)
                else:
                    self.sys_msg("\u2717 Announce failed — is FreeDV modem running?")

        elif mtype == "set_freedv_mode":
            # Change freedvtnc2 operating mode on the fly via port 8002
            mode = msg.get("mode", "DATAC1").upper()
            valid = ("DATAC0", "DATAC1", "DATAC3", "DATAC4")
            if mode not in valid:
                self.sys_msg("\u2717 Invalid FreeDV mode: " + mode)
            else:
                ok, resp = freedvtnc2_command(f"MODE {mode}")
                if ok:
                    self.sys_msg("\u2713 FreeDV mode set to " + mode)
                    # Persist the mode change to config.env so next restart
                    # uses the same mode
                    try:
                        cfg_path = str(CONFIG_ENV)
                        with open(cfg_path) as f_:
                            lines = f_.readlines()
                        with open(cfg_path, "w") as f_:
                            for ln in lines:
                                if ln.startswith("FREEDV_MODE="):
                                    f_.write(f"FREEDV_MODE={mode}\n")
                                else:
                                    f_.write(ln)
                    except Exception as e_:
                        app.logger.warning("set_freedv_mode: config.env update failed: %s", e_)
                else:
                    self.sys_msg("\u2717 Mode change failed: " + resp)

        elif mtype == "modem_status":
            # Report current modem state to console
            ardop_up   = _modem_manager.ardop_running()
            import subprocess as _sp
            r = _sp.run(["systemctl", "is-active", "freedvtnc2"],
                        capture_output=True, text=True)
            fdv_up = r.stdout.strip() == "active"
            r2 = _sp.run(["systemctl", "is-active", "rigctld"],
                         capture_output=True, text=True)
            rig_up = r2.stdout.strip() == "active"
            OK_  = "\u2713 running"
            OFF_ = "\u25cb stopped"
            self.sys_msg("Modem status:")
            self.sys_msg("  freedvtnc2 : " + (OK_ if fdv_up   else OFF_))
            self.sys_msg("  ardopc     : " + (OK_ if ardop_up else OFF_))
            self.sys_msg("  rigctld    : " + (OK_ if rig_up   else OFF_))
            config = load_config_env()
            self.sys_msg(f"  audio card : {config.get('AUDIO_CARD', '?')}")
            self.sys_msg(f"  serial port: {config.get('SERIAL_PORT', 'none')}")
            self.sys_msg(f"  FreeDV mode: {config.get('FREEDV_MODE', 'DATAC1')}")

        elif mtype == "bulletin":
            text = msg.get("text", "")
            if text:
                self._send_hub(_HUB_TYPE_CHAT,
                               _chat_payload(self.mycall, f"/bul {text}"))

        elif mtype == "adduser":
            call = msg.get("call", "").upper()
            pw   = msg.get("password", "")
            if call and pw:
                self._send_hub(_HUB_TYPE_CHAT,
                               _chat_payload(self.mycall, f"/adduser {call} {pw}"))

        elif mtype == "listusers":
            self._send_hub(_HUB_TYPE_CHAT, _chat_payload(self.mycall, "/listusers"))

        elif mtype == "storage":
            self._send_hub(_HUB_TYPE_CHAT, _chat_payload(self.mycall, "/storage"))

        else:
            app.logger.warning("Console: unknown msg type: %s", mtype)

@sock.route("/console/ws")
def console_ws(ws):
    """
    WebSocket endpoint for the Console page.

    Architecture:
    - Main thread: blocks on ws.receive() waiting for browser commands
    - Background threads (TCP callbacks): call session.send() which
      queues messages
    - A dedicated sender thread drains the queue and calls ws.send()

    This keeps all ws.send() calls in one thread and all ws.receive()
    calls in another — simple-websocket 1.1.0 supports this safely.
    """
    import simple_websocket

    session = ConsoleSession(ws)
    app.logger.info("Console WebSocket connected")

    # Sender thread: drains the outbound queue
    def sender():
        while True:
            item = session._send_q.get()
            if item is None:           # None = sentinel, stop
                break
            try:
                ws.send(item)
            except Exception as e:
                app.logger.info("Console ws.send error: %s", e)
                break

    sender_thread = threading.Thread(target=sender, daemon=True)
    sender_thread.start()

    try:
        while True:
            try:
                raw = ws.receive()
            except simple_websocket.ConnectionClosed:
                break
            if raw is None:
                break
            try:
                session.dispatch(json.loads(raw))
            except json.JSONDecodeError:
                app.logger.warning("Console: invalid JSON from browser")
            except Exception as e:
                app.logger.error("Console dispatch error: %s", e,
                                 exc_info=True)
                session.sys_msg(f"Error: {e}")

    finally:
        # Stop sender thread
        session._send_q.put(None)
        sender_thread.join(timeout=2)

        # Close transport with proper shutdown so hub detects disconnect
        try:
            with session._lock:
                if session.transport:
                    import socket as _socket
                    try:
                        if session.transport.client_socket:
                            session.transport.client_socket.shutdown(
                                _socket.SHUT_RDWR)
                    except Exception:
                        pass
                    session.transport.close()
        except Exception:
            pass
        app.logger.info("Console WebSocket closed")


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    # Promote Flask app logger and console logger to INFO
    # so errors appear in journalctl without debug=True
    app.logger.setLevel(logging.INFO)
    logging.getLogger("hf256").setLevel(logging.INFO)
    app.run(host="0.0.0.0", port=80, debug=False)
