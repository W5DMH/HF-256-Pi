"""
HF-256 Multi-Client TCP Transport
===================================
Replaces the original single-connection ``TCPTransport`` with an asyncio
server that accepts **multiple simultaneous spoke connections**.

Backward compatibility
----------------------
``TCPTransport`` is kept as a **client-mode-only** class (spoke side) —
its interface is unchanged so ConsoleSession in app.py works without edits.

``TCPServerTransport`` is the new hub-side multi-client server.

Architecture
------------
* asyncio event loop runs in a dedicated daemon thread so it coexists with
  Flask's threaded WSGI server and all existing threading code.
* Each accepted client gets its own ``asyncio.StreamReader/Writer`` pair
  isolated from all other clients.
* ``send_func`` provided to ``SessionManager.create_session()`` posts data
  back via ``asyncio.run_coroutine_threadsafe()`` so HubCore (which runs in
  OS threads) can call it safely.
* Frame format: identical 4-byte big-endian length prefix used everywhere.
* Handshake: identical ``HF256:<CALLSIGN>\n`` exchange as before — spoke-side
  code needs zero changes.

Wire frame: ``[len:4 BE][payload:len]``
Handshake:  spoke sends ``HF256:W1ABC\n`` → hub replies ``HF256:N0HUB\n``
"""

import asyncio
import logging
import socket
import struct
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("hf256.tcp")

