"""
HF-256 Hub Core
================
Multi-session protocol handler for the hub station.

Responsibilities
----------------
* Authenticate each connecting spoke against ``passwords.json``.
* Route incoming messages to the correct handler (chat, file request,
  store, retrieve, download, password change).
* **Broadcast** chat to ALL authenticated sessions simultaneously.
* Manage per-session file transfer locks so concurrent downloads don't
  interleave on the same session.
* Store and forward messages between offline stations.
* Notify the web UI (operator console) of protocol events via callbacks.

Wire format
-----------
Uses the same ``_hub_pack`` / ``_hub_unpack`` wire format as ``app.py``::

    [version:1][flags:1][msg_type:1][timestamp:4][iv:12][payload]

Message types
-------------
All constants from ``app.py`` are re-exported here.  New ones added:

  ``HUB_TYPE_BROADCAST  = 0x20``  hub → all authenticated spokes
  ``HUB_TYPE_PING       = 0x22``  keepalive
  ``HUB_TYPE_PONG       = 0x23``  keepalive response

Thread safety
-------------
``on_message()`` is called from transport reader threads and asyncio tasks
(via ``run_coroutine_threadsafe``).  All session state lives in
``ClientSession`` objects (see ``session_manager.py``); HubCore itself has
no per-session mutable state — only shared resources protected by locks.
"""

import hashlib
import json
import logging
import os
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger("hf256.hub")

# ── File paths ───────────────────────────────────────────────────────────────
KEY_FILE   = Path("/etc/hf256/network.key")
PW_FILE    = Path("/home/pi/.hf256/passwords.json")
MSG_BASE   = Path("/home/pi/.hf256/hub_messages")
FILES_DIR  = Path("/home/pi/.hf256/hub_files")
SETTINGS_F = Path("/etc/hf256/settings.json")

# ── Wire format constants (must match app.py) ────────────────────────────────
_HUB_VERSION   = 0x01
_HUB_FLAG_ENC  = 0x01

HUB_TYPE_CHAT        = 0x01
HUB_TYPE_FL_REQ      = 0x02
HUB_TYPE_FL_RSP      = 0x03
HUB_TYPE_FILE_DATA   = 0x04
HUB_TYPE_DL_REQ      = 0x06
HUB_TYPE_COMPLETE    = 0x07
HUB_TYPE_ERROR       = 0x08
HUB_TYPE_AUTH_REQ    = 0x10
HUB_TYPE_AUTH_RSP    = 0x11
HUB_TYPE_STORE       = 0x12
HUB_TYPE_RETRIEVE    = 0x13
HUB_TYPE_STORE_ACK   = 0x14
HUB_TYPE_RETRIEVE_RSP= 0x15
HUB_TYPE_PASSWD_REQ  = 0x16
HUB_TYPE_PASSWD_RSP  = 0x17
HUB_TYPE_BROADCAST   = 0x20   # new: hub → all authenticated spokes
HUB_TYPE_PING        = 0x22   # new: keepalive
HUB_TYPE_PONG        = 0x23   # new: keepalive response
HUB_TYPE_CONN_ACK    = 0x30   # hub → spoke: connection confirmed, please /auth

CHUNK_SIZE = 512               # file download chunk bytes


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Wire pack / unpack helpers (self-contained — no app.py dependency)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _load_key() -> Optional[bytes]:
    """Load 32-byte network key or return None."""
    try:
        key = KEY_FILE.read_bytes()
        return key if len(key) == 32 else None
    except Exception:
        return None


