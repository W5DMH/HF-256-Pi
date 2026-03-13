#!/usr/bin/env python3
"""
HF-256 Web Configuration Portal
Flask app serving setup wizard, status dashboard, and settings page.
Adapted from ReticulumHF setup-portal/app.py.
Runs as root on port 80 via hf256-portal.service.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for
)

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

# ------------------------------------------------------------------ #
# App
# ------------------------------------------------------------------ #
app = Flask(__name__)


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

    parts = [
        "rigctld",
        f"-m {radio['hamlib_id']}",
        f"-r {serial_port}",
        f"-s {radio['baud_rate']}",
        "-t 4532"
    ]
    ptt = radio.get("ptt_method", "")
    if ptt and ptt.upper() not in ("VOX", ""):
        parts.append(f"-P {ptt}")
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
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", 8002))
        sock.send(f"{command}\n".encode())
        response = sock.recv(1024).decode().strip()
        sock.close()
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
    data       = request.json or {}
    radio_id   = data.get("radio_id")
    serial_port = data.get("serial_port", "")
    audio_card = data.get("audio_card")
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
    data     = request.json or {}
    current  = load_settings()

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

        # Update settings
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

        settings["wifi_mode"]      = "client"
        settings["client_ssid"]    = ssid
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

    settings = load_settings()
    wifi_mode = settings.get("wifi_mode", "ap")
    ap_ssid   = settings.get("ap_ssid", "HF256-N0CALL")
    cl_ssid   = settings.get("client_ssid", "")
    ssid_disp = ap_ssid if wifi_mode == "ap" else cl_ssid

    return jsonify({
        "setup_complete": is_setup_complete(),
        "callsign": settings.get("callsign", "N0CALL"),
        "role":     settings.get("role", ""),
        "hf256":          {"running": svc_active("hf256")},
        "freedvtnc2":     {"running": svc_active("freedvtnc2")},
        "rigctld":        {"running": svc_active("rigctld")},
        "portal":         {"running": svc_active("hf256-portal")},
        "wifi": {
            "mode":    wifi_mode,
            "ssid":    ssid_disp,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
