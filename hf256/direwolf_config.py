"""
HF-256 Direwolf Config Generator
==================================
Generates ``direwolf.conf`` for the Raspberry Pi appliance.

Supports two radio configurations:
  * Single soundcard  — VHF only (9600 baud) or HF only (300 baud)
  * Dual soundcard    — VHF (port 0) + HF (port 1) simultaneously

Direwolf is controlled as a systemd service: ``direwolf.service``.
This module writes the config file and provides helpers to restart the
service so the new config takes effect.

Audio card selection
---------------------
HF-256 uses ALSA card numbers (as shown by ``arecord -l``).
When two DigiRig adapters are present, card 0 is the first enumerated and
card 1 is the second.  The user sets these in the web UI Settings page and
they are written to ``/etc/hf256/settings.json``.

Baud rate / modem selection
----------------------------
Direwolf ``MODEM`` line syntax:
  ``MODEM <baud-rate> [mark-freq] [space-freq]``

Standard values:
  * 9600 baud FSK:  ``MODEM 9600``       (G3RUH compatible)
  * 1200 baud AFSK: ``MODEM 1200``       (Bell 202, most common on VHF)
  * 300 baud AFSK:  ``MODEM 300 1600 1800``  (HF standard)

PTT control
-----------
Direwolf PTT is configured per-channel with the ``PTT`` directive:
  * ``PTT RIG <hamlib-model> <serial-port>``  — uses Hamlib via rigctld
  * ``PTT SERIAL <port> RTS``                 — direct RTS on serial port
  * ``PTT GPIO <pin>``                        — GPIO pin (Pi GPIO number)
  * ``PTT VOX``                               — audio-level-triggered VOX

Service management
------------------
The generated config file is written to ``/etc/direwolf/direwolf.conf``
(the location expected by the stock ``direwolf`` Debian package on Raspberry Pi OS).
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("hf256.direwolf_config")

DIREWOLF_CONF = Path("/etc/direwolf/direwolf.conf")
DIREWOLF_CONF.parent.mkdir(parents=True, exist_ok=True) if False else None  # type: ignore

SETTINGS_FILE = Path("/etc/hf256/settings.json")


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _alsa_device(card: int, custom: Optional[str] = None) -> tuple:
    """
    Return (alsa_device, arate) for a given card number.

    HF-256 uses direct ALSA access (plughw) — no PulseAudio.
    Direct ALSA gives deterministic low-latency TX audio with no
    daemon scheduling overhead or buffer management complexity.

    If a custom device name is explicitly set by the operator, use it.
    Otherwise use plughw:N,0 which works with the DigiRig and all
    standard USB audio devices that support RW_INTERLEAVED access.

    Note: The X6100 (CM108 chip, MMAP-only) is not supported —
    it requires PulseAudio which conflicts with reliable AX.25 TX.
    """
    if custom and custom.strip():
        dev = custom.strip().lower()
        # Warn if pulse is requested without PulseAudio installed
        if dev == "pulse":
            log.warning(
                "_alsa_device: 'pulse' requested but PulseAudio is not "
                "installed in this build — falling back to plughw:%d,0. "
                "Clear the Custom ALSA Device field in Settings → Direwolf.",
                card,
            )
            return f"plughw:{card},0", 44100
        rate = 44100
        return dev, rate

    return f"plughw:{card},0", 44100


def generate_direwolf_conf(
    mycall: str,
    # VHF channel
    vhf_audio_card: Optional[int] = None,
    vhf_serial_port: Optional[str] = None,
    vhf_ptt_method: str = "RTS",
    vhf_baud: int = 9600,
    vhf_gpio_pin: Optional[int] = None,
    # HF channel
    hf_audio_card: Optional[int] = None,
    hf_serial_port: Optional[str] = None,
    hf_ptt_method: str = "RTS",
    hf_baud: int = 300,
    hf_gpio_pin: Optional[int] = None,
    hf_hamlib_model: Optional[int] = None,   # Hamlib model for CAT PTT (e.g. 3021 = X1600)
    vhf_hamlib_model: Optional[int] = None,  # Hamlib model for VHF CAT PTT
    # Optional: override the auto-generated HF ALSA device name.
    # Use when the USB audio chip requires specific period/buffer parameters
    # that raw plughw cannot negotiate (e.g. C-Media CM108 needs period=2400).
    # Create /etc/alsa/conf.d/99-hf256-hf.conf defining the virtual device,
    # then set hf_alsa_device="hf256_hf" in settings.
    hf_alsa_device: Optional[str] = None,
    # Direwolf server settings
    agw_port: int = 8000,
    kiss_port: int = 8001,
    # Role — hub uses FRACK=7 to desynchronize polling from spoke (FRACK=10)
    # preventing half-duplex collision deadlocks when both sides poll in sync
    is_hub: bool = False,
    # TX delay in ms — hub 50ms, spoke 80ms to cover X6100 TX→RX switch time
    hf_txdelay_ms: int = 50,
) -> str:
    """
    Generate direwolf.conf validated against Direwolf 1.6.

    Four hardware configurations:

    A) VHF only  (hf_audio_card is None)
       Single ADEVICE, CHANNEL 0 = VHF.

    B) HF only   (vhf_audio_card is None)
       Single ADEVICE, CHANNEL 0 = HF.
       (channel 1 would be invalid with a single audio device)

    C) VHF + HF, SAME card  (vhf_audio_card == hf_audio_card)
       Single stereo ADEVICE: left=CHANNEL 0 (VHF), right=CHANNEL 1 (HF).
       Requires a stereo-capable USB audio device.

    D) VHF + HF, DIFFERENT cards  (two separate physical USB devices)
       Two ADEVICE lines: first=CHANNEL 0 (VHF), second=CHANNEL 1 (HF).

    ARATE: VHF uses 48000 Hz (or 96000 for 9600 baud); HF uses 44100 Hz.
    Direwolf uses raw ALSA (not PortAudio like ARDOP/FreeDV). Raw ALSA
    period-size negotiation fails for 48000 Hz on many cheap USB audio
    chips even though PortAudio succeeds — 44100 Hz is the safe default
    for HF. It gives 147 samples/symbol at 300 baud, more than adequate.
    """
    if vhf_audio_card is None and hf_audio_card is None:
        raise ValueError(
            "At least one audio card must be specified "
            "(vhf_audio_card or hf_audio_card)"
        )

    mycall    = mycall.upper().strip()
    lines     = []
    same_card = (vhf_audio_card is not None and
                 hf_audio_card  is not None and
                 vhf_audio_card == hf_audio_card)

    # ── Global header ──────────────────────────────────────────────────────
    lines += [
        "# HF-256 Direwolf Configuration",
        f"# Generated by hf256.direwolf_config  call={mycall}",
        f"# {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"MYCALL {mycall}",
        f"AGWPORT {agw_port}",
        f"KISSPORT {kiss_port}",
        "",
    ]

    # ── ADEVICE block(s) — MUST precede CHANNEL in Direwolf 1.6 ──────────
    if same_card:
        # Single stereo card: left=ch0 (VHF), right=ch1 (HF)
        lines += [
            f"ADEVICE plughw:{vhf_audio_card},0  plughw:{vhf_audio_card},0",
            "ARATE 48000",
            "",
        ]
    else:
        if vhf_audio_card is not None:
            if vhf_baud == 9600:
                vhf_dev, vhf_arate = f"plughw:{vhf_audio_card},0", 96000
            else:
                vhf_dev, vhf_arate = _alsa_device(vhf_audio_card)
            lines += [
                f"ADEVICE {vhf_dev}  {vhf_dev}",
                f"ARATE {vhf_arate}",
                "",
            ]
        if hf_audio_card is not None:
            # Use a custom ALSA device name if provided — this allows the
            # operator to define a virtual device in /etc/alsa/conf.d/ that
            # forces specific period/buffer sizes required by the USB chip.
            hf_dev, hf_arate = _alsa_device(hf_audio_card,
                                               custom=hf_alsa_device)
            lines += [
                f"ADEVICE {hf_dev}  {hf_dev}",
                f"ARATE {hf_arate}",
                "",
            ]

    # ── Channel 0 ─────────────────────────────────────────────────────────
    # VHF takes ch0 when configured; otherwise HF-only takes ch0.
    if vhf_audio_card is not None:
        vhf_ptt = _ptt_directive(0, vhf_ptt_method, vhf_serial_port, vhf_gpio_pin, vhf_hamlib_model)
        lines += ["CHANNEL 0"]
        lines += (["MODEM 9600"] if vhf_baud == 9600 else
                  ["MODEM 1200"] if vhf_baud == 1200 else [f"MODEM {vhf_baud}"])
        lines += [vhf_ptt, "TXDELAY 40", "TXTAIL 10",
                  "FRACK 3", "RETRY 10", "MAXFRAME 4", ""]
    else:
        # HF-only: use channel 0, not channel 1
        hf_ptt = _ptt_directive(0, hf_ptt_method, hf_serial_port, hf_gpio_pin, hf_hamlib_model)
        lines += [
            "CHANNEL 0",
            "MODEM 300",
            hf_ptt,
            f"TXDELAY {hf_txdelay_ms}", "TXTAIL 50",
            # Hub FRACK=7, spoke FRACK=10: desynchronizes polling timers so
            # repeated half-duplex collisions after an initial collision are
            # avoided. The 3-second offset breaks the deadlock cycle.
            f"FRACK {7 if is_hub else 10}", "RETRY 7", "MAXFRAME 1", "",
        ]
        lines += ["# End of HF-256 direwolf.conf"]
        return "\n".join(lines) + "\n"

    # ── Channel 1 — HF (only when VHF is also present) ────────────────────
    if hf_audio_card is not None:
        hf_ptt = _ptt_directive(1, hf_ptt_method, hf_serial_port, hf_gpio_pin, hf_hamlib_model)
        lines += [
            "CHANNEL 1",
            "MODEM 300",
            hf_ptt,
            f"TXDELAY {hf_txdelay_ms}", "TXTAIL 50",
            f"FRACK {7 if is_hub else 10}", "RETRY 7", "MAXFRAME 1", "",
        ]

    lines += ["# End of HF-256 direwolf.conf"]
    return "\n".join(lines) + "\n"


def _ptt_directive(
    channel: int,
    method: str,
    serial_port: Optional[str],
    gpio_pin: Optional[int],
    hamlib_model: Optional[int] = None,
) -> str:
    """
    Return the PTT directive string for a Direwolf channel.

    Direwolf 1.6 supported PTT methods:
      PTT /dev/ttyUSB0 RTS       ← hardware RTS (DigiRig, direct-wired interfaces)
      PTT /dev/ttyUSB0 DTR       ← hardware DTR
      PTT GPIO <pin>             ← Raspberry Pi GPIO
      PTT VOX                    ← audio-level triggered
      PTT RIG <hamlib_id> <port> ← Hamlib rigctld (CI-V/CAT radios: Xiegu, Icom)

    CAT radios (Xiegu X1600, X6100, G90, Icom IC-7300 etc.) use CI-V protocol
    for PTT — RTS will NOT work on their USB serial adapters. Use PTT RIG with
    the radio's Hamlib model number and rigctld must be running.
    """
    method = (method or "VOX").upper()

    if method == "VOX":
        log.info("direwolf_config: channel %d PTT = VOX", channel)
        return "PTT VOX"

    if method == "GPIO" and gpio_pin is not None:
        log.info("direwolf_config: channel %d PTT = GPIO %d", channel, gpio_pin)
        return f"PTT GPIO {gpio_pin}"

    if method == "CAT":
        # CAT PTT via Hamlib rigctld — for CI-V radios (Xiegu, Icom etc.)
        # rigctld must be running: rigctld -m <hamlib_model> -r <port> -s 19200
        if hamlib_model and serial_port and serial_port.strip():
            log.info("direwolf_config: channel %d PTT = RIG %d %s",
                     channel, hamlib_model, serial_port)
            return f"PTT RIG {hamlib_model} {serial_port}"
        log.warning(
            "direwolf_config: channel %d PTT CAT requested but hamlib_model=%r "
            "or serial_port=%r missing — falling back to VOX",
            channel, hamlib_model, serial_port,
        )
        return "PTT VOX"

    if method in ("RTS", "DTR") and serial_port and serial_port.strip():
        # Direwolf 1.6: PTT /dev/ttyUSB0 RTS   (no 'SERIAL' keyword)
        log.info("direwolf_config: channel %d PTT = %s %s",
                 channel, serial_port, method)
        return f"PTT {serial_port} {method}"

    # ── Fallback to VOX ───────────────────────────────────────────────
    log.warning(
        "direwolf_config: channel %d PTT FALLBACK TO VOX — "
        "method=%r serial_port=%r gpio_pin=%r. "
        "Radio will NOT transmit. "
        "FIX: set PTT method and serial port in Settings → Direwolf.",
        channel, method, serial_port, gpio_pin,
    )
    return "PTT VOX"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  File writing and service management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def write_direwolf_conf(content: str, path: Path = DIREWOLF_CONF) -> bool:
    """Write config file, creating parent dirs if needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.chmod(path, 0o644)
        log.info("direwolf_config: wrote %s (%d bytes)", path, len(content))
        return True
    except Exception as exc:
        log.error("direwolf_config: write error: %s", exc)
        return False


