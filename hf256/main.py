"""
HF-256 v2.0 - Encrypted HF messaging
FreeDV and TCP transports
Hub and spoke modes
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from hf256 import __version__
from hf256.crypto import (
    KeyManager, PasswordManager, HF256Crypto, PasswordDatabase
)
from hf256.chat import (
    HF256Message, ChatMessage, FileListRequest, FileListResponse,
    AuthRequest, AuthResponse, StoreMessage, RetrieveMessages,
    FileDownloadRequest, FileDataMessage, FileComplete, FileError,
    TYPE_CHAT, TYPE_FILE_LIST_REQ, TYPE_FILE_LIST_RESP,
    TYPE_AUTH_REQ, TYPE_AUTH_RESP, TYPE_STORE_MSG, TYPE_RETRIEVE_MSGS,
    TYPE_FILE_DOWNLOAD_REQ, TYPE_FILE_DATA, TYPE_FILE_COMPLETE,
    TYPE_FILE_ERROR
)
from hf256.tcp_transport import TCPTransport
from hf256.storage import MessageStore
from hf256.filetransfer import (
    FileServer, FileReceiver,
    FREEDV_MAX_FILE_SIZE, CHUNK_SIZE_FREEDV, CHUNK_SIZE_TCP
)

log = logging.getLogger("hf256")

CONFIG_DIR   = "/etc/hf256"
SETTINGS_FILE = "/etc/hf256/settings.json"
CONFIG_ENV    = "/etc/hf256/config.env"
SETUP_FLAG    = "/etc/hf256/.setup_complete"
LOG_FILE      = os.path.expanduser("~/.hf256/hf256.log")


def load_settings() -> dict:
    """Load /etc/hf256/settings.json."""
    defaults = {
        "callsign":         "N0CALL",
        "role":             "",
        "hub_address":      "",
        "encryption_enabled": True,
        "network_key_set":  False,
        "wifi_mode":        "ap",
        "ap_ssid":          "HF256-N0CALL",
        "ap_password":      "hf256setup",
        "client_ssid":      "",
        "client_password":  ""
    }
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        defaults.update(data)
    except Exception as e:
        log.warning("Could not load settings.json: %s", e)
    return defaults


def load_config_env() -> dict:
    """Load /etc/hf256/config.env key=value pairs."""
    config = {}
    try:
        with open(CONFIG_ENV) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    config[key.strip()] = val.strip().strip('"')
    except Exception:
        pass
    return config


def setup_logging():
    """Configure logging to file."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="a"),
        ]
    )


