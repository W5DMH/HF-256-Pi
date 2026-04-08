"""
Mercury HF Modem Transport for HF-256
Implements the VARA-compatible TCP TNC interface of Mercury v2.

Mercury v2 (by Rhizomatica / HERMES project) exposes two TCP ports:
  Control port  (default 8300) — CR-terminated ASCII commands + async status
  Data port     (default 8301) — Raw application payload (no framing)
  Broadcast     (default 8100) — KISS-framed broadcast (separate, not used here)

Commands are \r-terminated.  Async responses arrive on the control port
at any time (not just in response to commands).

Session flow:
  1. Connect TCP to control port (8300) and data port (8301)
  2. TX: MYCALL <call>\r        → RX: OK\r
  3. TX: BW2300\r               → RX: OK\r
  4. TX: LISTEN ON\r            → RX: OK\r
  5. Async RX on ctrl: CONNECTED <src> <dst> <bw>\r when linked
  6. Outgoing call: CONNECT <mycall> <theircall>\r
  7. Data: raw bytes in/out on data port (our app adds 2-byte length prefix)
  8. Teardown: DISCONNECT\r → async DISCONNECTED\r

Async responses on control port:
  CONNECTED <src> <dst> <bw>\r
  DISCONNECTED\r
  PTT ON\r / PTT OFF\r
  PENDING\r / CANCELPENDING\r
  BUFFER <bytes>\r          — TX buffer fill
  SN <value>\r              — SNR in dB
  BITRATE (<level>) <bps> BPS\r
  IAMALIVE\r                — heartbeat (idle keepalive)
  CQFRAME <src> <bw>\r      — decoded CQ frame

Public interface mirrors ARDOPConnection exactly so ConsoleSession can
use MercuryTransport as a drop-in replacement.

References:
  https://github.com/Rhizomatica/mercury  (README.md, TNC.md, ARQ.md)
"""

import socket
import threading
import time
import logging
from typing import Optional, Callable

log = logging.getLogger("hf256.mercury")