def _is_masked() -> bool:
    """Return True if the direwolf service is currently masked."""
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "direwolf"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "masked"
    except Exception:
        return False


def _ensure_unmasked_and_enabled() -> tuple:
    """
    Unmask and enable the direwolf service if needed.

    Systemd will refuse to start a masked unit — we must unmask it
    before the first ``systemctl start/restart``.  This is a one-time
    operation: subsequent calls are instant no-ops because unmask on an
    already-unmasked unit exits 0.

    Returns ``(success: bool, message: str)``.
    """
    steps = []

    # 1. Unmask (safe to run even if not masked; exits 0 either way)
    r = subprocess.run(
        ["systemctl", "unmask", "direwolf"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        log.error("direwolf_config: unmask failed: %s", msg)
        return False, f"Unmask failed: {msg}"
    steps.append("unmasked")
    log.info("direwolf_config: service unmasked")

    # 2. Enable so it survives reboots
    r = subprocess.run(
        ["systemctl", "enable", "direwolf"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        log.warning("direwolf_config: enable failed (non-fatal): %s", msg)
        # Non-fatal — the service can still be started without being enabled
    else:
        steps.append("enabled")
        log.info("direwolf_config: service enabled")

    # 3. Reload unit files so systemd picks up any changes
    subprocess.run(
        ["systemctl", "daemon-reload"],
        capture_output=True, timeout=10,
    )

    return True, " + ".join(steps)


def restart_direwolf() -> tuple:
    """
    Start or restart the ``direwolf`` systemd service.

    Handles the first-run case where the service is **masked** by the
    Pi image (to prevent auto-start before the operator configures it).
    Unmasks and enables automatically before restarting.

    Returns ``(success: bool, message: str)``.
    """
    # Unmask/enable if needed — harmless no-op on subsequent calls
    if _is_masked():
        log.info("direwolf_config: service is masked — unmasking before start")
        ok, msg = _ensure_unmasked_and_enabled()
        if not ok:
            return False, msg

    try:
        # Use 'restart' so it works whether the service is stopped or running.
        # After unmask we could also use 'start', but 'restart' is idempotent
        # and handles the already-running case cleanly.
        result = subprocess.run(
            ["systemctl", "restart", "direwolf"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log.info("direwolf_config: direwolf service started/restarted OK")
            return True, "Direwolf started"
        else:
            # Non-zero exit can still mean the service started — systemd
            # reports failure if ExecStartPre takes longer than Type=simple
            # expects, even though the service is running. Check actual state.
            if direwolf_running():
                log.info("direwolf_config: restart reported non-zero but service is active")
                return True, "Direwolf running"
            msg = (result.stderr or result.stdout or "unknown error").strip()
            log.error("direwolf_config: restart failed: %s", msg)
            return False, f"Restart failed: {msg}"
    except subprocess.TimeoutExpired:
        # Timeout from our subprocess — check if service actually started
        if direwolf_running():
            log.info("direwolf_config: restart timed out but service is active")
            return True, "Direwolf running (slow start)"
        return False, "Restart timed out after 60s — check journalctl -u direwolf"
    except Exception as exc:
        log.error("direwolf_config: restart exception: %s", exc)
        return False, f"Restart error: {exc}"


def stop_direwolf() -> tuple:
    """Stop the direwolf service. Returns (success, message)."""
    try:
        result = subprocess.run(
            ["systemctl", "stop", "direwolf"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0, (result.stderr or "OK").strip()
    except Exception as exc:
        return False, str(exc)


def direwolf_running() -> bool:
    """Return True if the direwolf service is active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "direwolf"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  High-level setup helper  (called from app.py setup wizard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _setup_rigctld_for_cat(serial_port: str, hamlib_model: int,
                           baud: int = 19200) -> tuple:
    """
    Write RIGCTLD_CMD to /etc/hf256/config.env and restart rigctld.
    Called automatically by setup_direwolf_from_settings when CAT PTT is chosen.
    Direwolf's 'PTT RIG <model> <port>' requires rigctld to be running on
    port 4532 before Direwolf starts.
    Returns (success: bool, message: str).
    """
    CONFIG_ENV = Path("/etc/hf256/config.env")
    rigctld_cmd = (f"rigctld -m {hamlib_model} -r {serial_port} "
                   f"-s {baud} -t 4532")
    try:
        # Read existing config.env or start fresh
        if CONFIG_ENV.exists():
            lines = CONFIG_ENV.read_text().splitlines()
        else:
            CONFIG_ENV.parent.mkdir(parents=True, exist_ok=True)
            lines = []

        # Replace or append RIGCTLD_CMD
        new_lines = [l for l in lines if not l.startswith("RIGCTLD_CMD=")]
        new_lines.append(f'RIGCTLD_CMD="{rigctld_cmd}"')
        CONFIG_ENV.write_text("\n".join(new_lines) + "\n")
        CONFIG_ENV.chmod(0o644)
        log.info("direwolf_config: wrote RIGCTLD_CMD to %s: %s",
                 CONFIG_ENV, rigctld_cmd)

        # Restart rigctld so it picks up the new command
        r = subprocess.run(
            ["systemctl", "restart", "rigctld"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            # rigctld may not be enabled yet — try to enable and start it
            subprocess.run(["systemctl", "enable", "rigctld"],
                           capture_output=True, timeout=10)
            r2 = subprocess.run(
                ["systemctl", "start", "rigctld"],
                capture_output=True, text=True, timeout=15,
            )
            if r2.returncode != 0:
                err = (r2.stderr or r2.stdout or "").strip()
                log.warning("direwolf_config: rigctld start failed: %s", err)
                return False, f"rigctld start failed: {err}"

        # Brief wait then confirm running
        time.sleep(1)
        rr = subprocess.run(
            ["systemctl", "is-active", "rigctld"],
            capture_output=True, text=True, timeout=5,
        )
        if rr.stdout.strip() != "active":
            return False, ("rigctld started but not active — "
                           "check: journalctl -u rigctld -n 10")

        log.info("direwolf_config: rigctld running for CAT PTT on %s "
                 "(model %d)", serial_port, hamlib_model)
        return True, f"rigctld running (model {hamlib_model} on {serial_port})"

    except Exception as exc:
        log.error("direwolf_config: _setup_rigctld_for_cat error: %s", exc)
        return False, f"rigctld setup error: {exc}"


def setup_direwolf_from_settings(settings: dict) -> tuple:
    """
    Generate and apply a Direwolf config from the hub settings dict.

    Called when the operator saves Direwolf settings in the web UI.
    Returns ``(success: bool, message: str)``.

    Expected settings keys (all optional; missing → channel disabled):
      ``direwolf_vhf_card``       int  — ALSA card number for VHF radio
      ``direwolf_vhf_serial``     str  — PTT serial port for VHF (e.g. /dev/ttyUSB0)
      ``direwolf_vhf_ptt``        str  — PTT method: RTS / DTR / VOX / GPIO
      ``direwolf_vhf_baud``       int  — 1200 or 9600
      ``direwolf_hf_card``        int  — ALSA card number for HF radio
      ``direwolf_hf_serial``      str  — PTT serial port for HF
      ``direwolf_hf_ptt``         str  — PTT method for HF
    """
    mycall = settings.get("callsign", "N0CALL").upper()

    # Pull values — None means channel disabled
    vhf_card   = settings.get("direwolf_vhf_card")
    hf_card    = settings.get("direwolf_hf_card")

    if vhf_card is None and hf_card is None:
        return False, "No audio cards configured — enable VHF or HF channel"

    vhf_card = int(vhf_card) if vhf_card is not None else None
    hf_card  = int(hf_card)  if hf_card  is not None else None

    # ── rigctld setup for CAT PTT ─────────────────────────────────────────
    # If HF PTT method is CAT, ensure rigctld is running before Direwolf
    # starts — Direwolf's 'PTT RIG' connects to rigctld at 127.0.0.1:4532.
    hf_ptt = (settings.get("direwolf_hf_ptt") or "").upper()
    hf_hamlib = settings.get("direwolf_hf_hamlib_model")
    hf_serial = settings.get("direwolf_hf_serial") or ""
    if hf_ptt == "CAT" and hf_card is not None:
        if not hf_hamlib or not hf_serial.strip():
            return False, ("CAT PTT selected but Hamlib Model or Serial Port "
                           "is missing — fill both in and Apply again")
        ok_rig, msg_rig = _setup_rigctld_for_cat(
            serial_port  = hf_serial.strip(),
            hamlib_model = int(hf_hamlib),
        )
        if not ok_rig:
            return False, f"rigctld setup failed: {msg_rig}"
        log.info("direwolf_config: rigctld ready — %s", msg_rig)

    is_hub = settings.get("role", "").lower() == "hub"
    try:
        conf = generate_direwolf_conf(
            mycall          = mycall,
            vhf_audio_card  = vhf_card,
            vhf_serial_port = settings.get("direwolf_vhf_serial") or None,
            vhf_ptt_method  = settings.get("direwolf_vhf_ptt", "RTS"),
            vhf_baud        = int(settings.get("direwolf_vhf_baud", 9600)),
            hf_audio_card   = hf_card,
            hf_serial_port  = settings.get("direwolf_hf_serial") or None,
            hf_ptt_method   = settings.get("direwolf_hf_ptt", "RTS"),
            hf_alsa_device  = (settings.get("direwolf_hf_alsa_device") or "").strip().lower() or None,
            hf_hamlib_model = settings.get("direwolf_hf_hamlib_model") or None,
            is_hub          = is_hub,
            hf_txdelay_ms   = 50 if is_hub else 80,
        )
    except ValueError as exc:
        return False, str(exc)

    if not write_direwolf_conf(conf):
        return False, "Failed to write /etc/direwolf/direwolf.conf"

    ok, msg = restart_direwolf()
    if not ok:
        return False, msg

    # Wait briefly then confirm it is still running.
    # A bad config (wrong card index, missing serial port) causes Direwolf
    # to exit within ~1 second.
    time.sleep(2)
    if not direwolf_running():
        # Retrieve the last few journal lines to surface the real error
        try:
            jr = subprocess.run(
                ["journalctl", "-u", "direwolf", "-n", "8",
                 "--no-pager", "--output=cat"],
                capture_output=True, text=True, timeout=5,
            )
            detail = jr.stdout.strip().splitlines()
            hint   = detail[-1] if detail else "check journalctl -u direwolf"
        except Exception:
            hint = "run: journalctl -u direwolf -n 20"

        return False, (
            "Direwolf started but crashed immediately. "
            f"Last log line: {hint}"
        )

    return True, "Direwolf configured and running"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Systemd service file (written once during Pi image build / first boot)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DIREWOLF_SERVICE = """\
[Unit]
Description=Direwolf AX.25 Soundcard TNC (HF-256)
After=network.target sound.target
Wants=sound.target

[Service]
Type=simple
ExecStart=/usr/bin/direwolf -t 0 -c /etc/direwolf/direwolf.conf
Restart=on-failure
RestartSec=10
# Run as pi so ALSA access works; direwolf doesn't need root for RTS PTT
User=pi
Group=pi
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def write_direwolf_service() -> bool:
    """Write /etc/systemd/system/direwolf.service and enable it."""
    service_path = Path("/etc/systemd/system/direwolf.service")
    try:
        service_path.write_text(DIREWOLF_SERVICE)
        subprocess.run(["systemctl", "daemon-reload"],
                       capture_output=True, timeout=10)
        subprocess.run(["systemctl", "enable", "direwolf"],
                       capture_output=True, timeout=10)
        log.info("direwolf_config: service file written and enabled")
        return True
    except Exception as exc:
        log.error("direwolf_config: write_direwolf_service error: %s", exc)
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Diagnostics helpers  (used by /api/direwolf/conf and /api/direwolf/logs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_direwolf_conf() -> str:
    """
    Return the current content of /etc/direwolf/direwolf.conf.
    Returns an explanatory string if the file doesn't exist.
    """
    try:
        if DIREWOLF_CONF.exists():
            return DIREWOLF_CONF.read_text()
        return "# /etc/direwolf/direwolf.conf does not exist yet.\n" \
               "# Configure Direwolf in Settings → Direwolf and click Apply."
    except Exception as exc:
        return f"# Error reading {DIREWOLF_CONF}: {exc}"


def get_direwolf_log(lines: int = 40) -> str:
    """
    Return the last ``lines`` lines from the Direwolf systemd journal.
    This is what you would see with: journalctl -u direwolf -n <lines>
    """
    try:
        r = subprocess.run(
            ["journalctl", "-u", "direwolf", f"-n{lines}",
             "--no-pager", "--output=short-monotonic"],
            capture_output=True, text=True, timeout=10,
        )
        output = (r.stdout or "").strip()
        if not output:
            return ("No Direwolf journal entries found.\n"
                    "Direwolf may not have started yet, or it has never run.")
        return output
    except FileNotFoundError:
        return "journalctl not found — is this a systemd system?"
    except Exception as exc:
        return f"Error reading Direwolf journal: {exc}"