class HF256Base:
    """Base class with common hub/spoke functionality."""

    def __init__(self, settings: dict, config: dict):
        self.mycall    = settings.get("callsign", "N0CALL").upper()
        self.settings  = settings
        self.config    = config
        self._running  = True

        # Inactivity tracking
        self._last_activity  = time.time()
        self._inactivity_timeout = 120

        # Message framing buffer
        self._rx_buffer = bytearray()

        # Transport
        self.transport      = None
        self.transport_mode = None

        # Encryption
        self.encryption_enabled = settings.get("encryption_enabled", True)
        self.key_manager = KeyManager(CONFIG_DIR)
        self.crypto      = None

        if self.key_manager.has_key():
            key = self.key_manager.get_key()
            self.crypto = HF256Crypto(key, self.encryption_enabled)
            log.info("Encryption: %s",
                     "ENABLED" if self.encryption_enabled else "DISABLED")
        else:
            log.warning("No network key - encryption disabled")
            self.encryption_enabled = False

    def _init_transport(self, mode: str):
        """
        Initialize transport based on mode string.
        mode: 'freedv' or 'tcp'
        """
        if mode == "freedv":
            from hf256.freedv import FreeDVTransport
            self.transport = FreeDVTransport(
                mycall=self.mycall,
                kiss_host="127.0.0.1",
                kiss_port=8001,
                cmd_port=8002
            )
            self.transport_mode = "freedv"
            log.info("Transport: FreeDV (freedvtnc2 KISS port 8001)")

        elif mode == "tcp":
            role = self.settings.get("role", "spoke")
            if role == "hub":
                self.transport = TCPTransport(
                    mycall=self.mycall,
                    mode="server",
                    host="0.0.0.0",
                    port=14256
                )
            else:
                hub_addr = self.settings.get("hub_address", "")
                host, _, port = hub_addr.partition(":")
                port = int(port) if port else 14256
                self.transport = TCPTransport(
                    mycall=self.mycall,
                    mode="client",
                    host=host or "127.0.0.1",
                    port=port
                )
            self.transport_mode = "tcp"
            log.info("Transport: TCP port 14256")
        else:
            log.error("Unknown transport mode: %s", mode)
            return False

        self.transport.on_state_change     = self._on_state_change
        self.transport.on_message_received = self._on_message_received
        self.transport.on_ptt_change       = self._on_ptt_change
        return True

    def _on_ptt_change(self, ptt_on: bool):
        """PTT state change - handled by freedvtnc2, stub here."""
        pass

    def _on_state_change(self, old_state, new_state, trigger=None):
        """Transport connection state changed."""
        log.info("Transport state: %s -> %s", old_state, new_state)
        STATE_CONNECTED = 2
        if new_state == STATE_CONNECTED:
            self._last_activity = time.time()

    def _on_message_received(self, data: bytes):
        """Received data from transport - must be implemented by subclass."""
        raise NotImplementedError

    def _extract_messages(self, data: bytes) -> list:
        """
        Extract complete length-prefixed messages from stream.
        Format: 4-byte big-endian length + message bytes.
        """
        import struct
        self._rx_buffer.extend(data)
        messages = []

        while len(self._rx_buffer) >= 4:
            msg_len = struct.unpack(">I",
                                    bytes(self._rx_buffer[:4]))[0]
            if msg_len == 0 or msg_len > 512 * 1024:
                log.error("Invalid message length %d - clearing buffer",
                          msg_len)
                self._rx_buffer.clear()
                break

            total = 4 + msg_len
            if len(self._rx_buffer) < total:
                break

            msg_data = bytes(self._rx_buffer[4:total])
            messages.append(msg_data)
            self._rx_buffer = self._rx_buffer[total:]

        return messages

    def send_message(self, msg: HF256Message) -> bool:
        """Pack and send a message over the active transport."""
        import struct
        if self.transport is None:
            log.warning("No transport - cannot send")
            return False

        if self.transport.state != 2:
            log.warning("Not connected - cannot send")
            return False

        wire   = msg.pack(self.crypto)
        prefix = struct.pack(">I", len(wire))
        self._last_activity = time.time()
        return self.transport.send_data(prefix + wire)

    def start(self) -> bool:
        """Connect transport. Override in subclass if needed."""
        if self.transport is None:
            log.error("Transport not initialized")
            return False
        return self.transport.connect()

    def shutdown(self):
        """Shutdown transport cleanly."""
        self._running = False
        if self.transport:
            self.transport.close()
        log.info("Shutdown complete")


