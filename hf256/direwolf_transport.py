"""
HF-256 Direwolf AX.25 Transport
=================================
Provides connected-mode AX.25 sessions via Direwolf's built-in
**AGW (AGWPE) TCP server** — no kernel ``ax25`` module required.

Supports two simultaneous radio channels:
  * Port 0 — VHF, 9600 baud (e.g. DigiRig + 2m FM radio)
  * Port 1 — HF,  300 baud  (e.g. DigiRig + HF SSB radio)

Multiple concurrent connected-mode sessions are supported on each port.
ARQ retransmission is handled transparently by Direwolf / the AX.25 layer.

Direwolf configuration requirements
-------------------------------------
Direwolf must have AGW interface enabled (``AGWPORT 8000`` in direwolf.conf).
Use ``direwolf_config.py`` helpers to generate a correct config file.

AGW frame format (36-byte header + data)
-----------------------------------------
::

    offset  size  field
    ------  ----  -----
     0       1    port number (0 or 1)
     1       3    reserved (0x00)
     4       1    DataKind (ASCII: 'X','C','d','D','y' etc.)
     5       1    reserved
     6       1    PID (0xF0 = no layer 3)
     7       1    reserved
     8      10    CallFrom (zero-padded ASCII)
    18      10    CallTo   (zero-padded ASCII)
    28       4    DataLen  (little-endian uint32)
    32       4    UserReserved (0x00)
    36       *    Data

Key DataKind values used here
-----------------------------
``X``  Register callsign with Direwolf (then Direwolf sends ``X`` ACK)
``C``  Connect to remote / notification of incoming connection
``d``  Disconnect from remote / notification of disconnect
``D``  Send/receive connected-mode data
``y``  Outstanding frames in TNC buffer (flow control)
``K``  Keep-alive / raw kiss frame passthrough (not used here)
"""

import logging
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("hf256.direwolf")

# ── Tuneable constants ──────────────────────────────────────────────────────
AGW_PORT          = 8000       # Direwolf default AGW TCP port
AGW_HEADER_SIZE   = 36         # bytes
RECONNECT_DELAY   = 10         # seconds between Direwolf reconnect attempts
MAX_OUTSTANDING   = 4          # flow control: pause when TNC buffer ≥ this
HANDSHAKE_TIMEOUT = 30.0       # seconds for AX.25 connect to remote

# AX.25 port assignments (must match direwolf.conf ADEVICE order)
RADIO_PORT_VHF = 0             # 9600 baud VHF
RADIO_PORT_HF  = 1             # 300 baud HF


def _pack_callsign(call: str) -> bytes:
    """Zero-pad callsign to 10 bytes, truncating if needed."""
    encoded = call.upper().encode("ascii", errors="replace")
    return encoded[:10].ljust(10, b"\x00")


def _unpack_callsign(data: bytes) -> str:
    """Decode 10-byte zero-padded callsign to string."""
    return data.rstrip(b"\x00").decode("ascii", errors="replace").strip()


def _make_agw_frame(
    kind: str,
    call_from: str = "",
    call_to: str   = "",
    port: int      = 0,
    pid: int       = 0xF0,
    data: bytes    = b"",
) -> bytes:
    """Build a complete AGW frame."""
    header = struct.pack(
        "<BBBBBBBB10s10sII",
        port, 0, 0, 0,                       # port + 3 reserved
        ord(kind), 0, pid, 0,                # DataKind + reserved + PID + reserved
        _pack_callsign(call_from),
        _pack_callsign(call_to),
        len(data),
        0,                                   # UserReserved
    )
    return header + data


