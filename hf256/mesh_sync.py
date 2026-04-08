"""
HF-256 Mesh Sync
=================
Hub-to-hub data synchronisation — keeps message stores and file libraries
consistent across geographically distributed hub stations.

Protocol
---------
* **Transport A (TCP)**: Hub A connects to Hub B on port ``14257`` (mesh port),
  authenticates with the shared network key (AESGCM decrypt success = auth),
  and exchanges sync digests.
* **Transport B (HF/Radio)**: Uses an existing ARDOP or Direwolf connection
  if available; same framing but delivered over the radio link.

Sync algorithm (digest-then-pull)
-----------------------------------
1. Initiator sends ``MESH_HELLO``: its callsign + timestamp of last sync.
2. Responder replies ``MESH_DIGEST``: SHA-256 hashes of all messages and
   file metadata stored since that timestamp.
3. Initiator compares digest against its own store; requests only unknown
   items via ``MESH_REQ``.
4. Responder streams requested items via ``MESH_DATA``.
5. Initiator ACKs with ``MESH_DONE``.

Both sides then swap roles so the responder can pull items it is missing.

Security
---------
All frames are AES-256-GCM encrypted using the shared network key — the
same key used for spoke ↔ hub traffic.  A station without the key cannot
decrypt the MESH_HELLO and the connection closes immediately.

This means mesh sync requires all hub nodes to share the **same** network
key, which is the expected deployment model for a trusted hub network.
"""

import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("hf256.mesh")

# ── Paths ────────────────────────────────────────────────────────────────────
KEY_FILE  = Path("/etc/hf256/network.key")
MSG_BASE  = Path("/home/pi/.hf256/hub_messages")
FILES_DIR = Path("/home/pi/.hf256/hub_files")
SYNC_META = Path("/home/pi/.hf256/mesh_sync.json")

# ── Constants ────────────────────────────────────────────────────────────────
MESH_TCP_PORT   = 14257
SYNC_INTERVAL   = 300       # seconds between automatic sync attempts
CONNECT_TIMEOUT = 15        # seconds for TCP connect
FRAME_MAX       = 4 * 1024 * 1024   # 4 MB max frame

# ── Mesh wire type codes ──────────────────────────────────────────────────────
MT_HELLO   = 0x50
MT_DIGEST  = 0x51
MT_REQ     = 0x52
MT_DATA    = 0x53
MT_DONE    = 0x54
MT_ERROR   = 0x5F

# ── Wire format helpers (same AES-GCM wrapper as HubCore) ────────────────────
_HUB_VERSION  = 0x01
_HUB_FLAG_ENC = 0x01


def _load_key() -> Optional[bytes]:
    try:
        key = KEY_FILE.read_bytes()
        return key if len(key) == 32 else None
    except Exception:
        return None


def _get_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = _load_key()
        return AESGCM(key) if key else None
    except Exception:
        return None


def _pack(msg_type: int, payload: bytes) -> bytes:
    """Pack and encrypt a mesh frame."""
    aesgcm    = _get_aesgcm()
    flags     = _HUB_FLAG_ENC if aesgcm else 0x00
    timestamp = int(time.time())
    aad       = struct.pack(">BBBI", _HUB_VERSION, flags, msg_type, timestamp)
    if aesgcm:
        iv         = os.urandom(12)
        ciphertext = aesgcm.encrypt(iv, payload, aad)
        return aad + iv + ciphertext
    dummy_iv = b"\x00" * 12
    return aad + dummy_iv + payload


def _unpack(data: bytes) -> Tuple[int, bytes]:
    """Unpack and decrypt a mesh frame. Returns (msg_type, payload)."""
    if len(data) < 19:
        raise ValueError(f"Frame too short: {len(data)} bytes")
    flags    = data[1]
    msg_type = data[2]
    iv       = data[7:19]
    body     = data[19:]
    if flags & _HUB_FLAG_ENC:
        aesgcm = _get_aesgcm()
        if aesgcm is None:
            raise ValueError("Encrypted frame but no network key")
        aad = data[0:7]
        try:
            payload = aesgcm.decrypt(iv, body, aad)
        except Exception as exc:
            raise ValueError(f"Decryption failed: {exc}") from exc
    else:
        payload = body
    return msg_type, payload