class HF256Spoke(HF256Base):
    """Spoke station - connects to a hub."""

    def __init__(self, settings: dict, config: dict,
                 transport_mode: str = "freedv"):
        super().__init__(settings, config)

        self.authenticated         = False
        self.remote_encryption     = None
        self.password_manager      = PasswordManager(CONFIG_DIR)
        self.file_receiver         = FileReceiver(
            os.path.expanduser("~/.hf256/downloads")
        )

        self._init_transport(transport_mode)

    def _on_state_change(self, old_state, new_state, trigger=None):
        super()._on_state_change(old_state, new_state, trigger)
        STATE_CONNECTED    = 2
        STATE_DISCONNECTED = 0

        if new_state == STATE_CONNECTED:
            log.info("Spoke connected to hub")
            self.authenticated     = False
            self.remote_encryption = None

        elif old_state == STATE_CONNECTED:
            log.info("Spoke disconnected from hub")
            self.authenticated = False
            if self._rx_buffer:
                self._rx_buffer.clear()
            if self.file_receiver.is_receiving():
                self.file_receiver.cancel()

    def _on_message_received(self, data: bytes):
        """Process received data from hub."""
        self._last_activity = time.time()
        messages = self._extract_messages(data)

        for msg_data in messages:
            try:
                msg = HF256Message.unpack(msg_data, self.crypto)
                self._dispatch(msg)
            except Exception as e:
                log.error("Failed to process message: %s", e,
                          exc_info=True)

    def _dispatch(self, msg: HF256Message):
        """Route message to appropriate handler."""
        t = msg.msg_type

        if t == TYPE_CHAT:
            chat = ChatMessage.from_message(msg)
            ts   = datetime.fromtimestamp(
                chat.timestamp).strftime("%H:%M")
            log.info("[%s] %s: %s", ts, chat.sender, chat.text)

        elif t == TYPE_AUTH_RESP:
            resp = AuthResponse.from_message(msg)
            if resp.success:
                self.authenticated = True
                log.info("Authentication successful")
            else:
                log.warning("Authentication failed: %s", resp.message)

        elif t == TYPE_FILE_LIST_RESP:
            fl = FileListResponse.from_message(msg)
            log.info("File list received: %d files",
                     len(fl.files))
            for fname, info in fl.files.items():
                log.info("  %s (%d bytes)",
                         fname, info.get("size", 0))

        elif t == TYPE_FILE_DATA:
            chunk = FileDataMessage.from_message(msg)
            if not self.file_receiver.is_receiving():
                def progress(fname, pct, done, total):
                    log.info("Download %s: %d%% (%d/%d)",
                             fname, int(pct * 100), done, total)
                self.file_receiver.start_receive(
                    chunk.filename, chunk.total_chunks,
                    chunk.file_hash, progress
                )
            done = self.file_receiver.receive_chunk(
                chunk.chunk_num, chunk.chunk_data
            )
            if done:
                log.info("Download complete: %s", chunk.filename)

        elif t == TYPE_FILE_COMPLETE:
            fc = FileComplete.from_message(msg)
            log.info("Transfer complete: %s success=%s",
                     fc.filename, fc.success)

        elif t == TYPE_FILE_ERROR:
            fe = FileError.from_message(msg)
            log.error("File error: %s", fe.error)
            self.file_receiver.cancel()

        else:
            log.warning("Unknown message type: 0x%02x", msg.msg_type)

    def authenticate(self, password: str):
        """Send authentication request to hub."""
        req = AuthRequest(
            self.mycall, password,
            encrypted=self.encryption_enabled
        )
        self.send_message(req)
        log.info("Auth request sent as %s", self.mycall)

    def send_chat(self, text: str):
        """Send a chat message to hub."""
        if not self.authenticated:
            log.warning("Not authenticated")
            return False
        msg = ChatMessage(
            self.mycall, text,
            encrypted=self.encryption_enabled
        )
        return self.send_message(msg)

    def request_file_list(self):
        """Request file list from hub."""
        if not self.authenticated:
            log.warning("Not authenticated")
            return False
        return self.send_message(
            FileListRequest(encrypted=self.encryption_enabled)
        )

    def download_file(self, filename: str):
        """Request file download from hub."""
        if not self.authenticated:
            log.warning("Not authenticated")
            return False
        req = FileDownloadRequest(
            filename, encrypted=self.encryption_enabled
        )
        return self.send_message(req)

    def send_stored_message(self, recipient: str, text: str):
        """Send message via hub for another station."""
        if not self.authenticated:
            log.warning("Not authenticated")
            return False
        chat = ChatMessage(
            self.mycall, text,
            encrypted=self.encryption_enabled
        )
        chat_data = chat.pack(self.crypto)
        store = StoreMessage(
            recipient.upper(), chat_data,
            encrypted=self.encryption_enabled
        )
        return self.send_message(store)

    def retrieve_messages(self):
        """Request stored messages from hub."""
        if not self.authenticated:
            log.warning("Not authenticated")
            return False
        return self.send_message(
            RetrieveMessages(encrypted=self.encryption_enabled)
        )