# ── Constants ───────────────────────────────────────────────────────────────
HF256_PORT        = 14256
HANDSHAKE_TIMEOUT = 10.0          # seconds for initial HF256: exchange
MAX_MSG_BYTES     = 1 * 1024 * 1024  # 1 MB hard cap per frame
INACTIVITY_SEC    = 120           # watchdog: client-side disconnect trigger


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Multi-client HUB server  (new)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TCPServerTransport:
    """
    Asyncio TCP server — accepts unlimited simultaneous spoke connections
    (up to the ``SessionManager`` cap).

    Usage (hub side)::

        from hf256.session_manager import SessionManager
        from hf256.tcp_transport   import TCPServerTransport

        sm = SessionManager()
        sm.start_watchdog()

        srv = TCPServerTransport(
            mycall           = "N0HUB",
            session_manager  = sm,
            on_client_message   = hub_core.on_message,
            on_client_connect   = hub_core.on_connect,
            on_client_disconnect= hub_core.on_disconnect,
        )
        srv.start()   # starts asyncio loop in daemon thread
    """

    def __init__(
        self,
        mycall: str,
        session_manager,                               # SessionManager instance
        on_client_message:    Callable,                # (session, bytes) -> None
        on_client_connect:    Optional[Callable] = None,  # (session,) -> None
        on_client_disconnect: Optional[Callable] = None,  # (session,) -> None
        host: str = "0.0.0.0",
        port: int = HF256_PORT,
    ) -> None:
        self.mycall            = mycall.upper()
        self.session_manager   = session_manager
        self.on_client_message = on_client_message
        self.on_client_connect = on_client_connect
        self.on_client_disconnect = on_client_disconnect
        self.host  = host
        self.port  = port

        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread]          = None
        self._server = None
        self.running = False

    # ------------------------------------------------------------------
    # Public API (called from OS threads)
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """
        Launch the asyncio event loop in a daemon thread.
        Returns True once the server socket is bound and listening.
        Blocks for up to 5 seconds waiting for the loop to start.
        """
        started = threading.Event()
        errors: list = []

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(
                    self._bind_server(started, errors)
                )
                if not errors:
                    self._loop.run_forever()
            except Exception as exc:
                errors.append(exc)
                started.set()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass
                log.info("TCPServerTransport: asyncio loop exited")

        self._thread = threading.Thread(
            target=_run, daemon=True, name="tcp-server-asyncio"
        )
        self._thread.start()
        started.wait(timeout=5.0)

        if errors:
            log.error("TCPServerTransport: failed to start — %s", errors[0])
            return False

        log.info(
            "TCPServerTransport: listening on %s:%d as %s",
            self.host, self.port, self.mycall,
        )
        return True

    def stop(self) -> None:
        """Shut down the server gracefully."""
        self.running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        log.info("TCPServerTransport: stopped")

    # ------------------------------------------------------------------
    # Asyncio internals
    # ------------------------------------------------------------------

    async def _bind_server(
        self,
        started: threading.Event,
        errors: list,
    ) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                self.host,
                self.port,
                reuse_address=True,
            )
            self.running = True
            log.info("TCPServerTransport: asyncio server bound to %s:%d",
                     self.host, self.port)
        except Exception as exc:
            errors.append(exc)
        finally:
            started.set()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Coroutine: one task per accepted client connection."""
        peer = writer.get_extra_info("peername", ("?", 0))
        log.info("TCPServer: incoming connection from %s", peer)

        # ── Handshake ────────────────────────────────────────────────
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\n"),
                timeout=HANDSHAKE_TIMEOUT,
            )
            hs = raw.decode("utf-8", errors="replace").strip()
            if not hs.startswith("HF256:"):
                log.warning("TCPServer: invalid handshake from %s: %r", peer, hs)
                writer.close()
                return
            remote_call = hs.split(":", 1)[1].upper().strip()

            writer.write(f"HF256:{self.mycall}\n".encode("utf-8"))
            await writer.drain()
            log.info("TCPServer: handshake OK with %s from %s",
                     remote_call, peer)
        except asyncio.IncompleteReadError:
            log.warning("TCPServer: %s disconnected during handshake", peer)
            try:
                writer.close()
            except Exception:
                pass
            return
        except asyncio.TimeoutError:
            log.warning("TCPServer: handshake timeout from %s", peer)
            try:
                writer.close()
            except Exception:
                pass
            return
        except Exception as exc:
            log.error("TCPServer: handshake error from %s: %s", peer, exc)
            try:
                writer.close()
            except Exception:
                pass
            return

        # ── Build send_func usable from OS threads ───────────────────
        loop = self._loop

        async def _async_send(data: bytes) -> bool:
            try:
                # Clients read a 4-byte big-endian length prefix, then payload
                prefix = struct.pack(">I", len(data))
                writer.write(prefix + data)
                await writer.drain()
                return True
            except Exception as exc:
                log.error("TCPServer send error [%s]: %s", remote_call, exc)
                return False

        def send_sync(data: bytes) -> bool:
            """Called from OS threads (HubCore). Posts to asyncio loop."""
            if loop is None or not loop.is_running():
                return False
            fut = asyncio.run_coroutine_threadsafe(_async_send(data), loop)
            try:
                return fut.result(timeout=5.0)
            except Exception as exc:
                log.error("TCPServer sync-send error [%s]: %s",
                          remote_call, exc)
                return False

        # ── Register session ─────────────────────────────────────────
        session = self.session_manager.create_session(
            transport_type = "TCP",
            send_func      = send_sync,
            callsign       = remote_call,
        )
        if session is None:
            # Hub at capacity — send rejection frame then close
            log.warning("TCPServer: rejecting %s — session limit reached",
                        remote_call)
            reject = (
                b'{"type":"hub_busy",'
                b'"message":"Hub is at session capacity - try again later"}'
            )
            try:
                writer.write(struct.pack(">I", len(reject)) + reject)
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
            except Exception:
                pass
            return

        if self.on_client_connect:
            try:
                self.on_client_connect(session)
            except Exception as exc:
                log.error("on_client_connect error: %s", exc, exc_info=True)

        # ── Read loop ────────────────────────────────────────────────
        try:
            while True:
                # 4-byte length prefix
                try:
                    header = await reader.readexactly(4)
                except asyncio.IncompleteReadError:
                    log.info("TCPServer: %s disconnected (EOF)", remote_call)
                    break

                msg_len = struct.unpack(">I", header)[0]
                if msg_len == 0 or msg_len > MAX_MSG_BYTES:
                    log.error(
                        "TCPServer: bad frame length %d from %s — closing",
                        msg_len, remote_call,
                    )
                    break

                try:
                    payload = await reader.readexactly(msg_len)
                except asyncio.IncompleteReadError:
                    log.info("TCPServer: %s EOF mid-frame", remote_call)
                    break

                session.touch()

                # Dispatch to HubCore (runs in thread pool / OS thread)
                try:
                    self.on_client_message(session, payload)
                except Exception as exc:
                    log.error(
                        "TCPServer message dispatch error [%s]: %s",
                        remote_call, exc, exc_info=True,
                    )

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("TCPServer read error [%s]: %s", remote_call, exc)
        finally:
            self.session_manager.close_session(session.session_id)
            if self.on_client_disconnect:
                try:
                    self.on_client_disconnect(session)
                except Exception as exc:
                    log.error("on_client_disconnect error: %s", exc,
                              exc_info=True)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("TCPServer: session closed for %s", remote_call)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Single-connection CLIENT transport  (backward compatible, unchanged API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TCPTransport:
    """
    TCP transport — **client (spoke) mode only**.

    Identical public interface to the original class so ConsoleSession and
    all existing spoke code continues to work unchanged.

    Hub servers should use ``TCPServerTransport`` instead.
    """

    STATE_DISCONNECTED = 0
    STATE_CONNECTING   = 1
    STATE_CONNECTED    = 2

    def __init__(
        self,
        mycall: str,
        mode: str   = "client",
        host: str   = "0.0.0.0",
        port: int   = HF256_PORT,
    ) -> None:
        self.mycall = mycall.upper()
        self.mode   = mode          # "client" only — "server" kept for compat
        self.host   = host
        self.port   = port

        self.state       = TCPTransport.STATE_DISCONNECTED
        self.remote_call = None
        self.running     = False

        self.server_socket: Optional[socket.socket] = None
        self.client_socket: Optional[socket.socket] = None

        self.on_state_change:     Optional[Callable] = None
        self.on_message_received: Optional[Callable] = None
        self.on_ptt_change:       Optional[Callable] = None

        self._lock          = threading.Lock()
        self._read_thread:  Optional[threading.Thread] = None
        self._accept_thread:Optional[threading.Thread] = None

        self.inactivity_timeout = INACTIVITY_SEC
        self._last_rx_time = 0.0
        self._last_tx_time = 0.0

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Start server listener OR connect as client depending on mode."""
        self.running = True
        if self.mode == "server":
            return self._start_server()
        return self._connect_client()

    def _start_server(self) -> bool:
        """
        Legacy single-client server mode — kept for backward compatibility.
        New code should use TCPServerTransport.
        """
        try:
            self.server_socket = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM
            )
            self.server_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
            )
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)
            log.info("TCPTransport (legacy server): listening %s:%d",
                     self.host, self.port)
            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                daemon=True, name="tcp-accept",
            )
            self._accept_thread.start()
            return True
        except Exception as exc:
            log.error("TCPTransport server start failed: %s", exc)
            return False

    def _accept_loop(self) -> None:
        """Legacy: accept one client at a time."""
        while self.running:
            try:
                self.server_socket.settimeout(1.0)
                try:
                    conn, addr = self.server_socket.accept()
                except socket.timeout:
                    continue
                log.info("TCPTransport: accepted connection from %s", addr)
                self._do_server_handshake(conn, addr)
            except Exception as exc:
                if self.running:
                    log.error("TCPTransport accept error: %s", exc)
                    time.sleep(1)

    def _do_server_handshake(
        self, conn: socket.socket, addr
    ) -> None:
        """Perform HF256: handshake as server."""
        try:
            conn.settimeout(HANDSHAKE_TIMEOUT)
            raw = b""
            while b"\n" not in raw:
                chunk = conn.recv(256)
                if not chunk:
                    raise ConnectionError("Closed during handshake")
                raw += chunk
            hs = raw.decode("utf-8").strip()
            if not hs.startswith("HF256:"):
                raise ValueError(f"Bad handshake: {hs!r}")
            remote_call = hs.split(":", 1)[1]
            conn.sendall(f"HF256:{self.mycall}\n".encode("utf-8"))
            conn.settimeout(None)
            log.info("TCPTransport server: handshake OK with %s", remote_call)
        except Exception as exc:
            log.error("TCPTransport server handshake error: %s", exc)
            try:
                conn.close()
            except Exception:
                pass
            return

        with self._lock:
            if self.client_socket:
                try:
                    self.client_socket.close()
                except Exception:
                    pass
            self.client_socket = conn
            self.remote_call   = remote_call

        self._set_state(TCPTransport.STATE_CONNECTED)
        self._start_read_thread()

    def _connect_client(self) -> bool:
        """Connect to a hub TCP server and perform HF256: handshake."""
        try:
            self._set_state(TCPTransport.STATE_CONNECTING)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.host, self.port))

            sock.sendall(f"HF256:{self.mycall}\n".encode("utf-8"))
            log.info("TCPTransport client: sent handshake")

            resp = b""
            while b"\n" not in resp:
                chunk = sock.recv(256)
                if not chunk:
                    raise ConnectionError("Hub closed during handshake")
                resp += chunk

            resp_str = resp.decode("utf-8").strip()
            if not resp_str.startswith("HF256:"):
                raise ValueError(f"Bad hub response: {resp_str!r}")
            remote_call = resp_str.split(":", 1)[1]
            log.info("TCPTransport client: hub is %s", remote_call)

            sock.settimeout(None)
            with self._lock:
                self.client_socket = sock
                self.remote_call   = remote_call

            self._set_state(TCPTransport.STATE_CONNECTED)
            self._start_read_thread()
            return True

        except ConnectionRefusedError:
            log.error("TCPTransport: connection refused %s:%d",
                      self.host, self.port)
        except socket.timeout:
            log.error("TCPTransport: connection timeout %s:%d",
                      self.host, self.port)
        except Exception as exc:
            log.error("TCPTransport: connect error: %s", exc)

        self._set_state(TCPTransport.STATE_DISCONNECTED)
        return False

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _start_read_thread(self) -> None:
        self._read_thread = threading.Thread(
            target=self._read_loop,
            daemon=True, name="tcp-read",
        )
        self._read_thread.start()

    def _read_loop(self) -> None:
        log.info("TCPTransport: read loop started")
        while self.running:
            with self._lock:
                sock = self.client_socket
            if sock is None:
                break
            try:
                header = self._recv_exact(sock, 4)
                if not header:
                    log.info("TCPTransport: connection closed by remote")
                    self._handle_disconnect()
                    break

                msg_len = struct.unpack(">I", header)[0]
                if msg_len == 0 or msg_len > MAX_MSG_BYTES:
                    log.error("TCPTransport: invalid frame length %d", msg_len)
                    self._handle_disconnect()
                    break

                data = self._recv_exact(sock, msg_len)
                if not data:
                    log.info("TCPTransport: EOF mid-frame")
                    self._handle_disconnect()
                    break

                self._last_rx_time = time.time()
                if self.on_message_received:
                    try:
                        self.on_message_received(data)
                    except Exception as exc:
                        log.error("on_message_received error: %s", exc,
                                  exc_info=True)

            except OSError as exc:
                if self.running:
                    log.error("TCPTransport read OSError: %s", exc)
                    self._handle_disconnect()
                break
            except Exception as exc:
                if self.running:
                    log.error("TCPTransport unexpected read error: %s",
                              exc, exc_info=True)
                    self._handle_disconnect()
                break
        log.info("TCPTransport: read loop exited")

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
        """Read exactly n bytes; return None on EOF or error."""
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

    def send_data(self, data: bytes) -> bool:
        """
        Send a length-prefixed frame.

        NOTE: ``data`` is the raw payload WITHOUT the 4-byte prefix.
        The prefix is added here so callers stay consistent.
        """
        with self._lock:
            sock = self.client_socket
        if sock is None:
            log.warning("TCPTransport.send_data: not connected")
            return False
        try:
            prefix = struct.pack(">I", len(data))
            sock.sendall(prefix + data)
            self._last_tx_time = time.time()
            return True
        except Exception as exc:
            log.error("TCPTransport send error: %s", exc)
            self._handle_disconnect()
            return False

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _watchdog(self) -> None:
        log.info("TCPTransport watchdog: started (timeout=%ds)",
                 self.inactivity_timeout)
        while True:
            time.sleep(5)
            if self.state != TCPTransport.STATE_CONNECTED:
                return
            if self.inactivity_timeout <= 0:
                continue
            now = time.time()
            if (
                self._last_rx_time > 0
                and now - self._last_rx_time > self.inactivity_timeout
            ):
                log.warning(
                    "TCPTransport: inactivity timeout (%.0fs) — disconnecting",
                    now - self._last_rx_time,
                )
                self._handle_disconnect()
                return

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _set_state(self, new_state: int, trigger=None) -> None:
        old_state  = self.state
        self.state = new_state
        _names = {0: "DISCONNECTED", 1: "CONNECTING", 2: "CONNECTED"}
        if old_state != new_state:
            log.info("TCPTransport state: %s → %s",
                     _names.get(old_state, "?"), _names.get(new_state, "?"))

        if old_state != new_state and new_state == TCPTransport.STATE_CONNECTED:
            self._last_rx_time = time.time()
            self._last_tx_time = time.time()
            threading.Thread(
                target=self._watchdog, daemon=True, name="tcp-watchdog"
            ).start()

        if self.on_state_change and old_state != new_state:
            try:
                self.on_state_change(old_state, new_state, trigger)
            except Exception as exc:
                log.error("on_state_change error: %s", exc, exc_info=True)

    def _handle_disconnect(self) -> None:
        with self._lock:
            if self.client_socket:
                try:
                    self.client_socket.close()
                except Exception:
                    pass
                self.client_socket = None
        self._set_state(TCPTransport.STATE_DISCONNECTED)

    def close(self) -> None:
        """Disconnect and clean up all sockets."""
        self.running = False
        with self._lock:
            for attr in ("client_socket", "server_socket"):
                sock = getattr(self, attr, None)
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)
        self._set_state(TCPTransport.STATE_DISCONNECTED)
