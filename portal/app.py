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

from flask import (request,
    Flask, render_template, request, jsonify, redirect, url_for
)
from flask_sock import Sock

from hardware import (
    load_radios, detect_serial_ports, detect_audio_devices,
    find_digirig, test_cat_connection, test_ptt,
    release_ptt, set_audio_levels, get_audio_controls,
    get_system_info, get_audio_levels
)

# ------------------------------------------------------------------ #
# Multi-session infrastructure  (v0.1.0)
# ------------------------------------------------------------------ #
try:
    from hf256.session_manager import SessionManager
    from hf256.tcp_transport   import TCPServerTransport
    from hf256.hub_core        import (
        HubCore, hub_pack, hub_unpack,
        HUB_TYPE_CHAT,        HUB_TYPE_FL_REQ,      HUB_TYPE_FL_RSP,
        HUB_TYPE_FILE_DATA,   HUB_TYPE_DL_REQ,      HUB_TYPE_COMPLETE,
        HUB_TYPE_ERROR,       HUB_TYPE_AUTH_REQ,    HUB_TYPE_AUTH_RSP,
        HUB_TYPE_STORE,       HUB_TYPE_RETRIEVE,    HUB_TYPE_STORE_ACK,
        HUB_TYPE_RETRIEVE_RSP,HUB_TYPE_PASSWD_REQ,  HUB_TYPE_PASSWD_RSP,
        HUB_TYPE_BROADCAST,   HUB_TYPE_PING,        HUB_TYPE_PONG,
    )
    _MSA_AVAILABLE = True
except ImportError as _msa_err:
    # Graceful degradation — portal still works without multi-session modules
    import logging as _log
    _log.getLogger("hf256").warning(
        "Multi-session modules not found (%s) — "
        "running in single-session compatibility mode", _msa_err
    )
    _MSA_AVAILABLE = False

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


def get_ip_address_str() -> str:
    """Return the Pi current IP address for display in sys_msg."""
    import subprocess as _sp
    for iface in ("wlan0", "eth0"):
        try:
            r = _sp.run(["ip", "addr", "show", iface],
                        capture_output=True, text=True, timeout=3)
            for line in r.stdout.split("\n"):
                line = line.strip()
                if line.startswith("inet ") and "127." not in line:
                    return line.split()[1].split("/")[0]
        except Exception:
            pass
    return ""


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


@app.route("/files")
def files_page():
    """Hub file management page."""
    return render_template("files.html",
                           system_info=get_system_info(),
                           settings=load_settings())


@app.route("/downloads")
def downloads_page():
    """Files downloaded from hubs."""
    return render_template("downloads.html",
                           system_info=get_system_info(),
                           settings=load_settings())