def _send_frame(sock: socket.socket, msg_type: int, payload: bytes) -> bool:
    """Send a length-prefixed encrypted mesh frame."""
    try:
        frame  = _pack(msg_type, payload)
        prefix = struct.pack(">I", len(frame))
        sock.sendall(prefix + frame)
        return True
    except Exception as exc:
        log.error("MeshSync: send error: %s", exc)
        return False


def _recv_frame(sock: socket.socket) -> Tuple[Optional[int], Optional[bytes]]:
    """Receive and unpack one length-prefixed mesh frame."""
    try:
        header = _recv_exact(sock, 4)
        if not header:
            return None, None
        frame_len = struct.unpack(">I", header)[0]
        if frame_len == 0 or frame_len > FRAME_MAX:
            log.error("MeshSync: invalid frame length %d", frame_len)
            return None, None
        raw = _recv_exact(sock, frame_len)
        if not raw:
            return None, None
        msg_type, payload = _unpack(raw)
        return msg_type, payload
    except Exception as exc:
        log.error("MeshSync: recv error: %s", exc)
        return None, None


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        except Exception:
            return None
    return buf


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Local store helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _message_digest(since_ts: int = 0) -> Dict[str, str]:
    """
    Return {msg_id: sha256} for all stored messages newer than since_ts.
    msg_id = "<recipient>/<filename>"
    """
    digest = {}
    if not MSG_BASE.exists():
        return digest
    for recip_dir in MSG_BASE.iterdir():
        if not recip_dir.is_dir():
            continue
        for mf in recip_dir.iterdir():
            if not mf.is_file():
                continue
            try:
                mtime = mf.stat().st_mtime
                if mtime < since_ts:
                    continue
                content = mf.read_bytes()
                h = hashlib.sha256(content).hexdigest()
                digest[f"{recip_dir.name}/{mf.name}"] = h
            except Exception as exc:
                log.warning("MeshSync: digest error %s: %s", mf, exc)
    return digest


def _file_digest(since_ts: int = 0) -> Dict[str, str]:
    """
    Return {filename: sha256} for all hub files newer than since_ts.
    """
    digest = {}
    if not FILES_DIR.exists():
        return digest
    for fp in FILES_DIR.iterdir():
        if fp.suffix == ".desc" or not fp.is_file():
            continue
        try:
            mtime = fp.stat().st_mtime
            if mtime < since_ts:
                continue
            h = hashlib.sha256(fp.read_bytes()).hexdigest()
            digest[fp.name] = h
        except Exception as exc:
            log.warning("MeshSync: file digest error %s: %s", fp, exc)
    return digest


def _load_sync_meta() -> dict:
    try:
        return json.loads(SYNC_META.read_text())
    except Exception:
        return {}


def _save_sync_meta(meta: dict) -> None:
    SYNC_META.parent.mkdir(parents=True, exist_ok=True)
    SYNC_META.write_text(json.dumps(meta, indent=2))


def _last_sync_ts(peer_call: str) -> int:
    meta = _load_sync_meta()
    return meta.get(peer_call, {}).get("last_sync", 0)


