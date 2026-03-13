"""
HF-256 Message Protocol
Defines all message types and packing/unpacking logic.
"""

import json
import struct
import time
import logging

log = logging.getLogger("hf256.chat")

# Message type constants
TYPE_CHAT             = 0x01
TYPE_AUTH_REQ         = 0x02
TYPE_AUTH_RESP        = 0x03
TYPE_FILE_LIST_REQ    = 0x04
TYPE_FILE_LIST_RESP   = 0x05
TYPE_STORE_MSG        = 0x06
TYPE_RETRIEVE_MSGS    = 0x07
TYPE_FILE_DOWNLOAD_REQ = 0x08
TYPE_FILE_DATA        = 0x09
TYPE_FILE_COMPLETE    = 0x0A
TYPE_FILE_ERROR       = 0x0B


class HF256Message:
    """
    Wire format:
      1 byte  - msg_type
      1 byte  - flags (bit 0 = encrypted)
      4 bytes - timestamp (uint32 big-endian, unix epoch)
      N bytes - payload (encrypted or plaintext JSON)
    """

    HEADER_SIZE = 6

    def __init__(self, msg_type: int, payload: bytes,
                 encrypted: bool = False, timestamp: int = None):
        self.msg_type  = msg_type
        self.payload   = payload
        self.encrypted = encrypted
        self.timestamp = timestamp or int(time.time())

    def pack(self, crypto=None) -> bytes:
        """Serialize and optionally encrypt."""
        flags = 0x01 if (crypto and crypto.enabled) else 0x00
        data  = self.payload

        if crypto and crypto.enabled:
            data = crypto.encrypt(data)

        header = struct.pack(">BBi", self.msg_type, flags, self.timestamp)
        return header + data

    @classmethod
    def unpack(cls, data: bytes, crypto=None) -> "HF256Message":
        """Deserialize and optionally decrypt."""
        if len(data) < cls.HEADER_SIZE:
            raise ValueError(f"Message too short: {len(data)} bytes")

        msg_type, flags, timestamp = struct.unpack(">BBi", data[:cls.HEADER_SIZE])
        encrypted = bool(flags & 0x01)
        payload   = data[cls.HEADER_SIZE:]

        if encrypted and crypto and crypto.enabled:
            try:
                payload = crypto.decrypt(payload)
            except Exception as e:
                raise ValueError(f"Decryption failed: {e}")

        return cls(msg_type, payload, encrypted=encrypted,
                   timestamp=timestamp)


class ChatMessage(HF256Message):
    """Plain text chat message."""

    def __init__(self, sender: str, text: str,
                 encrypted: bool = False, timestamp: int = None):
        self.sender = sender
        self.text   = text
        payload = json.dumps({
            "sender": sender,
            "text":   text
        }).encode()
        super().__init__(TYPE_CHAT, payload,
                         encrypted=encrypted, timestamp=timestamp)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "ChatMessage":
        data   = json.loads(msg.payload.decode())
        result = cls(data["sender"], data["text"],
                     encrypted=msg.encrypted, timestamp=msg.timestamp)
        return result