@app.route("/api/downloads")
def api_downloads_list():
    """Return list of files downloaded from hubs, newest first."""
    try:
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        files = []
        for f in sorted(DOWNLOADS_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.endswith(".meta"):
                meta_path = DOWNLOADS_DIR / (f.name + ".meta")
                meta = {}
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                    except Exception:
                        pass
                files.append({
                    "name":       f.name,
                    "size":       f.stat().st_size,
                    "modified":   int(f.stat().st_mtime),
                    "hub":        meta.get("hub", "unknown"),
                    "downloaded": meta.get("downloaded",
                                          int(f.stat().st_mtime)),
                })
        return jsonify({"files": files})
    except Exception as exc:
        app.logger.error("api_downloads_list error: %s", exc)
        return jsonify({"files": [], "error": str(exc)})


@app.route("/api/downloads/save", methods=["POST"])
def api_downloads_save():
    """
    Save a file assembled by the browser from download_progress chunks.
    Body JSON: {"filename": str, "data_hex": str, "hub": str}
    """
    try:
        data     = request.get_json(force=True) or {}
        filename = Path(data.get("filename", "")).name
        data_hex = data.get("data_hex", "")
        hub      = data.get("hub", "unknown")
        if not filename:
            return jsonify({"ok": False, "error": "Missing filename"}), 400
        if not data_hex:
            return jsonify({"ok": False, "error": "No data"}), 400
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest = DOWNLOADS_DIR / filename
        dest.write_bytes(bytes.fromhex(data_hex))
        meta_path = DOWNLOADS_DIR / (filename + ".meta")
        meta_path.write_text(json.dumps({
            "hub":        hub,
            "downloaded": int(time.time()),
            "size":       dest.stat().st_size,
        }, indent=2))
        app.logger.info("Downloads: saved %s (%d bytes) from %s",
                        filename, dest.stat().st_size, hub)
        return jsonify({"ok": True, "size": dest.stat().st_size})
    except Exception as exc:
        app.logger.error("api_downloads_save error: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/downloads/file/<path:filename>")
def api_downloads_file(filename):
    """Serve a downloaded file for the browser to open or save."""
    from flask import send_from_directory
    safe = Path(filename).name
    if not (DOWNLOADS_DIR / safe).exists():
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(str(DOWNLOADS_DIR), safe, as_attachment=True)


@app.route("/api/downloads/delete", methods=["POST"])
def api_downloads_delete():
    """Delete a downloaded file and its metadata sidecar."""
    try:
        data     = request.get_json(force=True) or {}
        filename = Path(data.get("filename", "")).name
        if not filename:
            return jsonify({"ok": False, "error": "Missing filename"}), 400
        for p in [DOWNLOADS_DIR / filename,
                  DOWNLOADS_DIR / (filename + ".meta")]:
            if p.exists():
                p.unlink()
        app.logger.info("Downloads: deleted %s", filename)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    """Serve the pre-rendered help page."""
    return render_template("help.html",
                           system_info=get_system_info(),
                           settings=load_settings())


# ------------------------------------------------------------------ #
# Hub File Management API
# ------------------------------------------------------------------ #

HUB_FILES_DIR = Path("/home/pi/.hf256/hub_files")
DOWNLOADS_DIR = Path("/home/pi/.hf256/downloads")   # files received from hubs

@app.route("/api/hub-files", methods=["GET"])
def api_hub_files_list():
    """Return list of files in hub_files directory."""
    HUB_FILES_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(HUB_FILES_DIR.iterdir()):
        if not f.is_file() or f.suffix == ".desc":
            continue
        desc_file = HUB_FILES_DIR / (f.name + ".desc")
        desc = desc_file.read_text().strip() if desc_file.exists() else ""
        files.append({
            "name":        f.name,
            "size":        f.stat().st_size,
            "description": desc,
            "modified":    int(f.stat().st_mtime),
        })
    return jsonify({"files": files})


@app.route("/api/hub-files/upload", methods=["POST"])
def api_hub_files_upload():
    """Upload a file to hub_files directory."""
    HUB_FILES_DIR.mkdir(parents=True, exist_ok=True)
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f       = request.files["file"]
    desc    = request.form.get("description", "").strip()
    fname   = Path(f.filename).name   # strip any path components
    if not fname:
        return jsonify({"error": "Invalid filename"}), 400
    dest = HUB_FILES_DIR / fname
    f.save(str(dest))
    if desc:
        (HUB_FILES_DIR / (fname + ".desc")).write_text(desc)
    elif (HUB_FILES_DIR / (fname + ".desc")).exists():
        pass   # keep existing description
    app.logger.info("Hub files: uploaded %s (%d bytes)", fname,
                    dest.stat().st_size)
    return jsonify({"ok": True, "name": fname, "size": dest.stat().st_size})


@app.route("/api/hub-files/delete", methods=["POST"])
def api_hub_files_delete():
    """Delete a file from hub_files directory."""
    data  = request.get_json(force=True) or {}
    fname = Path(data.get("name", "")).name
    if not fname:
        return jsonify({"error": "No filename"}), 400
    target = HUB_FILES_DIR / fname
    if not target.exists():
        return jsonify({"error": "File not found"}), 404
    target.unlink()
    desc = HUB_FILES_DIR / (fname + ".desc")
    if desc.exists():
        desc.unlink()
    app.logger.info("Hub files: deleted %s", fname)
    return jsonify({"ok": True})


@app.route("/api/hub-files/description", methods=["POST"])
def api_hub_files_description():
    """Update the description of a hub file."""
    data  = request.get_json(force=True) or {}
    fname = Path(data.get("name", "")).name
    desc  = data.get("description", "").strip()
    if not fname:
        return jsonify({"error": "No filename"}), 400
    target = HUB_FILES_DIR / fname
    if not target.exists():
        return jsonify({"error": "File not found"}), 404
    desc_file = HUB_FILES_DIR / (fname + ".desc")
    if desc:
        desc_file.write_text(desc)
    else:
        if desc_file.exists():
            desc_file.unlink()
    return jsonify({"ok": True})


# ------------------------------------------------------------------ #
# Setup API
# ------------------------------------------------------------------ #

@app.route("/api/detect-hardware")
def api_detect_hardware():
    serial_ports  = detect_serial_ports()
    audio_devices = detect_audio_devices()
    digirig       = find_digirig()
    # X6100 detection removed — X6100 uses CM108 USB audio (MMAP-only)
    # which requires PulseAudio. HF-256 uses direct ALSA only.

    recommended_port  = None
    recommended_audio = None

    if digirig.get("found"):
        recommended_port  = digirig.get("serial_port")
        recommended_audio = digirig.get("audio_card")
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
        "x6100":         {"found": False},   # kept for JS compat, always false
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

    # TCP-only mode — no radio hardware required
    if radio_id == "tcp-only":
        try:
            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            if CONFIG_ENV.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.copy2(CONFIG_ENV, BACKUPS_DIR / f"config.env.{ts}")
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            env_content = f"""# HF-256 Hardware Configuration
# Generated by setup wizard {datetime.now().strftime('%Y-%m-%d %H:%M')}
# TCP-only mode — no radio hardware configured

RADIO_ID=tcp-only
SERIAL_PORT=
AUDIO_CARD=
TX_OUTPUT_VOLUME=0

RIGCTLD_CMD=""
"""
            with open(CONFIG_ENV, "w") as f:
                f.write(env_content)
            os.chmod(CONFIG_ENV, 0o644)
            Path(SETUP_FLAG).touch()
            return jsonify({"success": True,
                            "message": "TCP-only setup complete"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

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

        rigctld_cmd = generate_rigctld_command(radio_id, serial_port) \
            if serial_port else ""

        # Write config.env
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        env_content = f"""# HF-256 Hardware Configuration
# Generated by setup wizard {datetime.now().strftime('%Y-%m-%d %H:%M')}

RADIO_ID={radio_id}
SERIAL_PORT={serial_port}
AUDIO_CARD={audio_card}
TX_OUTPUT_VOLUME=0

RIGCTLD_CMD="{rigctld_cmd}"
"""
        with open(CONFIG_ENV, "w") as f:
            f.write(env_content)
        os.chmod(CONFIG_ENV, 0o644)

        # Mark setup complete
        SETUP_FLAG.touch()
        os.chmod(SETUP_FLAG, 0o644)

        # Reload systemd and restart the portal; ardopc is launched
        # on demand by ModemManager when the user selects ARDOP transport.
        subprocess.run(["systemctl", "daemon-reload"],
                       capture_output=True)
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
        "callsign":             callsign,
        "role":                 role,
        "hub_address":          data.get("hub_address",
                                         current["hub_address"]),
        "encryption_enabled":   data.get("encryption_enabled",
                                         current["encryption_enabled"]),
        "wifi_mode":            data.get("wifi_mode",
                                         current["wifi_mode"]),
        "ap_ssid":              data.get("ap_ssid",
                                         current["ap_ssid"]),
        "ap_password":          data.get("ap_password",
                                         current["ap_password"]),
        "client_ssid":          data.get("client_ssid",
                                         current["client_ssid"]),
        "client_password":      data.get("client_password",
                                         current["client_password"]),
        # Session management — hub only but harmless to save on spoke
        "max_sessions":         int(data.get("max_sessions",
                                    current.get("max_sessions", 10))),
        "session_idle_timeout": int(data.get("session_idle_timeout",
                                    current.get("session_idle_timeout", 300))),
        "session_auth_timeout": int(data.get("session_auth_timeout",
                                    current.get("session_auth_timeout", 120))),
    })
    current["network_key_set"] = KEY_FILE.exists()

    if save_settings(current):
        if callsign and callsign != "N0CALL" and role:
            SETUP_FLAG.touch()
            os.chmod(SETUP_FLAG, 0o644)

        # If role just became hub and hub core is not yet initialised,
        # start hub services now — no reboot needed.
        # This covers the first-boot case where the portal started before
        # the operator set role=hub in the setup wizard.
        if role == "hub" and _hub_core is None:
            app.logger.info(
                "api_save_settings: role=hub and hub core not initialised "
                "— starting hub services now"
            )
            threading.Thread(
                target=_start_hub_services,
                daemon=True, name="hub-services-init",
            ).start()

        return jsonify({"success": True,
                        "message": "Settings saved",
                        "restart_required": False})
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

        result = subprocess.run(
            ["/opt/hf256/scripts/wifi-mode.sh", "ap", ssid, password],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            # Save only after script confirms success
            settings["wifi_mode"]   = "ap"
            settings["ap_ssid"]     = ssid
            settings["ap_password"] = password
            save_settings(settings)
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

        # Script timeout = 20s assoc + 15s DHCP + 5s buffer = 40s.
        # The script handles its own fallback to AP on failure and
        # reverts settings.json itself, so we do NOT save settings
        # before running — only save on confirmed success.
        try:
            result = subprocess.run(
                ["/opt/hf256/scripts/wifi-mode.sh",
                 "client", ssid, password],
                capture_output=True, text=True, timeout=60
            )
            success = result.returncode == 0
        except subprocess.TimeoutExpired:
            app.logger.error("wifi-mode.sh client timed out")
            # Script may have left AP stack down — force AP recovery
            subprocess.Popen(
                ["/opt/hf256/scripts/wifi-mode.sh", "reset"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return jsonify({
                "success": False,
                "error": "WiFi switch timed out — reverting to AP mode",
                "new_ip": None
            })

        if success:
            # Save client mode only after confirmed connection
            settings["wifi_mode"]       = "client"
            settings["client_ssid"]     = ssid
            settings["client_password"] = password
            save_settings(settings)
        # If script failed it already reverted settings.json to ap internally

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
            "success":  success,
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

    portal_up  = svc_active("hf256-portal")
    rig_up     = svc_active("rigctld")
    ardop_up   = _modem_manager.ardop_running()

    # Determine overall status and human-readable mode for the TFT display.
    # "Running"  = portal + the services needed for current transport are up
    # "Partial"  = portal up but something expected is missing
    # "Starting" = portal not yet up
    transport = _active_transport

    # Human-readable mode string for TFT display
    if transport == "ardop-fm":
        mode_str = "ARDOP FM"
    elif transport == "ardop-hf":
        mode_str = "ARDOP HF"
    elif transport == "tcp":
        mode_str = "TCP"
    else:
        mode_str = transport.upper() if transport else "TCP"

    # Overall status
    if not portal_up:
        overall = "Starting"
    elif transport == "tcp":
        overall = "Running"
    elif transport in ("ardop-hf", "ardop-fm"):
        # ARDOP: need ardopc process running
        overall = "Running" if ardop_up else "Partial"
    else:
        overall = "Running" if portal_up else "Starting"

    return jsonify({
        "setup_complete": is_setup_complete(),
        "callsign":       settings.get("callsign", "N0CALL"),
        "role":           settings.get("role", ""),
        "active_transport": transport,
        "mode":           mode_str,        # human-readable for TFT display
        "overall_status": overall,
        "rigctld":    {"running": rig_up},
        "portal":     {"running": portal_up},
        "ardopc":     {"running": ardop_up},
        "wifi": {
            "mode":       wifi_mode,
            "ssid":       ssid_disp,
            "ap_running": svc_active("hostapd"),
            # In client mode wifi is running if wlan0 has an IP
            # (hostapd is intentionally NOT running in client mode)
            "running":    (svc_active("hostapd") if wifi_mode == "ap"
                          else bool(get_ip_address_str()))
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


@app.route("/api/sync-time-auto", methods=["POST"])
def api_sync_time_auto():
    """
    Called silently by any page load to keep Pi clock accurate.
    Always sets the time if drift > 60 seconds — less conservative
    than set-time which only sets if drift > 24 hours.
    Used by spoke stations that never open a hub console.
    """
    import subprocess as _sp, datetime as _dt
    data      = request.get_json(silent=True) or {}
    unix_time = data.get("unix", 0)
    if not unix_time:
        return jsonify({"ok": False, "reason": "no time provided"})
    try:
        now   = _dt.datetime.utcnow()
        drift = abs(unix_time - now.timestamp())
        if drift < 60:
            return jsonify({"ok": True, "reason": "clock ok"})
        dt_str = _dt.datetime.utcfromtimestamp(unix_time).strftime(
            "%Y-%m-%d %H:%M:%S")
        _sp.run(["date", "-s", dt_str], capture_output=True, timeout=5)
        _sp.run(["hwclock", "-w"],      capture_output=True, timeout=5)
        app.logger.info("Auto time sync: set to %s (drift was %.0fs)",
                        dt_str, drift)
        return jsonify({"ok": True, "set": dt_str})
    except Exception as e:
        return jsonify({"ok": False, "reason": str(e)})


@app.route("/api/set-time", methods=["POST"])
def api_set_time():
    """
    Set the Pi system clock from the browser's time.
    Only honoured on hub stations — spokes do not store timestamped data.
    Silently ignored if NTP is already synced (system has internet time).
    """
    settings = load_settings()
    if settings.get("role") != "hub":
        return jsonify({"ok": False, "reason": "not a hub"})

    data = request.get_json(force=True) or {}
    iso  = data.get("iso")          # e.g. "2024-11-19T14:30:00Z"
    unix = data.get("unix")         # Unix timestamp in seconds (float)

    if not iso and not unix:
        return jsonify({"ok": False, "reason": "no time provided"})

    # Check if NTP is already providing good time — if the system clock
    # is within 24 hours of the browser time, NTP is probably working
    # and we leave it alone. If the clock is way off, set it.
    import time as _time, subprocess as _sp
    try:
        now_sys = _time.time()
        now_browser = float(unix) if unix else (
            _time.mktime(_time.strptime(iso.rstrip("Z"), "%Y-%m-%dT%H:%M:%S"))
        )
        drift = abs(now_sys - now_browser)

        if drift < 86400:  # within 24 hours — NTP is likely working
            return jsonify({"ok": True, "action": "skipped",
                            "reason": f"clock drift only {drift:.0f}s, NTP active"})

        # Clock is badly wrong — set it from browser time
        # Use 'date -s' which works when running as root
        dt_str = _time.strftime("%Y-%m-%d %H:%M:%S",
                                 _time.gmtime(now_browser))
        result = _sp.run(
            ["date", "-u", "-s", dt_str],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Also sync hardware clock if available
            _sp.run(["hwclock", "-w"], capture_output=True, timeout=5)
            app.logger.info("System time set to %s (drift was %.0fs)",
                            dt_str, drift)
            return jsonify({"ok": True, "action": "set",
                            "time": dt_str, "drift": round(drift)})
        else:
            app.logger.error("date -s failed: %s", result.stderr)
            return jsonify({"ok": False, "reason": result.stderr.strip()})

    except Exception as e:
        app.logger.error("api_set_time error: %s", e)
        return jsonify({"ok": False, "reason": str(e)})


@app.route("/api/get-time")
def api_get_time():
    """Return current system time for display."""
    import time as _time
    now = _time.time()
    return jsonify({
        "unix":    now,
        "utc":     _time.strftime("%Y-%m-%d %H:%M:%S UTC", _time.gmtime(now)),
        "iso":     _time.strftime("%Y-%m-%dT%H:%M:%SZ",    _time.gmtime(now)),
    })


@app.route("/api/service/<service>/<action>", methods=["POST"])
def api_service_control(service, action):
    allowed = ["ardopc", "rigctld",
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
    """Restart ardopc and the hf256 portal service."""
    try:
        # ardopc is managed by ModemManager directly (not systemctl),
        # so we use the modem manager to restart it if it's running.
        _modem_manager.stop_all()
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
        _modem_manager.stop_all()
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
    allowed = ["ardopc", "rigctld",
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
    transport = _active_transport

    # In TCP mode the modem is not in use — report that clearly
    if transport == "tcp":
        return jsonify({
            "success": True, "online": True,
            "mode": "TCP", "ptt": "N/A",
            "channel": "N/A",
            "active_transport": transport
        })

    if transport in ("ardop-hf", "ardop-fm"):
        ardop_up = _modem_manager.ardop_running()
        return jsonify({
            "success": True, "online": ardop_up,
            "mode": "ARDOP FM" if transport == "ardop-fm" else "ARDOP HF",
            "ptt": "--", "channel": "--",
            "active_transport": transport
        })

    # Unknown transport
    return jsonify({"success": False, "online": False,
                    "error": f"Unknown transport: {transport}",
                    "active_transport": transport})


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
_HUB_TYPE_STORE_ACK    = 0x14   # Hub → spoke: message stored confirmation
_HUB_TYPE_RETRIEVE_RSP = 0x15   # Hub → spoke: retrieve completion notice
_HUB_TYPE_PASSWD_REQ   = 0x16   # spoke→hub: change password request
_HUB_TYPE_PASSWD_RSP   = 0x17   # hub→spoke: password change result
# v0.1.0 multi-session additions
_HUB_TYPE_BROADCAST    = 0x20   # hub → all authenticated spokes
_HUB_TYPE_PING         = 0x22   # keepalive ping
_HUB_TYPE_PONG         = 0x23   # keepalive response
_HUB_TYPE_CONN_ACK     = 0x30   # hub → spoke: connected, please /auth


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
    the ARDOP HF and ARDOP FM modems (both use ardopc).

    Only one modem mode may run at a time because both share the same
    USB audio device and serial PTT port.

    The portal runs as root so subprocess.Popen and systemctl work
    without sudo.

    Usage:
        mm = ModemManager()
        ok, msg = mm.switch_to("ardop-hf")   # starts ardopc in HF mode
        ok, msg = mm.switch_to("ardop-fm")   # starts ardopc in FM mode
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

        transport values: "tcp", "ardop-hf", "ardop-fm"
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

            if transport in ("ardop-hf", "ardop-fm"):
                return self._start_ardop_locked(
                    transport, audio_card, serial_port, radio_id)

            return False, f"Unknown transport: {transport}"

    def stop_all(self):
        """Stop all modems unconditionally."""
        with self._lock:
            self._stop_all_locked()

    def ardop_running(self) -> bool:
        """True if ardopc is alive — checks subprocess first, then port 8515."""
        with self._lock:
            proc_alive = (self._ardop_proc is not None and
                          self._ardop_proc.poll() is None)
        if proc_alive:
            return True
        # Fallback: check if anything is listening on port 8515
        # (ardopc may have been started outside _modem_manager)
        import socket as _sock
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect(("127.0.0.1", 8515))
            s.close()
            return True
        except OSError:
            return False

    # ── Internal helpers (call only while holding self._lock) ─────

    def _stop_all_locked(self):
        """Kill ardopc subprocess and stop rigctld service."""
        self._kill_ardop_locked()
        try:
            subprocess.run(
                ["systemctl", "stop", "rigctld"],
                capture_output=True, timeout=10
            )
            app.logger.info("ModemManager: rigctld stopped")
        except Exception as e:
            app.logger.warning("ModemManager: stop rigctld error: %s", e)

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
    # Address byte 0x94 = IC-7300, 0xa4 = X6100, 0x6E = G90 (factory defaults).
    # X6100 CI-V address confirmed: hamlib xiegu.c priv_caps 0xa4,
    #   and Radioddity X6100 manual PTT cmd 0x1C/0x00/0x01.
    # Users may have customised their CI-V address — these are factory defaults.
    _CIV_PTT = {
        # radio_id : (key_hex, unkey_hex, baud)
        # Both hyphenated and underscored forms — config.env may use either.
        # CI-V PTT command: FE FE <addr> E0 1C 00 01 FD (TX on)
        #                   FE FE <addr> E0 1C 00 00 FD (TX off)
        # Xiegu X6100 — CI-V addr 0xA4
        "xiegu_x6100":  ("FEFEA4E01C0001FD", "FEFEA4E01C0000FD", 19200),
        "xiegu-x6100":  ("FEFEA4E01C0001FD", "FEFEA4E01C0000FD", 19200),
        # Xiegu G90 — CI-V addr 0x6E
        "xiegu_g90":    ("FEFE6EE01C0001FD", "FEFE6EE01C0000FD", 19200),
        "xiegu-g90":    ("FEFE6EE01C0001FD", "FEFE6EE01C0000FD", 19200),
        # Icom IC-7300 — CI-V addr 0x94
        "icom_ic7300":  ("FEFE94E01C0001FD", "FEFE94E01C0000FD", 19200),
        "icom-ic7300":  ("FEFE94E01C0001FD", "FEFE94E01C0000FD", 19200),
        # Icom IC-705 — CI-V addr 0xA4 (factory default per Icom manual)
        # NOTE: 0xA4 is shared with Xiegu X6100 — Xiegu cloned this address.
        # 0x88 is the IC-7100, NOT the IC-705.
        "icom_ic705":   ("FEFEA4E01C0001FD", "FEFEA4E01C0000FD", 19200),
        "icom-ic705":   ("FEFEA4E01C0001FD", "FEFEA4E01C0000FD", 19200),
        # Icom IC-9700 — CI-V addr 0xA2 (factory default)
        "icom_ic9700":  ("FEFEA2E01C0001FD", "FEFEA2E01C0000FD", 19200),
        "icom-ic9700":  ("FEFEA2E01C0001FD", "FEFEA2E01C0000FD", 19200),
        # Icom IC-7100 — CI-V addr 0x88 (factory default)
        "icom_ic7100":  ("FEFE88E01C0001FD", "FEFE88E01C0000FD", 19200),
        "icom-ic7100":  ("FEFE88E01C0001FD", "FEFE88E01C0000FD", 19200),
    }

    def _start_ardop_locked(self, transport: str,
                             audio_card: str,
                             serial_port: str,
                             radio_id: str) -> tuple:
        """
        Stop rigctld, then launch ardopc.

        PTT strategy (in priority order):
          1. If radio_id is in _CIV_PTT: use ardopcf -c/-k/-u CI-V CAT PTT.
             rigctld must be stopped first to release the serial port.
          2. If serial_port set but no CI-V entry: use ardopcf -p RTS PTT.
          3. No serial_port: VOX (no PTT flags).
        """
        # Stop rigctld — it holds the serial port
        try:
            subprocess.run(
                ["systemctl", "stop", "rigctld"],
                capture_output=True, timeout=10
            )
            app.logger.info("ModemManager: stopped rigctld")
        except Exception as e:
            app.logger.warning("ModemManager: stop rigctld error: %s", e)

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

        # Normalise radio_id — config.env may use hyphens or underscores
        rid = (radio_id or "").lower().replace("-", "_")
        civ = self._CIV_PTT.get(rid) or self._CIV_PTT.get(
            (radio_id or "").lower())
        if civ and serial_port:
            # CI-V CAT PTT — radio handles TX keying via serial CI-V commands
            # ardopc -c sets the CI-V serial port, -k/-u are the PTT hex strings.
            # ardopc 1.0.4.1.3 does not support a -b baud flag — it uses the
            # OS default baud for the port. CI-V radios auto-negotiate baud.
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
        label = "ARDOP HF" if transport == "ardop-hf" else "ARDOP FM"
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

# Active transport mode — updated by ConsoleSession when user switches
# transport. Read by api_service_status so the TFT display script
# can show the correct mode regardless of which modems are running.
_active_transport = "tcp"  # default on boot
_active_sessions  = set()  # active ConsoleSession instances
_hub_tcp_server   = None   # boot-time hub TCP server (module-level) for hub TCP dispatch
_active_sessions  = set()  # active ConsoleSession instances

# ── v0.1.0 Multi-session hub singletons ─────────────────────────────
# Populated by _start_hub_services() at portal boot.
# None when MSA modules are unavailable (graceful degradation to v0.0.x behaviour).
_session_manager = None    # SessionManager — tracks all active spoke sessions
_hub_core        = None    # HubCore — multi-session protocol handler
_direwolf        = None    # DirewolfTransport — VHF/HF AX.25 hub-side (multi-session)
_direwolf_spoke  = None    # DirewolfSpokeTransport — AX.25 spoke-side (single outgoing call)
_mesh_sync       = None    # MeshSyncManager — hub-to-hub sync

# Lock that serialises all DirewolfTransport creation/replacement.
# Prevents race between _start_hub_services() and concurrent
# _attach_direwolf() calls from multiple console hello handlers,
# which would create multiple AGW clients registered to the same
# callsign — causing Direwolf to route incoming SABM notifications
# to the wrong (orphaned) client.
_direwolf_lock   = threading.Lock()


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
        self.authenticated  = False              # True after /auth succeeds
        self.hybrid_mode    = False              # True = TCP server runs alongside radio transport
        self._tcp_server    = None               # Hybrid: persistent TCP transport
        self._lock          = threading.Lock()   # protects self.transport
        self._send_q        = queue.Queue()      # outbound messages for sender thread
        _active_sessions.add(self)               # register for hub TCP dispatch

    # ── Modem switching ──────────────────────────────────────────

    def _teardown_transport(self):
        """
        Cleanly close self.transport (radio transport) and null it out.
        Does NOT touch self._tcp_server — the hybrid TCP server runs
        independently and must survive radio transport switches.
        """
        with self._lock:
            old_t = self.transport
            # Safety: never tear down the hybrid TCP server via this path
            if old_t is not None and old_t is self._tcp_server:
                self.transport = None
                return
            self.transport = None
            # Clear receive buffers
            self._freedv_rx_buf = b""
            self._ardop_rx_buf  = b""

        if old_t is None:
            return

        # Silence callbacks first so dying threads don't fire events
        old_t.on_state_change     = None
        old_t.on_message_received = None
        try:
            old_t.on_announce_received = None
        except AttributeError:
            pass

        # Disconnect cleanly if connected
        try:
            if getattr(old_t, "state", 0) == 2:
                old_t.vara_disconnect()
        except Exception:
            pass

        # Close sockets — this unblocks reader threads immediately
        try:
            old_t.close()
        except Exception:
            pass

        # Wait for reader thread to exit (FreeDV has _reader_done Event)
        reader_done = getattr(old_t, "_reader_done", None)
        if reader_done:
            reader_done.wait(timeout=2.0)
        else:
            # ARDOP has no Event — give threads a moment to notice closed sockets
            import time as _t; _t.sleep(0.3)

        app.logger.info("_teardown_transport: old transport closed")

    def _switch_modem(self, transport: str):
        """
        Called in a daemon thread when the user clicks a transport button.

        Hub:   TCP always runs. ARDOP and AX.25 can additionally be active.
        Spoke: Transports are mutually exclusive. Selecting one tears down
               any currently active transport before activating the new one.
        """
        global _active_transport

        labels = {
            "tcp":       "TCP/Internet",
            "ardop-hf":  "ARDOP HF",
            "ardop-fm":  "ARDOP FM",
            "vhf-ax25":  "VHF AX.25 9600",
            "hf-ax25":   "HF AX.25 300",
        }
        label = labels.get(transport, transport)

        try:
            settings = load_settings()
            role     = settings.get("role", "")

            # ── Spoke: tear down current transport before switching ────
            if role != "hub":
                self._spoke_teardown_current(new_transport=transport)

            # ── AX.25 via Direwolf ────────────────────────────────────
            if transport in ("vhf-ax25", "hf-ax25"):
                _active_transport = transport
                dw_obj = _direwolf if role == "hub" else _direwolf_spoke

                if dw_obj is not None:
                    # Already attached — just confirm status
                    self.sys_msg(f"✓ {label} ready — Direwolf managing AX.25 sessions")
                    if role == "hub":
                        self.sys_msg("  Listening for incoming spoke connections")
                    else:
                        self.sys_msg("  Use /connect <HUBCALL> to call the hub")
                else:
                    # Auto-attach on demand — no portal restart required
                    self.sys_msg(f"{label} — attaching Direwolf...")
                    ok, attach_msg = _attach_direwolf()
                    if ok:
                        self.sys_msg(f"✓ {label} ready — {attach_msg}")
                        if role == "hub":
                            self.sys_msg("  Listening for incoming spoke connections")
                        else:
                            self.sys_msg("  Use /connect <HUBCALL> to call the hub")
                    else:
                        self.sys_msg(f"✗ {label}: {attach_msg}")
                        self.sys_msg(
                            "  Configure Direwolf in Settings → Direwolf, then Apply"
                        )
                return

            # ── TCP ───────────────────────────────────────────────────
            if transport == "tcp":
                _active_transport = "tcp"
                _modem_manager.stop_all()
                if role == "hub":
                    if _hub_tcp_server is not None:
                        self.sys_msg("✓ TCP/Internet — multi-client server on port 14256")
                        self.sys_msg("  Accepting spoke connections on all transports")
                    else:
                        threading.Thread(target=self._start_tcp_listener,
                                         daemon=True, name="tcp-listen").start()
                else:
                    self.sys_msg(f"✓ {label} ready — use /connect <IP> [port]")
                return

            # ── ARDOP HF / ARDOP FM ───────────────────────────────────
            with self._lock:
                current_t = self.transport
            if getattr(current_t, "state", 0) == 2:
                app.logger.info("_switch_modem(%s): active connection, "
                                "skipping teardown", transport)
                return
            self._teardown_transport()

            self.sys_msg(f"Switching to {label} — stopping other modems...")
            ok, msg = _modem_manager.switch_to(transport)

            if ok:
                _active_transport = transport
                self.sys_msg(f"✓ {msg}")
                if transport in ("ardop-hf", "ardop-fm"):
                    fm = (transport == "ardop-fm")
                    threading.Thread(target=self._start_ardop_listener,
                                     args=(fm,), daemon=True,
                                     name="ardop-listen").start()
            else:
                self.sys_msg(f"✗ {msg}")
                self.sys_msg("Transport mode unchanged — fix the issue and retry,")
                self.sys_msg("or select a different transport.")
                self.send({"type": "modem_error", "text": msg})

        except Exception as exc:
            app.logger.error("_switch_modem(%s) unhandled error: %s",
                             transport, exc, exc_info=True)
            self.sys_msg(f"✗ Transport switch error: {exc}")
            self.send({"type": "modem_error", "text": str(exc)})

    def _spoke_teardown_current(self, new_transport: str):
        """
        Spoke only: cleanly shut down the current active transport before
        switching to a different one.  No-op if already on the target transport
        or if nothing is active.  Fires a 'disconnected' event if an active
        radio or TCP connection is being dropped.
        """
        with self._lock:
            current_t    = self.transport
            current_mode = self.transport_mode

        if current_t is None or current_mode == new_transport:
            return

        was_connected = (getattr(current_t, "state", 0) == 2)

        if current_mode in ("ardop-hf", "ardop-fm"):
            try:
                current_t.vara_disconnect()
            except Exception:
                pass
            time.sleep(0.3)
            self._teardown_transport()

        elif current_mode in ("vhf-ax25", "hf-ax25"):
            # DirewolfSpokeTransport: send DISC frame but keep AGW connection alive
            # so it can be reused for the next /connect without re-attaching
            try:
                current_t.vara_disconnect()
            except Exception:
                pass
            with self._lock:
                self.transport = None

        else:
            # TCP client: close the outgoing connection
            import socket as _socket
            try:
                if getattr(current_t, "client_socket", None):
                    current_t.client_socket.shutdown(_socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                current_t.close()
            except Exception:
                pass
            with self._lock:
                self.transport = None

        if was_connected:
            self.authenticated = False
            self.send({"type": "disconnected"})


    # ── Browser helpers ───────────────────────────────────────────

    def send(self, obj: dict):
        """Queue a JSON message for delivery to the browser."""
        self._send_q.put(json.dumps(obj))

    def sys_msg(self, text: str):
        self.send({"type": "system", "text": text})

    # ── Transport ─────────────────────────────────────────────────

    def _start_hybrid_tcp(self, port: int = 14256):
        """
        Start persistent TCP server for hybrid mode.
        Re-uses the boot-time server if already running on port 14256,
        otherwise creates a new one. Survives radio transport switches.
        """
        app.logger.info("Hybrid TCP: starting on port %d", port)
        try:
            from hf256.tcp_transport import TCPTransport
            global _hub_tcp_server

            # Re-use boot-time server if available
            if _hub_tcp_server is not None:
                app.logger.info("Hybrid TCP: re-using boot-time TCP server")
                _hub_tcp_server.on_message_received = self._on_message_received
                self._tcp_server = _hub_tcp_server
                self.sys_msg(f"✓ Hybrid TCP active on port {port}")
                self.sys_msg("  TCP spokes can connect while radio transport is active")
                return

            t = TCPTransport(mycall=self.mycall, mode="server",
                             host="0.0.0.0", port=port)
            t.on_message_received = self._on_message_received
            t.on_state_change     = lambda o, n, tr=None: (
                self.sys_msg(f"Hybrid TCP: "
                             f"{'spoke connected' if n == 2 else 'spoke disconnected'}")
            )
            t.on_ptt_change = lambda x: None
            ok = t.connect()
            if ok:
                self._tcp_server = t
                self.sys_msg(f"✓ Hybrid TCP listening on port {port}")
                self.sys_msg("  TCP spokes can connect while radio transport is active")
            else:
                self.sys_msg(f"✗ Hybrid TCP failed — port {port} may be in use")
                self.hybrid_mode = False
        except Exception as e:
            app.logger.error("_start_hybrid_tcp error: %s", e)
            self.sys_msg(f"✗ Hybrid TCP error: {e}")
            self.hybrid_mode = False

    def _start_tcp_listener(self, port: int = 14256):
        """
        Start a TCPTransport in server mode so spokes can connect to this hub.
        Called automatically when a hub selects TCP transport.
        Binds to 0.0.0.0:14256 and accepts incoming spoke connections.
        """
        app.logger.info("_start_tcp_listener: starting on port %d", port)
        try:
            from hf256.tcp_transport import TCPTransport

            with self._lock:
                if self.transport is not None:
                    app.logger.info("_start_tcp_listener: transport already exists, skipping")
                    return   # already have a transport

            t = TCPTransport(
                mycall=self.mycall,
                mode="server",
                host="0.0.0.0",
                port=port
            )
            t.on_state_change     = self._on_state_change
            t.on_message_received = self._on_message_received
            t.on_ptt_change       = lambda x: None

            with self._lock:
                if self.transport is not None:
                    return   # race: another thread beat us
                self.transport = t

            ok = t.connect()   # TCPTransport.connect() calls _start_server()
            if ok:
                global _hub_tcp_server
                _hub_tcp_server = t   # expose to _start_hybrid_tcp so it
                                      # re-uses this server instead of trying
                                      # to bind port 14256 a second time.
                                      # This path runs when role was not hub
                                      # at boot time (boot server skipped),
                                      # so _hub_tcp_server was never set there.
                app.logger.info("TCP listener active on port %d", port)
                self.sys_msg(f"✓ TCP/Internet ready — listening on port {port}")
                self.sys_msg("Spokes can connect using /connect <this-IP>")
            else:
                with self._lock:
                    self.transport = None
                app.logger.error("TCP listener failed to bind on port %d", port)
                self.sys_msg(f"✗ TCP listener failed — port {port} may be in use")

        except Exception as e:
            app.logger.error("_start_tcp_listener error: %s", e)
            self.sys_msg(f"✗ TCP listener error: {e}")

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

    def _connect_ax25(self, target_call: str, radio_port: int):
        """
        Initiate an outgoing AX.25 call via DirewolfSpokeTransport.
        Runs in a daemon thread.  Only valid for spoke stations.

        Lifecycle:
          1.  Attach DirewolfSpokeTransport if not yet connected to AGW.
          2.  Send AGW 'C' frame → Direwolf keys PTT and transmits SABM.
          3.  Wait for Direwolf to send back:
              'C' → connection established (STATE_CONNECTED → "connected" event)
              'd' → connection failed / timeout  (STATE_DISCONNECTED → "disconnected")
          4.  Watchdog fires after AX25_CONNECT_TIMEOUT seconds if neither
              'C' nor 'd' arrives (e.g. PTT wiring issue, reader thread died).
        """
        # At 9600 baud: FRACK=3s × RETRY=10 = 30s worst case.
        # Add 30s margin to cover slow hardware and roundtrip ACK delays.
        AX25_CONNECT_TIMEOUT = 90   # seconds

        global _direwolf_spoke
        try:
            settings = load_settings()
            if settings.get("role") == "hub":
                self.sys_msg("✗ Hub stations do not initiate outgoing AX.25 calls")
                self.send({"type": "disconnected"})
                return

            # Attach on demand if not yet connected to Direwolf AGW
            if _direwolf_spoke is None:
                self.sys_msg("Attaching Direwolf AX.25...")
                ok, msg = _attach_direwolf()
                if not ok or _direwolf_spoke is None:
                    self.sys_msg(f"✗ Direwolf not available: {msg}")
                    self.send({"type": "disconnected"})
                    return

            # Verify the AGW socket is still alive before sending
            if not getattr(_direwolf_spoke, "_running", False):
                self.sys_msg("✗ Direwolf AGW connection dropped — reattaching...")
                ok, msg = _attach_direwolf()
                if not ok or _direwolf_spoke is None:
                    self.sys_msg(f"✗ Could not reconnect to Direwolf: {msg}")
                    self.send({"type": "disconnected"})
                    return

            # Wire callbacks to this ConsoleSession — state changes and received
            # data will route to our existing _on_state_change / _on_message_received
            _direwolf_spoke.on_state_change     = self._on_state_change
            _direwolf_spoke.on_message_received = self._on_message_received

            with self._lock:
                self.transport = _direwolf_spoke

            # Send AGW 'C' frame to Direwolf — it will transmit AX.25 SABM on air
            if not _direwolf_spoke.connect_to(target_call, radio_port):
                self.sys_msg(f"✗ Could not send connect request to Direwolf "
                             f"(AGW socket closed?)")
                self.sys_msg("  Check: journalctl -u direwolf -n 20")
                self.send({"type": "disconnected"})
                with self._lock:
                    self.transport = None
                return

            # Frame sent — Direwolf will now key PTT and transmit SABM
            port_label = "HF" if self.transport_mode == "hf-ax25" else "VHF"
            self.sys_msg(f"  AX.25 SABM sent via Direwolf ({port_label})")
            self.sys_msg(f"  Waiting up to {AX25_CONNECT_TIMEOUT}s for {target_call} to answer...")

            # ── Watchdog ──────────────────────────────────────────────
            # Fires if neither 'C' (connected) nor 'd' (failed) arrives from
            # Direwolf within the timeout.  Protects against:
            #  - PTT wiring failure (radio never keys; Direwolf never gets UA)
            #  - Direwolf AGW reader thread dying silently
            session_ref = self
            start_time  = time.time()
            target_ref  = target_call

            def _watchdog():
                while time.time() - start_time < AX25_CONNECT_TIMEOUT:
                    time.sleep(2)
                    with session_ref._lock:
                        t = session_ref.transport
                    current_state = getattr(t, "state", -1) if t else -1
                    # Left CONNECTING (1) → connected or disconnected — stop watching
                    if current_state != 1:
                        return

                # Still in CONNECTING after timeout — force disconnect
                app.logger.warning("_connect_ax25 watchdog: timeout for %s",
                                   target_ref)
                with session_ref._lock:
                    t = session_ref.transport
                if getattr(t, "state", 0) == 1:
                    # Tell Direwolf to abort the pending connection
                    try:
                        t.vara_disconnect()
                    except Exception:
                        pass
                    with session_ref._lock:
                        session_ref.transport = None
                    session_ref.authenticated = False
                    session_ref.sys_msg(
                        f"✗ AX.25 connection to {target_ref} timed out "
                        f"({AX25_CONNECT_TIMEOUT}s — no response from remote station)"
                    )
                    session_ref.sys_msg(
                        "  Possible causes: hub not listening, PTT not keying radio, "
                        "wrong frequency or callsign"
                    )
                    session_ref.sys_msg(
                        "  Diagnostics: journalctl -u direwolf -n 20"
                    )
                    session_ref.send({"type": "disconnected"})

            threading.Thread(target=_watchdog, daemon=True,
                             name="ax25-watchdog").start()

        except Exception as exc:
            app.logger.error("_connect_ax25 error: %s", exc, exc_info=True)
            self.send({"type": "error", "text": f"AX.25 connect error: {exc}"})
            self.send({"type": "disconnected"})

    def _start_ardop_listener(self, fm_mode: bool = False):
        """
        Create a passive ARDOPConnection and issue LISTEN TRUE so ardopc
        accepts incoming ARQ calls without the operator typing /connect.
        Called automatically when ARDOP-HF or ARDOP-FM transport is selected.
        This is the ARDOP equivalent of _start_freedv_listener().
        If a transport already exists (user already called /connect), no-op.
        """
        try:
            from hf256.ardop import ARDOPConnection
            import struct as _struct

            if not _modem_manager.ardop_running():
                app.logger.warning("ARDOP listener: ardopc not running")
                return

            with self._lock:
                if self.transport is not None:
                    return   # already have a transport

            t = ARDOPConnection(
                mycall=self.mycall,
                ardop_host="127.0.0.1",
                ardop_cmd_port=8515,
                ardop_data_port=8516
            )
            # Hub single-session gate for ARDOP.
            # Unlike FreeDV, ardopc accepts the ARQ call autonomously —
            # there is no pre-connection hook. When CONNECTED fires (0->2)
            # we check if the hub is already serving another transport. If
            # it is, we immediately send DISCONNECT to tear down the new
            # ARQ call. The spoke sees a brief connect+disconnect — it is
            # the only option without modifying the ardopc binary.
            _real_on_state = self._on_state_change
            def _ardop_state_gate(old, new, _real=_real_on_state,
                                  _session=self, _t=t):
                if new == ARDOPConnection.STATE_CONNECTED:
                    with _session._lock:
                        existing = _session.transport
                    if existing is not None and existing is not _t:
                        if getattr(existing, "state", 0) == 2:
                            busy_with = (getattr(existing, "remote_call", None)
                                         or type(existing).__name__)
                            remote = getattr(_t, "remote_call", "?")
                            app.logger.warning(
                                "Hub: ARDOP CONN from %s rejected — "
                                "hub already connected to %s",
                                remote, busy_with)
                            reason_str = (
                                f"Hub busy with {busy_with} — try again later")
                            # Store on session so the NATURAL ardopc DISCONNECTED
                            # event (which fires _on_state_change a moment later)
                            # carries the reason. Do NOT call _real() here — that
                            # would fire _on_state_change NOW, clear the reason,
                            # then ardopc fires it AGAIN (blank) overwriting the
                            # message in the UI.
                            _session._pending_reject_reason = reason_str
                            # Note: we do NOT attempt to send a hub_busy data
                            # frame over ARDOP here. vara_disconnect() tears down
                            # the ARQ session before ardopc can acknowledge and
                            # deliver any queued frame to the spoke. The spoke
                            # learns the reason through _pending_reject_reason,
                            # which _on_state_change reads when the natural
                            # DISCONNECTED event arrives from ardopc.
                            try:
                                _t.vara_disconnect()
                            except Exception:
                                pass
                            return
                _real(old, new)
            t.on_state_change     = _ardop_state_gate
            t.on_message_received = self._on_ardop_message
            t.on_ptt_change       = lambda x: None

            # Same 2-byte length prefix framing as _connect_ardop
            _ardop_send_raw = t.send_data
            def _ardop_send_framed(data: bytes, _raw=_ardop_send_raw) -> bool:
                framed = _struct.pack(">H", len(data)) + data
                return _raw(framed)
            t.send_data = _ardop_send_framed

            with self._lock:
                if self.transport is not None:
                    return   # race: another thread beat us
                self.transport      = t
                self._ardop_rx_buf  = b""

            ok = t.connect()
            if not ok:
                with self._lock:
                    self.transport = None
                app.logger.error("ARDOP listener: could not connect to ardopc")
                self.sys_msg("✗ ARDOP listener failed — check ardopc is running")
                return

            # Set FM bandwidth if needed
            if fm_mode:
                t._send_cmd("ARQBW 500MAX")

            # connect() already sent LISTEN TRUE — ardopc is now accepting calls
            label = "ARDOP FM" if fm_mode else "ARDOP HF"
            app.logger.info("ARDOP listener active (%s)", label)
            # Messages are role-specific:
            #   Hub  — passive listener, waits for spokes to call in
            #   Spoke — active caller, uses /connect to reach the hub
            settings = load_settings()
            if settings.get("role") == "hub":
                self.sys_msg(f"✓ {label} ready — listening for incoming spoke connections")
                self.sys_msg("  Use /connect <CALLSIGN> to call a spoke station")
            else:
                self.sys_msg(f"✓ {label} ready — use /connect <CALLSIGN> to call hub")

        except Exception as e:
            app.logger.error("_start_ardop_listener error: %s", e)

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

            # Tear down any existing transport cleanly before creating new one
            self._teardown_transport()

            t = ARDOPConnection(
                mycall=self.mycall,
                ardop_host="127.0.0.1",
                ardop_cmd_port=8515,
                ardop_data_port=8516
            )
            # Wrap on_state_change to detect a hub-busy rejection.
            # ARDOP has no protocol-level rejection message. When the hub is
            # busy it accepts the ARQ call then immediately sends DISCONNECT.
            # The spoke sees connected→disconnected in under ~5 seconds with
            # no data exchanged. Detect this and show a human-readable reason.
            _real_spoke_state = self._on_state_change
            def _ardop_spoke_state(old, new,
                                   _real=_real_spoke_state, _t=t):
                if (new == ARDOPConnection.STATE_DISCONNECTED
                        and old == ARDOPConnection.STATE_CONNECTED):
                    import time as _time
                    connect_time = getattr(_t, "_connect_time", None)
                    if connect_time and (_time.time() - connect_time) < 5.0:
                        # Connected for less than 5 seconds with no data —
                        # hub almost certainly rejected us because it was busy.
                        _real(old, new)   # fires _on_state_change normally
                        self.sys_msg(
                            "✗ Hub busy or rejected connection — try again later")
                        return
                _real(old, new)
            t.on_state_change     = _ardop_spoke_state
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
            label = "ARDOP FM" if fm_mode else "ARDOP HF"
            app.logger.info("ARDOP: calling %s via %s", target_call, label)
            self.sys_msg(f"Calling {target_call.upper()} via {label} ...")
            self.sys_msg("(This may take 30-120 seconds on HF)")
            t.vara_connect(target_call.upper())

        except Exception as e:
            self.send({"type": "error", "text": f"ARDOP connect error: {e}"})
            self.send({"type": "disconnected"})

    def _start_freedv_listener(self):
        """
        Create a passive FreeDVTransport connected to freedvtnc2 KISS port.
        Does NOT call vara_connect() — just listens for incoming CONN_REQ.
        Called automatically when FreeDV transport is selected, so the Pi
        accepts incoming connections without needing /connect first.
        This is required for hub operation.
        If a transport already exists (e.g. the user already called /connect),
        this is a no-op.
        """
        try:
            from hf256.freedv_transport import FreeDVTransport
            import struct as _struct

            with self._lock:
                if self.transport is not None:
                    # Already have a transport (connected or connecting)
                    return

            t = FreeDVTransport(
                mycall=self.mycall,
                kiss_host="127.0.0.1",
                kiss_port=8001
            )
            t.on_state_change      = self._on_state_change
            t.on_message_received  = self._on_freedv_message
            t.on_announce_received = self._on_announce_received
            t.on_ptt_change        = lambda x: None

            # Hub single-session gate: called by FreeDVTransport before
            # sending CONN_ACK. If another transport is already connected,
            # return False — the transport sends CONN_REJ over the air and
            # the connecting station is cleanly refused before any session
            # state is created.
            def _conn_req_gate(src_call, _session=self):
                with _session._lock:
                    existing = _session.transport
                if existing is not None and existing is not t:
                    if getattr(existing, "state", 0) == 2:
                        busy_with = (getattr(existing, "remote_call", None)
                                     or type(existing).__name__)
                        app.logger.warning(
                            "Hub: rejecting FreeDV CONN_REQ from %s — "
                            "hub already connected to %s", src_call, busy_with)
                        return False
                return True
            t.on_conn_req = _conn_req_gate

            _fdv_send_raw = t.send_data
            def _fdv_send_framed(data: bytes, _raw=_fdv_send_raw) -> bool:
                framed = _struct.pack(">H", len(data)) + data
                return _raw(framed)
            t.send_data = _fdv_send_framed

            with self._lock:
                if self.transport is not None:
                    return   # Race: another thread beat us
                self.transport      = t
                self._freedv_rx_buf = b""

            ok = t.connect()
            if not ok:
                with self._lock:
                    self.transport = None
                app.logger.error("FreeDV listener: could not connect to "
                                 "freedvtnc2 port 8001")
                self.sys_msg("✗ FreeDV listener failed — check freedvtnc2")
            else:
                app.logger.info("FreeDV listener active on port 8001")

        except Exception as e:
            app.logger.error("_start_freedv_listener error: %s", e)

    def _connect_freedv(self, target_call: str):
        """
        Initiate a P2P FreeDV session to target_call.
        Runs in a daemon thread.
        freedvtnc2 must already be running (started by _switch_modem).
        """
        try:
            from hf256.freedv_transport import FreeDVTransport

            # Tear down any existing transport (e.g. the passive listener
            # Tear down any existing transport (listener or prior connection)
            # before creating the new outgoing transport.
            self._teardown_transport()
            app.logger.info("FreeDV: old transport torn down, starting new connection")

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
            # Check for a rejection reason. Two sources:
            # 1. self.transport._reject_reason — set by FreeDVTransport on
            #    CONN_REJ (the transport IS self.transport in that case).
            # 2. self._pending_reject_reason — set by _ardop_state_gate before
            #    calling us, because the rejected ARDOP transport is never
            #    assigned to self.transport so we can't read it from there.
            reason = getattr(self, "_pending_reject_reason", None)
            if not reason and self.transport:
                reason = getattr(self.transport, "_reject_reason", None)
                if reason:
                    self.transport._reject_reason = None
            if hasattr(self, "_pending_reject_reason"):
                self._pending_reject_reason = None
            if reason:
                self.send({"type": "disconnected", "reason": reason})
                self.sys_msg(f"✗ {reason}")
            else:
                self.send({"type": "disconnected"})

    def _hub_send(self, wire: bytes) -> bool:
        """
        Send a pre-packed wire message over the current transport.
        Safe to call from any thread — hub response handlers MUST use
        this via threading.Thread to avoid deadlocking the reader thread.
        Returns True if sent and ACKed, False otherwise.
        """
        with self._lock:
            t = self.transport
        if t and t.state == 2:
            ok = t.send_data(wire)
            if not ok:
                app.logger.warning("_hub_send: send_data failed")
            return bool(ok)
        else:
            app.logger.warning("_hub_send: transport not connected")
            return False

    def _on_message_received(self, data: bytes):
        """
        Data from hub after Pi's TCPTransport._read_loop strips the
        4-byte length prefix. Hub send_message builds [prefix][wire]
        and calls send_data which sends as-is (no extra prefix).
        So we receive bare wire bytes ready for HF256Message.unpack.
        """
        import json as _json

        # Check for hub_busy notice BEFORE attempting HF256 wire unpack.
        # When the hub rejects a TCP connection because it is serving another
        # spoke, it sends a plain JSON frame {"type": "hub_busy", ...}.
        # This is not a valid HF256 wire message so _hub_unpack would discard
        # it with a parse error. Intercept it here first.
        if data and data[0] == ord('{'):
            try:
                candidate = _json.loads(data.decode('utf-8', errors='ignore'))
                if candidate.get('type') == 'hub_busy':
                    msg = candidate.get('message', 'Hub is busy — try again later')
                    app.logger.warning("Hub busy: %s", msg)
                    self.sys_msg(f"✗ {msg}")
                    self.send({"type": "disconnected", "reason": msg})
                    return
            except Exception:
                pass

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

        elif msg_type == _HUB_TYPE_PASSWD_RSP:
            # Hub responded to our password change request
            try:
                rsp = json.loads(payload.decode())
                ok  = rsp.get("ok", False)
                msg_text = rsp.get("msg", "")
                self.sys_msg(("✓ " if ok else "✗ ") + msg_text)
            except Exception as e:
                app.logger.error("PASSWD_RSP parse error: %s", e)

        elif msg_type == _HUB_TYPE_CONN_ACK:
            # Hub confirmed the AX.25/radio connection is bidirectional.
            # JSON: {"hub": str, "call": str, "msg": str}
            # Show the hub's message and prompt for /auth.
            try:
                import json as _json
                obj      = _json.loads(payload.decode())
                hub_call = obj.get("hub", "HUB")
                hub_msg  = obj.get("msg", "Connected")
                app.logger.info("Console RX CONN_ACK from %s", hub_call)
                self.sys_msg(f"✓ {hub_call}: {hub_msg}")
                # Confirm connected state to browser and signal it to show auth prompt
                self.send({
                    "type":        "connected",
                    "remote_call": hub_call,
                    "conn_ack":    True,
                })
            except Exception as exc:
                app.logger.warning("CONN_ACK parse error: %s", exc)
                self.sys_msg("✓ Hub confirmed — use /auth <password>")

        elif msg_type == _HUB_TYPE_AUTH_RSP:
            try:
                import json as _json
                obj = _json.loads(payload.decode())
                if obj.get("success"):
                    self.authenticated = True
                    self.send({"type": "auth_ok"})
                    if obj.get("message"):
                        self.sys_msg(obj["message"])
                else:
                    self.authenticated = False
                    self.send({"type": "auth_fail",
                               "reason": obj.get("message", "rejected")})
            except Exception as e:
                app.logger.info("Console RX auth_rsp parse error: %s", e)

        elif msg_type == _HUB_TYPE_AUTH_REQ:
            # Hub side: spoke is authenticating with us.
            # Validate callsign/password against ~/.hf256/passwords.json
            # and send AUTH_RSP back over the same transport.
            settings = load_settings()
            if settings.get("role") != "hub":
                # We are not a hub — ignore auth requests
                app.logger.warning("Console: received AUTH_REQ but role is not hub")
                return
            try:
                import json as _json
                import hashlib as _hl
                creds    = _json.loads(payload.decode())
                callsign = creds.get("callsign", "").upper()
                password = creds.get("password", "")
                app.logger.info("Hub: AUTH_REQ from %s", callsign)

                # Suppress duplicate AUTH_REQ within 5 seconds only —
                # just long enough to prevent simultaneous duplicate packets.
                # Must allow retries after ARQ timeout (~14s).
                _last_auth = getattr(self, "_last_auth_rsp", {})
                if _last_auth.get(callsign, 0) > time.time() - 5:
                    app.logger.info("Hub: duplicate AUTH_REQ from %s — suppressed",
                                    callsign)
                    return

                # Load password database
                pw_file  = Path("/home/pi/.hf256/passwords.json")
                success  = False
                if pw_file.exists():
                    db   = _json.loads(pw_file.read_text())
                    h    = _hl.sha256(password.encode()).hexdigest()
                    success = db.get(callsign) == h

                if success:
                    # Count waiting messages to notify spoke at login
                    msg_dir = Path("/home/pi/.hf256/hub_messages") / callsign
                    pending = 0
                    if msg_dir.exists():
                        pending = sum(1 for f in msg_dir.iterdir() if f.is_file())
                    if pending:
                        msg_text = (f"Welcome {callsign} — "
                                    f"{pending} message(s) waiting, use /retrieve")
                    else:
                        msg_text = f"Welcome {callsign}"
                else:
                    msg_text = "Invalid callsign or password"
                rsp_payload = _json.dumps(
                    {"success": success, "message": msg_text}
                ).encode()

                app.logger.info("Hub: AUTH_RSP to %s: success=%s",
                                callsign, success)
                # Record time so duplicate AUTH_REQ retries are suppressed
                if not hasattr(self, "_last_auth_rsp"):
                    self._last_auth_rsp = {}
                self._last_auth_rsp[callsign] = time.time()
                wire = _hub_pack(_HUB_TYPE_AUTH_RSP, rsp_payload,
                                 encrypt=self.enc_enabled)
                # Send AUTH_RSP after a short delay.
                # The hub just sent DATA_ACK for AUTH_REQ via _send_packet.
                # We wait 1 second for the spoke to receive its DATA_ACK
                # and switch from TX→RX before hub starts transmitting AUTH_RSP.
                # Without this the hub starts transmitting while the spoke
                # is still processing its DATA_ACK receipt — collision.
                def _send_rsp(w=wire, cs=callsign):
                    time.sleep(1.0)
                    ok = self._hub_send(w)
                    if not ok:
                        app.logger.warning("Hub: AUTH_RSP send failed for %s", cs)
                threading.Thread(
                    target=_send_rsp,
                    daemon=True, name="hub-auth-rsp"
                ).start()
            except Exception as e:
                app.logger.error("Hub: AUTH_REQ handling error: %s", e)

        elif msg_type == _HUB_TYPE_CHAT:
            # Received on SPOKE: chat from hub/another station — display it.
            # Received on HUB: incoming chat from spoke — display + echo back
            # so the spoke sees the message appear on their screen.
            try:
                import struct as _struct, json as _json
                sender_len = _struct.unpack(">H", payload[0:2])[0]
                sender = payload[2:2+sender_len].decode("utf-8", errors="replace")
                text   = payload[2+sender_len:].decode("utf-8", errors="replace")
                app.logger.info("Console RX chat from %s: %s", sender, text[:80])
                self.send({"type": "chat", "sender": sender, "text": text})
                # Hub side: echo the message back so the spoke sees it displayed
                if load_settings().get("role") == "hub":
                    sndr_b = sender.encode("utf-8")
                    body_b = text.encode("utf-8")
                    p      = _struct.pack(">H", len(sndr_b)) + sndr_b + body_b
                    wire   = _hub_pack(_HUB_TYPE_CHAT, p,
                                       encrypt=self.enc_enabled)
                    threading.Thread(
                        target=self._hub_send, args=(wire,),
                        daemon=True, name="hub-chat-echo"
                    ).start()
            except Exception as e:
                app.logger.info("Console RX chat parse error: %s", e)

        elif msg_type == _HUB_TYPE_FL_REQ:
            # Spoke is requesting the file list.
            settings = load_settings()
            if settings.get("role") != "hub":
                return
            try:
                import json as _json
                files_dir = Path("/home/pi/.hf256/hub_files")
                file_list = {}
                if files_dir.exists():
                    for f_ in sorted(files_dir.iterdir()):
                        if f_.suffix == ".desc" or not f_.is_file():
                            continue
                        desc_file = f_.parent / (f_.name + ".desc")
                        desc = desc_file.read_text().strip()                                if desc_file.exists() else ""
                        file_list[f_.name] = {
                            "size":        f_.stat().st_size,
                            "description": desc
                        }
                rsp = _json.dumps(file_list).encode()
                wire = _hub_pack(_HUB_TYPE_FL_RSP, rsp,
                                 encrypt=self.enc_enabled)
                app.logger.info("Hub: FL_RSP with %d files", len(file_list))
                threading.Thread(
                    target=self._hub_send, args=(wire,),
                    daemon=True, name="hub-fl-rsp"
                ).start()
            except Exception as e:
                app.logger.error("Hub: FL_REQ error: %s", e)

        elif msg_type == _HUB_TYPE_RETRIEVE:
            # Spoke is asking for stored messages.
            # For FreeDV hub: check ~/.hf256/hub_messages/ for this callsign.
            settings = load_settings()
            if settings.get("role") != "hub":
                return
            try:
                import json as _json, struct as _struct
                remote = getattr(
                    getattr(self, "transport", None), "remote_call", None
                ) or ""
                msg_dir = Path("/home/pi/.hf256/hub_messages") / remote.upper()
                messages = []
                if msg_dir.exists():
                    for mf in sorted(msg_dir.iterdir()):
                        try:
                            messages.append(_json.loads(mf.read_text()))
                            mf.unlink()   # delete after delivery
                        except Exception:
                            pass

                # Build all outgoing packets first, then send in a
                # background thread (send_data blocks on ARQ — must not
                # be called from the reader thread).
                import datetime as _dt
                packets = []
                for m in messages:
                    sndr   = m.get("sender", "?")
                    body   = m.get("text", "")
                    ts     = m.get("timestamp", 0)
                    # Format timestamp as readable date/time
                    try:
                        dt_str = _dt.datetime.utcfromtimestamp(ts).strftime(
                            "%Y-%m-%d %H:%MZ")
                    except Exception:
                        dt_str = "unknown time"
                    # Prepend datestamp to body so receiver sees when it was stored
                    full_body = f"[Stored {dt_str}] {body}"
                    sndr_b = sndr.encode("utf-8")
                    body_b = full_body.encode("utf-8")
                    p      = _struct.pack(">H", len(sndr_b)) + sndr_b + body_b
                    packets.append(_hub_pack(_HUB_TYPE_CHAT, p,
                                             encrypt=self.enc_enabled))
                # Completion notice
                rsp = _json.dumps(
                    {"messages": len(messages),
                     "message": str(len(messages)) + " message(s) delivered"}
                ).encode()
                packets.append(_hub_pack(_HUB_TYPE_RETRIEVE_RSP, rsp,
                                         encrypt=self.enc_enabled))

                n = len(messages)
                def _send_retrieved(pkts=packets, cnt=n, rem=remote):
                    # Brief delay before transmitting back — gives the ARQ
                    # layer time to complete the ACK handshake for the
                    # RETRIEVE request before the hub starts responding.
                    import time as _t; _t.sleep(1.5)
                    for i, pkt in enumerate(pkts):
                        ok = self._hub_send(pkt)
                        if not ok:
                            app.logger.warning("Hub: retrieve send failed "
                                               "on packet %d/%d", i+1, len(pkts))
                        # Inter-packet gap after every packet except the last —
                        # gives ARQ time to complete before next transmission.
                        # Applied to ALL multi-packet responses (>= 2 packets).
                        if i < len(pkts) - 1:
                            _t.sleep(2.0)
                    app.logger.info("Hub: delivered %d messages to %s", cnt, rem)

                threading.Thread(target=_send_retrieved, daemon=True,
                                 name="hub-retrieve").start()
            except Exception as e:
                app.logger.error("Hub: RETRIEVE error: %s", e)

        elif msg_type == _HUB_TYPE_PASSWD_REQ:
            # Spoke requesting password change
            settings = load_settings()
            if settings.get("role") != "hub":
                return
            try:
                req      = json.loads(payload.decode())
                call     = req.get("callsign", "").upper()
                curr_pw  = req.get("current_pw", "")
                new_pw   = req.get("new_pw", "")
                import hashlib as _hl, json as _jl
                pw_file  = Path("/home/pi/.hf256/passwords.json")
                db       = _jl.loads(pw_file.read_text()) if pw_file.exists() else {}
                stored   = db.get(call)
                if not stored:
                    result, ok = f"No account for {call}", False
                elif _hl.sha256(curr_pw.encode()).hexdigest() != stored:
                    result, ok = "Current password incorrect", False
                elif len(new_pw) < 4:
                    result, ok = "New password too short (min 4 chars)", False
                else:
                    db[call] = _hl.sha256(new_pw.encode()).hexdigest()
                    pw_file.write_text(_jl.dumps(db, indent=2))
                    pw_file.chmod(0o600)
                    result, ok = "Password changed successfully", True
                rsp_payload = json.dumps({"ok": ok, "msg": result}).encode()
                wire = _hub_pack(_HUB_TYPE_PASSWD_RSP, rsp_payload,
                                 encrypt=self.enc_enabled)
                with self._lock:
                    t = self.transport
                if t:
                    t.send_data(wire)
                app.logger.info("Hub PASSWD_REQ %s: %s", call, result)
            except Exception as e:
                app.logger.error("PASSWD_REQ error: %s", e)

        elif msg_type == _HUB_TYPE_STORE:
            # Spoke is sending a message to store for another callsign.
            # Payload format: [recip_len:2][recip][inner_wire]
            # inner_wire = full hub wire format message containing CHAT payload
            # CHAT payload = [sender_len:2][sender][text]
            settings = load_settings()
            if settings.get("role") != "hub":
                return
            try:
                import json as _json, struct as _struct, time as _time
                off = 0
                to_len  = _struct.unpack(">H", payload[off:off+2])[0]; off += 2
                to_call = payload[off:off+to_len].decode("utf-8");     off += to_len
                inner_wire = payload[off:]   # remainder is encrypted hub wire

                # Unpack the inner wire to extract sender and text
                try:
                    inner_type, inner_payload, _ = _hub_unpack(inner_wire)
                except Exception as e:
                    app.logger.error("Hub: STORE inner_wire unpack failed: %s", e)
                    return

                if inner_type != _HUB_TYPE_CHAT:
                    app.logger.warning("Hub: STORE inner type=0x%02x unexpected",
                                       inner_type)
                    return

                sender_len = _struct.unpack(">H", inner_payload[0:2])[0]
                fr_call    = inner_payload[2:2+sender_len].decode("utf-8",
                                                                   errors="replace")
                text       = inner_payload[2+sender_len:].decode("utf-8",
                                                                   errors="replace")

                # Load user database for validation
                # Check if recipient is hub callsign without a mailbox (headless)
                hub_call = settings.get("callsign", "N0CALL").upper()
                if to_call == hub_call:
                    import json as _jl
                    _pw = Path("/home/pi/.hf256/passwords.json")
                    _db = _jl.loads(_pw.read_text()) if _pw.exists() else {}
                    if hub_call not in _db:
                        rej = json.dumps({
                            "ok":      False,
                            "message": (f"Hub is operating headless — "
                                        f"no mailbox for {hub_call}. "
                                        f"Hub operator: /adduser {hub_call} <password>")
                        }).encode()
                        rej_wire = _hub_pack(_HUB_TYPE_STORE_ACK, rej,
                                             encrypt=self.enc_enabled)
                        with self._lock:
                            t = self.transport
                        if t:
                            t.send_data(rej_wire)
                        return
                pw_file = Path("/home/pi/.hf256/passwords.json")
                known_calls = set()
                if pw_file.exists():
                    known_calls = {c.upper() for c in
                                   _json.loads(pw_file.read_text()).keys()}

                # Determine recipients — "*BUL*" means all registered users
                if to_call.upper() == "*BUL*":
                    # Don't store for the sender themselves
                    recipients = [c for c in known_calls
                                  if c.upper() != fr_call.upper()]
                    app.logger.info("Hub: bulletin from %s → %d recipients",
                                    fr_call, len(recipients))
                else:
                    # Validate recipient is a known callsign
                    if to_call.upper() not in known_calls:
                        app.logger.warning("Hub: STORE rejected — unknown "
                                           "recipient %s from %s",
                                           to_call, fr_call)
                        # Send rejection back to spoke
                        rej_payload = _json.dumps(
                            {"ok": False,
                             "to": to_call,
                             "message": (f"Unknown callsign {to_call} — "
                                         f"message not stored")}
                        ).encode()
                        rej_wire = _hub_pack(_HUB_TYPE_STORE_ACK, rej_payload,
                                             encrypt=self.enc_enabled)
                        threading.Thread(
                            target=self._hub_send, args=(rej_wire,),
                            daemon=True, name="hub-store-rej"
                        ).start()
                        return
                    recipients = [to_call.upper()]

                ts = int(_time.time())
                for recip in recipients:
                    msg_dir = Path("/home/pi/.hf256/hub_messages") / recip
                    msg_dir.mkdir(parents=True, exist_ok=True)
                    fname = msg_dir / str(int(_time.time() * 1000))
                    fname.write_text(_json.dumps(
                        {"sender": fr_call, "text": text,
                         "timestamp": ts}
                    ))
                app.logger.info("Hub: stored message from %s to %s: %s",
                                fr_call, to_call, text[:40])
                # Show on hub console
                self.send({"type": "chat",
                           "sender": "HUB",
                           "text": f"[STORE] {fr_call}→{to_call}: {text[:60]}"})
                # Send STORE_ACK back to spoke
                if to_call.upper() == "*BUL*":
                    ack_msg = f"Bulletin stored for {len(recipients)} station(s)"
                else:
                    ack_msg = f"Message stored for {to_call}"
                ack_payload = _json.dumps(
                    {"ok": True,
                     "to": to_call,
                     "message": ack_msg}
                ).encode()
                wire = _hub_pack(_HUB_TYPE_STORE_ACK, ack_payload,
                                 encrypt=self.enc_enabled)
                threading.Thread(
                    target=self._hub_send, args=(wire,),
                    daemon=True, name="hub-store-ack"
                ).start()
            except Exception as e:
                app.logger.error("Hub: STORE error: %s", e)

        elif msg_type == _HUB_TYPE_DL_REQ:
            # Spoke is requesting a file download.
            settings = load_settings()
            if settings.get("role") != "hub":
                return
            try:
                import json as _json, struct as _struct
                req      = _json.loads(payload.decode())
                filename = req.get("filename", "")
                app.logger.info("Hub: DL_REQ for file: %s", filename)

                files_dir = Path("/home/pi/.hf256/hub_files")
                file_path = files_dir / filename
                if not file_path.exists() or not file_path.is_file():
                    # File not found — send error
                    err = _json.dumps({"filename": filename,
                                       "error": "File not found"}).encode()
                    wire = _hub_pack(_HUB_TYPE_ERROR, err,
                                     encrypt=self.enc_enabled)
                    threading.Thread(
                        target=self._hub_send, args=(wire,),
                        daemon=True, name="hub-dl-err"
                    ).start()
                    return

                file_data  = file_path.read_bytes()
                # Send in chunks of 512 bytes to fit within FreeDV packet limits
                CHUNK_SIZE = 512
                total      = (len(file_data) + CHUNK_SIZE - 1) // CHUNK_SIZE
                fn_b       = filename.encode("utf-8")
                import hashlib as _hl
                file_hash  = _hl.md5(file_data).hexdigest().encode()

                self._dl_cancel = False   # reset for new download
                def _send_file(fd=file_data, fn=fn_b, fh=file_hash,
                               tot=total, enc=self.enc_enabled,
                               fname=filename):
                    for i in range(tot):
                        if self._dl_cancel:
                            app.logger.info("Hub: download cancelled at chunk %d/%d",
                                            i, tot)
                            return
                        chunk = fd[i*CHUNK_SIZE:(i+1)*CHUNK_SIZE]
                        # [fn_len:2][fn][chunk_num:4][total:4][hash_len:2][hash][data]
                        pkt = (_struct.pack(">H", len(fn)) + fn +
                               _struct.pack(">II", i, tot) +
                               _struct.pack(">H", len(fh)) + fh +
                               chunk)
                        wire = _hub_pack(_HUB_TYPE_FILE_DATA, pkt, encrypt=enc)
                        self._hub_send(wire)
                    # Send completion
                    done = _json.dumps({"filename": fname,
                                        "success": True}).encode()
                    wire = _hub_pack(_HUB_TYPE_COMPLETE, done, encrypt=enc)
                    self._hub_send(wire)
                    app.logger.info("Hub: sent %d chunks for %s", tot, fname)

                threading.Thread(target=_send_file, daemon=True,
                                 name="hub-dl").start()

            except Exception as e:
                app.logger.error("Hub: DL_REQ error: %s", e)

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

        elif msg_type == _HUB_TYPE_RETRIEVE_RSP:
            # Hub finished delivering stored messages
            try:
                import json as _json
                obj = _json.loads(payload.decode())
                n   = obj.get("messages", 0)
                if n == 0:
                    self.sys_msg("No messages waiting")
                else:
                    self.sys_msg(f"✓ {n} message(s) retrieved")
            except Exception as e:
                app.logger.info("Console RX retrieve_rsp parse error: %s", e)

        elif msg_type == _HUB_TYPE_STORE_ACK:
            # Hub confirmed it stored our message or bulletin
            try:
                import json as _json
                obj = _json.loads(payload.decode())
                if obj.get("ok"):
                    self.sys_msg(f"✓ {obj.get('message', 'Message stored')}")
                else:
                    self.sys_msg(f"✗ Store failed: {obj.get('message', 'unknown error')}")
            except Exception as e:
                app.logger.info("Console RX store_ack parse error: %s", e)

        elif msg_type == _HUB_TYPE_ERROR:
            # JSON: {"error": str} or {"filename": str, "error": str}
            # Hub sends this plaintext (encrypt=False) so the spoke can read it
            # even when there is a key mismatch — the error text diagnoses the problem.
            try:
                import json as _json
                obj = _json.loads(payload.decode())
                err = obj.get("error", "Unknown hub error")
                self.sys_msg(f"✗ Hub error: {err}")
                # Broadcast to browser so it's visible in the console
                self.send({"type": "error", "text": err})
            except Exception as exc:
                app.logger.info("Console RX error frame parse error: %s", exc)
                self.sys_msg(f"✗ Hub sent an error frame (unreadable: {exc})")

        else:
            app.logger.info("Console RX: unhandled type 0x%02x", msg_type)

    # ── Send ──────────────────────────────────────────────────────

    # ── Hub-local operation helpers ──────────────────────────────────

    def _hub_local_auth(self, password: str) -> bool:
        """Check password against passwords.json for hub callsign."""
        import hashlib as _hl, json as _jl
        pw_file = Path("/home/pi/.hf256/passwords.json")
        try:
            db = _jl.loads(pw_file.read_text()) if pw_file.exists() else {}
            stored = db.get(self.mycall.upper())
            if not stored:
                return False
            return _hl.sha256(password.encode()).hexdigest() == stored
        except Exception:
            return False

    def _hub_local_store(self, to_call: str, from_call: str,
                         text: str) -> tuple:
        """
        Store a chat message locally on the hub.
        Returns (ok: bool, error_msg: str).
        Rejects if recipient not in passwords.json.
        If recipient is hub callsign and no hub user exists, returns
        a headless rejection message.
        """
        import json as _jl
        pw_file  = Path("/home/pi/.hf256/passwords.json")
        msg_base = Path("/home/pi/.hf256/hub_messages")
        try:
            db       = _jl.loads(pw_file.read_text()) if pw_file.exists() else {}
            hub_call = load_settings().get("callsign", "N0CALL").upper()
            if to_call == hub_call and hub_call not in db:
                return (False,
                        f"Hub is operating headless — no mailbox for {hub_call}. "
                        f"Hub operator: run /adduser {hub_call} <password>.")
            if to_call not in db:
                return (False, f"Unknown recipient: {to_call}")
            msg_dir = msg_base / to_call
            msg_dir.mkdir(parents=True, exist_ok=True)
            ts    = int(time.time())           # seconds, matches STORE handler
            ts_ms = int(time.time() * 1000)   # milliseconds for filename uniqueness
            body  = _jl.dumps({"sender": from_call, "text": text,
                                "timestamp": ts})
            (msg_dir / str(ts_ms)).write_text(body)
            return (True, "")
        except Exception as e:
            return (False, str(e))

    def _hub_local_retrieve(self) -> list:
        """Read and delete stored messages for hub callsign. Returns list of (from, text, ts)."""
        import json as _jl
        msg_dir  = Path("/home/pi/.hf256/hub_messages") / self.mycall.upper()
        messages = []
        if not msg_dir.exists():
            return messages
        for f in sorted(msg_dir.iterdir()):
            if not f.is_file():
                continue
            try:
                body = _jl.loads(f.read_text())
                # Field names match STORE handler: "sender", "text", "timestamp"
                messages.append((body.get("sender", body.get("from", "?")),
                                  body.get("text", ""),
                                  body.get("timestamp", body.get("ts", 0))))
                f.unlink()
            except Exception:
                pass
        return messages

    def _hub_local_passwd(self, current_pw: str, new_pw: str) -> tuple:
        """Change password for hub callsign. Returns (ok, message)."""
        import hashlib as _hl, json as _jl
        pw_file = Path("/home/pi/.hf256/passwords.json")
        try:
            db     = _jl.loads(pw_file.read_text()) if pw_file.exists() else {}
            call   = self.mycall.upper()
            stored = db.get(call)
            if not stored:
                return (False, f"No account for {call} — use /adduser first")
            if _hl.sha256(current_pw.encode()).hexdigest() != stored:
                return (False, "Current password incorrect")
            if len(new_pw) < 4:
                return (False, "New password must be at least 4 characters")
            db[call] = _hl.sha256(new_pw.encode()).hexdigest()
            pw_file.write_text(_jl.dumps(db, indent=2))
            pw_file.chmod(0o600)
            return (True, "Password changed successfully")
        except Exception as e:
            return (False, str(e))

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
            self.sys_msg("✗ Not connected — use /connect <IP> first")
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
            self.enc_enabled  = bool(msg.get("encrypt", self.enc_enabled))
            transport         = msg.get("transport", "tcp")
            self.transport_mode = transport
            _, key = _hub_crypto()
            status = "key loaded" if key else "NO KEY — plaintext only"
            self.sys_msg(f"Session ready — {self.mycall} — {status}")

            # Hub operators are always authenticated — they don't need /auth
            settings = load_settings()
            if settings.get("role") == "hub":
                self.authenticated = True
            # Restore authenticated state on WebSocket reconnect ONLY —
            # only restore if the radio transport was already connected
            # before this hello arrived (i.e. mid-session WS reconnect).
            # Do NOT restore on a fresh connection where the spoke still
            # needs to authenticate over the air.
            elif msg.get("authenticated") and not self.authenticated:
                with self._lock:
                    t = self.transport
                    already_connected = (t is not None and
                                         getattr(t, "state", 0) == 2)
                if already_connected:
                    self.authenticated = True
                    app.logger.info("hello: restored authenticated=True "
                                    "(WebSocket reconnect, transport still up)")
                # If transport is not connected, browser auth state is stale —
                # spoke must re-authenticate on the new radio connection

            # Start the correct listener for the hub on first console load.
            # Skip _switch_modem only if the active transport already matches
            # what the browser is requesting — a WebSocket reconnect mid-session
            # must not tear down an active FreeDV/ARDOP connection.
            # But if the transport type has changed (e.g. TCP server running
            # but browser wants FreeDV), we must switch.
            settings = load_settings()
            if settings.get("role") == "hub":
                with self._lock:
                    t = self.transport
                active_type = type(t).__name__ if t is not None else None
                # Map transport name to expected type
                type_map = {
                    "tcp":       "TCPServerTransport",
                    "ardop-fm":  "ARDOPConnection",
                    "ardop-hf":  "ARDOPConnection",
                    "vhf-ax25":  None,   # managed by _direwolf singleton, no per-session transport
                    "hf-ax25":   None,
                }
                expected_type = type_map.get(transport)
                already_correct = (active_type is not None and
                                   active_type == expected_type)
                if not already_correct:
                    threading.Thread(
                        target=self._switch_modem,
                        args=(transport,),
                        daemon=True, name="hub-init-transport"
                    ).start()
                else:
                    app.logger.info("hello: transport %s already active "
                                    "for %s, skipping _switch_modem",
                                    active_type, transport)

        elif mtype == "set_transport":
            new_mode = msg.get("transport", "tcp")
            self.transport_mode = new_mode
            # NOTE: _active_transport is updated inside _switch_modem
            # AFTER the modem is confirmed ready, not here.
            # This prevents a "Partial" flash while the modem is starting.
            threading.Thread(
                target=self._switch_modem,
                args=(new_mode,),
                daemon=True
            ).start()

        elif mtype == "set_hybrid":
            # Hybrid mode is retired — TCP is always on for hubs.
            # This handler is kept for backward compatibility with any
            # cached browser state; it is a no-op.
            app.logger.info("set_hybrid received (deprecated — TCP always on)")
            self.sys_msg("ℹ TCP server is always active on hub stations")

        elif mtype == "tcp_enabled":
            # New: spoke-side toggle to enable/disable TCP outgoing connections.
            # Hub side: TCP server is always running; this is a no-op.
            enabled = bool(msg.get("enabled", True))
            settings = load_settings()
            if settings.get("role") == "hub":
                self.sys_msg("ℹ TCP server always running on hub (port 14256)")
            else:
                if not enabled:
                    # Spoke disconnects TCP if currently connected via TCP
                    with self._lock:
                        t = self.transport
                    if t and self.transport_mode == "tcp":
                        try:
                            t.close()
                        except Exception:
                            pass
                        with self._lock:
                            self.transport = None
                        self.send({"type": "disconnected"})
                        self.sys_msg("TCP disabled — use a radio transport to connect")

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
            elif tmode in ("vhf-ax25", "hf-ax25"):
                target = msg.get("target_call", "").strip().upper()
                if not target:
                    self.sys_msg("\u2717 Usage: /connect <CALLSIGN>  (AX.25 mode)")
                    return
                from hf256.direwolf_transport import RADIO_PORT_VHF, RADIO_PORT_HF
                if tmode == "vhf-ax25":
                    radio_port = RADIO_PORT_VHF
                else:
                    # HF is on channel 1 when VHF is also configured,
                    # or channel 0 when HF is the only active radio port.
                    settings  = load_settings()
                    vhf_card  = settings.get("direwolf_vhf_card")
                    radio_port = RADIO_PORT_HF if vhf_card is not None else RADIO_PORT_VHF
                self.send({"type": "connecting"})
                threading.Thread(target=self._connect_ax25,
                                 args=(target, radio_port), daemon=True).start()
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
                    elif self.transport_mode in ("vhf-ax25", "hf-ax25"):
                        # AX.25: send DISC frame via DirewolfSpokeTransport
                        try:
                            self.transport.vara_disconnect()
                        except Exception:
                            pass
                        # Don't null out _direwolf_spoke itself — just detach
                        # from this session so it can be reused for the next call
                        self.transport = None
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
                # Show immediately as local echo — don't wait for hub ARQ queue
                self.send({"type": "chat", "sender": self.mycall,
                           "text": text, "local": True})
                # Send in background thread so we can report ACK receipt
                def _send_chat(t=text):
                    ok = self._send_hub(
                        _HUB_TYPE_CHAT, _chat_payload(self.mycall, t)
                    )
                    with self._lock:
                        tport = self.transport
                    tmode = getattr(tport, '__class__', type(None)).__name__
                    is_radio = "Direwolf" in tmode or "Spoke" in tmode
                    with self._lock:
                        remote = (getattr(self.transport, "remote_call", None)
                                  if self.transport else None)
                    dest = remote if remote else "hub"
                    if ok:
                        if is_radio:
                            # For radio: AX.25 ACK confirms frame reached hub's
                            # data link layer. Application-level confirmation
                            # arrives as the hub echo — console.html shows that
                            # echo as "✓ Hub received: ..." when it arrives.
                            self.sys_msg(f"↗ Transmitting to {dest}...")
                        else:
                            self.sys_msg(f"✓ Message delivered to {dest}")
                    else:
                        self.sys_msg(f"✗ Send failed — not connected")
                threading.Thread(target=_send_chat, daemon=True,
                                 name="chat-send").start()

        elif mtype == "auth":
            password = msg.get("password", "")
            if load_settings().get("role") == "hub":
                if self._hub_local_auth(password):
                    self.authenticated = True
                    msg_dir = (Path("/home/pi/.hf256/hub_messages")
                               / self.mycall.upper())
                    pending = (sum(1 for f in msg_dir.iterdir() if f.is_file())
                               if msg_dir.exists() else 0)
                    # Notify browser so ST.authenticated is set client-side
                    self.send({"type": "authenticated",
                               "callsign": self.mycall,
                               "pending": pending})
                    self.sys_msg(f"✓ Authenticated as {self.mycall}")
                    if pending:
                        self.sys_msg(f"  {pending} message(s) waiting — use /retrieve")
                else:
                    self.sys_msg("✗ Authentication failed — wrong password")
                    self.sys_msg("  (No account? Ask hub operator to /adduser)")
            else:
                payload = json.dumps(
                    {"callsign": self.mycall, "password": password}
                ).encode()

                def _send_auth_and_wait(pw_payload=payload):
                    """
                    Send AUTH_REQ and treat AUTH_RSP as the confirmation.

                    Strategy:
                    - Use normal ARQ send_data() for AUTH_REQ delivery
                    - Hub ACKs AUTH_REQ then immediately sends AUTH_RSP
                    - Hub AUTH_RSP is also sent via normal ARQ
                    - Spoke waits for self.authenticated to be set
                    - If not authenticated within observed window, retry
                    - Keepalive cannot collide — watchdog skips if send_lock held

                    The 2s post-CONN_ACK delay in freedv_transport ensures
                    the channel is clear before AUTH_REQ is transmitted.
                    """
                    self.sys_msg("Sending authentication over radio...")

                    # Observed DATAC1 round trip: AUTH_REQ (~18s) + AUTH_RSP (~18s)
                    # Use 45s window — covers full round trip with margin.
                    # Retry once if no response.
                    for attempt in range(1, 3):
                        if attempt > 1:
                            self.sys_msg("  No response — retrying...")

                        try:
                            ok = self._send_hub(_HUB_TYPE_AUTH_REQ, pw_payload)
                        except Exception as e:
                            app.logger.exception("AUTH_REQ send error: %s", e)
                            self.sys_msg(f"✗ Authentication error: {e}")
                            return

                        if not ok:
                            if self.authenticated:
                                return
                            self.sys_msg("✗ Authentication failed — could not transmit")
                            return

                        # AUTH_REQ ACKed — now wait for AUTH_RSP
                        # Window = 45s: covers AUTH_RSP airtime + ARQ retry
                        for _ in range(90):   # 90 × 0.5s = 45s
                            if self.authenticated:
                                return
                            time.sleep(0.5)
                            if self.authenticated:
                                return
                            with self._lock:
                                still_on = (self.transport is not None and
                                            getattr(self.transport, "state", 0) == 2)
                            if not still_on:
                                self.sys_msg("✗ Authentication failed — link dropped")
                                return

                    if not self.authenticated:
                        self.sys_msg("✗ Authentication timed out — hub did not respond")
                        self.sys_msg("  Check hub is running and callsign is registered")

                threading.Thread(target=_send_auth_and_wait, daemon=True,
                                 name="auth-send").start()

        elif mtype == "send":
            to   = msg.get("to", "").upper()
            text = msg.get("text", "")
            if to and text:
                if load_settings().get("role") == "hub":
                    if not self.authenticated:
                        self.sys_msg("✗ Must authenticate first — use /auth <password>")
                    else:
                        ok, err = self._hub_local_store(to, self.mycall, text)
                        if ok:
                            self.sys_msg(f"✓ Message stored for {to}")
                        else:
                            self.sys_msg(f"✗ {err}")
                else:
                    import struct as _struct
                    inner_wire    = _hub_pack(_HUB_TYPE_CHAT,
                                             _chat_payload(self.mycall, text),
                                             encrypt=self.enc_enabled)
                    recip_bytes   = to.encode("utf-8")
                    store_payload = _struct.pack(">H", len(recip_bytes)) + recip_bytes + inner_wire
                    def _do_send(p=store_payload, dest=to):
                        ok = self._send_hub(_HUB_TYPE_STORE, p)
                        if not ok:
                            self.sys_msg(f"✗ Transmission failed — hub did not ACK")
                    threading.Thread(target=_do_send, daemon=True,
                                     name="send-msg").start()

        elif mtype == "retrieve":
            if load_settings().get("role") == "hub":
                if not self.authenticated:
                    self.sys_msg("✗ Must authenticate first — use /auth <password>")
                else:
                    messages = self._hub_local_retrieve()
                    if not messages:
                        self.sys_msg("No messages waiting")
                    else:
                        import datetime as _dt
                        self.sys_msg(f"─── {len(messages)} message(s) ───────────────")
                        for frm, txt, ts in messages:
                            dt = _dt.datetime.utcfromtimestamp(
                                ts / 1000).strftime("%Y-%m-%d %H:%MZ")
                            self.sys_msg(f"[{dt}] From {frm}: {txt}")
                        self.sys_msg("─────────────────────────────────────────")
            else:
                self._send_hub(_HUB_TYPE_RETRIEVE, b"{}")

        elif mtype == "files":
            if load_settings().get("role") == "hub":
                files_dir = Path("/home/pi/.hf256/hub_files")
                files = [f for f in sorted(files_dir.iterdir())
                         if f.is_file() and f.suffix != ".desc"]                         if files_dir.exists() else []
                if not files:
                    self.sys_msg("No files available on hub")
                else:
                    self.sys_msg("─── Hub Files ────────────────────────────")
                    for f in files:
                        desc_f = files_dir / (f.name + ".desc")
                        desc   = desc_f.read_text().strip() if desc_f.exists() else ""
                        self.sys_msg(
                            f"  {f.name}  ({f.stat().st_size:,} bytes)"
                            + (f"  — {desc}" if desc else ""))
                    self.sys_msg("  Use /download <filename> to save a file")
                    self.sys_msg("─────────────────────────────────────────")
            else:
                self._send_hub(_HUB_TYPE_FL_REQ, b"{}")

        elif mtype == "download":
            filename = msg.get("filename", "")
            if filename:
                if load_settings().get("role") == "hub":
                    safe = Path(filename).name
                    f    = Path("/home/pi/.hf256/hub_files") / safe
                    if not f.exists() or not f.is_file():
                        self.sys_msg(f"✗ File not found: {safe}")
                    else:
                        self.sys_msg(f"✓ {safe} ({f.stat().st_size:,} bytes)")
                        self.sys_msg("  Hub files are accessible via the Hub Files page")
                else:
                    payload = json.dumps({"filename": filename}).encode()
                    self._send_hub(_HUB_TYPE_DL_REQ, payload)

        elif mtype == "cancel":
            self._dl_cancel = True
            self.sys_msg("Download cancelled")

        elif mtype == "modem_status":
            # Report current modem state to console
            ardop_up = _modem_manager.ardop_running()
            import subprocess as _sp
            r2 = _sp.run(["systemctl", "is-active", "rigctld"],
                         capture_output=True, text=True)
            rig_up = r2.stdout.strip() == "active"
            OK_  = "\u2713 running"
            OFF_ = "\u25cb stopped"
            self.sys_msg("Modem status:")
            self.sys_msg("  ardopc     : " + (OK_ if ardop_up else OFF_))
            self.sys_msg("  rigctld    : " + (OK_ if rig_up   else OFF_))
            config = load_config_env()
            self.sys_msg(f"  audio card : {config.get('AUDIO_CARD', '?')}")
            self.sys_msg(f"  serial port: {config.get('SERIAL_PORT', 'none')}")

        elif mtype == "passwd":
            current_pw = msg.get("current", "")
            new_pw     = msg.get("new", "")
            if not current_pw or not new_pw:
                self.sys_msg("✗ Usage: /passwd <current_password> <new_password>")
            elif load_settings().get("role") == "hub":
                ok, result = self._hub_local_passwd(current_pw, new_pw)
                self.sys_msg(("✓ " if ok else "✗ ") + result)
            else:
                if not self.authenticated:
                    self.sys_msg("✗ Must authenticate first — use /auth <password>")
                else:
                    payload = json.dumps({
                        "callsign":   self.mycall,
                        "current_pw": current_pw,
                        "new_pw":     new_pw
                    }).encode()
                    self._send_hub(_HUB_TYPE_PASSWD_REQ, payload)

        elif mtype == "bulletin":
            text = msg.get("text", "")
            if not text:
                return
            settings = load_settings()
            if settings.get("role") == "hub":
                # Hub sending bulletin: store for all local users + announce over air
                try:
                    import json as _json, time as _time
                    pw_file = Path("/home/pi/.hf256/passwords.json")
                    all_calls = []
                    if pw_file.exists():
                        all_calls = list(_json.loads(pw_file.read_text()).keys())
                    recipients = [c for c in all_calls
                                  if c.upper() != self.mycall.upper()]
                    ts = int(_time.time())
                    for recip in recipients:
                        msg_dir = Path("/home/pi/.hf256/hub_messages") / recip.upper()
                        msg_dir.mkdir(parents=True, exist_ok=True)
                        fname = msg_dir / str(int(_time.time() * 1000))
                        fname.write_text(_json.dumps(
                            {"sender": self.mycall, "text": text,
                             "timestamp": ts}
                        ))
                    self.sys_msg(f"✓ Bulletin stored for {len(recipients)} station(s)")
                    app.logger.info("Hub: bulletin stored for %d stations",
                                    len(recipients))
                    # Also transmit as FreeDV ANNOUNCE if FreeDV transport active
                    with self._lock:
                        t = self.transport
                    if t and hasattr(t, "send_announce"):
                        try:
                            t.send_announce(f"BUL {self.mycall}: {text}")
                        except Exception:
                            pass
                except Exception as e:
                    self.sys_msg(f"✗ Bulletin error: {e}")
            else:
                # Spoke: send STORE to hub with sentinel recipient "*BUL*"
                import struct as _struct
                inner_wire = _hub_pack(_HUB_TYPE_CHAT,
                                       _chat_payload(self.mycall, text),
                                       encrypt=self.enc_enabled)
                recip_bytes   = b"*BUL*"
                store_payload = _struct.pack(">H", len(recip_bytes)) + recip_bytes + inner_wire
                def _do_bul(p=store_payload):
                    ok = self._send_hub(_HUB_TYPE_STORE, p)
                    if not ok:
                        self.sys_msg("✗ Bulletin transmission failed — hub did not ACK")
                threading.Thread(target=_do_bul, daemon=True,
                                 name="bul-send").start()

        elif mtype == "adduser":
            call = msg.get("call", "").strip().upper()
            pw   = msg.get("password", "").strip()
            if not call or not pw:
                self.sys_msg("✗ Usage: /adduser <CALLSIGN> <password>")
            elif load_settings().get("role") == "hub":
                # Hub station: write directly to local passwords.json
                try:
                    import hashlib as _hl, json as _json
                    pw_file = Path("/home/pi/.hf256/passwords.json")
                    pw_file.parent.mkdir(parents=True, exist_ok=True)
                    db = {}
                    if pw_file.exists():
                        db = _json.loads(pw_file.read_text())
                    db[call] = _hl.sha256(pw.encode()).hexdigest()
                    pw_file.write_text(_json.dumps(db, indent=2))
                    pw_file.chmod(0o600)
                    self.sys_msg(f"✓ User added: {call}")
                    app.logger.info("Hub: added user %s", call)
                except Exception as e:
                    self.sys_msg(f"✗ adduser failed: {e}")
            else:
                # Spoke: send command to remote hub over transport
                self._send_hub(_HUB_TYPE_CHAT,
                               _chat_payload(self.mycall,
                                             "/adduser " + call + " " + pw))

        elif mtype == "listusers":
            if load_settings().get("role") == "hub":
                # Hub station: read directly from local passwords.json
                try:
                    import json as _json
                    pw_file = Path("/home/pi/.hf256/passwords.json")
                    if pw_file.exists():
                        db = _json.loads(pw_file.read_text())
                        users = sorted(db.keys())
                        self.sys_msg(
                            "Hub users (" + str(len(users)) + "): " +
                            (", ".join(users) if users else "(none)")
                        )
                    else:
                        self.sys_msg("No users registered yet")
                except Exception as e:
                    self.sys_msg(f"✗ listusers failed: {e}")
            else:
                self._send_hub(_HUB_TYPE_CHAT,
                               _chat_payload(self.mycall, "/listusers"))

        elif mtype == "storage":
            settings = load_settings()
            if settings.get("role") == "hub":
                # Hub: read storage stats directly from local filesystem
                try:
                    msg_base  = Path("/home/pi/.hf256/hub_messages")
                    files_dir = Path("/home/pi/.hf256/hub_files")
                    # Count queued messages per recipient
                    msg_stats = {}
                    if msg_base.exists():
                        for recip_dir in sorted(msg_base.iterdir()):
                            if recip_dir.is_dir():
                                count = sum(1 for f in recip_dir.iterdir()
                                            if f.is_file())
                                if count:
                                    msg_stats[recip_dir.name] = count
                    total_msgs = sum(msg_stats.values())
                    # Count hub files
                    file_count = 0
                    if files_dir.exists():
                        file_count = sum(1 for f in files_dir.iterdir()
                                         if f.is_file()
                                         and f.suffix != ".desc")
                    self.sys_msg("─── Hub Storage ──────────────────────────")
                    self.sys_msg(f"  Files available : {file_count}")
                    self.sys_msg(f"  Queued messages : {total_msgs}")
                    if msg_stats:
                        for call, cnt in msg_stats.items():
                            self.sys_msg(f"    {call}: {cnt} message(s)")
                    else:
                        self.sys_msg("  No queued messages")
                    self.sys_msg("──────────────────────────────────────────")
                except Exception as e:
                    self.sys_msg(f"✗ Storage read error: {e}")
            else:
                # Spoke: send as chat command to hub for relay
                self._send_hub(_HUB_TYPE_CHAT, _chat_payload(self.mycall, "/storage"))

        # ── v0.1.0 multi-session hub commands ──────────────────────────

        elif mtype == "hub_broadcast":
            # Hub operator broadcasts to all connected authenticated spokes
            text = msg.get("text", "").strip()
            if not text:
                return
            if _hub_core is not None:
                _hub_core.broadcast(text, from_call=self.mycall)
                n = _session_manager.count() if _session_manager else 0
                self.sys_msg(f"✓ Broadcast sent to {n} session(s)")
            else:
                # Fallback: store as bulletin when MSA not available
                self.dispatch({"type": "bulletin", "text": text})

        elif mtype == "hub_sessions":
            # Return live session list to browser console
            if _session_manager is not None:
                sessions = _session_manager.status_list()
            else:
                sessions = []
            self.send({"type": "session_list", "sessions": sessions})

        elif mtype == "direwolf":
            # Browser clicked VHF AX.25 or HF AX.25 transport button
            transport_label = msg.get("label", "AX.25")
            if _direwolf is not None:
                self.sys_msg(f"✓ {transport_label} transport active via Direwolf")
                self.sys_msg("  Listening for incoming AX.25 connections")
            else:
                from hf256.direwolf_config import direwolf_running
                if direwolf_running():
                    self.sys_msg(
                        f"✗ {transport_label} transport: Direwolf is running but "
                        "not connected — restart portal to re-attach"
                    )
                else:
                    self.sys_msg(
                        f"✗ {transport_label} transport: Direwolf is not running"
                    )
                    self.sys_msg(
                        "  Configure via Settings → Direwolf then restart portal"
                    )

        elif mtype == "mesh_sync":
            # Hub operator triggers immediate sync with a peer
            peer = msg.get("peer", "").strip()
            if not peer:
                self.sys_msg("✗ Usage: /mesh_sync <peer-ip>")
                return
            if _mesh_sync is not None:
                self.sys_msg(f"Mesh sync started with {peer}...")
                threading.Thread(
                    target=_mesh_sync.sync_now,
                    args=(peer,),
                    daemon=True,
                    name="mesh-sync-manual",
                ).start()
            else:
                self.sys_msg("✗ Mesh sync not running — add peers in Settings")

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
        """
        Drain _send_q and forward to WebSocket.
        On send error the WebSocket is broken — log it, discard remaining
        queued items, and exit. The browser's onclose handler will
        auto-reconnect within 5 seconds.
        """
        ws_broken = False
        while True:
            item = session._send_q.get()
            if item is None:           # None = sentinel, stop
                break
            if ws_broken:
                continue               # drain queue without sending
            try:
                ws.send(item)
            except Exception as e:
                app.logger.info("Console ws.send error (browser disconnected): %s", e)
                ws_broken = True       # keep draining so queue doesn't block

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
        _active_sessions.discard(session)
        app.logger.info("Console WebSocket closed")


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

# ------------------------------------------------------------------ #
# v0.1.0 Multi-session hub REST API endpoints
# ------------------------------------------------------------------ #

@app.route("/api/hub/sessions")
def api_hub_sessions():
    """Return all active spoke sessions for the status dashboard."""
    if _session_manager is None:
        return jsonify({"sessions": [], "total": 0})
    return jsonify({
        "sessions": _session_manager.status_list(),
        "total":    _session_manager.count(),
    })


@app.route("/api/hub/broadcast", methods=["POST"])
def api_hub_broadcast():
    """Hub operator broadcasts a message to all authenticated spokes."""
    if _hub_core is None:
        return jsonify({"ok": False, "error": "Hub not running"}), 503
    data = request.get_json(force=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    _hub_core.broadcast(text)
    app.logger.info("Hub broadcast: %s", text[:80])
    return jsonify({"ok": True})


@app.route("/api/hub/send", methods=["POST"])
def api_hub_send():
    """Hub operator sends a direct message to one spoke callsign."""
    if _hub_core is None:
        return jsonify({"ok": False, "error": "Hub not running"}), 503
    data     = request.get_json(force=True) or {}
    callsign = data.get("callsign", "").upper().strip()
    text     = data.get("text", "").strip()
    if not callsign or not text:
        return jsonify({"ok": False, "error": "Missing callsign or text"}), 400
    ok = _hub_core.send_to(callsign, text)
    return jsonify({"ok": ok})


@app.route("/api/hub/disconnect", methods=["POST"])
def api_hub_disconnect_session():
    """Hub operator force-disconnects a session by ID."""
    if _session_manager is None:
        return jsonify({"ok": False, "error": "Hub not running"}), 503
    data       = request.get_json(force=True) or {}
    session_id = data.get("session_id", "").strip()
    if not session_id:
        return jsonify({"ok": False, "error": "Missing session_id"}), 400
    session = _session_manager.get(session_id)
    if session is None:
        return jsonify({"ok": False, "error": "Session not found"}), 404
    _session_manager.close_session(session_id)
    app.logger.info("Hub: force-disconnected session %s", session_id)
    return jsonify({"ok": True})


@app.route("/api/direwolf/set-levels", methods=["POST"])
def api_direwolf_set_levels():
    """
    Set ALSA capture (RX) and playback (TX) levels for the configured
    Direwolf audio cards.

    Body JSON: {"rx_pct": int, "tx_pct": int}
    Defaults: rx_pct=40, tx_pct=70

    Direwolf recommends audio level ~50 on its meter.
    DigiRig typically needs rx_pct 20-30% to reach that (depends on radio AF out).

    Levels are persisted via 'alsactl store' so they survive reboots.
    alsa-restore.service (part of alsa-utils, installed by default on Pi OS)
    runs 'alsactl restore' at boot to reload them automatically.
    """
    data    = request.get_json(force=True) or {}
    rx_pct  = int(data.get("rx_pct", 40))
    tx_pct  = int(data.get("tx_pct", 70))
    rx_pct  = max(10, min(rx_pct, 100))
    tx_pct  = max(10, min(tx_pct, 100))

    settings = load_settings()
    vhf_card = settings.get("direwolf_vhf_card")
    hf_card  = settings.get("direwolf_hf_card")

    results = []
    errors  = []

    for label, card in [("VHF", vhf_card), ("HF", hf_card)]:
        if card is None:
            continue
        try:
            r = set_audio_levels(int(card), speaker_pct=tx_pct, mic_pct=rx_pct)
            results.append(f"{label} card {card}: {'; '.join(r.get('results', []))}")
        except Exception as exc:
            errors.append(f"{label} card {card}: {exc}")
            app.logger.error("set-levels %s card %s: %s", label, card, exc)

    if errors and not results:
        return jsonify({"ok": False, "error": "; ".join(errors)})

    # Persist levels to ALSA state file — alsa-restore.service loads this at boot
    try:
        subprocess.run(["alsactl", "store"], timeout=5, capture_output=True)
        app.logger.info("api_direwolf_set_levels: alsactl store complete")
    except Exception as exc:
        app.logger.warning("api_direwolf_set_levels: alsactl store failed: %s", exc)
        errors.append(f"alsactl store failed: {exc}")

    # Save rx/tx percentages to settings.json so Settings page can reload them
    # and _attach_direwolf can re-apply them automatically after any restart
    settings["direwolf_rx_pct"] = rx_pct
    settings["direwolf_tx_pct"] = tx_pct
    save_settings(settings)
    app.logger.info("api_direwolf_set_levels: saved rx=%d%% tx=%d%% to settings",
                    rx_pct, tx_pct)

    return jsonify({"ok": True, "results": results, "errors": errors})


@app.route("/api/direwolf/status")
def api_direwolf_status():
    """Return Direwolf service status and connection state."""
    try:
        from hf256.direwolf_config import direwolf_running
        running   = direwolf_running()
    except ImportError:
        running   = False
    connected = (_direwolf is not None or _direwolf_spoke is not None)
    return jsonify({"running": running, "connected": connected})


@app.route("/api/direwolf/conf")
def api_direwolf_conf():
    """Return the current content of /etc/direwolf/direwolf.conf for diagnostics."""
    try:
        from hf256.direwolf_config import get_direwolf_conf
        content = get_direwolf_conf()
    except ImportError:
        content = "# direwolf_config module not available"
    return jsonify({"content": content})


@app.route("/api/direwolf/logs")
def api_direwolf_logs():
    """Return recent Direwolf journal entries for diagnostics."""
    lines = int(request.args.get("lines", 50))
    lines = max(10, min(lines, 200))   # clamp 10–200
    try:
        from hf256.direwolf_config import get_direwolf_log
        content = get_direwolf_log(lines)
    except ImportError:
        content = "direwolf_config module not available"
    return jsonify({"content": content, "lines": lines})


@app.route("/api/direwolf/setup", methods=["POST"])
def api_direwolf_setup():
    """
    Configure and (re)start Direwolf from posted settings.
    Keys: direwolf_vhf_card, direwolf_vhf_serial, direwolf_vhf_ptt,
          direwolf_vhf_baud, direwolf_hf_card, direwolf_hf_serial, direwolf_hf_ptt
    """
    try:
        from hf256.direwolf_config import setup_direwolf_from_settings
    except ImportError:
        return jsonify({"ok": False,
                        "message": "direwolf_config module not found"}), 503
    data     = request.get_json(force=True) or {}
    settings = load_settings()
    settings.update(data)
    ok, msg = setup_direwolf_from_settings(settings)
    if ok:
        # Persist the new Direwolf keys to settings.json
        for key in ("direwolf_vhf_card", "direwolf_vhf_serial",
                    "direwolf_vhf_ptt",  "direwolf_vhf_baud",
                    "direwolf_hf_card",  "direwolf_hf_serial",
                    "direwolf_hf_ptt",   "direwolf_hf_alsa_device",
                    "direwolf_hf_hamlib_model"):
            if key in data:
                settings[key] = data[key]
        save_settings(settings)

        # Attach DirewolfTransport / DirewolfSpokeTransport without portal restart.
        # Run in a daemon thread so the HTTP response returns immediately.
        def _attach_bg():
            ok2, msg2 = _attach_direwolf()
            if ok2:
                app.logger.info("api_direwolf_setup: Direwolf attached — %s", msg2)
            else:
                app.logger.warning("api_direwolf_setup: attach failed — %s", msg2)
        threading.Thread(target=_attach_bg, daemon=True,
                         name="direwolf-attach").start()

    return jsonify({"ok": ok, "message": msg})


@app.route("/api/mesh/peers", methods=["GET"])
def api_mesh_peers_get():
    """Return the configured mesh peer list."""
    settings = load_settings()
    return jsonify({"peers": settings.get("mesh_peers", [])})


@app.route("/api/mesh/peers", methods=["POST"])
def api_mesh_peers_post():
    """Add or remove a mesh peer hub address."""
    data   = request.get_json(force=True) or {}
    action = data.get("action", "add")
    addr   = data.get("address", "").strip()
    if not addr:
        return jsonify({"ok": False, "error": "Missing address"}), 400

    settings = load_settings()
    peers    = settings.get("mesh_peers", [])

    if action == "add" and addr not in peers:
        peers.append(addr)
        if _mesh_sync:
            _mesh_sync.add_peer(addr)
    elif action == "remove" and addr in peers:
        peers.remove(addr)
        if _mesh_sync:
            _mesh_sync.remove_peer(addr)

    settings["mesh_peers"] = peers
    save_settings(settings)
    return jsonify({"ok": True, "peers": peers})


@app.route("/api/mesh/sync-now", methods=["POST"])
def api_mesh_sync_now():
    """Trigger an immediate mesh sync with a specific peer."""
    if _mesh_sync is None:
        return jsonify({"ok": False, "error": "Mesh sync not running"}), 503
    data = request.get_json(force=True) or {}
    addr = data.get("address", "").strip()
    if not addr:
        return jsonify({"ok": False, "error": "Missing address"}), 400
    threading.Thread(
        target=_mesh_sync.sync_now, args=(addr,),
        daemon=True, name="mesh-sync-api",
    ).start()
    return jsonify({"ok": True, "message": f"Sync started with {addr}"})


# ------------------------------------------------------------------ #
# Hub services — starts at portal boot, no browser session needed
# Replaces the old single-client _start_hub_tcp_server().
# ------------------------------------------------------------------ #

def _apply_stored_audio_levels(settings: dict) -> None:
    """
    Apply the stored rx/tx audio levels from settings.json to ALSA.

    Called automatically after Direwolf attaches so the operator-configured
    levels are always in effect — even after a reboot or Direwolf restart.
    Levels are saved to settings.json by api_direwolf_set_levels() and are
    also persisted via 'alsactl store' for the alsa-restore.service mechanism.
    This function is a belt-and-suspenders fallback in case alsactl restore
    hasn't run yet when the portal starts.
    """
    rx_pct  = settings.get("direwolf_rx_pct")
    tx_pct  = settings.get("direwolf_tx_pct")
    if rx_pct is None and tx_pct is None:
        return   # operator has not configured levels — leave ALSA defaults alone
    rx_pct = int(rx_pct or 40)
    tx_pct = int(tx_pct or 70)
    vhf_card = settings.get("direwolf_vhf_card")
    hf_card  = settings.get("direwolf_hf_card")
    for label, card in [("VHF", vhf_card), ("HF", hf_card)]:
        if card is None:
            continue
        try:
            r = set_audio_levels(int(card), speaker_pct=tx_pct, mic_pct=rx_pct)
            app.logger.info(
                "_apply_stored_audio_levels: %s card %d rx=%d%% tx=%d%% → %s",
                label, card, rx_pct, tx_pct, r.get("results", []),
            )
        except Exception as exc:
            app.logger.warning(
                "_apply_stored_audio_levels: %s card %d error: %s", label, card, exc
            )


def _attach_direwolf() -> tuple:
    """
    Connect DirewolfTransport (hub) or DirewolfSpokeTransport (spoke) to the
    Direwolf AGW interface on port 8000.

    Safe to call at any time — idempotent on repeated calls.
    Called automatically by api_direwolf_setup after a successful Apply,
    and on-demand from _switch_modem when the operator selects AX.25.

    Returns (success: bool, message: str).
    """
    global _direwolf, _direwolf_spoke

    settings = load_settings()
    role     = settings.get("role", "")
    callsign = settings.get("callsign", "N0CALL").upper()
    vhf_card = settings.get("direwolf_vhf_card")
    hf_card  = settings.get("direwolf_hf_card")

    if vhf_card is None and hf_card is None:
        return False, "No Direwolf channels configured in Settings"

    try:
        from hf256.direwolf_config import direwolf_running
        if not direwolf_running():
            return False, "Direwolf service is not running"
    except ImportError:
        return False, "direwolf_config module not available"

    try:
        from hf256.direwolf_transport import DirewolfTransport, DirewolfSpokeTransport
    except ImportError:
        return False, "direwolf_transport module not available"

    if role == "hub":
        # Serialise hub DirewolfTransport creation — prevents concurrent
        # calls from creating multiple AGW clients for the same callsign.
        with _direwolf_lock:
            if _session_manager is None or _hub_core is None:
                return False, ("Hub core not initialised — "
                               "portal may still be starting up")
            # If already connected and healthy, skip re-creation.
            # _attach_direwolf is called from both _start_hub_services() at
            # boot and from the console auto-attach on WS open. Without this
            # guard, the second call tears down the healthy transport and
            # creates a new AGW client — causing Direwolf to log three AGW
            # client connections and a brief gap in AX.25 listening.
            if _direwolf is not None:
                try:
                    if _direwolf._sock is not None:
                        app.logger.debug(
                            "_attach_direwolf: hub already attached, skipping")
                        return True, "Direwolf AX.25 already attached (hub)"
                except Exception:
                    pass
                # Socket is dead — stop cleanly before re-creating
                try:
                    _direwolf.stop()
                except Exception:
                    pass
            dw = DirewolfTransport(
                mycall               = callsign,
                session_manager      = _session_manager,
                on_client_message    = _hub_core.on_message,
                on_client_connect    = _hub_core.on_connect,
                on_client_disconnect = _hub_core.on_disconnect,
                vhf_enabled = (vhf_card is not None),
                hf_enabled  = (hf_card  is not None),
            )
            if dw.start():
                _direwolf = dw
                app.logger.info("_attach_direwolf: hub DirewolfTransport attached")
                # Re-apply stored audio levels — ensures correct RX/TX gain
                # is in effect after every Direwolf start, not just after
                # the operator manually sets levels via the Settings page.
                threading.Thread(
                    target=_apply_stored_audio_levels,
                    args=(load_settings(),),
                    daemon=True, name="alsa-levels",
                ).start()
                return True, "Direwolf AX.25 attached (hub)"
            return False, "DirewolfTransport: could not connect to Direwolf AGW port 8000"

    else:
        # Spoke: single-session DirewolfSpokeTransport
        if _direwolf_spoke is not None:
            try:
                _direwolf_spoke.close()
            except Exception:
                pass
        dws = DirewolfSpokeTransport(
            mycall      = callsign,
            vhf_enabled = (vhf_card is not None),
            hf_enabled  = (hf_card  is not None),
        )
        if dws.start():
            _direwolf_spoke = dws
            app.logger.info("_attach_direwolf: spoke DirewolfSpokeTransport attached")
            threading.Thread(
                target=_apply_stored_audio_levels,
                args=(load_settings(),),
                daemon=True, name="alsa-levels",
            ).start()
            return True, "Direwolf AX.25 attached (spoke)"
        return False, "DirewolfSpokeTransport: could not connect to Direwolf AGW port 8000"


def _start_hub_services():
    """
    Start all hub transport services at portal boot.

    When _MSA_AVAILABLE is True (all new modules present):
      - Creates shared SessionManager and HubCore singletons
      - Starts multi-client TCPServerTransport on port 14256
      - Optionally starts DirewolfTransport if Direwolf is running
      - Optionally starts MeshSyncManager if peers are configured
      - Wires HubCore.on_ui_event → all active ConsoleSession instances

    When _MSA_AVAILABLE is False (modules missing):
      - Falls back to the original single-client _start_hub_tcp_server()
        behaviour so the portal degrades gracefully without crashing.
    """
    global _session_manager, _hub_core, _hub_tcp_server
    global _direwolf, _mesh_sync

    settings = load_settings()
    if settings.get("role") != "hub":
        app.logger.info("Hub services: role is not hub — skipping")
        return

    callsign = settings.get("callsign", "N0CALL").upper()

    if not _MSA_AVAILABLE:
        app.logger.warning(
            "Hub services: MSA modules not available — "
            "falling back to single-client TCP server"
        )
        _start_hub_tcp_server_legacy()
        return

    app.logger.info("Hub services: starting as %s (MSA v0.1.0)", callsign)

    # ── SessionManager ────────────────────────────────────────────────
    _session_manager = SessionManager(
        max_sessions  = settings.get("max_sessions",        10),
        idle_timeout  = settings.get("session_idle_timeout", 300),
        auth_timeout  = settings.get("session_auth_timeout", 120),
    )
    _session_manager.start_watchdog()

    # ── HubCore ───────────────────────────────────────────────────────
    def _ui_event(event: dict):
        """Push protocol events to every open browser console."""
        for console_session in list(_active_sessions):
            try:
                console_session.send(event)
            except Exception:
                pass

    _hub_core = HubCore(
        mycall          = callsign,
        session_manager = _session_manager,
        on_ui_event     = _ui_event,
    )

    # ── Multi-client TCP server ───────────────────────────────────────
    try:
        tcp_srv = TCPServerTransport(
            mycall               = callsign,
            session_manager      = _session_manager,
            on_client_message    = _hub_core.on_message,
            on_client_connect    = _hub_core.on_connect,
            on_client_disconnect = _hub_core.on_disconnect,
            host = "0.0.0.0",
            port = 14256,
        )
        if tcp_srv.start():
            # Expose as _hub_tcp_server so ConsoleSession's legacy transport
            # selection code (_start_tcp_listener / _start_hybrid_tcp) can
            # detect it and not try to bind port 14256 a second time.
            _hub_tcp_server = tcp_srv
            app.logger.info(
                "Hub TCP: multi-client asyncio server on 0.0.0.0:14256"
            )
        else:
            app.logger.error("Hub TCP: multi-client server failed to start")
    except Exception as exc:
        app.logger.error("Hub TCP: startup error: %s", exc, exc_info=True)

    # ── Direwolf AX.25 transport (optional) ──────────────────────────
    vhf_card = settings.get("direwolf_vhf_card")
    hf_card  = settings.get("direwolf_hf_card")

    if vhf_card is not None or hf_card is not None:
        def _attach_direwolf_with_retry():
            """
            Poll until Direwolf is running then attach.
            Direwolf may take up to 45s to start if PulseAudio socket
            activation is slow. _start_hub_services() runs in a thread
            so we can block here without affecting the portal.
            """
            try:
                from hf256.direwolf_config    import direwolf_running
                from hf256.direwolf_transport import DirewolfTransport
            except ImportError as exc:
                app.logger.error("Hub Direwolf: import error: %s", exc)
                return

            for attempt in range(60):           # poll up to 60s
                if direwolf_running():
                    break
                time.sleep(1)
            else:
                app.logger.warning(
                    "Hub Direwolf: service did not start within 60s — "
                    "configure via Settings → Direwolf"
                )
                return

            with _direwolf_lock:
                if _direwolf is not None:
                    app.logger.info(
                        "Hub Direwolf: already attached by concurrent call — skipping"
                    )
                    return
                dw = DirewolfTransport(
                    mycall               = callsign,
                    session_manager      = _session_manager,
                    on_client_message    = _hub_core.on_message,
                    on_client_connect    = _hub_core.on_connect,
                    on_client_disconnect = _hub_core.on_disconnect,
                    vhf_enabled = (vhf_card is not None),
                    hf_enabled  = (hf_card  is not None),
                )
                if dw.start():
                    _direwolf = dw
                    app.logger.info(
                        "Hub Direwolf: AX.25 transport active (VHF=%s HF=%s)",
                        bool(vhf_card), bool(hf_card),
                    )
                    threading.Thread(
                        target=_apply_stored_audio_levels,
                        args=(load_settings(),),
                        daemon=True, name="alsa-levels",
                    ).start()
                else:
                    app.logger.warning(
                        "Hub Direwolf: failed to connect to AGW port 8000"
                    )

        threading.Thread(
            target=_attach_direwolf_with_retry,
            daemon=True, name="hub-direwolf-attach",
        ).start()

    # ── Mesh sync (optional) ─────────────────────────────────────────
    peers = settings.get("mesh_peers", [])
    if peers:
        try:
            from hf256.mesh_sync import MeshSyncManager
            _mesh_sync = MeshSyncManager(
                mycall        = callsign,
                peers         = list(peers),
                sync_interval = settings.get("mesh_sync_interval", 300),
            )
            _mesh_sync.start()
            app.logger.info(
                "Hub mesh: sync manager started with %d peer(s)", len(peers)
            )
        except Exception as exc:
            app.logger.error(
                "Hub mesh: startup error: %s", exc, exc_info=True
            )

    app.logger.info("Hub services: all transports started")


def _start_hub_tcp_server_legacy():
    """
    Original single-client TCP server — used as graceful fallback when
    the v0.1.0 MSA modules are not installed.  Identical to the old
    _start_hub_tcp_server() from v0.0.x.
    """
    settings = load_settings()
    if settings.get("role") != "hub":
        return
    try:
        from hf256.tcp_transport import TCPTransport
        callsign = settings.get("callsign", "N0CALL").upper()
        app.logger.info(
            "Hub TCP (legacy): starting on 0.0.0.0:14256 as %s", callsign
        )

        t = TCPTransport(mycall=callsign, mode="server",
                         host="0.0.0.0", port=14256)

        def _on_msg(data):
            for session in list(_active_sessions):
                try:
                    session._on_message_received(data)
                    return
                except Exception as e:
                    app.logger.error("Hub TCP dispatch error: %s", e)
            app.logger.info("Hub TCP: message with no active console session")

        _pre_tcp_transport = {}

        def _on_state(old_s, new_s, trigger=None):
            global _hub_tcp_server
            app.logger.info("Hub TCP: state %d->%d remote=%s",
                            old_s, new_s, getattr(t, "remote_call", "?"))
            if new_s == 2:
                for session in list(_active_sessions):
                    with session._lock:
                        existing = session.transport
                    if existing is not None and existing is not t:
                        if getattr(existing, "state", 0) == 2:
                            busy_with = (getattr(existing, "remote_call", None)
                                         or type(existing).__name__)
                            app.logger.warning(
                                "Hub TCP (legacy): rejected incoming spoke %s "
                                "— hub already connected to %s",
                                getattr(t, "remote_call", "?"), busy_with)
                            import json as _json, struct as _struct
                            try:
                                notice = _json.dumps({
                                    "type":    "hub_busy",
                                    "message": ("Hub is busy with " +
                                                str(busy_with) +
                                                " — try again later")
                                }).encode()
                                framed = _struct.pack(">I", len(notice)) + notice
                                if t.client_socket:
                                    t.client_socket.sendall(framed)
                            except Exception:
                                pass
                            try:
                                with t._lock:
                                    cs = t.client_socket
                                    t.client_socket = None
                                    t.state = t.STATE_DISCONNECTED
                                    t.remote_call = None
                                if cs:
                                    try:
                                        cs.shutdown(
                                            __import__('socket').SHUT_RDWR
                                        )
                                    except Exception:
                                        pass
                                    cs.close()
                            except Exception:
                                pass
                            return

                for session in list(_active_sessions):
                    try:
                        with session._lock:
                            _pre_tcp_transport[id(session)] = session.transport
                            session.transport      = t
                            session.transport_mode = "tcp"
                        t.on_message_received = session._on_message_received
                        session._on_state_change(old_s, new_s)
                        app.logger.info("Hub TCP (legacy): transport handed to session")
                        return
                    except Exception as e:
                        app.logger.error("Hub TCP handoff error: %s", e)
                app.logger.warning("Hub TCP: spoke connected, no console open")

            elif new_s == 0:
                for session in list(_active_sessions):
                    try:
                        with session._lock:
                            if session.transport is t:
                                prev = _pre_tcp_transport.pop(id(session), None)
                                session.transport = prev
                                app.logger.info(
                                    "Hub TCP: transport restored to %s",
                                    type(prev).__name__ if prev else "None")
                        session._on_state_change(old_s, new_s)
                    except Exception:
                        pass

        t.on_message_received = _on_msg
        t.on_state_change     = _on_state
        t.on_ptt_change       = lambda x: None

        ok = t.connect()
        if ok:
            _hub_tcp_server = t
            app.logger.info("Hub TCP (legacy): listening on 0.0.0.0:14256")
        else:
            app.logger.error(
                "Hub TCP (legacy): failed to bind port 14256"
            )
    except Exception as e:
        app.logger.error("_start_hub_tcp_server_legacy error: %s", e)


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    app.logger.setLevel(logging.INFO)
    logging.getLogger("hf256").setLevel(logging.INFO)

    # Start hub transport services at boot — no browser session required.
    # With MSA modules: multi-client TCP + Direwolf + Mesh.
    # Without MSA modules: legacy single-client TCP (graceful degradation).
    threading.Thread(
        target=_start_hub_services,
        daemon=True,
        name="hub-services-boot",
    ).start()

    app.run(host="0.0.0.0", port=80, debug=False)