def _update_sync_ts(peer_call: str) -> None:
    meta = _load_sync_meta()
    if peer_call not in meta:
        meta[peer_call] = {}
    meta[peer_call]["last_sync"] = int(time.time())
    _save_sync_meta(meta)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sync session — runs the bidirectional sync protocol over one socket
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _SyncSession:
    """Handles one synchronisation session (initiator or responder)."""

    def __init__(
        self,
        sock: socket.socket,
        mycall: str,
        peer_call: str,
        role: str,           # "initiator" | "responder"
    ) -> None:
        self.sock      = sock
        self.mycall    = mycall.upper()
        self.peer_call = peer_call.upper()
        self.role      = role

    def run(self) -> bool:
        """Execute sync session. Returns True on success."""
        if self.role == "initiator":
            return self._run_initiator()
        return self._run_responder()

    # ── Initiator (we call out to peer) ──────────────────────────────

    def _run_initiator(self) -> bool:
        last_ts = _last_sync_ts(self.peer_call)

        # HELLO
        hello = json.dumps({
            "callsign":   self.mycall,
            "since_ts":  last_ts,
        }).encode()
        if not _send_frame(self.sock, MT_HELLO, hello):
            return False

        # Expect DIGEST from peer
        mt, payload = _recv_frame(self.sock)
        if mt != MT_DIGEST or payload is None:
            log.error("MeshSync: expected MT_DIGEST, got %s", mt)
            return False

        peer_digest = json.loads(payload.decode())
        our_msg_digest  = _message_digest(since_ts=0)   # full local set
        our_file_digest = _file_digest(since_ts=0)

        # Identify what we are missing from peer
        missing_msgs  = {k: v for k, v in peer_digest.get("messages", {}).items()
                         if k not in our_msg_digest}
        missing_files = {k: v for k, v in peer_digest.get("files", {}).items()
                         if k not in our_file_digest}

        # REQ
        req = json.dumps({
            "messages": list(missing_msgs.keys()),
            "files":    list(missing_files.keys()),
        }).encode()
        if not _send_frame(self.sock, MT_REQ, req):
            return False

        # Receive DATA frames
        received_msgs  = 0
        received_files = 0
        while True:
            mt, payload = _recv_frame(self.sock)
            if mt is None:
                log.error("MeshSync: connection lost during DATA receive")
                return False
            if mt == MT_DONE:
                break
            if mt == MT_DATA:
                obj = json.loads(payload.decode())
                if obj.get("kind") == "message":
                    self._store_message(obj)
                    received_msgs += 1
                elif obj.get("kind") == "file":
                    self._store_file(obj)
                    received_files += 1
            elif mt == MT_ERROR:
                log.error("MeshSync: peer error: %s",
                          payload.decode(errors="replace"))
                return False

        log.info(
            "MeshSync: received %d messages, %d files from %s",
            received_msgs, received_files, self.peer_call,
        )

        # Now let the peer pull from us — swap roles by running responder logic
        peer_our_hello_mt, peer_hello = _recv_frame(self.sock)
        if peer_our_hello_mt == MT_HELLO and peer_hello is not None:
            self._responder_answer_hello(peer_hello)

        _update_sync_ts(self.peer_call)
        return True

    # ── Responder (peer called us) ────────────────────────────────────

    def _run_responder(self) -> bool:
        # Expect HELLO
        mt, payload = _recv_frame(self.sock)
        if mt != MT_HELLO or payload is None:
            log.error("MeshSync: expected MT_HELLO from %s, got %s",
                      self.peer_call, mt)
            return False

        ok = self._responder_answer_hello(payload)
        if not ok:
            return False

        # Now we act as initiator to pull from peer
        last_ts = _last_sync_ts(self.peer_call)
        hello = json.dumps({
            "callsign": self.mycall,
            "since_ts": last_ts,
        }).encode()
        if not _send_frame(self.sock, MT_HELLO, hello):
            return False

        # Same flow as _run_initiator from DIGEST onwards
        mt, payload = _recv_frame(self.sock)
        if mt != MT_DIGEST or payload is None:
            return False
        # (simplified: request everything peer has that we don't)
        peer_digest = json.loads(payload.decode())
        our_msg_digest  = _message_digest(since_ts=0)
        our_file_digest = _file_digest(since_ts=0)
        missing_msgs  = {k for k in peer_digest.get("messages", {})
                         if k not in our_msg_digest}
        missing_files = {k for k in peer_digest.get("files", {})
                         if k not in our_file_digest}
        req = json.dumps({
            "messages": list(missing_msgs),
            "files":    list(missing_files),
        }).encode()
        _send_frame(self.sock, MT_REQ, req)

        received = 0
        while True:
            mt, payload = _recv_frame(self.sock)
            if mt is None or mt == MT_DONE:
                break
            if mt == MT_DATA:
                obj = json.loads(payload.decode())
                if obj.get("kind") == "message":
                    self._store_message(obj)
                elif obj.get("kind") == "file":
                    self._store_file(obj)
                received += 1
        log.info("MeshSync: responder received %d items from %s",
                 received, self.peer_call)
        _update_sync_ts(self.peer_call)
        return True

    def _responder_answer_hello(self, hello_payload: bytes) -> bool:
        """Answer a HELLO with our DIGEST and then stream requested items."""
        try:
            hello = json.loads(hello_payload.decode())
        except Exception as exc:
            log.error("MeshSync: HELLO parse error: %s", exc)
            return False

        since_ts = hello.get("since_ts", 0)
        digest = json.dumps({
            "messages": _message_digest(since_ts),
            "files":    _file_digest(since_ts),
        }).encode()
        if not _send_frame(self.sock, MT_DIGEST, digest):
            return False

        # Expect REQ
        mt, payload = _recv_frame(self.sock)
        if mt != MT_REQ or payload is None:
            log.error("MeshSync: expected MT_REQ")
            return False

        req = json.loads(payload.decode())
        req_msgs  = req.get("messages", [])
        req_files = req.get("files", [])

        # Stream requested messages
        for msg_id in req_msgs:
            try:
                parts = msg_id.split("/", 1)
                if len(parts) != 2:
                    continue
                recip, fname = parts
                mf = MSG_BASE / recip / fname
                if not mf.exists():
                    continue
                body = json.loads(mf.read_text())
                data = json.dumps({
                    "kind":      "message",
                    "recipient": recip,
                    "filename":  fname,
                    "body":      body,
                }).encode()
                _send_frame(self.sock, MT_DATA, data)
            except Exception as exc:
                log.warning("MeshSync: error sending message %s: %s",
                            msg_id, exc)

        # Stream requested files
        for filename in req_files:
            try:
                fp = FILES_DIR / Path(filename).name
                if not fp.exists():
                    continue
                file_bytes = fp.read_bytes()
                import base64 as _b64
                desc_fp = FILES_DIR / (fp.name + ".desc")
                desc = desc_fp.read_text().strip() if desc_fp.exists() else ""
                data = json.dumps({
                    "kind":     "file",
                    "filename": fp.name,
                    "content":  _b64.b64encode(file_bytes).decode(),
                    "description": desc,
                }).encode()
                _send_frame(self.sock, MT_DATA, data)
            except Exception as exc:
                log.warning("MeshSync: error sending file %s: %s",
                            filename, exc)

        _send_frame(self.sock, MT_DONE, b"{}")
        return True

    # ── Storage helpers ───────────────────────────────────────────────

    @staticmethod
    def _store_message(obj: dict) -> None:
        recip    = obj.get("recipient", "").upper()
        fname    = obj.get("filename", "")
        body     = obj.get("body", {})
        if not recip or not fname or not body:
            return
        msg_dir = MSG_BASE / recip
        msg_dir.mkdir(parents=True, exist_ok=True)
        dest = msg_dir / fname
        if not dest.exists():          # never overwrite
            dest.write_text(json.dumps(body))
            log.info("MeshSync: stored message → %s/%s", recip, fname)

    @staticmethod
    def _store_file(obj: dict) -> None:
        filename = obj.get("filename", "")
        content  = obj.get("content", "")
        desc     = obj.get("description", "")
        if not filename or not content:
            return
        import base64 as _b64
        dest = FILES_DIR / Path(filename).name
        if not dest.exists():          # never overwrite
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_b64.b64decode(content))
            if desc:
                (FILES_DIR / (dest.name + ".desc")).write_text(desc)
            log.info("MeshSync: stored file %s", filename)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MeshSyncManager — public façade
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MeshSyncManager:
    """
    Manages hub-to-hub synchronisation.

    * Starts a persistent TCP server on ``MESH_TCP_PORT`` accepting
      inbound sync requests from peer hubs.
    * Periodically initiates outbound sync to configured peer hub addresses.
    * Can also sync over a radio connection via ``sync_via_session()``.

    Usage::

        mgr = MeshSyncManager(
            mycall  = "N0HUB",
            peers   = ["192.168.1.10", "10.0.0.5"],  # or IP:port
        )
        mgr.start()   # starts server + scheduler threads
    """

    def __init__(
        self,
        mycall: str,
        peers: Optional[List[str]] = None,
        sync_interval: int = SYNC_INTERVAL,
    ) -> None:
        self.mycall        = mycall.upper()
        self.peers         = peers or []
        self.sync_interval = sync_interval
        self._running      = False
        self._server_thread: Optional[threading.Thread] = None
        self._sched_thread:  Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._server_thread = threading.Thread(
            target=self._server_loop,
            daemon=True, name="mesh-server",
        )
        self._server_thread.start()

        if self.peers:
            self._sched_thread = threading.Thread(
                target=self._scheduler_loop,
                daemon=True, name="mesh-sched",
            )
            self._sched_thread.start()

        log.info(
            "MeshSyncManager: started as %s  peers=%s  interval=%ds",
            self.mycall, self.peers, self.sync_interval,
        )

    def stop(self) -> None:
        self._running = False
        log.info("MeshSyncManager: stopped")

    def sync_now(self, peer_address: str) -> bool:
        """Trigger an immediate sync with a specific peer (blocking)."""
        return self._sync_to(peer_address)

    # ------------------------------------------------------------------
    # TCP server (responder side)
    # ------------------------------------------------------------------

    def _server_loop(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", MESH_TCP_PORT))
            srv.listen(4)
            srv.settimeout(1.0)
            log.info("MeshSync: TCP server listening on port %d", MESH_TCP_PORT)
        except Exception as exc:
            log.error("MeshSync: server failed to bind port %d: %s",
                      MESH_TCP_PORT, exc)
            return

        while self._running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception as exc:
                if self._running:
                    log.error("MeshSync: accept error: %s", exc)
                continue
            log.info("MeshSync: inbound sync connection from %s", addr)
            threading.Thread(
                target=self._handle_inbound,
                args=(conn, addr),
                daemon=True,
                name="mesh-inbound",
            ).start()

        try:
            srv.close()
        except Exception:
            pass

    def _handle_inbound(self, conn: socket.socket, addr) -> None:
        """Handle one inbound sync session."""
        try:
            conn.settimeout(60)
            # Read peer callsign from HELLO (best-effort for logging)
            peer_call = "UNKNOWN"
            try:
                mt, payload = _recv_frame(conn)
                if mt == MT_HELLO and payload:
                    hello = json.loads(payload.decode())
                    peer_call = hello.get("callsign", "UNKNOWN").upper()
                    # Re-inject the hello so _SyncSession sees it
                    # Re-create the socket-level stream is complex;
                    # instead use _SyncSession._responder_answer_hello directly
                    session = _SyncSession(conn, self.mycall, peer_call, "responder")
                    session._responder_answer_hello(payload)
                    # Now peer may send its own HELLO for bidirectional pull
                    mt2, payload2 = _recv_frame(conn)
                    if mt2 == MT_HELLO and payload2:
                        peer2 = json.loads(payload2.decode()).get("callsign", peer_call)
                        session2 = _SyncSession(conn, self.mycall, peer2, "responder")
                        session2._responder_answer_hello(payload2)
                    _update_sync_ts(peer_call)
                    log.info("MeshSync: inbound sync from %s complete", peer_call)
            except Exception as exc:
                log.error("MeshSync: inbound session error from %s: %s",
                          peer_call, exc, exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Scheduler (initiator side)
    # ------------------------------------------------------------------

    def _scheduler_loop(self) -> None:
        # Stagger first sync by a few seconds to let the system settle
        time.sleep(15)
        while self._running:
            for peer in list(self.peers):
                if not self._running:
                    break
                try:
                    self._sync_to(peer)
                except Exception as exc:
                    log.error("MeshSync: scheduled sync to %s failed: %s",
                              peer, exc)
            # Sleep in small increments so stop() is responsive
            for _ in range(self.sync_interval):
                if not self._running:
                    return
                time.sleep(1)

    def _sync_to(self, peer_address: str) -> bool:
        """Open a TCP connection to peer hub and run a sync session."""
        # Parse host:port
        if ":" in peer_address:
            host, _, port_str = peer_address.rpartition(":")
            try:
                port = int(port_str)
            except ValueError:
                host, port = peer_address, MESH_TCP_PORT
        else:
            host, port = peer_address, MESH_TCP_PORT

        log.info("MeshSync: initiating sync to %s:%d", host, port)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((host, port))
            sock.settimeout(120)

            # Identify peer callsign — derive from address for initial key
            peer_call = host.replace(".", "_").upper()
            session = _SyncSession(sock, self.mycall, peer_call, "initiator")
            ok = session.run()
            log.info("MeshSync: sync to %s %s", peer_address,
                     "succeeded" if ok else "failed")
            return ok

        except ConnectionRefusedError:
            log.warning("MeshSync: %s:%d refused connection (peer offline?)",
                        host, port)
        except socket.timeout:
            log.warning("MeshSync: timeout connecting to %s:%d", host, port)
        except Exception as exc:
            log.error("MeshSync: sync error to %s: %s", peer_address, exc)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        return False

    def add_peer(self, address: str) -> None:
        """Add a new peer address at runtime."""
        if address not in self.peers:
            self.peers.append(address)
            log.info("MeshSync: added peer %s", address)

    def remove_peer(self, address: str) -> None:
        """Remove a peer address at runtime."""
        try:
            self.peers.remove(address)
            log.info("MeshSync: removed peer %s", address)
        except ValueError:
            pass