class MercuryTransport:
    """
    Mercury HF modem transport — VARA-compatible TCP TNC interface.

    External API is identical to ARDOPConnection so ConsoleSession
    can swap it in without changes to the message-handling logic.

    Data-port framing note
    ----------------------
    Mercury's data port is unframed (raw bytes).  The HF-256 hub wire
    format uses a 2-byte big-endian length prefix so both ends can
    reassemble complete messages.  ConsoleSession wraps send_data() with
    _mercury_send_framed() (same pattern as ARDOP) and _on_mercury_message()
    buffers incoming bytes and strips those prefixes before dispatching.
    """

    STATE_DISCONNECTED = 0
    STATE_CONNECTING   = 1
    STATE_CONNECTED    = 2

    # Seconds of bidirectional link silence before the watchdog sends DISCONNECT.
    # The timer measures max(last_rx, last_tx): activity in EITHER direction
    # resets the clock, because on a half-duplex HF link we cannot receive
    # while we are transmitting.
    #
    # 120 s minimum timing budget for a Mercury auth session:
    #   ~16 s: CONNECTED → user submits /auth (UI interaction)
    #   ~16 s: AUTH_REQ delivery (DATAC4 ARQ, 77 bytes at 87 bps)
    #   ~30 s: Hub processes + queues AUTH_RSP + ARQ delivery back to spoke
    #   ─────
    #   ~62 s worst-case at good SNR; more at poor SNR with ARQ retries.
    # 60 s was too short — watchdog fired mid-auth before AUTH_RSP arrived.
    # IAMALIVE heartbeats on the control port do NOT reset the timer.
    DEFAULT_INACTIVITY_TIMEOUT = 120

    # Seconds to keep Mercury's TCP sockets open after a DISCONNECT so Mercury
    # can transmit over-air DISCONNECT control frames (DATAC13) to the remote.
    # Mercury signals DISCONNECTED locally the instant it accepts the DISCONNECT
    # command — it hasn't PTT'd yet.  Closing immediately prevents the remote
    # from ever receiving the disconnect notification.
    # 5 s ≈ 1 complete DATAC13 frame transmission: enough to reach the remote
    # on a live link, short enough to avoid the "endless PTT" problem.
    _DISCONNECT_DRAIN_S = 5.0

    def __init__(self,
                 mycall: str,
                 host: str = "127.0.0.1",
                 ctrl_port: int = 8300,
                 data_port: int = 8301):
        """
        Parameters
        ----------
        mycall    : local station callsign (max 15 chars, per TNC.md MYCALL)
        host      : Mercury process host (almost always 127.0.0.1)
        ctrl_port : Mercury ARQ control port (default 8300)
        data_port : Mercury ARQ data port (default ctrl_port + 1 = 8301)
        """
        self.mycall    = mycall.strip().upper()[:15]
        self.host      = host
        self.ctrl_port = ctrl_port
        self.data_port = data_port

        # ── State ────────────────────────────────────────────────
        self.state:       int           = MercuryTransport.STATE_DISCONNECTED
        self.remote_call: Optional[str] = None
        self.running:     bool          = False

        # Telemetry (updated by control-port reader, readable by caller)
        self.buffer_size: int            = 0       # TX bytes in Mercury queue
        self.snr:         Optional[float] = None   # last SN <value> in dB
        self.bitrate_bps: Optional[int]  = None   # last BITRATE in bps
        self.bandwidth:   Optional[str]  = None   # negotiated BW token (500/2300/2750)

        # Inactivity watchdog
        self.inactivity_timeout = MercuryTransport.DEFAULT_INACTIVITY_TIMEOUT
        self._last_rx_time      = 0.0
        self._last_tx_time      = 0.0
        self._connect_time      = 0.0  # timestamp of CONNECTED event

        # ── Callbacks (same names as ARDOPConnection) ─────────────
        self.on_state_change:     Optional[Callable] = None  # (old:int, new:int)
        self.on_message_received: Optional[Callable] = None  # (data:bytes)
        self.on_ptt_change:       Optional[Callable] = None  # (keyed:bool)
        # Optional extra telemetry callbacks
        self.on_snr_update:    Optional[Callable] = None  # (snr_db:float)
        self.on_buffer_update: Optional[Callable] = None  # (bytes:int)

        # ── Sockets ───────────────────────────────────────────────
        self._ctrl_sock: Optional[socket.socket] = None
        self._data_sock: Optional[socket.socket] = None

        # ── Locks ─────────────────────────────────────────────────
        self._lock      = threading.Lock()  # state / socket references
        self._ctrl_lock = threading.Lock()  # serialise ctrl socket writes
        self._send_lock = threading.Lock()  # serialise data socket writes

        # ── Threads ───────────────────────────────────────────────
        self._ctrl_thread: Optional[threading.Thread] = None
        self._data_thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Open control and data sockets to Mercury, start reader threads,
        then send MYCALL / BW2300 / LISTEN ON.

        Returns True on success, False if Mercury is not reachable.
        """
        try:
            log.info("Mercury: connecting %s → %s ctrl=%d data=%d",
                     self.mycall, self.host, self.ctrl_port, self.data_port)

            # ── Control socket ────────────────────────────────────
            ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            ctrl.settimeout(5.0)
            ctrl.connect((self.host, self.ctrl_port))
            ctrl.settimeout(0.25)     # short poll for reader loop
            log.info("Mercury: control socket open (%s:%d)", self.host, self.ctrl_port)

            # ── Data socket ───────────────────────────────────────
            data = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            data.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            data.settimeout(5.0)
            data.connect((self.host, self.data_port))
            data.settimeout(None)     # blocking read — reader thread handles it
            log.info("Mercury: data socket open (%s:%d)", self.host, self.data_port)

            with self._lock:
                self._ctrl_sock = ctrl
                self._data_sock = data
                self.running    = True

            # ── Start reader threads ──────────────────────────────
            self._ctrl_thread = threading.Thread(
                target=self._ctrl_reader, daemon=True, name="mercury-ctrl")
            self._data_thread = threading.Thread(
                target=self._data_reader, daemon=True, name="mercury-data")
            self._ctrl_thread.start()
            self._data_thread.start()

            # Brief settle before sending init commands
            time.sleep(0.15)

            # ── Initialisation commands ───────────────────────────
            # Responses (OK/WRONG) arrive asynchronously in the ctrl reader
            # and are logged there; we don't block waiting for them.
            self._send_ctrl("MYCALL {}".format(self.mycall))
            time.sleep(0.1)
            self._send_ctrl("BW2300")
            time.sleep(0.1)

            # Guard: if close() was called concurrently (e.g. WebSocket
            # closed while init was in progress), the ctrl socket will
            # already be None.  LISTEN ON must be sent for Mercury to
            # accept incoming calls — if we can't send it, treat the
            # whole connect() as failed so the caller does not announce
            # "listener active" when Mercury is actually not listening.
            with self._lock:
                _sock_ok = self._ctrl_sock is not None
            if not _sock_ok:
                log.warning("Mercury: ctrl socket closed before LISTEN ON "
                            "— aborting init (transport was closed mid-init)")
                self._cleanup_sockets()
                return False

            self._send_ctrl("LISTEN ON")
            time.sleep(0.15)   # let OK response land before caller proceeds

            log.info("Mercury: init complete — listening for connections")
            return True

        except ConnectionRefusedError:
            log.error("Mercury: connection refused %s:%d — mercury not running?",
                      self.host, self.ctrl_port)
            self._cleanup_sockets()
            return False
        except socket.timeout:
            log.error("Mercury: connect timeout %s:%d", self.host, self.ctrl_port)
            self._cleanup_sockets()
            return False
        except OSError as e:
            log.error("Mercury: connect OSError: %s", e)
            self._cleanup_sockets()
            return False
        except Exception as e:
            log.error("Mercury: connect error: %s", e, exc_info=True)
            self._cleanup_sockets()
            return False

    def vara_connect(self, target_call: str):
        """
        Initiate an ARQ connection to target_call.

        Mercury transmits CALL frames on DATAC13 and waits for ACCEPT.
        Async notification sequence:
          (nothing until link setup completes)
          CONNECTED <mycall> <target> <bw>\r

        PENDING is sent when an outgoing call is in progress.
        If the call fails or times out, DISCONNECTED is sent.
        """
        with self._lock:
            if self.state != MercuryTransport.STATE_DISCONNECTED:
                log.warning("Mercury: vara_connect while state=%d (ignoring)",
                            self.state)
                return
            old = self.state
            self.state = MercuryTransport.STATE_CONNECTING

        log.info("Mercury: calling %s", target_call.upper())

        if self.on_state_change:
            try:
                self.on_state_change(old, MercuryTransport.STATE_CONNECTING)
            except Exception as e:
                log.error("Mercury: on_state_change error: %s", e)

        self._send_ctrl("CONNECT {} {}".format(self.mycall,
                                               target_call.upper().strip()))

    def vara_disconnect(self):
        """
        Send DISCONNECT to Mercury.  The async DISCONNECTED response will
        trigger the STATE_DISCONNECTED transition via the ctrl reader.
        """
        log.info("Mercury: sending DISCONNECT")
        self._send_ctrl("DISCONNECT")

    def send_data(self, data: bytes) -> bool:
        """
        Write raw bytes to Mercury's data port.
        Mercury handles ARQ segmentation and retransmission internally.

        Returns True on success, False if not connected or socket error.
        Only valid when state == STATE_CONNECTED.
        """
        if self.state != MercuryTransport.STATE_CONNECTED:
            log.warning("Mercury: send_data while not CONNECTED (state=%d)",
                        self.state)
            return False

        with self._lock:
            sock = self._data_sock

        if sock is None:
            log.error("Mercury: send_data — data socket is None")
            return False

        try:
            with self._send_lock:
                sock.sendall(data)
            self._last_tx_time = time.time()
            log.debug("Mercury: sent %d bytes to data port", len(data))
            return True
        except OSError as e:
            log.error("Mercury: send_data OSError: %s", e)
            self._handle_disconnect("data socket write error")
            return False
        except Exception as e:
            log.error("Mercury: send_data error: %s", e, exc_info=True)
            return False

    def send_ctrl_command(self, cmd: str):
        """
        Send an arbitrary command to the Mercury control port.
        Useful for querying telemetry (SN, BITRATE, BUFFER) from the UI.
        Response arrives asynchronously via on_snr_update / on_buffer_update.
        """
        self._send_ctrl(cmd)

    def close(self):
        """
        Cleanly shut down — stop reader threads and close both sockets.
        Sets state to DISCONNECTED but does NOT fire on_state_change
        (caller is responsible for state tracking after close).
        """
        log.info("Mercury: close() called (state=%d)", self.state)
        self.running = False
        with self._lock:
            self.state = MercuryTransport.STATE_DISCONNECTED
        self._cleanup_sockets()

    # ── Internal helpers ──────────────────────────────────────────

    def _send_ctrl(self, cmd: str):
        """
        Send a CR-terminated command on the control socket.
        Thread-safe via _ctrl_lock.
        """
        with self._lock:
            sock = self._ctrl_sock
        if sock is None:
            log.warning("Mercury: _send_ctrl — no ctrl socket (cmd=%s)", cmd)
            return
        try:
            with self._ctrl_lock:
                sock.sendall((cmd + "\r").encode("ascii"))
            log.info("Mercury TX: %s", cmd)
        except OSError as e:
            log.error("Mercury: ctrl send OSError: %s (cmd=%s)", e, cmd)
        except Exception as e:
            log.error("Mercury: ctrl send error: %s (cmd=%s)", e, cmd)

    def _ctrl_reader(self):
        """
        Background thread: reads CR-terminated lines from the control socket
        and dispatches each complete line to _process_ctrl_line().
        """
        log.info("Mercury: ctrl reader started")
        buf = b""

        while self.running:
            with self._lock:
                sock = self._ctrl_sock
            if sock is None:
                break

            try:
                chunk = sock.recv(1024)
                if not chunk:
                    log.warning("Mercury: ctrl socket EOF")
                    if self.running:
                        self._handle_disconnect("ctrl socket EOF")
                    break
                buf += chunk

                # Dispatch all complete CR-terminated lines
                while b"\r" in buf:
                    line_b, buf = buf.split(b"\r", 1)
                    text = line_b.decode("ascii", errors="ignore").strip()
                    if text:
                        try:
                            self._process_ctrl_line(text)
                        except Exception as e:
                            log.error("Mercury: _process_ctrl_line error: %s",
                                      e, exc_info=True)

            except socket.timeout:
                continue   # expected — short timeout set on ctrl socket
            except OSError as e:
                if self.running:
                    log.error("Mercury: ctrl reader OSError: %s", e)
                    self._handle_disconnect("ctrl reader OSError")
                break
            except Exception as e:
                if self.running:
                    log.error("Mercury: ctrl reader error: %s", e, exc_info=True)
                break

        log.info("Mercury: ctrl reader exited")

    def _data_reader(self):
        """
        Background thread: reads raw bytes from the data socket and
        delivers them to on_message_received.

        Mercury provides no framing on the data port — bytes arrive in
        whatever chunks TCP delivers them.  ConsoleSession's
        _on_mercury_message() buffers and reassembles complete HF-256
        wire messages using the 2-byte length prefix that send_data adds.
        """
        log.info("Mercury: data reader started")

        while self.running:
            with self._lock:
                sock = self._data_sock
            if sock is None:
                break

            try:
                chunk = sock.recv(8192)
                if not chunk:
                    log.info("Mercury: data socket EOF "
                             "(control port will send DISCONNECTED)")
                    break

                self._last_rx_time = time.time()
                log.debug("Mercury: data RX %d bytes", len(chunk))

                if self.on_message_received:
                    try:
                        self.on_message_received(chunk)
                    except Exception as e:
                        log.error("Mercury: on_message_received error: %s",
                                  e, exc_info=True)

            except OSError as e:
                if self.running:
                    log.error("Mercury: data reader OSError: %s", e)
                break
            except Exception as e:
                if self.running:
                    log.error("Mercury: data reader error: %s", e, exc_info=True)
                break

        log.info("Mercury: data reader exited")

    def _process_ctrl_line(self, line: str):
        """
        Parse one CR-terminated control-port line and update state /
        fire callbacks.  Called from _ctrl_reader — must not block.

        Full response list per TNC.md:
          CONNECTED <src> <dst> <bw>
          DISCONNECTED
          PTT ON / PTT OFF
          PENDING / CANCELPENDING
          BUFFER <bytes>
          SN <value>
          BITRATE (<level>) <bps> BPS
          IAMALIVE
          CQFRAME <src> <bw>
          OK / WRONG   (command acknowledgements)
        """
        log.info("Mercury CTRL: %s", line)

        # ── CONNECTED <src> <dst> <bw> ────────────────────────────
        if line.startswith("CONNECTED"):
            parts = line.split()
            # parts[0]="CONNECTED" parts[1]=src parts[2]=dst parts[3]=bw
            src = parts[1] if len(parts) > 1 else "?"
            dst = parts[2] if len(parts) > 2 else "?"
            bw  = parts[3] if len(parts) > 3 else "?"

            # The station that initiated the call is always src (per TNC.md).
            # If src == mycall we called them; otherwise they called us.
            remote = dst if src.upper() == self.mycall.upper() else src

            with self._lock:
                old_state            = self.state
                self.state           = MercuryTransport.STATE_CONNECTED
                self.remote_call     = remote.upper()
                self.bandwidth       = bw
                self._connect_time   = time.time()
                self._last_rx_time   = time.time()
                self._last_tx_time   = time.time()

            log.info("Mercury: CONNECTED  remote=%s  bw=%s", remote, bw)

            if self.on_state_change:
                try:
                    self.on_state_change(old_state,
                                         MercuryTransport.STATE_CONNECTED)
                except Exception as e:
                    log.error("Mercury: on_state_change CONNECTED error: %s", e)

            # Launch inactivity watchdog for this session
            threading.Thread(target=self._watchdog, daemon=True,
                             name="mercury-watchdog").start()
            return

        # ── DISCONNECTED ─────────────────────────────────────────
        if line.startswith("DISCONNECTED"):
            with self._lock:
                old_state = self.state
                if old_state == MercuryTransport.STATE_DISCONNECTED:
                    # Duplicate DISCONNECTED (Mercury retries or watchdog already
                    # handled it).  The drain thread will close sockets; just return
                    # and let the ctrl reader keep running until then.
                    log.debug("Mercury: DISCONNECTED (already — drain thread "
                              "will close sockets)")
                    return
                self.state       = MercuryTransport.STATE_DISCONNECTED
                self.remote_call = None
                self.bandwidth   = None

            log.info("Mercury: DISCONNECTED")

            if self.on_state_change:
                try:
                    self.on_state_change(old_state,
                                         MercuryTransport.STATE_DISCONNECTED)
                except Exception as e:
                    log.error("Mercury: on_state_change DISCONNECTED error: %s", e)

            # Keep the ctrl reader ALIVE during the drain window.
            #
            # Mercury signals DISCONNECTED locally the instant it accepts our
            # DISCONNECT command — the over-air DISCONNECT frames are queued
            # but not yet transmitted.  If we set running=False here the ctrl
            # reader exits and we lose visibility of PTT ON/OFF during the
            # over-air exchange.  More importantly, an unread ctrl socket can
            # back-pressure Mercury's status writes.
            #
            # The drain thread sets running=False just before closing sockets.
            # This causes the ctrl reader to hit OSError on its next recv(),
            # see running=False, and exit cleanly with no error logged.
            # app.py's on_state_change callback handles hub reconnect by calling
            # _start_mercury_listener() — no need to send LISTEN ON here.
            def _drain_then_close(_self=self):
                time.sleep(MercuryTransport._DISCONNECT_DRAIN_S)
                log.info("Mercury: disconnect drain complete — closing TCP sockets")
                _self.running = False   # signal ctrl reader to exit cleanly
                _self._cleanup_sockets()

            threading.Thread(target=_drain_then_close, daemon=True,
                             name="mercury-disconnect-drain").start()
            log.info("Mercury: DISCONNECTED — drain window %.0fs "
                     "(ctrl reader active so PTT is logged)",
                     MercuryTransport._DISCONNECT_DRAIN_S)
            return

        # ── PTT ON / PTT OFF ─────────────────────────────────────
        if line == "PTT ON":
            if self.on_ptt_change:
                try:
                    self.on_ptt_change(True)
                except Exception as e:
                    log.error("Mercury: on_ptt_change PTT ON error: %s", e)
            return

        if line == "PTT OFF":
            if self.on_ptt_change:
                try:
                    self.on_ptt_change(False)
                except Exception as e:
                    log.error("Mercury: on_ptt_change PTT OFF error: %s", e)
            return

        # ── PENDING ───────────────────────────────────────────────
        # Incoming call request detected or outgoing CQ began transmitting.
        if line == "PENDING":
            with self._lock:
                old_state = self.state
                # Only advance to CONNECTING from DISCONNECTED; do not
                # overwrite CONNECTED or already-CONNECTING state.
                if old_state == MercuryTransport.STATE_DISCONNECTED:
                    self.state = MercuryTransport.STATE_CONNECTING
                new_state = self.state

            if old_state != new_state and self.on_state_change:
                try:
                    self.on_state_change(old_state, new_state)
                except Exception as e:
                    log.error("Mercury: on_state_change PENDING error: %s", e)
            return

        # ── CANCELPENDING ─────────────────────────────────────────
        # Incoming call attempt failed or outgoing CQ TX finished.
        if line == "CANCELPENDING":
            with self._lock:
                old_state = self.state
                if old_state == MercuryTransport.STATE_CONNECTING:
                    self.state       = MercuryTransport.STATE_DISCONNECTED
                    self.remote_call = None
                new_state = self.state

            if old_state != new_state and self.on_state_change:
                try:
                    self.on_state_change(old_state, new_state)
                except Exception as e:
                    log.error("Mercury: on_state_change CANCELPENDING error: %s", e)
            return

        # ── BUFFER <bytes> ────────────────────────────────────────
        if line.startswith("BUFFER"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    self.buffer_size = int(parts[1])
                    if self.on_buffer_update:
                        self.on_buffer_update(self.buffer_size)
                except (ValueError, IndexError):
                    pass
            return

        # ── SN <value> ────────────────────────────────────────────
        if line.startswith("SN "):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    self.snr = float(parts[1])
                    if self.on_snr_update:
                        self.on_snr_update(self.snr)
                except (ValueError, IndexError):
                    pass
            return

        # ── BITRATE (<level>) <bps> BPS ──────────────────────────
        if line.startswith("BITRATE"):
            # Format: "BITRATE (2) 490 BPS"
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "BPS" and i >= 1:
                    try:
                        self.bitrate_bps = int(parts[i - 1])
                    except (ValueError, IndexError):
                        pass
                    break
            return

        # ── IAMALIVE ─────────────────────────────────────────────
        # Periodic heartbeat — proves control socket is alive.
        # Does NOT update _last_rx_time (only data activity does that).
        if line == "IAMALIVE":
            log.debug("Mercury: IAMALIVE heartbeat")
            return

        # ── CQFRAME <src> <bw> ────────────────────────────────────
        if line.startswith("CQFRAME"):
            log.info("Mercury: heard CQ: %s", line)
            return

        # ── OK / WRONG ────────────────────────────────────────────
        if line in ("OK", "WRONG"):
            log.debug("Mercury: cmd ack: %s", line)
            return

        # Unknown / unhandled
        log.debug("Mercury: unhandled ctrl line: %r", line)

    def _handle_disconnect(self, reason: str = ""):
        """
        Force STATE_DISCONNECTED on an unexpected socket error and fire
        on_state_change.  Called from reader threads only.
        """
        log.warning("Mercury: _handle_disconnect — %s", reason)
        with self._lock:
            old_state = self.state
            if old_state == MercuryTransport.STATE_DISCONNECTED:
                return
            self.state       = MercuryTransport.STATE_DISCONNECTED
            self.remote_call = None
            self.bandwidth   = None

        self._cleanup_sockets()

        if self.on_state_change:
            try:
                self.on_state_change(old_state,
                                     MercuryTransport.STATE_DISCONNECTED)
            except Exception as e:
                log.error("Mercury: _handle_disconnect on_state_change: %s", e)

    def _cleanup_sockets(self):
        """Close both sockets.  Safe to call from any thread or state."""
        with self._lock:
            ctrl = self._ctrl_sock
            data = self._data_sock
            self._ctrl_sock = None
            self._data_sock = None

        for sock in (ctrl, data):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _watchdog(self):
        """
        Inactivity watchdog — sends DISCONNECT and transitions to
        STATE_DISCONNECTED if there has been no link activity for
        inactivity_timeout seconds in EITHER direction.

        Half-duplex HF links are asymmetric: while AUTH_REQ or a large
        message is being transmitted (TX active), no data arrives on the
        RX path.  Counting only RX silence caused premature disconnects
        mid-authentication on weak links.  The correct measure of "the
        link is doing something useful" is max(last_rx, last_tx): if
        EITHER end has exchanged data recently, the session is alive.

        IAMALIVE heartbeats on the control port are NOT counted as link
        activity — they only prove the local TCP socket to Mercury is
        open, not that the remote station is reachable.
        """
        log.info("Mercury: watchdog started (inactivity=%ds)",
                 self.inactivity_timeout)

        while True:
            time.sleep(5)

            if self.state != MercuryTransport.STATE_CONNECTED:
                log.info("Mercury: watchdog exiting (state=%d)", self.state)
                return

            if self.inactivity_timeout <= 0:
                continue

            now = time.time()
            # Use the most recent activity in either direction.
            # TX time counts because on a half-duplex link we cannot
            # receive while we are transmitting.
            last_activity = max(self._last_rx_time, self._last_tx_time)

            if last_activity > 0 and now - last_activity > self.inactivity_timeout:
                idle_rx = now - self._last_rx_time if self._last_rx_time > 0 else -1
                idle_tx = now - self._last_tx_time if self._last_tx_time > 0 else -1
                log.warning(
                    "Mercury: inactivity timeout — "
                    "rx_idle=%.0fs tx_idle=%.0fs (limit=%ds) — sending DISCONNECT",
                    idle_rx, idle_tx, self.inactivity_timeout)
                # Set reject_reason before calling on_state_change so the
                # hub console shows a specific "inactivity" message rather
                # than a silent disconnect.  app.py reads _reject_reason
                # from the transport in _on_state_change(2→0).
                self._reject_reason = (
                    "Connection lost — no activity for "
                    f"{int(self.inactivity_timeout)}s "
                    "(remote station may have disconnected silently)")
                try:
                    self._send_ctrl("DISCONNECT")
                except Exception:
                    pass
                with self._lock:
                    old_state        = self.state
                    # Guard: ctrl reader may have already processed DISCONNECTED
                    # and started the drain thread. Let it handle socket cleanup.
                    if old_state == MercuryTransport.STATE_DISCONNECTED:
                        log.debug("Mercury: watchdog — state already DISCONNECTED "
                                  "(drain thread will handle socket close)")
                        return
                    self.state       = MercuryTransport.STATE_DISCONNECTED
                    self.remote_call = None
                    self.bandwidth   = None
                if self.on_state_change:
                    try:
                        self.on_state_change(
                            old_state, MercuryTransport.STATE_DISCONNECTED)
                    except Exception as e:
                        log.error("Mercury: watchdog on_state_change: %s", e)

                # Keep ctrl reader alive during drain so PTT ON/OFF from
                # Mercury's over-air DISCONNECT exchange are logged.
                # running=False is set just before _cleanup_sockets() so the
                # ctrl reader exits cleanly (OSError with running=False →
                # no error logged, just breaks).
                log.info("Mercury: watchdog drain window %.0fs — "
                         "ctrl reader active, waiting for DISCONNECT frames",
                         self._DISCONNECT_DRAIN_S)
                time.sleep(self._DISCONNECT_DRAIN_S)
                log.info("Mercury: watchdog drain complete — closing TCP sockets")
                self.running = False   # signal ctrl reader to exit cleanly
                self._cleanup_sockets()
                return
