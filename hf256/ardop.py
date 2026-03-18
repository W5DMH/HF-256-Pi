"""
ARDOP (Amateur Radio Digital Open Protocol) interface for HF-256
Open source alternative to VARA HF
"""

import socket
import threading
import time
import logging
from typing import Callable

log = logging.getLogger("hf256.ardop")


class ARDOPConnection:
    """ARDOP modem connection handler"""

    STATE_DISCONNECTED = 0
    STATE_CONNECTING = 1
    STATE_CONNECTED = 2

    def __init__(self, mycall: str,
                 ardop_host: str = "127.0.0.1",
                 ardop_cmd_port: int = 8515,
                 ardop_data_port: int = 8516):

        self.mycall = mycall.upper()
        self.ardop_host = ardop_host
        self.ardop_cmd_port = ardop_cmd_port
        self.ardop_data_port = ardop_data_port

        self.cmd_socket = None
        self.data_socket = None
        self.state = ARDOPConnection.STATE_DISCONNECTED
        self.remote_call = None
        self.running = False
        
        # Buffer tracking for smart chunk pacing
        self.buffer_size = 0
        self._buffer_lock = threading.Lock()

        # Inactivity watchdog
        self.inactivity_timeout = 120   # seconds silence → disconnect
        self._last_rx_time      = 0.0
        self._last_tx_time      = 0.0

        # Callbacks
        self.on_state_change = None
        self.on_message_received = None
        self.on_ptt_change = None

        self._lock = threading.Lock()
        self._send_lock = threading.Lock()  # Protect data socket writes

    def connect(self) -> bool:
        """Connect to ARDOP_Win modem - uses ports 8515 (cmd) and 8516 (data)"""
        try:
            log.info("Connecting to ARDOP_Win at %s:%d/%d",
                     self.ardop_host, self.ardop_cmd_port, self.ardop_data_port)

            # Port 8515: Commands
            self.cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.cmd_socket.settimeout(5)
            self.cmd_socket.connect((self.ardop_host, self.ardop_cmd_port))
            self.cmd_socket.settimeout(0.1)
            
            # Disable Nagle's algorithm on command socket too!
            import socket as sock_module
            self.cmd_socket.setsockopt(sock_module.IPPROTO_TCP, sock_module.TCP_NODELAY, 1)
            log.info("ARDOP command socket connected to port %d, TCP_NODELAY enabled", self.ardop_cmd_port)

            # Port 8516: Data (sequential port)
            self.data_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.data_socket.settimeout(5)
            self.data_socket.connect((self.ardop_host, self.ardop_data_port))
            self.data_socket.settimeout(None)  # Blocking for data
            
            # Verify sockets
            cmd_local = self.cmd_socket.getsockname()
            cmd_remote = self.cmd_socket.getpeername()
            data_local = self.data_socket.getsockname()
            data_remote = self.data_socket.getpeername()
            
            log.info("ARDOP socket verification:")
            log.info("  Command socket: %s -> %s", cmd_local, cmd_remote)
            log.info("  Data socket: %s -> %s", data_local, data_remote)
            
            # Disable Nagle's algorithm on data socket
            import socket as sock_module
            self.data_socket.setsockopt(sock_module.IPPROTO_TCP, sock_module.TCP_NODELAY, 1)
            log.info("ARDOP data socket connected to port %d, TCP_NODELAY enabled", 
                     self.ardop_data_port)

            # Start reader threads
            self.running = True
            threading.Thread(target=self._cmd_reader, daemon=True).start()
            threading.Thread(target=self._data_reader, daemon=True).start()

            # Brief wait for readers to start
            time.sleep(0.1)
            
            # Configure ARDOP (NO C: prefix!)
            self._send_cmd("INITIALIZE")
            time.sleep(0.1)
            self._send_cmd("MYCALL {}".format(self.mycall))
            time.sleep(0.1)
            self._send_cmd("ARQBW 2000MAX")
            time.sleep(0.1)
            self._send_cmd("LISTEN TRUE")

            log.info("ARDOP configured")
            return True

        except Exception as e:
            log.error("ARDOP connection failed: %s", e)
            return False

    def vara_connect(self, target_call: str):
        """Initiate connection to remote station (compatibility method name)"""
        with self._lock:
            if self.state != ARDOPConnection.STATE_DISCONNECTED:
                log.warning("Already connected/connecting")
                return

            self.state = ARDOPConnection.STATE_CONNECTING
            log.info("Connecting to %s", target_call)
            
            # Send ARQCALL command (no C: prefix)
            cmd = f"ARQCALL {target_call.upper()} 10"
            log.info("Sending command: %s", cmd)
            self._send_cmd(cmd)

    def vara_disconnect(self):
        """Disconnect from remote station"""
        log.info("Disconnecting...")
        self._send_cmd("DISCONNECT")
        
    def send_data(self, data: bytes):
        """Send data over ARDOP_Win - format is just length + data (NO D: prefix!)"""
        if self.state != ARDOPConnection.STATE_CONNECTED:
            log.warning("Not connected - cannot send (state=%d)", self.state)
            return False

        if not self.data_socket:
            log.warning("No data socket - cannot send")
            return False

        # Per gARIM source code (datathread.c line 124-127):
        # Format is: 2-byte length (big-endian) + data
        # NO "D:" prefix!
        import struct
        
        length = len(data)
        if length > 0xFFFF:
            log.error("Data too large for ARDOP: %d bytes (max 65535)", length)
            return False
        
        # Build frame: length (2 bytes MSB first) + data
        frame = struct.pack(">H", length) + data
        
        try:
            log.info("ARDOP send_data: Sending frame (gARIM format)")
            log.info("  - Length: %d (0x%04X) as big-endian", length, length)
            log.info("  - Data: %d bytes", len(data))
            log.info("  - Total frame: %d bytes (NO D: prefix!)", len(frame))
            log.info("  - First 32 bytes (hex): %s", frame[:32].hex())
            
            with self._send_lock:
                self.data_socket.sendall(frame)
            self._last_tx_time = time.time()
            log.info("ARDOP send_data: Frame sent on DATA socket (port 8516)")
            return True
        except Exception as e:
            log.error("Send failed: %s", e, exc_info=True)
            return False
    
    def _calculate_crc16(self, data: bytes) -> int:
        """Calculate CRC16 (x^16 + x^12 + x^5 + 1) used by ARDOP"""
        crc = 0xFFFF
        
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc = crc << 1
                crc &= 0xFFFF
        
        return crc

    def _send_cmd(self, cmd: str):
        """Send command to ARDOP"""
        if self.cmd_socket:
            try:
                self.cmd_socket.sendall((cmd + "\r").encode())
                log.info("CMD sent: %s", cmd)
            except Exception as e:
                log.error("Command send failed: %s", e)

    def _cmd_reader(self):
        """Read ARDOP command responses from port 8515"""
        buf = b""
        
        log.info("ARDOP command reader started")

        while self.running:
            try:
                chunk = self.cmd_socket.recv(1024)
                if not chunk:
                    log.warning("ARDOP command socket closed by remote")
                    break

                buf += chunk
                log.info("ARDOP cmd received %d bytes: %s", len(chunk), chunk[:100])

                # ARDOP_Win responses start with "c:" and end with \r
                while b"\r" in buf:
                    try:
                        line, buf = buf.split(b"\r", 1)
                        line = line.decode('ascii', errors='ignore').strip()
                        
                        log.info("ARDOP cmd line (raw): '%s'", line)

                        # Strip "c:" prefix if present
                        if line.startswith("c:"):
                            line = line[2:].strip()
                        
                        if line:
                            log.info("ARDOP cmd (processed): '%s'", line)
                            self._process_cmd(line)
                    except Exception as e:
                        log.error("ARDOP: Error processing command line: %s", e, exc_info=True)
                        # Don't break - keep reading

            except socket.timeout:
                continue
            except Exception as e:
                log.error("Command reader error: %s", e, exc_info=True)
                # Don't break immediately - log and continue
                time.sleep(0.1)
        
        log.warning("ARDOP command reader exiting")
    
    def _data_reader(self):
        """Read data from ARDOP_Win on port 8516 - gARIM format"""
        buf = b""
        
        while self.running:
            try:
                chunk = self.data_socket.recv(8192)
                if not chunk:
                    break
                
                buf += chunk
                
                # gARIM format (ardop_data.c line 232-246):
                # Bytes 0-1: Length (big-endian)
                # Bytes 2-4: Mode ("ARQ", "FEC", "ERR", "IDF")
                # Bytes 5+: Data
                
                while len(buf) >= 5:
                    import struct
                    
                    # Extract length
                    length = struct.unpack(">H", buf[0:2])[0]
                    
                    # Check if we have complete frame
                    if len(buf) < (2 + length):
                        break  # Need more data
                    
                    # Extract mode and data
                    mode = buf[2:5].decode('ascii', errors='ignore')
                    data = buf[5:2+length]
                    
                    log.info("ARDOP RX: mode=%s, %d bytes data", mode, len(data))
                    
                    if mode == "ARQ" and self.on_message_received:
                        self._last_rx_time = time.time()
                        self.on_message_received(data)
                    
                    # Remove processed frame from buffer
                    buf = buf[2+length:]

            except Exception as e:
                log.error("Data reader error: %s", e)
                break

    def _process_cmd(self, cmd: str):
        """Process ARDOP command"""
        log.info("ARDOP: %s", cmd)

        # Track buffer size for smart chunk pacing
        if cmd.startswith("BUFFER"):
            parts = cmd.split()
            if len(parts) >= 2:
                try:
                    buffer_bytes = int(parts[1])
                    with self._buffer_lock:
                        self.buffer_size = buffer_bytes
                except ValueError:
                    pass

        # ARDOP uses different keywords than VARA
        if cmd.startswith("CONNECTED"):
            # CONNECTED <remote_call>
            parts = cmd.split()
            with self._lock:
                old_state = self.state
                self.state = ARDOPConnection.STATE_CONNECTED
                if len(parts) >= 2:
                    self.remote_call = parts[1]
                self._last_rx_time = time.time()
                self._last_tx_time = time.time()

                log.info("Connected to %s", self.remote_call)

                if self.on_state_change:
                    self.on_state_change(old_state, self.state)

            # Start inactivity watchdog
            threading.Thread(target=self._watchdog, daemon=True,
                             name="ardop-watchdog").start()

        elif cmd.startswith("DISCONNECTED") or cmd.startswith("NEWSTATE DISC"):
            with self._lock:
                old_state = self.state
                self.state = ARDOPConnection.STATE_DISCONNECTED
                self.remote_call = None

                log.info("Disconnected")

                if self.on_state_change:
                    self.on_state_change(old_state, self.state)

        elif cmd.startswith("PTT"):
            # PTT TRUE or PTT FALSE
            if "TRUE" in cmd:
                if self.on_ptt_change:
                    self.on_ptt_change(True)
            elif "FALSE" in cmd:
                if self.on_ptt_change:
                    self.on_ptt_change(False)

    def wait_for_buffer_drain(self, threshold_bytes: int = 2048, timeout: float = 120.0) -> bool:
        """Wait for ARDOP buffer to drain below threshold
        
        Args:
            threshold_bytes: Wait until buffer is below this size
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if buffer drained, False if timeout
        """
        start_time = time.time()
        last_log_time = start_time
        
        while time.time() - start_time < timeout:
            with self._buffer_lock:
                current_buffer = self.buffer_size
            
            if current_buffer < threshold_bytes:
                return True
            
            # Log progress every 5 seconds
            now = time.time()
            if now - last_log_time >= 5.0:
                log.debug("Waiting for buffer drain: %d bytes (threshold: %d)", 
                         current_buffer, threshold_bytes)
                last_log_time = now
            
            time.sleep(0.5)  # Check twice per second
        
        log.warning("Buffer drain timeout after %.1f seconds (buffer: %d bytes)",
                   timeout, self.buffer_size)
        return False

    def _watchdog(self):
        """
        Inactivity watchdog — disconnects if no data received for
        inactivity_timeout seconds. Mirrors FreeDVTransport watchdog.
        """
        log.info("ARDOP watchdog started (inactivity=%ds)",
                 self.inactivity_timeout)
        while True:
            time.sleep(5)
            if self.state != ARDOPConnection.STATE_CONNECTED:
                log.info("ARDOP watchdog exiting (not connected)")
                return
            if self.inactivity_timeout <= 0:
                continue
            now = time.time()
            if (self._last_rx_time > 0 and
                    now - self._last_rx_time > self.inactivity_timeout):
                log.warning(
                    "ARDOP inactivity timeout (%.0fs) — disconnecting",
                    now - self._last_rx_time
                )
                try:
                    self._send_cmd("DISCONNECT")
                except Exception:
                    pass
                with self._lock:
                    old_state = self.state
                    self.state = ARDOPConnection.STATE_DISCONNECTED
                    self.remote_call = None
                if self.on_state_change:
                    try:
                        self.on_state_change(old_state,
                                             ARDOPConnection.STATE_DISCONNECTED)
                    except Exception as e:
                        log.error("ARDOP watchdog on_state_change error: %s", e)
                return

    def close(self):
        """Close connection"""
        self.running = False

        if self.cmd_socket:
            try:
                self.cmd_socket.close()
            except:
                pass
        
        if self.data_socket and self.data_socket != self.cmd_socket:
            try:
                self.data_socket.close()
            except:
                pass