class AuthRequest(HF256Message):
    """Spoke -> Hub authentication request."""

    def __init__(self, callsign: str, password: str,
                 encrypted: bool = False):
        self.callsign = callsign
        self.password = password
        payload = json.dumps({
            "callsign": callsign,
            "password": password
        }).encode()
        super().__init__(TYPE_AUTH_REQ, payload, encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "AuthRequest":
        data = json.loads(msg.payload.decode())
        return cls(data["callsign"], data["password"],
                   encrypted=msg.encrypted)


class AuthResponse(HF256Message):
    """Hub -> Spoke authentication response."""

    def __init__(self, success: bool, message: str,
                 encrypted: bool = False):
        self.success = success
        self.message = message
        payload = json.dumps({
            "success": success,
            "message": message
        }).encode()
        super().__init__(TYPE_AUTH_RESP, payload, encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "AuthResponse":
        data = json.loads(msg.payload.decode())
        return cls(data["success"], data["message"],
                   encrypted=msg.encrypted)


class FileListRequest(HF256Message):
    """Spoke -> Hub request for file list."""

    def __init__(self, encrypted: bool = False):
        super().__init__(TYPE_FILE_LIST_REQ, b"{}", encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "FileListRequest":
        return cls(encrypted=msg.encrypted)


class FileListResponse(HF256Message):
    """Hub -> Spoke file list."""

    def __init__(self, files: dict, encrypted: bool = False):
        self.files = files
        payload    = json.dumps({"files": files}).encode()
        super().__init__(TYPE_FILE_LIST_RESP, payload, encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "FileListResponse":
        data = json.loads(msg.payload.decode())
        return cls(data.get("files", {}), encrypted=msg.encrypted)


class StoreMessage(HF256Message):
    """Spoke -> Hub store message for another callsign."""

    def __init__(self, recipient: str, message_data: bytes,
                 encrypted: bool = False):
        self.recipient    = recipient
        self.message_data = message_data
        import base64
        payload = json.dumps({
            "recipient": recipient,
            "data":      base64.b64encode(message_data).decode()
        }).encode()
        super().__init__(TYPE_STORE_MSG, payload, encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "StoreMessage":
        import base64
        data = json.loads(msg.payload.decode())
        return cls(
            data["recipient"],
            base64.b64decode(data["data"]),
            encrypted=msg.encrypted
        )


class RetrieveMessages(HF256Message):
    """Spoke -> Hub retrieve stored messages."""

    def __init__(self, encrypted: bool = False):
        super().__init__(TYPE_RETRIEVE_MSGS, b"{}", encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "RetrieveMessages":
        return cls(encrypted=msg.encrypted)


class FileDownloadRequest(HF256Message):
    """Spoke -> Hub request to download a file."""

    def __init__(self, filename: str, encrypted: bool = False):
        self.filename = filename
        payload = json.dumps({"filename": filename}).encode()
        super().__init__(TYPE_FILE_DOWNLOAD_REQ, payload,
                         encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "FileDownloadRequest":
        data = json.loads(msg.payload.decode())
        return cls(data["filename"], encrypted=msg.encrypted)


class FileDataMessage(HF256Message):
    """Hub -> Spoke file chunk."""

    def __init__(self, filename: str, chunk_num: int,
                 total_chunks: int, data: bytes,
                 file_hash: str, encrypted: bool = False):
        self.filename     = filename
        self.chunk_num    = chunk_num
        self.total_chunks = total_chunks
        self.chunk_data   = data
        self.file_hash    = file_hash
        import base64
        payload = json.dumps({
            "filename":     filename,
            "chunk_num":    chunk_num,
            "total_chunks": total_chunks,
            "data":         base64.b64encode(data).decode(),
            "hash":         file_hash
        }).encode()
        super().__init__(TYPE_FILE_DATA, payload, encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "FileDataMessage":
        import base64
        d = json.loads(msg.payload.decode())
        return cls(
            d["filename"], d["chunk_num"], d["total_chunks"],
            base64.b64decode(d["data"]), d["hash"],
            encrypted=msg.encrypted
        )


class FileComplete(HF256Message):
    """Hub -> Spoke file transfer complete notification."""

    def __init__(self, filename: str, success: bool,
                 message: str, encrypted: bool = False):
        self.filename = filename
        self.success  = success
        self.message  = message
        payload = json.dumps({
            "filename": filename,
            "success":  success,
            "message":  message
        }).encode()
        super().__init__(TYPE_FILE_COMPLETE, payload, encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "FileComplete":
        d = json.loads(msg.payload.decode())
        return cls(d["filename"], d["success"], d["message"],
                   encrypted=msg.encrypted)


class FileError(HF256Message):
    """Hub -> Spoke file transfer error."""

    def __init__(self, error: str, encrypted: bool = False):
        self.error = error
        payload    = json.dumps({"error": error}).encode()
        super().__init__(TYPE_FILE_ERROR, payload, encrypted=encrypted)

    @classmethod
    def from_message(cls, msg: HF256Message) -> "FileError":
        d = json.loads(msg.payload.decode())
        return cls(d["error"], encrypted=msg.encrypted)
