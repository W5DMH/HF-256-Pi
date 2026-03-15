"""
HF-256 FreeDV Transport
Session layer over freedvtnc2 KISS interface (port 8001).

freedvtnc2 is a raw packet modem with no connection concept.
This module adds:
  - P2P connection handshake  (CONN_REQ / CONN_ACK / CONN_REJ)
  - Software ARQ              (DATA + DATA_ACK / DATA_NAK, 5 retries, 60s timeout)
  - Graceful disconnect       (DISC / DISC_ACK)
  - Broadcast announce        (ANNOUNCE — no connection, no ACK)

Packet wire format (inside KISS payload):
  [magic:2][pkt_type:1][src_len:1][src_call][dst_len:1][dst_call][body]

  magic   = 0x48 0x46  ("HF") — identifies HF-256 packets vs other KISS traffic
  pkt_type — see PKT_* constants below
  src/dst — amateur callsigns, 1-9 bytes each (length-prefixed)
  body    — type-specific payload

KISS framing is handled by hf256.kiss (encode / decode).
"""

import socket
import struct
import threading
import time
import logging

log = logging.getLogger("hf256.freedv")

# ── Protocol constants ──────────────────────────────────────────────────────

MAGIC = b"\x48\x46"          # "HF" — packet identifier

PKT_CONN_REQ  = 0x01          # Caller → callee: request connection
PKT_CONN_ACK  = 0x02          # Callee → caller: accepted
PKT_CONN_REJ  = 0x03          # Callee → caller: rejected / busy
PKT_DATA      = 0x04          # Data frame:  body = [seq:1][payload]
PKT_DATA_ACK  = 0x05          # ACK:         body = [seq:1]
PKT_DATA_NAK  = 0x06          # NAK/retry:   body = [seq:1]
PKT_DISC      = 0x07          # Disconnect request
PKT_DISC_ACK  = 0x08          # Disconnect acknowledged
PKT_ANNOUNCE  = 0x09          # Broadcast to all; dst = "*"

BROADCAST = "*"               # Wildcard destination for announce

# ARQ parameters
ARQ_MAX_RETRIES = 5
ARQ_TIMEOUT     = 60.0        # seconds per attempt

# KISS constants (matches hf256/kiss.py)
FEND      = 0xC0
FESC      = 0xDB
TFEND     = 0xDC
TFESC     = 0xDD
DATA_FRAME = 0x00


# ── KISS encode / decode (inline to avoid import path issues in portal) ─────

def _kiss_encode(data: bytes) -> bytes:
    escaped = bytearray()
    for b in data:
        if b == FEND:
            escaped.extend([FESC, TFEND])
        elif b == FESC:
            escaped.extend([FESC, TFESC])
        else:
            escaped.append(b)
    frame = bytearray([FEND, DATA_FRAME])
    frame.extend(escaped)
    frame.append(FEND)
    return bytes(frame)


def _kiss_decode(buf: bytearray) -> list:
    """Extract complete KISS DATA frames from buf (mutates in-place)."""
    frames = []
    while True:
        try:
            start = buf.index(FEND)
        except ValueError:
            buf.clear()
            break
        if start > 0:
            del buf[:start]
        try:
            end = buf.index(FEND, 1)
        except ValueError:
            break
        content = bytes(buf[1:end])
        del buf[:end + 1]
        if not content:
            continue
        cmd = content[0] & 0x0F
        if cmd != DATA_FRAME:
            continue
        raw = content[1:] if len(content) > 1 else b""
        # unescape
        payload = bytearray()
        i = 0
        while i < len(raw):
            if raw[i] == FESC and i + 1 < len(raw):
                nxt = raw[i + 1]
                if nxt == TFEND:
                    payload.append(FEND)
                elif nxt == TFESC:
                    payload.append(FESC)
                i += 2
            else:
                payload.append(raw[i])
                i += 1
        if payload:
            frames.append(bytes(payload))
    return frames


# ── Packet pack / unpack ─────────────────────────────────────────────────────

def _pack(pkt_type: int, src: str, dst: str, body: bytes = b"") -> bytes:
    """Build a HF-256 FreeDV packet."""
    src_b = src.upper().encode("ascii")
    dst_b = dst.upper().encode("ascii")
    return (MAGIC
            + bytes([pkt_type])
            + bytes([len(src_b)]) + src_b
            + bytes([len(dst_b)]) + dst_b
            + body)