class HF256Hub(HF256Base):
    """Hub station - 24/7 store and forward."""

    def __init__(self, settings: dict, config: dict,
                 transport_mode: str = "freedv"):
        super().__init__(settings, config)

        self.message_store = MessageStore(
            os.path.expanduser("~/.hf256/hub_messages")
        )
        self.file_server = FileServer(
            os.path.expanduser("~/.hf256/hub_files")
        )
        self.password_db           = PasswordDatabase(CONFIG_DIR)
        self.authenticated_stations = {}

        self._init_transport(transport_mode)
        threading.Thread(
            target=self._cleanup_loop,
            daemon=True
        ).start()

    def _cleanup_loop(self):
        """Periodic cleanup of old messages."""
        while self._running:
            time.sleep(3600)
            self.message_store.cleanup_old_messages(max_age_days=30)

    def _on_state_change(self, old_state, new_state, trigger=None):
        super()._on_state_change(old_state, new_state, trigger)
        if new_state == 2:
            log.info("Hub: station connected")
        elif old_state == 2:
            log.info("Hub: station disconnected")
            remote = getattr(self.transport, "remote_call", None)
            if remote and remote in self.authenticated_stations:
                del self.authenticated_stations[remote]

    def _on_message_received(self, data: bytes):
        """Process received data from spoke."""
        self._last_activity = time.time()
        messages = self._extract_messages(data)

        for msg_data in messages:
            try:
                msg    = HF256Message.unpack(msg_data, self.crypto)
                remote = getattr(
                    self.transport, "remote_call", "UNKNOWN"
                )
                self._dispatch(msg, remote)
            except Exception as e:
                log.error("Hub failed to process message: %s", e,
                          exc_info=True)

    def _dispatch(self, msg: HF256Message, remote: str):
        """Route message to appropriate handler."""
        t = msg.msg_type

        if t == TYPE_AUTH_REQ:
            self._handle_auth(msg, remote)

        elif t == TYPE_CHAT:
            if not self._is_authenticated(remote):
                return
            chat = ChatMessage.from_message(msg)
            ts   = datetime.fromtimestamp(
                chat.timestamp).strftime("%H:%M")
            log.info("[%s] %s: %s", ts, chat.sender, chat.text)
            ack = ChatMessage(
                "HUB", "✓ Message received",
                encrypted=self.encryption_enabled
            )
            self.send_message(ack)

        elif t == TYPE_FILE_LIST_REQ:
            if not self._is_authenticated(remote):
                return
            self._send_file_list(remote)

        elif t == TYPE_STORE_MSG:
            if not self._is_authenticated(remote):
                return
            self._handle_store(msg, remote)

        elif t == TYPE_RETRIEVE_MSGS:
            if not self._is_authenticated(remote):
                return
            self._handle_retrieve(remote)

        elif t == TYPE_FILE_DOWNLOAD_REQ:
            if not self._is_authenticated(remote):
                return
            threading.Thread(
                target=self._handle_download,
                args=(msg, remote),
                daemon=True
            ).start()

        else:
            log.warning("Hub: unknown message type 0x%02x from %s",
                        msg.msg_type, remote)

    def _handle_auth(self, msg: HF256Message, remote: str):
        """Authenticate a connecting spoke."""
        try:
            req   = AuthRequest.from_message(msg)
            call  = req.callsign.upper()
            users = self.password_db.list_users()

            if call not in users:
                resp = AuthResponse(
                    False,
                    f"{call} is not registered on this hub",
                    encrypted=self.encryption_enabled
                )
                self.send_message(resp)
                log.warning("Auth rejected - unknown callsign: %s", call)
                return

            if self.password_db.verify(call, req.password):
                self.authenticated_stations[remote] = time.time()
                self.authenticated_stations[call]   = time.time()
                resp = AuthResponse(
                    True, "Welcome",
                    encrypted=self.encryption_enabled
                )
                self.send_message(resp)
                log.info("Auth success: %s", call)

                count = self.message_store.get_message_count(call)
                if count > 0:
                    notice = ChatMessage(
                        "HUB",
                        f"You have {count} message(s). "
                        f"Use /retrieve to download.",
                        encrypted=self.encryption_enabled
                    )
                    self.send_message(notice)
            else:
                resp = AuthResponse(
                    False, "Invalid password",
                    encrypted=self.encryption_enabled
                )
                self.send_message(resp)
                log.warning("Auth failed wrong password: %s", call)

        except Exception as e:
            log.error("Auth handler error: %s", e, exc_info=True)

    def _is_authenticated(self, remote: str) -> bool:
        return (remote in self.authenticated_stations or
                remote.upper() in self.authenticated_stations)

    def _send_file_list(self, remote: str):
        """Send file list to authenticated spoke."""
        self.file_server.scan_files()
        files = self.file_server.get_file_list()
        resp  = FileListResponse(
            files, encrypted=self.encryption_enabled
        )
        self.send_message(resp)
        log.info("Sent file list (%d files) to %s",
                 len(files), remote)

    def _handle_store(self, msg: HF256Message, remote: str):
        """Store a message for another callsign."""
        try:
            req       = StoreMessage.from_message(msg)
            recipient = req.recipient.upper()
            users     = self.password_db.list_users()

            if recipient not in users:
                err = ChatMessage(
                    "HUB",
                    f"Unknown recipient: {recipient}",
                    encrypted=self.encryption_enabled
                )
                self.send_message(err)
                return

            msg_id = self.message_store.store_message(
                recipient=recipient,
                encrypted_data=req.message_data,
                sender=remote
            )
            if msg_id:
                ack = ChatMessage(
                    "HUB",
                    f"✓ Message stored for {recipient}",
                    encrypted=self.encryption_enabled
                )
                self.send_message(ack)
            else:
                err = ChatMessage(
                    "HUB", "✗ Failed to store message",
                    encrypted=self.encryption_enabled
                )
                self.send_message(err)

        except Exception as e:
            log.error("Store handler error: %s", e, exc_info=True)

    def _handle_retrieve(self, remote: str):
        """Send stored messages to authenticated spoke."""
        callsign = remote.upper()
        messages = self.message_store.retrieve_messages(
            callsign, delete=False
        )

        if not messages:
            notice = ChatMessage(
                "HUB", "No messages waiting",
                encrypted=self.encryption_enabled
            )
            self.send_message(notice)
            return

        notice = ChatMessage(
            "HUB",
            f"Sending {len(messages)} message(s)...",
            encrypted=self.encryption_enabled
        )
        self.send_message(notice)

        for stored in messages:
            try:
                orig = HF256Message.unpack(
                    stored["data"], self.crypto
                )
                if orig.msg_type == TYPE_CHAT:
                    chat = ChatMessage.from_message(orig)
                    ts   = datetime.fromtimestamp(
                        stored.get("received", time.time())
                    ).strftime("%Y-%m-%d %H:%M")
                    new_chat = ChatMessage(
                        chat.sender,
                        f"[{ts}]\n{chat.text}",
                        encrypted=self.encryption_enabled
                    )
                    self.send_message(new_chat)
                else:
                    self.transport.send_data(stored["data"])
                time.sleep(0.5)
            except Exception as e:
                log.error("Retrieve message error: %s", e,
                          exc_info=True)

        self.message_store.retrieve_messages(callsign, delete=True)
        log.info("Sent and deleted %d messages for %s",
                 len(messages), callsign)

    def _handle_download(self, msg: HF256Message, remote: str):
        """Handle file download request in background thread."""
        try:
            req      = FileDownloadRequest.from_message(msg)
            filename = req.filename
            self.file_server.scan_files()
            info = self.file_server.get_file_info(filename)

            if not info:
                err = ChatMessage(
                    "HUB",
                    f"✗ File not found: {filename}",
                    encrypted=self.encryption_enabled
                )
                self.send_message(err)
                return

            chunk_size = (CHUNK_SIZE_FREEDV
                          if self.transport_mode == "freedv"
                          else CHUNK_SIZE_TCP)

            if (self.transport_mode == "freedv" and
                    info.size > FREEDV_MAX_FILE_SIZE):
                err = ChatMessage(
                    "HUB",
                    f"✗ File too large for FreeDV: {filename} "
                    f"({info.size // 1024}KB, "
                    f"limit {FREEDV_MAX_FILE_SIZE // 1024}KB)",
                    encrypted=self.encryption_enabled
                )
                self.send_message(err)
                return

            num_chunks = (info.size + chunk_size - 1) // chunk_size
            notice = ChatMessage(
                "HUB",
                f"Sending {filename} "
                f"({info.size // 1024}KB, {num_chunks} chunks)",
                encrypted=self.encryption_enabled
            )
            self.send_message(notice)

            filepath = os.path.join(
                self.file_server.files_dir, filename
            )
            with open(filepath, "rb") as f:
                for chunk_num in range(num_chunks):
                    chunk_data = f.read(chunk_size)
                    if not chunk_data:
                        break
                    chunk_msg = FileDataMessage(
                        filename=filename,
                        chunk_num=chunk_num,
                        total_chunks=num_chunks,
                        data=chunk_data,
                        file_hash=info.sha256,
                        encrypted=self.encryption_enabled
                    )
                    if not self.send_message(chunk_msg):
                        log.error("Failed sending chunk %d", chunk_num)
                        return
                    if self.transport_mode == "freedv":
                        time.sleep(
                            (len(chunk_data) * 8 / 1500) + 2
                        )

            complete = FileComplete(
                filename=filename, success=True,
                message=f"Transfer complete: {filename}",
                encrypted=self.encryption_enabled
            )
            self.send_message(complete)
            log.info("File transfer complete: %s to %s",
                     filename, remote)

        except Exception as e:
            log.error("Download handler error: %s", e, exc_info=True)


