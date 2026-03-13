"""
FreeDV Transport for HF-256.
Connects to freedvtnc2 KISS TCP port (8001) for data
and command port (8002) for status and control.

Provides the same interface as TCPTransport so main.py
can use either transport interchangeably.
"""

import socket
import threading
import time
import logging
from typing import Callable, Optional

from hf256.kiss import KISSBuffer, encode as kiss_encode

log = logging.getLogger("hf256.freedv")


class FreeDVTransport:
    """
    FreeDV transport via freedvtnc2 KISS TCP interface.

    Interface is identical to TCPTransport:
      - on_state_change(old_state, new_state, trigger=None)
      - on_message_received(data: bytes)
      - on_ptt_change(ptt_on: bool)  -- stub, freedvtnc2 handles PTT
    """

    STATE_DISCONNECTED = 0
    STATE_CONNECTING   = 1
    STATE_CONNECTED    = 2

    def __init__(self,
                 mycall: str,
                 kiss_host: str = "127.0.0.1",
                 kiss_port: int = 8001,
                 cmd_port:  int = 8002):

        self.mycall    = mycall.upper()
        self.kiss_host = kiss_host
        self.kiss_port = kiss_port
        self.cmd_port  = cmd_port

        self.state       = FreeDVTransport.STATE_DISCONNECTED
        self.remote_call = None
        self.running     = False

        # Callbacks
        self.on_state_change:    Optional[Callable] = None
        self.on_message_received: Optional[Callable] = None
        self.on_ptt_change:      Optional[Callable] = None

        self._kiss_socket: Optional[socket.socket] = None
        self._kiss_buffer = KISSBuffer()
        self._read_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Connect to freedvtnc2 KISS port.
        Returns True if connection succeeded.
        """
        log.info("Connecting to freedvtnc2 KISS at %s:%d",
                 self.kiss_host, self.kiss_port)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.kiss_host, self.kiss_port))
            sock.settimeout(None)  # Switch to blocking after connect

            with self._lock:
                self._kiss_socket = sock
                self.running = True

            self._read_thread = threading.Thread(
                target=self._read_loop,
                daemon=True,
                name="freedv-read"
            )
            self._read_thread.start()

            self._set_state(FreeDVTransport.STATE_CONNECTED)
            log.info("Connected to freedvtnc2 KISS port")
            return True

        except ConnectionRefusedError:
            log.error("freedvtnc2 not running - connection refused on %s:%d",
                      self.kiss_host, self.kiss_port)
            return False
        except socket.timeout:
            log.error("Timeout connecting to freedvtnc2 on %s:%d",
                      self.kiss_host, self.kiss_port)
            return False
        except Exception as e:
            log.error("FreeDV connect error: %s", e)
            return False

    def send_data(self, data: bytes) -> bool:
        """
        Send raw bytes over FreeDV via KISS framing.
        Returns True if data was sent successfully.
        """
        with self._lock:
            sock = self._kiss_socket

        if sock is None:
            log.warning("send_data called but not connected")
            return False

        try:
            framed = kiss_encode(data)
            sock.sendall(framed)
            log.debug("Sent %d bytes (%d KISS framed)", len(data), len(framed))
            return True
        except Exception as e:
            log.error("Send error: %s", e)
            self._handle_disconnect()
            return False

    def close(self):
        """Disconnect and clean up."""
        log.info("FreeDVTransport closing")
        self.running = False
        with self._lock:
            if self._kiss_socket:
                try:
                    self._kiss_socket.close()
                except Exception:
                    pass
                self._kiss_socket = None
        self._set_state(FreeDVTransport.STATE_DISCONNECTED)

    # ------------------------------------------------------------------
    # FreeDV-specific command interface
    # ------------------------------------------------------------------

    def send_command(self, command: str, timeout: float = 5.0) -> tuple:
        """
        Send a command to freedvtnc2 command port (8002).
        Returns (success: bool, response: str).
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((self.kiss_host, self.cmd_port))
            sock.send(f"{command}\n".encode())
            response = sock.recv(1024).decode().strip()
            sock.close()
            return response.startswith("OK"), response
        except Exception as e:
            return False, f"ERROR {e}"

    def get_modem_status(self) -> dict:
        """Query freedvtnc2 status via command port."""
        ok, response = self.send_command("STATUS")
        if not ok:
            return {"online": False, "error": response}

        status = {"online": True}
        # Parse: OK STATUS MODE=DATAC1 VOLUME=0 PTT=OFF CHANNEL=CLEAR
        if response.startswith("OK STATUS "):
            for part in response[10:].split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    status[k.lower()] = v
        return status

    def get_modem_levels(self) -> dict:
        """Query audio levels via command port."""
        ok, response = self.send_command("LEVELS")
        if not ok:
            return {"online": False}

        levels = {"online": True}
        if response.startswith("OK LEVELS "):
            for part in response[10:].split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    try:
                        levels[k.lower()] = float(v)
                    except ValueError:
                        levels[k.lower()] = v
        return levels

    def set_mode(self, mode: str) -> bool:
        """Change FreeDV mode (DATAC1/DATAC3/DATAC4)."""
        ok, response = self.send_command(f"MODE {mode}")
        if ok:
            log.info("FreeDV mode set to %s", mode)
        else:
            log.error("Failed to set mode %s: %s", mode, response)
        return ok

    def set_volume(self, db: int) -> bool:
        """Set TX output volume in dB (-20 to 0)."""
        db = max(-20, min(0, db))
        ok, response = self.send_command(f"VOLUME {db}")
        return ok

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_loop(self):
        """Background thread: read KISS frames from freedvtnc2."""
        log.info("FreeDV read loop started")

        while self.running:
            with self._lock:
                sock = self._kiss_socket

            if sock is None:
                break

            try:
                data = sock.recv(4096)
                if not data:
                    log.warning("freedvtnc2 closed connection")
                    self._handle_disconnect()
                    break

                self._kiss_buffer.feed(data)
                frames = self._kiss_buffer.get_frames()

                for frame in frames:
                    if frame and self.on_message_received:
                        log.debug("Received KISS frame: %d bytes", len(frame))
                        try:
                            self.on_message_received(frame)
                        except Exception as e:
                            log.error("on_message_received error: %s", e,
                                      exc_info=True)

            except OSError as e:
                if self.running:
                    log.error("Read error: %s", e)
                    self._handle_disconnect()
                break
            except Exception as e:
                if self.running:
                    log.error("Unexpected read error: %s", e, exc_info=True)
                    self._handle_disconnect()
                break

        log.info("FreeDV read loop exited")

    def _handle_disconnect(self):
        """Handle unexpected disconnection from freedvtnc2."""
        with self._lock:
            if self._kiss_socket:
                try:
                    self._kiss_socket.close()
                except Exception:
                    pass
                self._kiss_socket = None

        self._kiss_buffer.clear()
        self._set_state(FreeDVTransport.STATE_DISCONNECTED)

    def _set_state(self, new_state: int, trigger=None):
        """Update state and fire callback."""
        old_state = self.state
        if old_state == new_state:
            return

        self.state = new_state
        state_names = {0: "DISCONNECTED", 1: "CONNECTING", 2: "CONNECTED"}
        log.info("FreeDV state: %s -> %s",
                 state_names.get(old_state, old_state),
                 state_names.get(new_state, new_state))

        if self.on_state_change:
            try:
                self.on_state_change(old_state, new_state, trigger)
            except Exception as e:
                log.error("on_state_change callback error: %s", e,
                          exc_info=True)
