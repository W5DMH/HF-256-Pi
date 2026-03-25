"""
HF-256 FreeDV Transport
Session layer over freedvtnc2 KISS interface (port 8001).

freedvtnc2 is a raw packet modem with no connection concept.
This module adds:
  - P2P connection handshake  (CONN_REQ / CONN_ACK / CONN_REJ)
  - Software ARQ              (DATA + DATA_ACK / DATA_NAK, 3 retries, dynamic timeout)
  - Graceful disconnect       (DISC / DISC_ACK)
  - Broadcast announce        (ANNOUNCE — no connection, no ACK)

Packet wire format (inside KISS payload):
  [magic:2][pkt_type:1][src_len:1][src_call][dst_len:1][dst_call][body]

  magic   = 0x48 0x46  ("HF") — identifies HF-256 packets vs other KISS traffic
  pkt_type — see PKT_* constants below
  src/dst — amateur callsigns, 1-9 bytes each (length-prefixed)
  body    — type-specific payload

KISS framing is handled by hf256.kiss (encode / decode).

Fixes applied over previous working file:
  FIX 1 — _handle_packet: _last_rx_time now updated AFTER the destination
           filter, not before. Previously any overheard packet (or freedvtnc2's
           echo of our own TX) reset the inactivity timer, making it impossible
           to time out a dead link. Now only packets genuinely addressed to us
           count as received activity.

  FIX 2 — vara_connect: removed the direct self.state assignment inside the
           lock before calling _set_state(). The direct write caused _set_state
           to see old==new and silently skip firing on_state_change(0→1), so
           the "Connecting..." UI state was never reliably shown.

  FIX 3 — _arq_pending removed: was initialized but never read or written after
           __init__. Removed to eliminate misleading dead code.

  FIX 4 — _watchdog: keepalive now skipped if send_data is in progress
           (_send_lock held). On half-duplex FreeDV, a keepalive transmitted
           while ARQ is waiting for DATA_ACK blocks the return channel and
           causes the ACK to be missed, forcing an unnecessary retry.

  FIX 5 — _send_packet: _last_tx_time write moved inside _lock so it is
           consistent with reads in the watchdog thread.

  FIX 6 — _handle_packet: KEEPALIVE from remote no longer resets _last_rx_time.
           A keepalive proves the link is up but carries no application data;
           resetting the inactivity timer on keepalive would allow the timer to
           be satisfied by our own keepalives being echoed back, which defeats
           its purpose. Real data packets (DATA, CONN_ACK, etc.) still reset it.
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
PKT_KEEPALIVE = 0x0A          # Keepalive ping — no payload, no ACK

# Packet types that count as genuine received activity for the inactivity timer.
# KEEPALIVE is excluded: it only proves the channel is open, not that the remote
# is actively engaged. Counting keepalives would let our own echoed keepalives
# permanently defeat the inactivity timeout.
_ACTIVITY_PKTS = frozenset({
    PKT_CONN_REQ, PKT_CONN_ACK, PKT_CONN_REJ,
    PKT_DATA, PKT_DATA_ACK, PKT_DATA_NAK,
    PKT_DISC, PKT_DISC_ACK,
})

BROADCAST = "*"               # Wildcard destination for announce

# ARQ parameters
ARQ_MAX_RETRIES = 3           # attempts per packet before giving up

# DATAC1 physical layer constants for TX duration estimation.
# Used to compute when our own transmission ends so we can measure
# the ACK wait window from that point rather than from TX start.
_DATAC1_PREAMBLE_S = 3.5     # seconds of preamble per transmission
_DATAC1_BPS        = 980     # raw bit rate

# After our TX ends, how long to wait for the remote's DATA_ACK.
# Derived from observed timing on a real link:
#   freedvtnc2 decode + KISS delivery: ~1.5s (observed from logs)
#   ACK preamble + 20-byte body at 980bps: ~3.7s
#   Total expected ACK round-trip after TX ends: ~5.2s
#   ACK_WINDOW = 7.0s gives 1.8s margin above theoretical minimum.
#
# This is the key fix for dead-air during file transfers:
#   Old design: timeout measured from TX START including TX duration
#               → 80s timeout on a 604-byte chunk → 72s dead air on failure
#   New design: timeout measured from TX END
#               → 7s dead air maximum regardless of packet size
_ACK_WINDOW_S = 7.0

def _tx_duration(data_len: int) -> float:
    """Estimate airtime in seconds for a packet of data_len bytes on DATAC1."""
    return _DATAC1_PREAMBLE_S + (data_len * 8 / _DATAC1_BPS)

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
        self._arq_seq_sent = 0
        self._arq_event    = threading.Event()
        self._reader_done  = threading.Event()  # set when reader thread exits
        self._arq_acked    = False
        self._arq_nak      = False

        # Keepalive / inactivity watchdog
        self.inactivity_timeout  = 120   # seconds of silence → disconnect
        self.keepalive_interval  = 60    # seconds between outgoing pings
        self._last_rx_time       = 0.0   # time of last genuine RX from remote
        self._last_tx_time       = 0.0   # time of last TX (protected by _lock)

        # Callbacks
        self.on_state_change      = None
        self.on_message_received  = None
        self.on_announce_received = None
        self.on_ptt_change        = None  # compat
        # Optional gatekeeper: called with (src_call) before accepting a
        # CONN_REQ. Return True to accept (default), False to reject with
        # CONN_REJ. Allows app layer to refuse connections when hub is busy.
        self.on_conn_req          = None
        # Set to a string by _handle_packet when CONN_REJ received, so the
        # app layer can surface the reason in the UI.
        self._reject_reason       = None

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
            self._reader_done.clear()
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
        Sends CONN_REQ and waits for CONN_ACK (up to CONN_TIMEOUT seconds).
        Fires on_state_change(0→1) immediately, then (1→2) on ACK.
        """
        # FIX 2: Do NOT set self.state directly here before calling _set_state.
        # The previous code set self.state=CONNECTING inside the lock, then
        # called _set_state(CONNECTING) which read old==new and silently skipped
        # firing on_state_change(0→1). The UI "Connecting..." state was never
        # shown. Now we only guard against double-entry, then let _set_state
        # handle the transition and fire the callback correctly.
        with self._lock:
            if self.state != FreeDVTransport.STATE_DISCONNECTED:
                log.warning("FreeDV: already connected/connecting")
                return

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
        Send data with ARQ (ARQ_MAX_RETRIES attempts, post-TX ACK window).
        data should be [2-byte prefix][wire] as built by _ardop_send_framed.
        Returns True if ACKed, False if all attempts exhausted.

        The timeout is measured from the ESTIMATED END OF TRANSMISSION,
        not from TX start. This eliminates dead air during file transfers:

        Old design (broken):
          timeout = 20s + (len * 0.10s) measured from TX START
          For a 604-byte chunk: 80.4s total → 72s of dead air after TX ends

        New design:
          timeout = _ACK_WINDOW_S (7s) measured from estimated TX END
          For a 604-byte chunk: 8.4s TX + 7s window = 15.4s total
          If packet lost: retry after 7s of silence, not 72s

        The ACK window is fixed because the ACK round-trip time after TX
        ends is determined by physics (decode latency + ACK airtime),
        not by the size of the packet we just sent.

        On retry exhaustion returns False but does NOT disconnect.
        The watchdog inactivity timer handles genuinely dead links.
        """
        if self.state != FreeDVTransport.STATE_CONNECTED:
            log.warning("FreeDV: send_data called while not connected")
            return False

        with self._send_lock:
            seq = self._arq_seq
            self._arq_seq = (self._arq_seq + 1) & 0xFF

            body   = bytes([seq]) + data
            remote = self.remote_call or ""
            tx_dur = _tx_duration(len(body))

            for attempt in range(1, ARQ_MAX_RETRIES + 1):
                self._arq_event.clear()
                self._arq_acked    = False
                self._arq_nak      = False
                self._arq_seq_sent = seq

                # Record time just before sending so we can compute TX end.
                t_send = time.time()
                log.info("FreeDV: DATA seq=%d attempt=%d/%d (%d bytes, "
                         "TX~%.1fs + %.1fs ACK window)",
                         seq, attempt, ARQ_MAX_RETRIES,
                         len(data), tx_dur, _ACK_WINDOW_S)
                self._send_packet(PKT_DATA, remote, body)

                # Wait for ACK measured from estimated TX end.
                # If ACK arrives before TX even ends that is fine — wait()
                # returns immediately when the event is set.
                tx_end   = t_send + tx_dur
                deadline = tx_end + _ACK_WINDOW_S
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    if self._arq_event.wait(timeout=remaining):
                        break

                if self._arq_acked:
                    log.info("FreeDV: DATA seq=%d ACKed", seq)
                    return True
                if self._arq_nak:
                    log.warning("FreeDV: DATA seq=%d NAKed — retry", seq)
                    continue
                log.warning("FreeDV: DATA seq=%d no ACK after %.1fs post-TX "
                            "(attempt %d/%d) — retrying",
                            seq, _ACK_WINDOW_S, attempt, ARQ_MAX_RETRIES)

            log.error("FreeDV: DATA seq=%d failed after %d attempts — "
                      "staying connected, watchdog will disconnect if link is dead",
                      seq, ARQ_MAX_RETRIES)
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
            sock = self._sock
            self._sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        self._go_disconnected()

    # ── Internal ────────────────────────────────────────────────────

    def _connect_thread(self, target_call: str):
        """Send CONN_REQ and wait for CONN_ACK / CONN_REJ."""
        CONN_TIMEOUT = 60.0  # seconds to keep trying before giving up
        deadline = time.time() + CONN_TIMEOUT
        attempt  = 0

        while time.time() < deadline:
            attempt += 1
            with self._lock:
                st = self.state
            if st == FreeDVTransport.STATE_CONNECTED:
                return
            if st == FreeDVTransport.STATE_DISCONNECTED:
                return

            log.info("FreeDV: CONN_REQ → %s (attempt %d)", target_call, attempt)
            self._send_packet(PKT_CONN_REQ, target_call, b"")

            # Wait up to 10s for a response before retrying
            waited = 0
            while waited < 10 and time.time() < deadline:
                with self._lock:
                    st = self.state
                if st == FreeDVTransport.STATE_CONNECTED:
                    return
                if st == FreeDVTransport.STATE_DISCONNECTED:
                    return
                time.sleep(0.5)
                waited += 0.5

        log.error("FreeDV: CONN_REQ to %s timed out", target_call)
        self._go_disconnected()

    def _send_packet(self, pkt_type: int, dst: str, body: bytes):
        """Pack and KISS-encode a packet, then send to freedvtnc2."""
        pkt   = _pack(pkt_type, self.mycall, dst, body)
        frame = _kiss_encode(pkt)
        try:
            with self._lock:
                sock = self._sock
                if sock:
                    sock.sendall(frame)
                    # FIX 5: _last_tx_time updated inside the lock so it is
                    # consistent with reads in _watchdog (also lock-protected).
                    self._last_tx_time = time.time()
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
        self._reader_done.set()
        if self.running:
            self._go_disconnected()

    def _handle_packet(self, data: bytes):
        """Dispatch a received KISS payload."""
        parsed = _unpack(data)
        if parsed is None:
            return
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

        # All other packets: must be addressed to us
        if dst != self.mycall and dst != BROADCAST:
            log.debug("FreeDV: pkt for %s — ignoring (we are %s)",
                      dst, self.mycall)
            return

        log.info("FreeDV: pkt type=0x%02x from=%s", pkt_type, src)

        # FIX 1 + FIX 6: _last_rx_time is updated AFTER the destination filter
        # (so echoes of our own TX and foreign-addressed packets don't count),
        # and only for packet types in _ACTIVITY_PKTS (so KEEPALIVE echoes
        # don't silently defeat the inactivity timer either).
        if pkt_type in _ACTIVITY_PKTS:
            self._last_rx_time = time.time()

        if pkt_type == PKT_CONN_REQ:
            self._handle_conn_req(src)

        elif pkt_type == PKT_CONN_ACK:
            _start_watchdog = False
            with self._lock:
                current = self.state
                if current == FreeDVTransport.STATE_CONNECTING:
                    self.remote_call   = src
                    self._last_rx_seq  = {}
                    self._last_rx_time = time.time()
                    self._last_tx_time = time.time()
                    _start_watchdog    = True
                elif current == FreeDVTransport.STATE_CONNECTED:
                    log.debug("FreeDV: duplicate CONN_ACK from %s — ignored", src)
                    return
            self._set_state(FreeDVTransport.STATE_CONNECTED)
            if _start_watchdog:
                threading.Thread(target=self._watchdog, daemon=True,
                                 name="freedv-watchdog").start()

        elif pkt_type == PKT_CONN_REJ:
            log.warning("FreeDV: CONN_REJ from %s — hub busy", src)
            # Flag the reason so the app layer can surface it in the UI
            self._reject_reason = f"Hub busy (CONN_REJ from {src}) — try again later"
            self._go_disconnected()

        elif pkt_type == PKT_DATA:
            self._handle_data(src, body)

        elif pkt_type == PKT_DATA_ACK:
            if body:
                seq = body[0]
                if seq == self._arq_seq_sent and not self._arq_acked:
                    self._arq_acked = True
                    self._arq_event.set()
                else:
                    log.debug("FreeDV: duplicate DATA_ACK seq=%d — ignored", seq)

        elif pkt_type == PKT_DATA_NAK:
            if body:
                seq = body[0]
                if seq == self._arq_seq_sent:
                    self._arq_nak = True
                    self._arq_event.set()

        elif pkt_type == PKT_KEEPALIVE:
            # FIX 6: Do not reset _last_rx_time for KEEPALIVE. A keepalive
            # only proves the channel is open — it is not application activity.
            # Counting it would let echoed keepalives defeat the inactivity timer.
            log.debug("FreeDV: KEEPALIVE from %s", src)

        elif pkt_type == PKT_DISC:
            log.info("FreeDV: DISC from %s", src)
            self._send_packet(PKT_DISC_ACK, src, b"")
            self._go_disconnected()

        elif pkt_type == PKT_DISC_ACK:
            log.info("FreeDV: DISC_ACK from %s", src)
            self._go_disconnected()

    def _watchdog(self):
        """
        Watchdog thread: sends KEEPALIVE pings and disconnects on inactivity.
        Started when a session becomes CONNECTED. Exits when disconnected.
        """
        log.info("FreeDV: watchdog started (keepalive=%ds, inactivity=%ds)",
                 self.keepalive_interval, self.inactivity_timeout)
        while True:
            time.sleep(5)
            with self._lock:
                st = self.state
            if st != FreeDVTransport.STATE_CONNECTED:
                log.info("FreeDV: watchdog exiting (state=%d)", st)
                return

            now = time.time()
            with self._lock:
                remote        = self.remote_call
                last_tx_time  = self._last_tx_time

            # FIX 4: Try to acquire _send_lock before transmitting keepalive.
            # If send_data is holding _send_lock (mid-ARQ), skip the keepalive
            # this cycle rather than colliding on the half-duplex channel.
            # The watchdog checks every 5s so the keepalive will be sent on
            # the next cycle once the ARQ exchange completes.
            if (self.keepalive_interval > 0 and remote and
                    now - last_tx_time > self.keepalive_interval):
                if self._send_lock.acquire(blocking=False):
                    try:
                        log.debug("FreeDV: sending KEEPALIVE to %s", remote)
                        self._send_packet(PKT_KEEPALIVE, remote, b"")
                    finally:
                        self._send_lock.release()
                else:
                    log.debug("FreeDV: KEEPALIVE skipped — ARQ send in progress")

            # Disconnect on inactivity — only genuine received packets count
            # (_last_rx_time is only updated for packets in _ACTIVITY_PKTS
            # that pass the destination filter, so echoed TX and keepalives
            # cannot prevent this from firing).
            if (self.inactivity_timeout > 0 and
                    self._last_rx_time > 0 and
                    now - self._last_rx_time > self.inactivity_timeout):
                log.warning(
                    "FreeDV: inactivity timeout (%.0fs) — disconnecting",
                    now - self._last_rx_time
                )
                if remote:
                    try:
                        self._send_packet(PKT_DISC, remote, b"")
                    except Exception:
                        pass
                self._go_disconnected()
                return

    def _handle_conn_req(self, src: str):
        """Handle incoming connection request."""
        with self._lock:
            state       = self.state
            remote_call = self.remote_call

        if state == FreeDVTransport.STATE_CONNECTING:
            log.info("FreeDV: CONN_REQ from %s — rejecting (connecting)", src)
            self._send_packet(PKT_CONN_REJ, src, b"")
            return

        if state == FreeDVTransport.STATE_CONNECTED:
            if remote_call and remote_call.upper() == src.upper():
                # Same station retrying — our CONN_ACK was lost over the air
                log.info("FreeDV: CONN_REQ retry from %s — resending CONN_ACK",
                         src)
                self._send_packet(PKT_CONN_ACK, src, b"")
                return
            else:
                # Different station — previous link dropped without DISC
                log.info("FreeDV: CONN_REQ from %s — previous link dropped, "
                         "accepting (was connected to %s)", src, remote_call)
                with self._lock:
                    self.remote_call = None
                    self.state = FreeDVTransport.STATE_DISCONNECTED

        # Consult the app-layer gatekeeper before accepting.
        # If it returns False, send CONN_REJ and stop — the caller gets
        # a clean rejection without us ever going to CONNECTED state.
        if self.on_conn_req is not None:
            try:
                if not self.on_conn_req(src):
                    log.info("FreeDV: CONN_REQ from %s — rejected by app (hub busy)",
                             src)
                    self._send_packet(PKT_CONN_REJ, src, b"")
                    return
            except Exception as _e:
                log.error("FreeDV: on_conn_req callback error: %s", _e)

        log.info("FreeDV: CONN_REQ from %s — accepting", src)
        self._send_packet(PKT_CONN_ACK, src, b"")
        self._last_rx_time = time.time()
        with self._lock:
            self._last_tx_time = time.time()
            self.remote_call   = src
            self._last_rx_seq  = {}
        self._set_state(FreeDVTransport.STATE_CONNECTED)
        threading.Thread(target=self._watchdog, daemon=True,
                         name="freedv-watchdog").start()

    def _handle_data(self, src: str, body: bytes):
        """Receive a DATA packet, send ACK, deliver payload."""
        if not body:
            return
        seq     = body[0]
        payload = body[1:]

        # Always ACK immediately — the sender needs this even if we already processed it.
        # No artificial delay: the sender now uses a post-TX ACK window (_ACK_WINDOW_S)
        # measured from estimated TX end. If our ACK arrives during the sender's TX
        # it will be ignored and the window expires naturally; the sender retries
        # within 7s rather than 80s. Speed of ACK is therefore always correct.
        self._send_packet(PKT_DATA_ACK, src, bytes([seq]))

        # Deduplicate: if we already delivered this seq, don't deliver again
        last_seq = getattr(self, "_last_rx_seq", {})
        key = src.upper()
        if last_seq.get(key) == seq:
            log.debug("FreeDV: duplicate DATA seq=%d from %s — ACKed, not redelivered",
                      seq, src)
            return
        last_seq[key] = seq
        self._last_rx_seq = last_seq

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