def _unpack(data: bytes):
    """
    Parse a HF-256 FreeDV packet.
    Returns (pkt_type, src, dst, body) or None if invalid.
    """
    if len(data) < 5:
        return None
    if data[:2] != MAGIC:
        return None
    pkt_type = data[2]
    offset = 3
    src_len = data[offset]; offset += 1
    if offset + src_len > len(data):
        return None
    src = data[offset:offset + src_len].decode("ascii", errors="ignore")
    offset += src_len
    if offset >= len(data):
        return None
    dst_len = data[offset]; offset += 1
    if offset + dst_len > len(data):
        return None
    dst = data[offset:offset + dst_len].decode("ascii", errors="ignore")
    offset += dst_len
    body = data[offset:]
    return pkt_type, src, dst, body


# ── Transport class ──────────────────────────────────────────────────────────

class FreeDVTransport:
    """
    FreeDV KISS session transport.

    Same external interface as ARDOPConnection:
      connect()          — connect to freedvtnc2 KISS port
      vara_connect(call) — initiate P2P session to remote callsign
      vara_disconnect()  — graceful disconnect
      send_data(data)    — send payload (ARQ-protected)
      send_announce(text)— broadcast announce (no ARQ)
      close()            — shut down

    Callbacks:
      on_state_change(old, new)
      on_message_received(data)
      on_announce_received(src, text)   — called for ANNOUNCE packets
      on_ptt_change(bool)               — not used; kept for interface compat
    """

    STATE_DISCONNECTED = 0
    STATE_CONNECTING   = 1
    STATE_CONNECTED    = 2

    def __init__(self, mycall: str,
                 kiss_host: str = "127.0.0.1",
                 kiss_port: int = 8001):
        self.mycall    = mycall.upper()
        self.kiss_host = kiss_host
        self.kiss_port = kiss_port

        self.state       = FreeDVTransport.STATE_DISCONNECTED
        self.remote_call = None
        self.running     = False

        self._sock      = None
        self._rx_buf    = bytearray()
        self._lock      = threading.Lock()
        self._send_lock = threading.Lock()

        # ARQ state
        self._arq_seq      = 0          # outgoing sequence number (0-255)
        self._arq_pending  = None       # bytes waiting for ACK
        self._arq_seq_sent = 0
        self._arq_event    = threading.Event()
        self._arq_acked    = False
        self._arq_nak      = False

        # Callbacks
        self.on_state_change      = None
        self.on_message_received  = None
        self.on_announce_received = None
        self.on_ptt_change        = None  # compat

    # ── Public API ─────────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to freedvtnc2 KISS port 8001."""
        try:
            log.info("FreeDV: connecting to KISS port %s:%d",
                     self.kiss_host, self.kiss_port)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((self.kiss_host, self.kiss_port))
            sock.settimeout(None)
            self._sock    = sock
            self.running  = True
            threading.Thread(target=self._reader, daemon=True,
                             name="freedv-rx").start()
            log.info("FreeDV: KISS connection established")
            return True
        except Exception as e:
            log.error("FreeDV: KISS connect failed: %s", e)
            return False

    def vara_connect(self, target_call: str):
        """
        Initiate a P2P connection to target_call.
        Sends CONN_REQ and waits for CONN_ACK (up to ARQ_TIMEOUT seconds).
        Fires on_state_change(0→1) immediately, then (1→2) on ACK.
        """
        with self._lock:
            if self.state != FreeDVTransport.STATE_DISCONNECTED:
                log.warning("FreeDV: already connected/connecting")
                return
            self.state = FreeDVTransport.STATE_CONNECTING

        self._set_state(FreeDVTransport.STATE_CONNECTING)
        log.info("FreeDV: CONN_REQ → %s", target_call)

        threading.Thread(target=self._connect_thread,
                         args=(target_call.upper(),),
                         daemon=True, name="freedv-connect").start()

    def vara_disconnect(self):
        """Send DISC and wait briefly for DISC_ACK, then go DISCONNECTED."""
        with self._lock:
            if self.state == FreeDVTransport.STATE_DISCONNECTED:
                return
            remote = self.remote_call or ""

        log.info("FreeDV: sending DISC to %s", remote)
        self._send_packet(PKT_DISC, remote, b"")
        # Give remote 3s to ACK before forcing disconnect
        time.sleep(3)
        self._go_disconnected()

    def send_data(self, data: bytes) -> bool:
        """
        Send data with ARQ (5 retries, 60s timeout per attempt).
        data should be [2-byte prefix][wire] as built by _ardop_send_framed.
        Returns True if ACKed, False if all retries exhausted.
        """
        if self.state != FreeDVTransport.STATE_CONNECTED:
            log.warning("FreeDV: send_data called while not connected")
            return False

        with self._send_lock:
            seq = self._arq_seq
            self._arq_seq = (self._arq_seq + 1) & 0xFF

            body = bytes([seq]) + data
            remote = self.remote_call or ""

            for attempt in range(1, ARQ_MAX_RETRIES + 1):
                self._arq_event.clear()
                self._arq_acked  = False
                self._arq_nak    = False
                self._arq_seq_sent = seq

                log.info("FreeDV: DATA seq=%d attempt=%d/%d (%d bytes)",
                         seq, attempt, ARQ_MAX_RETRIES, len(data))
                self._send_packet(PKT_DATA, remote, body)

                got = self._arq_event.wait(timeout=ARQ_TIMEOUT)
                if got and self._arq_acked:
                    log.info("FreeDV: DATA seq=%d ACKed", seq)
                    return True
                if got and self._arq_nak:
                    log.warning("FreeDV: DATA seq=%d NAKed — retry", seq)
                    continue
                log.warning("FreeDV: DATA seq=%d timeout attempt %d",
                            seq, attempt)

            log.error("FreeDV: DATA seq=%d failed after %d retries",
                      seq, ARQ_MAX_RETRIES)
            # Link is dead — disconnect
            self._go_disconnected()
            return False

    def send_announce(self, text: str) -> bool:
        """
        Broadcast an announce message to all listening stations.
        No connection or ARQ — single transmission.
        """
        if not self._sock:
            return False
        body = text.encode("utf-8", errors="replace")
        log.info("FreeDV: ANNOUNCE (%d bytes)", len(body))
        self._send_packet(PKT_ANNOUNCE, BROADCAST, body)
        return True

    def close(self):
        """Shut down the transport."""
        self.running = False
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        self._go_disconnected()

    # ── Internal ────────────────────────────────────────────────────

    def _connect_thread(self, target_call: str):
        """Send CONN_REQ and wait for CONN_ACK / CONN_REJ."""
        deadline = time.time() + ARQ_TIMEOUT
        attempt  = 0

        while time.time() < deadline:
            attempt += 1
            log.info("FreeDV: CONN_REQ → %s (attempt %d)", target_call, attempt)
            self._send_packet(PKT_CONN_REQ, target_call, b"")

            # Poll for state change with 5s between retries
            waited = 0
            while waited < 10 and time.time() < deadline:
                with self._lock:
                    st = self.state
                if st == FreeDVTransport.STATE_CONNECTED:
                    return   # ACK received by _handle_packet
                if st == FreeDVTransport.STATE_DISCONNECTED:
                    return   # REJ received
                time.sleep(0.5)
                waited += 0.5

        log.error("FreeDV: CONN_REQ to %s timed out", target_call)
        self._go_disconnected()

    def _send_packet(self, pkt_type: int, dst: str, body: bytes):
        """Pack and KISS-encode a packet, then send to freedvtnc2."""
        pkt = _pack(pkt_type, self.mycall, dst, body)
        frame = _kiss_encode(pkt)
        try:
            with self._lock:
                sock = self._sock
            if sock:
                sock.sendall(frame)
        except Exception as e:
            log.error("FreeDV: send error: %s", e)

    def _reader(self):
        """Background thread: read KISS frames from freedvtnc2."""
        log.info("FreeDV: reader started")
        while self.running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    log.warning("FreeDV: KISS socket closed")
                    break
                self._rx_buf.extend(chunk)
                for payload in _kiss_decode(self._rx_buf):
                    self._handle_packet(payload)
            except OSError:
                if self.running:
                    log.error("FreeDV: reader socket error")
                break
            except Exception as e:
                log.error("FreeDV: reader error: %s", e, exc_info=True)
        log.info("FreeDV: reader exited")
        if self.running:
            self._go_disconnected()

    def _handle_packet(self, data: bytes):
        """Dispatch a received KISS payload."""
        parsed = _unpack(data)
        if parsed is None:
            return   # Not a HF-256 packet — ignore
        pkt_type, src, dst, body = parsed
        src = src.upper()
        dst = dst.upper()

        # ANNOUNCE — deliver to everyone regardless of dst
        if pkt_type == PKT_ANNOUNCE:
            log.info("FreeDV: ANNOUNCE from %s", src)
            if self.on_announce_received:
                try:
                    self.on_announce_received(src,
                        body.decode("utf-8", errors="replace"))
                except Exception as e:
                    log.error("FreeDV: on_announce_received error: %s", e)
            return

        # All other packets: check if we are the intended destination
        if dst != self.mycall and dst != BROADCAST:
            log.debug("FreeDV: pkt for %s — ignoring (we are %s)",
                      dst, self.mycall)
            return

        log.info("FreeDV: pkt type=0x%02x from=%s", pkt_type, src)

        if pkt_type == PKT_CONN_REQ:
            self._handle_conn_req(src)

        elif pkt_type == PKT_CONN_ACK:
            with self._lock:
                if self.state == FreeDVTransport.STATE_CONNECTING:
                    self.remote_call = src
            self._set_state(FreeDVTransport.STATE_CONNECTED)

        elif pkt_type == PKT_CONN_REJ:
            log.warning("FreeDV: CONN_REJ from %s", src)
            self._go_disconnected()

        elif pkt_type == PKT_DATA:
            self._handle_data(src, body)

        elif pkt_type == PKT_DATA_ACK:
            if body:
                seq = body[0]
                if seq == self._arq_seq_sent:
                    self._arq_acked = True
                    self._arq_event.set()

        elif pkt_type == PKT_DATA_NAK:
            if body:
                seq = body[0]
                if seq == self._arq_seq_sent:
                    self._arq_nak = True
                    self._arq_event.set()

        elif pkt_type == PKT_DISC:
            log.info("FreeDV: DISC from %s", src)
            self._send_packet(PKT_DISC_ACK, src, b"")
            self._go_disconnected()

        elif pkt_type == PKT_DISC_ACK:
            log.info("FreeDV: DISC_ACK from %s", src)
            self._go_disconnected()

    def _handle_conn_req(self, src: str):
        """Handle incoming connection request."""
        with self._lock:
            busy = (self.state != FreeDVTransport.STATE_DISCONNECTED)

        if busy:
            log.info("FreeDV: CONN_REQ from %s — rejecting (busy)", src)
            self._send_packet(PKT_CONN_REJ, src, b"")
            return

        log.info("FreeDV: CONN_REQ from %s — accepting", src)
        self._send_packet(PKT_CONN_ACK, src, b"")
        with self._lock:
            self.remote_call = src
        self._set_state(FreeDVTransport.STATE_CONNECTED)

    def _handle_data(self, src: str, body: bytes):
        """Receive a DATA packet, send ACK, deliver payload."""
        if not body:
            return
        seq     = body[0]
        payload = body[1:]

        # Send ACK
        self._send_packet(PKT_DATA_ACK, src, bytes([seq]))
        log.info("FreeDV: DATA seq=%d from %s (%d bytes) — ACKed",
                 seq, src, len(payload))

        if self.on_message_received:
            try:
                self.on_message_received(payload)
            except Exception as e:
                log.error("FreeDV: on_message_received error: %s", e)

    def _go_disconnected(self):
        """Force state to DISCONNECTED."""
        with self._lock:
            old = self.state
            self.state       = FreeDVTransport.STATE_DISCONNECTED
            self.remote_call = None
        if old != FreeDVTransport.STATE_DISCONNECTED:
            self._arq_event.set()   # unblock any waiting ARQ send
            if self.on_state_change:
                try:
                    self.on_state_change(old,
                                         FreeDVTransport.STATE_DISCONNECTED)
                except Exception as e:
                    log.error("FreeDV: on_state_change error: %s", e)

    def _set_state(self, new_state: int):
        """Update state and fire callback."""
        with self._lock:
            old        = self.state
            self.state = new_state
        if old != new_state and self.on_state_change:
            try:
                self.on_state_change(old, new_state)
            except Exception as e:
                log.error("FreeDV: on_state_change error: %s", e)