def _get_aesgcm():
    """Return AESGCM instance or None if key not present."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = _load_key()
        return AESGCM(key) if key else None
    except Exception:
        return None


def hub_pack(
    msg_type: int,
    payload: bytes,
    encrypt: bool = True,
) -> bytes:
    """Pack a hub wire message (with optional AES-GCM encryption)."""
    aesgcm    = _get_aesgcm() if encrypt else None
    flags     = _HUB_FLAG_ENC if aesgcm else 0x00
    timestamp = int(time.time())
    aad       = struct.pack(">BBBI", _HUB_VERSION, flags, msg_type, timestamp)

    if aesgcm:
        iv         = os.urandom(12)
        ciphertext = aesgcm.encrypt(iv, payload, aad)
        return aad + iv + ciphertext
    else:
        dummy_iv = b"\x00" * 12
        return aad + dummy_iv + payload


def hub_unpack(data: bytes) -> tuple:
    """
    Unpack a hub wire message.
    Returns ``(msg_type, payload, encrypted)`` or raises ``ValueError``.
    """
    if len(data) < 19:
        raise ValueError(f"Message too short: {len(data)} bytes")
    flags     = data[1]
    msg_type  = data[2]
    iv        = data[7:19]
    body      = data[19:]
    encrypted = bool(flags & _HUB_FLAG_ENC)

    if encrypted:
        aesgcm = _get_aesgcm()
        if aesgcm is None:
            raise ValueError("Encrypted message but no network key configured")
        aad = data[0:7]
        try:
            payload = aesgcm.decrypt(iv, body, aad)
        except Exception as exc:
            raise ValueError(f"Decryption failed: {exc}") from exc
    else:
        payload = body

    return msg_type, payload, encrypted


def _chat_payload(sender: str, text: str) -> bytes:
    """Binary chat payload: [sender_len:2 BE][sender][text]"""
    sender_b = sender.encode("utf-8")
    return struct.pack(">H", len(sender_b)) + sender_b + text.encode("utf-8")


def _chat_payload_unpack(payload: bytes) -> tuple:
    """Returns (sender, text) from chat binary payload."""
    if len(payload) < 2:
        raise ValueError("Chat payload too short")
    sender_len = struct.unpack(">H", payload[:2])[0]
    sender = payload[2 : 2 + sender_len].decode("utf-8", errors="replace")
    text   = payload[2 + sender_len :].decode("utf-8", errors="replace")
    return sender, text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HubCore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HubCore:
    """
    Multi-session hub protocol handler.

    Wired up by app.py or main.py::

        hub = HubCore(
            mycall  = "N0HUB",
            session_manager = sm,
            on_ui_event = lambda ev: push_to_browser(ev),  # optional
        )
        # Pass hub.on_message / on_connect / on_disconnect
        # to transport constructors.
    """

    def __init__(
        self,
        mycall: str,
        session_manager,
        on_ui_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.mycall          = mycall.upper()
        self.session_manager = session_manager
        self.on_ui_event     = on_ui_event   # sends events to operator console

        # Per-session duplicate AUTH_REQ suppression:  callsign → last_time
        self._last_auth: Dict[str, float] = {}
        self._auth_lock = threading.Lock()

        # Hook session manager callbacks
        self.session_manager.on_session_open  = self._on_session_open
        self.session_manager.on_session_close = self._on_session_close

        # Encryption preference (read from settings)
        settings = self._load_settings()
        self.enc_enabled = settings.get("encryption_enabled", True)

        log.info(
            "HubCore: started as %s  encryption=%s",
            self.mycall,
            "ON" if self.enc_enabled else "OFF",
        )

    # ------------------------------------------------------------------
    # Session lifecycle callbacks
    # ------------------------------------------------------------------

    def _on_session_open(self, session) -> None:
        log.info("HubCore: session opened — %s", session)
        self._ui({
            "type":      "session_open",
            "session_id": session.session_id,
            "transport": session.transport_type,
            "callsign":  session.callsign,
        })

    def _on_session_close(self, session) -> None:
        log.info("HubCore: session closed — %s", session)
        if session.authenticated:
            # Notify other authenticated spokes that this station left
            self._broadcast_except(
                session,
                f"★ {session.callsign} disconnected [{session.transport_type}]",
                from_call="HUB",
            )
        self._ui({
            "type":      "session_close",
            "session_id": session.session_id,
            "callsign":  session.callsign,
        })

    # ------------------------------------------------------------------
    # Main entry point (called by ALL transports)
    # ------------------------------------------------------------------

    def on_message(self, session, data: bytes) -> None:
        """
        Dispatch an incoming message from any transport.

        ``session`` is the ``ClientSession`` for the sending station.
        ``data``    is the raw wire payload (no 4-byte TCP length prefix).
        """
        try:
            msg_type, payload, encrypted = hub_unpack(data)
        except Exception as exc:
            err_str = str(exc)
            log.warning(
                "HubCore: unpack error from %s: %s | hex=%s",
                session.session_id, err_str, data[:32].hex(),
            )
            # Send a plaintext error back so the operator can diagnose without
            # needing SSH.  Key-mismatch is the most common cause here.
            if "Decryption" in err_str or "key" in err_str.lower():
                hint = ("Key mismatch — export key from hub Settings and "
                        "import it on spoke, or use /encrypt off")
            elif "too short" in err_str:
                hint = "Frame too short — possible radio framing error"
            else:
                hint = f"Unpack error: {err_str}"
            try:
                err_wire = hub_pack(
                    HUB_TYPE_ERROR,
                    json.dumps({"error": hint}).encode(),
                    encrypt=False,   # plaintext so spoke can read it even with wrong key
                )
                session.send(err_wire)
            except Exception:
                pass
            return

        log.info(
            "HubCore: RX type=0x%02x enc=%s len=%d from %s",
            msg_type, encrypted, len(payload), session.callsign or session.session_id,
        )

        dispatch = {
            HUB_TYPE_AUTH_REQ:  self._handle_auth,
            HUB_TYPE_CHAT:      self._handle_chat,
            HUB_TYPE_FL_REQ:    self._handle_file_list,
            HUB_TYPE_DL_REQ:    self._handle_download,
            HUB_TYPE_STORE:     self._handle_store,
            HUB_TYPE_RETRIEVE:  self._handle_retrieve,
            HUB_TYPE_PASSWD_REQ:self._handle_passwd,
            HUB_TYPE_PING:      self._handle_ping,
        }
        handler = dispatch.get(msg_type)
        if handler:
            try:
                handler(session, payload)
            except Exception as exc:
                log.error(
                    "HubCore: handler 0x%02x error for %s: %s",
                    msg_type, session.session_id, exc, exc_info=True,
                )
        else:
            log.info("HubCore: unhandled type 0x%02x from %s",
                     msg_type, session.session_id)

    def on_connect(self, session) -> None:
        """
        Called by transport when a new AX.25/TCP connection is accepted.

        Sends CONN_ACK to the spoke immediately.  This serves two purposes:
          1. Proves the radio link is bidirectional — if the spoke receives
             this, TX→RX works in both directions.
          2. Signals the spoke console to prompt for /auth so the user knows
             what to do next without guessing.
        """
        log.info("HubCore.on_connect: %s via %s",
                 session.callsign, session.transport_type)
        # Small delay to let Direwolf/ARDOP finish the connection handshake
        # before we transmit.  AX.25: the UA frame is still in flight; without
        # the delay the hub's reply I-frame can collide with it.
        def _send_ack(s=session):
            time.sleep(0.5)
            payload = json.dumps({
                "hub":  self.mycall,
                "call": s.callsign,
                "msg":  "Connected — send /auth <password> to authenticate",
            }).encode()
            wire = hub_pack(HUB_TYPE_CONN_ACK, payload,
                            encrypt=self.enc_enabled)
            ok = s.send(wire)
            if ok:
                log.info("HubCore: CONN_ACK sent to %s", s.callsign)
            else:
                log.warning("HubCore: CONN_ACK to %s failed — "
                            "radio TX may not be working", s.callsign)
        threading.Thread(target=_send_ack, daemon=True,
                         name="hub-conn-ack").start()

    def on_disconnect(self, session) -> None:
        """Called by transport when a connection is dropped."""
        log.info("HubCore.on_disconnect: %s", session)

    # ------------------------------------------------------------------
    # Hub operator API (called from web console)
    # ------------------------------------------------------------------

    def send_to(self, callsign: str, text: str) -> bool:
        """Hub operator sends a chat message to one specific spoke."""
        session = self.session_manager.by_callsign(callsign)
        if session is None:
            log.warning("HubCore.send_to: %s not connected", callsign)
            return False
        return self._send_chat(session, self.mycall, text)

    def broadcast(self, text: str, from_call: str = None) -> None:
        """Hub operator broadcasts to all authenticated spokes."""
        from_call = from_call or self.mycall
        for s in self.session_manager.authenticated():
            self._send_chat(s, from_call, text)

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def _handle_auth(self, session, payload: bytes) -> None:
        """Authenticate a spoke station."""
        try:
            creds    = json.loads(payload.decode())
            callsign = creds.get("callsign", "").upper().strip()
            password = creds.get("password", "")
        except Exception as exc:
            log.error("HubCore: AUTH_REQ parse error: %s", exc)
            return

        if not callsign:
            self._send_auth_response(session, False, "Missing callsign")
            return

        # Duplicate suppression — only 5 second window
        with self._auth_lock:
            last = self._last_auth.get(callsign, 0)
            if time.time() - last < 5:
                log.info("HubCore: duplicate AUTH_REQ from %s — suppressed",
                         callsign)
                return
            self._last_auth[callsign] = time.time()

        # Validate password
        success = self._check_password(callsign, password)
        if success:
            session.callsign     = callsign
            session.authenticated = True
            pending_count = self._count_pending_messages(callsign)
            if pending_count:
                msg = (f"Welcome {callsign} — "
                       f"{pending_count} message(s) waiting, use /retrieve")
            else:
                msg = f"Welcome {callsign}"
            log.info("HubCore: AUTH OK for %s via %s",
                     callsign, session.transport_type)
        else:
            msg = "Invalid callsign or password"
            log.warning("HubCore: AUTH FAIL for %s", callsign)

        # Brief delay so spoke finishes processing its last TX ACK
        def _send_delayed():
            time.sleep(1.0)
            self._send_auth_response(session, success, msg)
            if success:
                # Notify other spokes of new arrival
                self._broadcast_except(
                    session,
                    f"★ {callsign} connected [{session.transport_type}]",
                    from_call="HUB",
                )
                self._ui({
                    "type":     "spoke_authenticated",
                    "callsign": callsign,
                    "transport":session.transport_type,
                })

        threading.Thread(target=_send_delayed, daemon=True,
                         name="hub-auth-rsp").start()

    def _handle_chat(self, session, payload: bytes) -> None:
        """Chat message from spoke — broadcast to all, show on console."""
        if not session.authenticated:
            log.warning("HubCore: chat from unauthenticated %s — ignored",
                        session.session_id)
            return
        try:
            sender, text = _chat_payload_unpack(payload)
        except Exception as exc:
            log.error("HubCore: chat parse error: %s", exc)
            return

        log.info("HubCore: CHAT from %s: %s", sender, text[:80])

        # Show on operator console
        self._ui({"type": "chat", "sender": sender, "text": text})

        # Echo back to sender so their screen shows the message
        self._send_chat(session, sender, text)

        # Broadcast to all OTHER authenticated sessions
        self._broadcast_except(session, text, from_call=sender)

    def _handle_file_list(self, session, payload: bytes) -> None:
        """Send list of hub files to requesting spoke."""
        if not session.authenticated:
            return
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        file_list: Dict[str, dict] = {}
        for fp in sorted(FILES_DIR.iterdir()):
            if fp.suffix == ".desc" or not fp.is_file():
                continue
            desc_path = FILES_DIR / (fp.name + ".desc")
            desc = desc_path.read_text().strip() if desc_path.exists() else ""
            file_list[fp.name] = {"size": fp.stat().st_size, "description": desc}

        resp_payload = json.dumps(file_list).encode()
        wire = hub_pack(HUB_TYPE_FL_RSP, resp_payload, encrypt=self.enc_enabled)
        threading.Thread(
            target=session.send, args=(wire,),
            daemon=True, name="hub-fl-rsp",
        ).start()
        log.info("HubCore: FL_RSP with %d files to %s",
                 len(file_list), session.callsign)

    def _handle_download(self, session, payload: bytes) -> None:
        """File download request — stream in background thread."""
        if not session.authenticated:
            return
        try:
            req      = json.loads(payload.decode())
            filename = req.get("filename", "")
        except Exception as exc:
            log.error("HubCore: DL_REQ parse error: %s", exc)
            return

        filepath = FILES_DIR / Path(filename).name
        if not filepath.exists() or not filepath.is_file():
            err_wire = hub_pack(
                HUB_TYPE_ERROR,
                json.dumps({"filename": filename,
                            "error": "File not found"}).encode(),
                encrypt=self.enc_enabled,
            )
            session.send(err_wire)
            log.warning("HubCore: DL_REQ file not found: %s", filename)
            return

        # Per-session transfer lock — only one download at a time per spoke
        if not session.download_lock.acquire(blocking=False):
            err_wire = hub_pack(
                HUB_TYPE_ERROR,
                json.dumps({"filename": filename,
                            "error": "Download already in progress"}).encode(),
                encrypt=self.enc_enabled,
            )
            session.send(err_wire)
            return

        threading.Thread(
            target=self._stream_file,
            args=(session, filepath),
            daemon=True, name="hub-dl",
        ).start()

    def _stream_file(self, session, filepath: Path) -> None:
        """Background thread: stream a file in CHUNK_SIZE chunks."""
        filename = filepath.name
        try:
            file_data  = filepath.read_bytes()
            total_chunks = (len(file_data) + CHUNK_SIZE - 1) // CHUNK_SIZE
            import hashlib as _hl
            file_hash  = _hl.md5(file_data).hexdigest().encode()
            fn_b       = filename.encode("utf-8")

            log.info("HubCore: streaming %s (%d bytes, %d chunks) to %s",
                     filename, len(file_data), total_chunks, session.callsign)

            for i in range(total_chunks):
                if not session.authenticated:
                    log.info("HubCore: download aborted — %s disconnected",
                             session.callsign)
                    return
                chunk = file_data[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
                # Packet: [fn_len:2][fn][chunk_num:4][total:4][hash_len:2][hash][data]
                pkt = (
                    struct.pack(">H", len(fn_b)) + fn_b
                    + struct.pack(">II", i, total_chunks)
                    + struct.pack(">H", len(file_hash)) + file_hash
                    + chunk
                )
                wire = hub_pack(HUB_TYPE_FILE_DATA, pkt,
                                encrypt=self.enc_enabled)
                if not session.send(wire):
                    log.warning("HubCore: send failed at chunk %d/%d for %s",
                                i, total_chunks, session.callsign)
                    return
                # Brief yield between chunks — avoids flooding slow transports
                time.sleep(0.05)

            done = hub_pack(
                HUB_TYPE_COMPLETE,
                json.dumps({"filename": filename, "success": True}).encode(),
                encrypt=self.enc_enabled,
            )
            session.send(done)
            log.info("HubCore: download complete: %s → %s",
                     filename, session.callsign)

        except Exception as exc:
            log.error("HubCore: _stream_file error: %s", exc, exc_info=True)
            err_wire = hub_pack(
                HUB_TYPE_ERROR,
                json.dumps({"filename": filename,
                            "error": str(exc)}).encode(),
                encrypt=self.enc_enabled,
            )
            session.send(err_wire)
        finally:
            session.download_lock.release()

    def _handle_store(self, session, payload: bytes) -> None:
        """Store a message for another callsign."""
        if not session.authenticated:
            return
        try:
            off = 0
            to_len  = struct.unpack(">H", payload[off:off+2])[0]; off += 2
            to_call = payload[off:off+to_len].decode("utf-8").upper(); off += to_len
            inner_wire = payload[off:]

            # Unpack inner wire to extract sender + text
            inner_type, inner_payload, _ = hub_unpack(inner_wire)
            if inner_type != HUB_TYPE_CHAT:
                log.warning("HubCore: STORE inner type unexpected: 0x%02x",
                            inner_type)
                return
            sender, text = _chat_payload_unpack(inner_payload)

        except Exception as exc:
            log.error("HubCore: STORE parse error: %s", exc)
            return

        # Validate recipient
        known = self._known_callsigns()
        if to_call not in known and to_call != "*BUL*":
            self._send_store_ack(session, False, to_call,
                                 f"Unknown callsign: {to_call}")
            return

        # Determine recipients
        if to_call == "*BUL*":
            recipients = [c for c in known if c != session.callsign]
        else:
            recipients = [to_call]

        ts = int(time.time())
        for recip in recipients:
            msg_dir = MSG_BASE / recip
            msg_dir.mkdir(parents=True, exist_ok=True)
            fname = msg_dir / str(int(time.time() * 1000))
            fname.write_text(json.dumps({
                "sender":    sender,
                "text":      text,
                "timestamp": ts,
            }))

        log.info("HubCore: stored message from %s → %s",
                 sender, to_call)
        self._ui({
            "type":   "chat",
            "sender": "HUB",
            "text":   f"[STORE] {sender}→{to_call}: {text[:60]}",
        })

        # Check if recipient is currently online — deliver immediately
        for recip in recipients:
            target_session = self.session_manager.by_callsign(recip)
            if target_session:
                self._send_chat(
                    target_session,
                    f"{sender} [stored]",
                    text,
                )

        msg = (f"Bulletin stored for {len(recipients)} station(s)"
               if to_call == "*BUL*"
               else f"Message stored for {to_call}")
        self._send_store_ack(session, True, to_call, msg)

    def _handle_retrieve(self, session, payload: bytes) -> None:
        """Deliver stored messages to authenticated spoke."""
        if not session.authenticated:
            return
        callsign = session.callsign
        msg_dir  = MSG_BASE / callsign
        messages = []

        if msg_dir.exists():
            for mf in sorted(msg_dir.iterdir()):
                if not mf.is_file():
                    continue
                try:
                    messages.append(json.loads(mf.read_text()))
                    mf.unlink()
                except Exception as exc:
                    log.warning("HubCore: error reading message %s: %s",
                                mf, exc)

        def _deliver(msgs=messages, s=session, call=callsign):
            # Determine transport speed — HF AX.25 at 300 baud needs generous
            # inter-message gaps so the remote can ARQ each frame before the
            # next one arrives. The AGW socket accepts frames faster than
            # Direwolf can transmit them, so we must throttle here.
            ttype = getattr(s, "transport_type", "")
            is_hf = "HF" in ttype.upper()
            # HF: 4s gap — longer than FRACK (10s) is too slow, but we need
            #   enough time for the remote to TX an RR ACK and hub to receive it.
            #   At 300 baud a short RR takes ~1s to transmit + propagation.
            #   2 seconds is marginal; 4 seconds gives breathing room.
            # VHF: 0.5s — 1200 baud is fast enough that 0.5s is adequate.
            # TCP: no delay needed.
            msg_gap = 4.0 if is_hf else (0.5 if "AX25" in ttype.upper() else 0.0)

            time.sleep(1.0)   # give ARQ time to finish last ACK before first msg
            if not msgs:
                rsp = hub_pack(
                    HUB_TYPE_RETRIEVE_RSP,
                    json.dumps({"messages": 0}).encode(),
                    encrypt=self.enc_enabled,
                )
                s.send(rsp)
                return

            for i, m in enumerate(msgs):
                sndr = m.get("sender", "?")
                text = m.get("text", "")
                ts   = m.get("timestamp", 0)
                try:
                    dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%MZ"
                    )
                except Exception:
                    dt_str = "unknown time"

                chat_wire = hub_pack(
                    HUB_TYPE_CHAT,
                    _chat_payload(sndr, f"[Stored {dt_str}] {text}"),
                    encrypt=self.enc_enabled,
                )
                if not s.send(chat_wire):
                    log.warning("HubCore: retrieve send failed at msg %d/%d",
                                i + 1, len(msgs))
                    return
                if i < len(msgs) - 1 and msg_gap > 0:
                    log.debug("HubCore: retrieve inter-msg gap %.1fs (%s)",
                              msg_gap, ttype)
                    time.sleep(msg_gap)

            # Add a gap before the final RSP so last message is ACKed first
            if msg_gap > 0:
                time.sleep(msg_gap)
            rsp = hub_pack(
                HUB_TYPE_RETRIEVE_RSP,
                json.dumps({"messages": len(msgs),
                            "message": f"{len(msgs)} message(s) delivered"}).encode(),
                encrypt=self.enc_enabled,
            )
            s.send(rsp)
            log.info("HubCore: delivered %d messages to %s", len(msgs), call)

        threading.Thread(target=_deliver, daemon=True,
                         name="hub-retrieve").start()

    def _handle_passwd(self, session, payload: bytes) -> None:
        """Handle password change request from authenticated spoke."""
        if not session.authenticated:
            return
        try:
            req      = json.loads(payload.decode())
            call     = req.get("callsign", "").upper()
            curr_pw  = req.get("current_pw", "")
            new_pw   = req.get("new_pw", "")
        except Exception as exc:
            log.error("HubCore: PASSWD_REQ parse error: %s", exc)
            return

        db = self._load_pw_db()
        stored = db.get(call)
        if not stored:
            result, ok = f"No account for {call}", False
        elif hashlib.sha256(curr_pw.encode()).hexdigest() != stored:
            result, ok = "Current password incorrect", False
        elif len(new_pw) < 4:
            result, ok = "New password too short (min 4 chars)", False
        else:
            db[call] = hashlib.sha256(new_pw.encode()).hexdigest()
            self._save_pw_db(db)
            result, ok = "Password changed successfully", True

        rsp_wire = hub_pack(
            HUB_TYPE_PASSWD_RSP,
            json.dumps({"ok": ok, "msg": result}).encode(),
            encrypt=self.enc_enabled,
        )
        session.send(rsp_wire)
        log.info("HubCore: PASSWD_REQ %s: %s", call, result)

    def _handle_ping(self, session, payload: bytes) -> None:
        """Respond to keepalive ping."""
        pong = hub_pack(HUB_TYPE_PONG, b"{}", encrypt=self.enc_enabled)
        session.send(pong)
        session.touch()

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    def _broadcast_except(
        self,
        exclude_session,
        text: str,
        from_call: str = "HUB",
    ) -> None:
        """Send a chat message to all authenticated sessions except one."""
        wire = hub_pack(
            HUB_TYPE_CHAT,
            _chat_payload(from_call, text),
            encrypt=self.enc_enabled,
        )
        for s in self.session_manager.authenticated():
            if s.session_id != exclude_session.session_id:
                s.send(wire)

    def _send_chat(
        self,
        session,
        sender: str,
        text: str,
    ) -> bool:
        wire = hub_pack(
            HUB_TYPE_CHAT,
            _chat_payload(sender, text),
            encrypt=self.enc_enabled,
        )
        return session.send(wire)

    def _send_auth_response(
        self,
        session,
        success: bool,
        message: str,
    ) -> None:
        wire = hub_pack(
            HUB_TYPE_AUTH_RSP,
            json.dumps({"success": success, "message": message}).encode(),
            encrypt=self.enc_enabled,
        )
        session.send(wire)

    def _send_store_ack(
        self,
        session,
        ok: bool,
        to_call: str,
        message: str,
    ) -> None:
        def _send(s=session, o=ok, t=to_call, m=message):
            # Delay before responding — for HF AX.25 at 300 baud the spoke
            # may still be processing its RR ACK from its own STORE frame.
            # Without a pause the hub sends STORE_ACK while the spoke is
            # still in TX and it collides. 1.5s gives enough gap.
            time.sleep(1.5)
            wire = hub_pack(
                HUB_TYPE_STORE_ACK,
                json.dumps({"ok": o, "to": t, "message": m}).encode(),
                encrypt=self.enc_enabled,
            )
            s.send(wire)
        threading.Thread(target=_send, daemon=True,
                         name="hub-store-ack").start()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_pw_db(self) -> dict:
        try:
            return json.loads(PW_FILE.read_text()) if PW_FILE.exists() else {}
        except Exception as exc:
            log.error("HubCore: error loading passwords.json: %s", exc)
            return {}

    def _save_pw_db(self, db: dict) -> None:
        PW_FILE.parent.mkdir(parents=True, exist_ok=True)
        PW_FILE.write_text(json.dumps(db, indent=2))
        PW_FILE.chmod(0o600)

    def _check_password(self, callsign: str, password: str) -> bool:
        db = self._load_pw_db()
        stored = db.get(callsign.upper())
        if not stored:
            return False
        return hashlib.sha256(password.encode()).hexdigest() == stored

    def _known_callsigns(self) -> set:
        return {c.upper() for c in self._load_pw_db()}

    def _count_pending_messages(self, callsign: str) -> int:
        msg_dir = MSG_BASE / callsign.upper()
        if not msg_dir.exists():
            return 0
        return sum(1 for f in msg_dir.iterdir() if f.is_file())

    @staticmethod
    def _load_settings() -> dict:
        try:
            return json.loads(SETTINGS_F.read_text())
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # UI event helper
    # ------------------------------------------------------------------

    def _ui(self, event: dict) -> None:
        """Push an event to the operator console (if callback set)."""
        if self.on_ui_event:
            try:
                self.on_ui_event(event)
            except Exception as exc:
                log.error("HubCore._ui callback error: %s", exc)