def main():
    """Main entry point - reads settings.json and starts hub or spoke."""
    setup_logging()

    log.info("HF-256 v%s starting", __version__)

    if not Path(SETUP_FLAG).exists():
        log.error("Setup not complete - run web portal first")
        sys.exit(1)

    settings = load_settings()
    config   = load_config_env()

    callsign = settings.get("callsign", "N0CALL").upper()
    role     = settings.get("role", "").lower()

    if not role:
        log.error("No role configured in settings.json")
        sys.exit(1)

    # Determine transport mode
    # If FreeDV services are configured and running use FreeDV
    # otherwise fall back to TCP
    freedv_cmd = config.get("FREEDVTNC2_CMD", "")
    if freedv_cmd:
        transport_mode = "freedv"
    else:
        transport_mode = "tcp"

    log.info("Callsign: %s  Role: %s  Transport: %s",
             callsign, role.upper(), transport_mode.upper())

    try:
        if role == "hub":
            station = HF256Hub(settings, config, transport_mode)
        elif role == "spoke":
            station = HF256Spoke(settings, config, transport_mode)
        else:
            log.error("Invalid role: %s (must be hub or spoke)", role)
            sys.exit(1)

        if not station.start():
            log.error("Failed to start transport")
            sys.exit(1)

        log.info("HF-256 running - press Ctrl+C to stop")

        while station._running:
            time.sleep(1)

    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        if "station" in locals():
            station.shutdown()


if __name__ == "__main__":
    main()