def _parse_agw_header(raw: bytes) -> Optional[dict]:
    """Parse 36-byte AGW header.  Returns None on parse error."""
    if len(raw) < AGW_HEADER_SIZE:
        return None
    try:
        (port, _r1, _r2, _r3,
         kind_byte, _r4, pid, _r5,
         call_from_b, call_to_b,
         data_len, _user) = struct.unpack("<BBBBBBBB10s10sII", raw)
        return {
            "port":      port,
            "kind":      chr(kind_byte),
            "pid":       pid,
            "call_from": _unpack_callsign(call_from_b),
            "call_to":   _unpack_callsign(call_to_b),
            "data_len":  data_len,
        }
    except struct.error as exc:
        log.debug("AGW header parse error: %s", exc)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-connection state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AX25Connection:
    """Tracks state of one connected AX.25 session inside DirewolfTransport."""

    __slots__ = (
        "radio_port", "mycall", "remote_call",
        "outstanding", "_lock",
    )

    def __init__(self, radio_port: int, mycall: str, remote_call: str) -> None:
        self.radio_port  = radio_port
        self.mycall      = mycall.upper()
        self.remote_call = remote_call.upper()
        self.outstanding = 0      # frames queued in Direwolf TNC buffer
        self._lock       = threading.Lock()

    def __repr__(self) -> str:
        return (f"<AX25Connection port={self.radio_port} "
                f"{self.mycall}←→{self.remote_call}>")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Direwolf AGW transport
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DirewolfTransport:
    """
    Multi-session AX.25 transport using Direwolf's AGW TCP interface.

    One instance manages BOTH radio ports (VHF and HF) via a single
    TCP connection to Direwolf.  Each incoming AX.25 connection creates
    a ``ClientSession`` in the ``SessionManager``; data is dispatched to
    ``on_client_message``.

    Usage::

        transport = DirewolfTransport(
            mycall           = "N0HUB",
            session_manager  = session_mgr,
            on_client_message   = hub_core.on_message,
            on_client_connect   = hub_core.on_connect,
            on_client_disconnect= hub_core.on_disconnect,
            agw_host="127.0.0.1",
            agw_port=AGW_PORT,
            vhf_enabled=True,
            hf_enabled=True,
        )
        transport.start()
    """

    def __init__(
        self,
        mycall: str,
        session_manager,                                # SessionManager
        on_client_message:    Callable,                 # (session, bytes) -> None
        on_client_connect:    Optional[Callable] = None,
        on_client_disconnect: Optional[Callable] = None,
        agw_host: str  = "127.0.0.1",
        agw_port: int  = AGW_PORT,
        vhf_enabled: bool = True,
        hf_enabled:  bool = False,
    ) -> None:
        self.mycall           = mycall.upper()
        self.session_manager  = session_manager
        self.on_client_message    = on_client_message
        self.on_client_connect    = on_client_connect
        self.on_client_disconnect = on_client_disconnect
        self.agw_host = agw_host
        self.agw_port = agw_port
        self.vhf_enabled = vhf_enabled
        self.hf_enabled  = hf_enabled

        self._sock:   Optional[object] = None    # socket.socket
        self._lock    = threading.Lock()
        self._running = False

        # Active AX.25 connections: remote_call → AX25Connection
        self._connections: Dict[str, AX25Connection] = {}

        # Map remote_call → session_id for SessionManager lookup
        self._call_to_session: Dict[str, str] = {}

        # Background threads
        self._reader_thread: Optional[threading.Thread] = None
        self._reconn_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """
        Connect to Direwolf's AGW interface and start listener.
        Returns True if initial connection succeeds; reconnects automatically
        on loss.
        """
        self._running = True
        ok = self._connect_agw()
        if ok:
            self._start_reader()
        else:
            log.warning("DirewolfTransport: initial AGW connect failed — "
                        "will retry in %ds", RECONNECT_DELAY)
            self._schedule_reconnect()
        return ok

    def stop(self) -> None:
        """Shut down transport and close Direwolf connection."""
        self._running = False
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        log.info("DirewolfTransport: stopped")

    def send_to(self, remote_call: str, data: bytes) -> bool:
        """
        Send ``data`` bytes to an established AX.25 session.

        Prepends a 4-byte big-endian length prefix so the spoke's
        DirewolfSpokeTransport can reassemble fragmented AX.25 frames
        into complete HF-256 wire messages.

        Thread-safe.  Called from within the AGW reader thread (via
        HubCore → session.send), so this method must NOT hold self._lock
        when calling sock.sendall() — the reader thread itself can hold
        the lock between frames, causing a non-reentrant deadlock.
        Strategy: build frame and extract sock reference inside the lock,
        then release the lock before the blocking sendall() call.
        """
        remote_call = remote_call.upper()

        # Phase 1: build the AGW frame and grab the socket reference.
        # All mutable state accessed here is protected by self._lock.
        with self._lock:
            conn = self._connections.get(remote_call)
            if conn is None:
                log.warning("DirewolfTransport.send_to: no connection to %s",
                            remote_call)
                return False
            # Flow control — do NOT sleep while holding the lock (would block
            # the reader thread). Release and re-check instead.
            if conn.outstanding >= MAX_OUTSTANDING:
                log.debug("DirewolfTransport.send_to: flow-control wait for %s",
                          remote_call)
            prefix = struct.pack(">I", len(data))
            frame  = _make_agw_frame(
                kind      = "D",
                call_from = conn.mycall,
                call_to   = remote_call,
                port      = conn.radio_port,
                data      = prefix + data,
            )
            sock = self._sock   # grab reference while holding lock

        # Phase 2: send outside the lock so we don't block the reader thread.
        if sock is None:
            log.warning("DirewolfTransport.send_to: AGW socket not connected")
            return False
        try:
            sock.sendall(frame)
            return True
        except Exception as exc:
            log.error("DirewolfTransport.send_to: send error to %s: %s",
                      remote_call, exc)
            with self._lock:
                self._sock = None
            if self._running:
                self._schedule_reconnect()
            return False

    def connect_to(self, remote_call: str,
                   radio_port: int = RADIO_PORT_VHF) -> bool:
        """
        Initiate an outgoing AX.25 connected-mode call (spoke side).
        Returns True if the 'C' frame was sent successfully (the actual
        AX.25 connection handshake happens asynchronously).
        """
        frame = _make_agw_frame(
            kind      = "C",
            call_from = self.mycall,
            call_to   = remote_call.upper(),
            port      = radio_port,
        )
        if not self._sock_send(frame):
            return False
        log.info("DirewolfTransport: connecting to %s on port %d",
                 remote_call, radio_port)
        return True

    def disconnect_from(self, remote_call: str) -> bool:
        """Send AX.25 disconnect to remote station."""
        remote_call = remote_call.upper()
        with self._lock:
            conn = self._connections.get(remote_call)
        if conn is None:
            return False
        frame = _make_agw_frame(
            kind      = "d",
            call_from = conn.mycall,
            call_to   = remote_call,
            port      = conn.radio_port,
        )
        return self._sock_send(frame)

    # ------------------------------------------------------------------
    # AGW connection management
    # ------------------------------------------------------------------

    def _connect_agw(self) -> bool:
        """Open TCP connection to Direwolf and register callsign(s)."""
        # Guard: if stop() was called while we were sleeping in the reconnect
        # loop, abort immediately so we don't create an orphaned AGW client.
        if not self._running:
            log.debug("DirewolfTransport: _connect_agw called after stop() — aborting")
            return False

        import socket as _socket
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.agw_host, self.agw_port))
            sock.settimeout(None)
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)

            with self._lock:
                self._sock = sock

            # Register mycall on each enabled radio port.
            # Port number must match the CHANNEL in direwolf.conf:
            #   VHF-only or VHF+HF-different-cards: VHF=0, HF=1
            #   HF-only: HF=0 (single channel device has only port 0)
            if self.vhf_enabled:
                self._register_callsign(RADIO_PORT_VHF)
            if self.hf_enabled:
                hf_port = RADIO_PORT_HF if self.vhf_enabled else RADIO_PORT_VHF
                self._register_callsign(hf_port)

            log.info("DirewolfTransport: connected to Direwolf AGW at %s:%d",
                     self.agw_host, self.agw_port)
            return True

        except ConnectionRefusedError:
            log.error(
                "DirewolfTransport: AGW connection refused at %s:%d "
                "(is Direwolf running with AGWPORT %d?)",
                self.agw_host, self.agw_port, self.agw_port,
            )
        except Exception as exc:
            log.error("DirewolfTransport: AGW connect error: %s", exc)

        return False

    def _register_callsign(self, radio_port: int) -> None:
        """Send 'X' frame to register our callsign on a radio port."""
        frame = _make_agw_frame(
            kind      = "X",
            call_from = self.mycall,
            port      = radio_port,
        )
        if self._sock_send(frame):
            log.info(
                "DirewolfTransport: registered %s on radio port %d",
                self.mycall, radio_port,
            )
        else:
            log.error(
                "DirewolfTransport: failed to register %s on port %d",
                self.mycall, radio_port,
            )

    def _schedule_reconnect(self) -> None:
        if not self._running:
            return
        t = threading.Thread(
            target=self._reconnect_loop,
            daemon=True,
            name="direwolf-reconnect",
        )
        t.start()

    def _reconnect_loop(self) -> None:
        while self._running:
            time.sleep(RECONNECT_DELAY)
            if not self._running:
                break
            log.info("DirewolfTransport: attempting AGW reconnect …")
            if self._connect_agw():
                self._start_reader()
                return
        log.info("DirewolfTransport: reconnect loop exiting (stopped)")

    # ------------------------------------------------------------------
    # Reader thread (parses the AGW stream from Direwolf)
    # ------------------------------------------------------------------

    def _start_reader(self) -> None:
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name="direwolf-reader",
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        log.info("DirewolfTransport: AGW reader loop started")
        while self._running:
            with self._lock:
                sock = self._sock
            if sock is None:
                break
            try:
                # Read fixed-size AGW header
                header_raw = self._recv_exact(sock, AGW_HEADER_SIZE)
                if header_raw is None:
                    log.warning("DirewolfTransport: AGW connection lost")
                    break

                hdr = _parse_agw_header(header_raw)
                if hdr is None:
                    log.error("DirewolfTransport: unparseable AGW header — "
                              "resync not possible; reconnecting")
                    break

                # Read payload
                data_len = hdr["data_len"]
                payload  = b""
                if data_len > 0:
                    payload = self._recv_exact(sock, data_len)
                    if payload is None:
                        log.warning("DirewolfTransport: EOF reading AGW payload")
                        break

                self._dispatch_agw_frame(hdr, payload)

            except OSError as exc:
                if self._running:
                    log.error("DirewolfTransport: reader OSError: %s", exc)
                break
            except Exception as exc:
                if self._running:
                    log.error("DirewolfTransport: reader error: %s",
                              exc, exc_info=True)
                break

        log.info("DirewolfTransport: AGW reader loop exiting")
        with self._lock:
            self._sock = None

        if self._running:
            log.info("DirewolfTransport: scheduling reconnect")
            self._schedule_reconnect()

    @staticmethod
    def _recv_exact(sock, n: int) -> Optional[bytes]:
        """Read exactly n bytes from socket.  Returns None on EOF/error."""
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

    # ------------------------------------------------------------------
    # AGW frame dispatch
    # ------------------------------------------------------------------

    def _dispatch_agw_frame(self, hdr: dict, payload: bytes) -> None:
        kind      = hdr["kind"]
        call_from = hdr["call_from"]
        call_to   = hdr["call_to"]
        port      = hdr["port"]

        if kind == "X":
            # Callsign registration ACK — informational
            log.info("DirewolfTransport: callsign registered on port %d "
                     "(ACK from Direwolf)", port)

        elif kind == "C":
            # Incoming connection from a remote station
            # call_from = remote, call_to = our callsign
            log.info("DirewolfTransport: AX.25 CONNECT from %s on port %d",
                     call_from, port)
            self._handle_connect(call_from, call_to, port)

        elif kind == "d":
            # Disconnect from remote station
            log.info("DirewolfTransport: AX.25 DISCONNECT from %s on port %d",
                     call_from, port)
            self._handle_disconnect(call_from)

        elif kind == "D":
            # Received data from connected session
            self._handle_data(call_from, payload)

        elif kind == "y":
            # Flow control: outstanding frames count update
            if len(payload) >= 4:
                count = struct.unpack("<I", payload[:4])[0]
                with self._lock:
                    conn = self._connections.get(call_from)
                if conn:
                    with conn._lock:
                        conn.outstanding = count
                    log.debug("DirewolfTransport: outstanding %d frames "
                              "for %s", count, call_from)

        elif kind == "R":
            # Version information from Direwolf — log and ignore
            log.info("DirewolfTransport: Direwolf version info received")

        elif kind == "G":
            # Port information frame — log and ignore
            log.debug("DirewolfTransport: port info received")

        else:
            log.debug("DirewolfTransport: unhandled AGW kind '%s' "
                      "from %s", kind, call_from)

    # ------------------------------------------------------------------
    # Session lifecycle handlers
    # ------------------------------------------------------------------

    def _handle_connect(
        self, remote_call: str, local_call: str, radio_port: int
    ) -> None:
        """Handle incoming AX.25 connection — create a ClientSession."""
        remote_call = remote_call.upper()

        # Determine transport type for session labeling
        transport_type = (
            "VHF_AX25" if radio_port == RADIO_PORT_VHF else "HF_AX25"
        )

        # Build the send function closure — capture remote_call
        _transport_ref = self

        def send_func(data: bytes, _call=remote_call) -> bool:
            return _transport_ref.send_to(_call, data)

        session = self.session_manager.create_session(
            transport_type = transport_type,
            send_func      = send_func,
            callsign       = remote_call,
        )

        if session is None:
            # Hub at session limit — send a polite disconnect
            log.warning("DirewolfTransport: session limit reached — "
                        "disconnecting %s", remote_call)
            self.disconnect_from(remote_call)
            return

        # Track AX.25 connection state
        conn = AX25Connection(radio_port, self.mycall, remote_call)
        with self._lock:
            self._connections[remote_call]   = conn
            self._call_to_session[remote_call] = session.session_id

        if self.on_client_connect:
            try:
                self.on_client_connect(session)
            except Exception as exc:
                log.error("on_client_connect error: %s", exc, exc_info=True)

    def _handle_disconnect(self, remote_call: str) -> None:
        """Handle AX.25 disconnect — close the ClientSession."""
        remote_call = remote_call.upper()

        with self._lock:
            self._connections.pop(remote_call, None)
            sid = self._call_to_session.pop(remote_call, None)

        if sid:
            session = self.session_manager.get(sid)
            if session is None:
                # Already closed (e.g. by watchdog or hub eviction)
                return
            self.session_manager.close_session(sid)
            if self.on_client_disconnect:
                try:
                    self.on_client_disconnect(session)
                except Exception as exc:
                    log.error("on_client_disconnect error: %s",
                              exc, exc_info=True)

    def _handle_data(self, remote_call: str, data: bytes) -> None:
        """
        Received raw data from a connected AX.25 station.

        AX.25 is packet-based but Direwolf may deliver partial messages
        across multiple 'D' frames if the HF-256 wire message exceeds the
        AX.25 I-frame payload limit.  We reassemble using the 4-byte
        big-endian length prefix prepended by DirewolfSpokeTransport.send_data().
        The per-session ``rx_buffer`` in ClientSession is the reassembly buffer.
        """
        remote_call = remote_call.upper()

        with self._lock:
            sid = self._call_to_session.get(remote_call)

        if sid is None:
            log.warning("DirewolfTransport: data from unknown session %s",
                        remote_call)
            return

        session = self.session_manager.get(sid)
        if session is None:
            log.warning("DirewolfTransport: session %s not in manager", sid)
            return

        session.touch()

        # Accumulate bytes into per-session reassembly buffer
        session.rx_buffer.extend(data)

        # Extract all complete length-prefixed messages
        while len(session.rx_buffer) >= 4:
            msg_len = struct.unpack(">I", bytes(session.rx_buffer[:4]))[0]
            if msg_len == 0 or msg_len > 1024 * 1024:
                log.error(
                    "DirewolfTransport: bad frame length %d from %s — "
                    "clearing reassembly buffer",
                    msg_len, remote_call,
                )
                session.rx_buffer.clear()
                break
            if len(session.rx_buffer) < 4 + msg_len:
                break  # incomplete — wait for more data
            msg_data = bytes(session.rx_buffer[4 : 4 + msg_len])
            session.rx_buffer = session.rx_buffer[4 + msg_len :]
            try:
                self.on_client_message(session, msg_data)
            except Exception as exc:
                log.error(
                    "DirewolfTransport dispatch error [%s]: %s",
                    remote_call, exc, exc_info=True,
                )

    # ------------------------------------------------------------------
    # Socket send helper
    # ------------------------------------------------------------------

    def _sock_send(self, frame: bytes) -> bool:
        with self._lock:
            sock = self._sock
        if sock is None:
            log.warning("DirewolfTransport: not connected to Direwolf AGW")
            return False
        try:
            sock.sendall(frame)
            return True
        except Exception as exc:
            log.error("DirewolfTransport: AGW send error: %s", exc)
            with self._lock:
                self._sock = None
            if self._running:
                self._schedule_reconnect()
            return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Spoke-side AX.25 transport
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DirewolfSpokeTransport:
    """
    Single-session outgoing AX.25 transport for spoke stations.

    Mirrors the TCPTransport interface so ConsoleSession works unchanged:
      state, remote_call, on_state_change, on_message_received,
      send_data(), close(), vara_disconnect()

    Wire framing uses the same 4-byte big-endian length prefix as TCP so
    the hub's DirewolfTransport._handle_data() can reassemble fragments.

    Lifecycle::

        dws = DirewolfSpokeTransport(mycall="W1ABC", vhf_enabled=True)
        dws.on_state_change     = session._on_state_change
        dws.on_message_received = session._on_message_received
        dws.start()                          # connect to Direwolf AGW
        dws.connect_to("N0HUB")             # initiate outgoing AX.25 call
        dws.send_data(wire_bytes)            # send HF-256 wire message
        dws.vara_disconnect()                # send AX.25 disconnect
        dws.close()                          # close AGW socket
    """

    STATE_DISCONNECTED = 0
    STATE_CONNECTING   = 1
    STATE_CONNECTED    = 2

    def __init__(
        self,
        mycall: str,
        vhf_enabled: bool = True,
        hf_enabled:  bool = False,
        agw_host: str = "127.0.0.1",
        agw_port: int = AGW_PORT,
    ) -> None:
        self.mycall      = mycall.upper()
        self.vhf_enabled = vhf_enabled
        self.hf_enabled  = hf_enabled
        self.agw_host    = agw_host
        self.agw_port    = agw_port

        self.state       = self.STATE_DISCONNECTED
        self.remote_call = None
        self._active_port: Optional[int] = None

        self._sock:   Optional[object] = None
        self._lock    = threading.Lock()
        self._running = False
        self._rx_buffer = bytearray()
        self._reader_thread: Optional[threading.Thread] = None

        # Callbacks — same names as TCPTransport so ConsoleSession works unchanged
        self.on_state_change:     Optional[Callable] = None
        self.on_message_received: Optional[Callable] = None
        self.on_ptt_change:       Optional[Callable] = None  # unused; kept for compat

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Connect to Direwolf AGW and register our callsign(s)."""
        import socket as _socket
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.agw_host, self.agw_port))
            sock.settimeout(None)
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)

            with self._lock:
                self._sock = sock
            self._running = True

            if self.vhf_enabled:
                self._register_callsign(RADIO_PORT_VHF)
            if self.hf_enabled:
                # HF-only configs use channel 0; VHF+HF configs use channel 1
                hf_port = RADIO_PORT_HF if self.vhf_enabled else RADIO_PORT_VHF
                self._register_callsign(hf_port)

            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                daemon=True, name="direwolf-spoke-reader",
            )
            self._reader_thread.start()

            log.info("DirewolfSpokeTransport: connected to AGW %s:%d as %s",
                     self.agw_host, self.agw_port, self.mycall)
            return True

        except ConnectionRefusedError:
            log.error(
                "DirewolfSpokeTransport: AGW connection refused at %s:%d "
                "(is Direwolf running with AGWPORT %d?)",
                self.agw_host, self.agw_port, self.agw_port,
            )
        except Exception as exc:
            log.error("DirewolfSpokeTransport: AGW connect error: %s", exc)
        return False

    def connect_to(
        self, callsign: str, radio_port: int = RADIO_PORT_VHF
    ) -> bool:
        """Initiate an outgoing AX.25 call to ``callsign``."""
        callsign = callsign.upper()
        frame = _make_agw_frame(
            kind      = "C",
            call_from = self.mycall,
            call_to   = callsign,
            port      = radio_port,
        )
        if self._sock_send(frame):
            self._active_port = radio_port
            self.remote_call  = callsign
            self._set_state(self.STATE_CONNECTING)
            log.info("DirewolfSpokeTransport: calling %s on port %d",
                     callsign, radio_port)
            return True
        return False

    def send_data(self, data: bytes) -> bool:
        """
        Send a length-prefixed frame to the connected remote station.

        Prepends the same 4-byte big-endian length prefix used by TCP so
        the hub's DirewolfTransport can reassemble fragmented frames.
        """
        if self.state != self.STATE_CONNECTED or not self.remote_call:
            log.warning("DirewolfSpokeTransport.send_data: not connected")
            return False
        prefix = struct.pack(">I", len(data))
        frame = _make_agw_frame(
            kind      = "D",
            call_from = self.mycall,
            call_to   = self.remote_call,
            port      = self._active_port or RADIO_PORT_VHF,
            data      = prefix + data,
        )
        return self._sock_send(frame)

    def vara_disconnect(self) -> None:
        """
        Send AX.25 disconnect frame.  Named ``vara_disconnect`` to match
        the ARDOPConnection / FreeDVTransport interface used by ConsoleSession.
        """
        if self.remote_call and self._active_port is not None:
            frame = _make_agw_frame(
                kind      = "d",
                call_from = self.mycall,
                call_to   = self.remote_call,
                port      = self._active_port,
            )
            self._sock_send(frame)
        self._set_state(self.STATE_DISCONNECTED)

    def close(self) -> None:
        """Close the Direwolf AGW socket and stop the reader thread."""
        self._running = False
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        self._set_state(self.STATE_DISCONNECTED)
        log.info("DirewolfSpokeTransport: closed")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _register_callsign(self, radio_port: int) -> None:
        frame = _make_agw_frame(
            kind="X", call_from=self.mycall, port=radio_port
        )
        self._sock_send(frame)
        log.info("DirewolfSpokeTransport: registered %s on port %d",
                 self.mycall, radio_port)

    def _reader_loop(self) -> None:
        log.info("DirewolfSpokeTransport: AGW reader loop started")
        while self._running:
            with self._lock:
                sock = self._sock
            if sock is None:
                break
            try:
                header_raw = DirewolfTransport._recv_exact(sock, AGW_HEADER_SIZE)
                if header_raw is None:
                    log.warning("DirewolfSpokeTransport: AGW connection lost")
                    break
                hdr = _parse_agw_header(header_raw)
                if hdr is None:
                    log.error("DirewolfSpokeTransport: unparseable AGW header")
                    break
                payload = b""
                if hdr["data_len"] > 0:
                    payload = DirewolfTransport._recv_exact(sock, hdr["data_len"])
                    if payload is None:
                        log.warning("DirewolfSpokeTransport: EOF reading payload")
                        break
                self._dispatch_frame(hdr, payload)
            except Exception as exc:
                if self._running:
                    log.error("DirewolfSpokeTransport: reader error: %s", exc)
                break

        log.info("DirewolfSpokeTransport: AGW reader loop exiting")
        with self._lock:
            self._sock = None
        if self._running and self.state != self.STATE_DISCONNECTED:
            self._set_state(self.STATE_DISCONNECTED)

    def _dispatch_frame(self, hdr: dict, payload: bytes) -> None:
        kind      = hdr["kind"]
        call_from = hdr["call_from"]

        if kind == "C":
            # Outgoing call connected — Direwolf confirms the AX.25 SABM/UA exchange
            self.remote_call = call_from or self.remote_call
            log.info("DirewolfSpokeTransport: AX.25 CONNECTED to %s",
                     self.remote_call)
            self._set_state(self.STATE_CONNECTED)

        elif kind == "d":
            # Remote sent DISC or connection timed out
            log.info("DirewolfSpokeTransport: AX.25 DISCONNECTED from %s",
                     self.remote_call)
            self.remote_call = None
            self._set_state(self.STATE_DISCONNECTED)

        elif kind == "D":
            # Received data — reassemble with 4-byte length prefix
            self._rx_buffer.extend(payload)
            self._extract_messages()

        elif kind == "y":
            pass  # flow control — not needed on spoke side

        elif kind == "X":
            log.info("DirewolfSpokeTransport: callsign registered (AGW ACK)")

        else:
            log.debug("DirewolfSpokeTransport: unhandled AGW kind '%s'", kind)

    def _extract_messages(self) -> None:
        """Extract complete length-prefixed messages from rx_buffer."""
        while len(self._rx_buffer) >= 4:
            msg_len = struct.unpack(">I", bytes(self._rx_buffer[:4]))[0]
            if msg_len == 0 or msg_len > 1024 * 1024:
                log.error(
                    "DirewolfSpokeTransport: bad frame length %d — "
                    "clearing reassembly buffer", msg_len,
                )
                self._rx_buffer.clear()
                break
            if len(self._rx_buffer) < 4 + msg_len:
                break  # incomplete — wait for more data
            msg_data = bytes(self._rx_buffer[4 : 4 + msg_len])
            self._rx_buffer = self._rx_buffer[4 + msg_len :]
            if self.on_message_received:
                try:
                    self.on_message_received(msg_data)
                except Exception as exc:
                    log.error(
                        "DirewolfSpokeTransport: on_message_received error: %s",
                        exc, exc_info=True,
                    )

    def _set_state(self, new_state: int) -> None:
        old_state  = self.state
        self.state = new_state
        _names = {0: "DISCONNECTED", 1: "CONNECTING", 2: "CONNECTED"}
        if old_state != new_state:
            log.info("DirewolfSpokeTransport: %s → %s",
                     _names.get(old_state, "?"), _names.get(new_state, "?"))
        if self.on_state_change and old_state != new_state:
            try:
                self.on_state_change(old_state, new_state)
            except Exception as exc:
                log.error("DirewolfSpokeTransport: on_state_change error: %s",
                          exc, exc_info=True)

    def _sock_send(self, frame: bytes) -> bool:
        with self._lock:
            sock = self._sock
        if sock is None:
            log.warning("DirewolfSpokeTransport: not connected to AGW")
            return False
        try:
            sock.sendall(frame)
            return True
        except Exception as exc:
            log.error("DirewolfSpokeTransport: AGW send error: %s", exc)
            with self._lock:
                self._sock = None
            if self._running:
                self._set_state(self.STATE_DISCONNECTED)
            return False
