"""
HF-256 TCP Transport
Direct TCP connection as alternative to FreeDV HF radio.
Provides identical interface to FreeDVTransport.
"""

import socket
import threading
import logging
import time

log = logging.getLogger("hf256.tcp")


class TCPTransport:
    """
    TCP transport - same interface as FreeDVTransport.
    mode='server' for hub, mode='client' for spoke.
    """

    STATE_DISCONNECTED = 0
    STATE_CONNECTING   = 1
    STATE_CONNECTED    = 2

    def __init__(self, mycall: str, mode: str = "client",
                 host: str = "0.0.0.0", port: int = 14256):
        self.mycall = mycall.upper()
        self.mode   = mode
        self.host   = host
        self.port   = port

        self.state       = TCPTransport.STATE_DISCONNECTED
        self.remote_call = None
        self.running     = False

        self.server_socket = None
        self.client_socket = None

        self.on_state_change     = None
        self.on_message_received = None
        self.on_ptt_change       = None

        self._lock          = threading.Lock()
        self._read_thread   = None
        self._accept_thread = None
        self._rx_buffer     = b""

    def connect(self) -> bool:
        """Start server or connect as client."""
        self.running = True

        if self.mode == "server":
            return self._start_server()
        else:
            return self._connect_client()

    def _start_server(self) -> bool:
        """Start TCP server and wait for incoming connections."""
        try:
            self.server_socket = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM
            )
            self.server_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
            )
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(1)

            log.info("TCP server listening on %s:%d", self.host, self.port)

            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                daemon=True,
                name="tcp-accept"
            )
            self._accept_thread.start()
            return True

        except Exception as e:
            log.error("TCP server start failed: %s", e)
            return False

    def _accept_loop(self):
        """Accept incoming client connections."""
        while self.running:
            try:
                self.server_socket.settimeout(1.0)
                try:
                    conn, addr = self.server_socket.accept()
                except socket.timeout:
                    continue

                log.info("TCP client connected from %s", addr)

                with self._lock:
                    # Close existing connection if any
                    if self.client_socket:
                        try:
                            self.client_socket.close()
                        except Exception:
                            pass
                    self.client_socket = conn
                    self.remote_call   = str(addr[0])

                self._set_state(TCPTransport.STATE_CONNECTED)
                self._start_read_thread()

            except Exception as e:
                if self.running:
                    log.error("Accept error: %s", e)
                    time.sleep(1)

    def _connect_client(self) -> bool:
        """Connect to a TCP server and perform HF256 handshake."""
        try:
            self._set_state(TCPTransport.STATE_CONNECTING)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.host, self.port))

            # --- HF256 handshake ---
            # Send our callsign to the hub
            handshake = f"HF256:{self.mycall}\n"
            sock.sendall(handshake.encode("utf-8"))
            log.info("Sent handshake: %s", handshake.strip())

            # Read hub's callsign response
            response = b""
            while b"\n" not in response:
                chunk = sock.recv(256)
                if not chunk:
                    raise ConnectionError("Hub closed connection during handshake")
                response += chunk

            response_str = response.decode("utf-8").strip()
            if not response_str.startswith("HF256:"):
                raise ValueError(f"Unexpected hub response: {response_str!r}")

            remote_call = response_str.split(":", 1)[1]
            log.info("Hub identified as: %s", remote_call)
            # --- handshake complete ---

            sock.settimeout(None)

            with self._lock:
                self.client_socket = sock
                self.remote_call   = remote_call

            self._set_state(TCPTransport.STATE_CONNECTED)
            self._start_read_thread()
            log.info("TCP connected to %s:%d", self.host, self.port)
            return True

        except ConnectionRefusedError:
            log.error("TCP connection refused on %s:%d",
                      self.host, self.port)
            self._set_state(TCPTransport.STATE_DISCONNECTED)
            return False
        except socket.timeout:
            log.error("TCP connection timeout to %s:%d",
                      self.host, self.port)
            self._set_state(TCPTransport.STATE_DISCONNECTED)
            return False
        except Exception as e:
            log.error("TCP connect error: %s", e)
            self._set_state(TCPTransport.STATE_DISCONNECTED)
            return False

    def _start_read_thread(self):
        """Start background read thread."""
        self._read_thread = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name="tcp-read"
        )
        self._read_thread.start()

    def _read_loop(self):
        """Read length-prefixed messages from socket."""
        import struct
        log.info("TCP read loop started")

        while self.running:
            with self._lock:
                sock = self.client_socket

            if sock is None:
                break

            try:
                # Read 4-byte length prefix
                header = self._recv_exact(sock, 4)
                if not header:
                    log.info("TCP connection closed by remote")
                    self._handle_disconnect()
                    break

                msg_len = struct.unpack(">I", header)[0]

                if msg_len == 0 or msg_len > 1024 * 1024:
                    log.error("Invalid message length: %d", msg_len)
                    self._handle_disconnect()
                    break

                # Read message body
                data = self._recv_exact(sock, msg_len)
                if not data:
                    log.info("TCP connection closed mid-message")
                    self._handle_disconnect()
                    break

                if self.on_message_received:
                    try:
                        self.on_message_received(data)
                    except Exception as e:
                        log.error("on_message_received error: %s", e,
                                  exc_info=True)

            except OSError as e:
                if self.running:
                    log.error("TCP read error: %s", e)
                    self._handle_disconnect()
                break
            except Exception as e:
                if self.running:
                    log.error("Unexpected read error: %s", e, exc_info=True)
                    self._handle_disconnect()
                break

        log.info("TCP read loop exited")

    def _recv_exact(self, sock: socket.socket, n: int):
        """Read exactly n bytes from socket."""
        data = b""
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    return None
                data += chunk
            except Exception:
                return None
        return data

    def send_data(self, data: bytes) -> bool:
        """Send length-prefixed message."""
        import struct
        with self._lock:
            sock = self.client_socket

        if sock is None:
            log.warning("send_data: not connected")
            return False

        try:
            prefix  = struct.pack(">I", len(data))
            sock.sendall(prefix + data)
            return True
        except Exception as e:
            log.error("TCP send error: %s", e)
            self._handle_disconnect()
            return False

    def close(self):
        """Disconnect and clean up."""
        self.running = False
        with self._lock:
            if self.client_socket:
                try:
                    self.client_socket.close()
                except Exception:
                    pass
                self.client_socket = None
            if self.server_socket:
                try:
                    self.server_socket.close()
                except Exception:
                    pass
                self.server_socket = None
        self._set_state(TCPTransport.STATE_DISCONNECTED)

    def _handle_disconnect(self):
        """Handle unexpected disconnection."""
        with self._lock:
            if self.client_socket:
                try:
                    self.client_socket.close()
                except Exception:
                    pass
                self.client_socket = None
        self._set_state(TCPTransport.STATE_DISCONNECTED)

    def _set_state(self, new_state: int, trigger=None):
        """Update state and fire callback."""
        old_state  = self.state
        self.state = new_state

        state_names = {0: "DISCONNECTED", 1: "CONNECTING", 2: "CONNECTED"}
        if old_state != new_state:
            log.info("TCP state: %s -> %s",
                     state_names.get(old_state),
                     state_names.get(new_state))

        if self.on_state_change and old_state != new_state:
            try:
                self.on_state_change(old_state, new_state, trigger)
            except Exception as e:
                log.error("on_state_change error: %s", e, exc_info=True)
