"""
HF-256 Encryption Module - Single Network Key
AES-256-GCM authenticated encryption
"""

import os
import json
import base64
import hashlib
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_CONFIGDIR = "/etc/hf256"
KEY_FILE          = "/etc/hf256/network.key"


class KeyManager:
    """Manages the shared AES-256 network key."""

    def __init__(self, configdir: str = DEFAULT_CONFIGDIR):
        self.configdir = configdir
        self.key_file  = KEY_FILE

    def has_key(self) -> bool:
        return os.path.exists(self.key_file)

    def get_key(self) -> bytes:
        with open(self.key_file, "rb") as f:
            return f.read()

    def set_key(self, key: bytes):
        os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
        with open(self.key_file, "wb") as f:
            f.write(key)
        os.chmod(self.key_file, 0o600)

    @staticmethod
    def generate_key() -> bytes:
        return os.urandom(32)

    @staticmethod
    def export_key_text(key: bytes) -> str:
        return base64.b64encode(key).decode()

    @staticmethod
    def import_key_text(text: str) -> bytes:
        text = text.strip()
        try:
            key = base64.b64decode(text)
        except Exception:
            raise ValueError("Invalid base64 key string")
        if len(key) != 32:
            raise ValueError(f"Key must be 32 bytes, got {len(key)}")
        return key


class HF256Crypto:
    """AES-256-GCM encryption/decryption for HF-256 messages."""

    def __init__(self, key: bytes, enabled: bool = True):
        if len(key) != 32:
            raise ValueError("Key must be 32 bytes")
        self.key     = key
        self.enabled = enabled
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext. Returns nonce + ciphertext."""
        if not self.enabled:
            return plaintext
        nonce = os.urandom(12)
        ct    = self._aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ct

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt nonce + ciphertext. Returns plaintext."""
        if not self.enabled:
            return data
        if len(data) < 12:
            raise ValueError("Data too short to contain nonce")
        nonce = data[:12]
        ct    = data[12:]
        return self._aesgcm.decrypt(nonce, ct, None)


class PasswordManager:
    """Password storage for spoke authentication."""

    def __init__(self, configdir: str = DEFAULT_CONFIGDIR):
        self.configdir = configdir
        self.pwfile    = os.path.join(configdir, "password.json")
        self.password_hash = None
        self._load()

    def _load(self):
        if os.path.exists(self.pwfile):
            try:
                with open(self.pwfile) as f:
                    self.password_hash = json.load(f).get("hash")
            except Exception:
                pass

    def save_password(self):
        os.makedirs(self.configdir, exist_ok=True)
        with open(self.pwfile, "w") as f:
            json.dump({"hash": self.password_hash}, f)
        try:
            os.chmod(self.pwfile, 0o600)
        except Exception:
            pass

    def set_password(self, password: str):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt = os.urandom(32)
        kdf  = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000
        )
        key = kdf.derive(password.encode())
        self.password_hash = base64.b64encode(salt + key).decode()
        self.save_password()

    def verify_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            raw  = base64.b64decode(self.password_hash)
            salt = raw[:32]
            stored_key = raw[32:]
            kdf  = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000
            )
            key = kdf.derive(password.encode())
            return key == stored_key
        except Exception:
            return False

    def has_password(self) -> bool:
        return self.password_hash is not None


class PasswordDatabase:
    """
    Hub user database - stores callsign:password_hash pairs.
    Used by hub to authenticate connecting spokes.
    """

    def __init__(self, configdir: str = DEFAULT_CONFIGDIR):
        self.configdir = configdir
        self.db_file   = os.path.join(configdir, "users.json")
        self._db       = {}
        self._load()

    def _load(self):
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file) as f:
                    self._db = json.load(f)
            except Exception:
                self._db = {}

    def _save(self):
        os.makedirs(self.configdir, exist_ok=True)
        with open(self.db_file, "w") as f:
            json.dump(self._db, f, indent=2)
        try:
            os.chmod(self.db_file, 0o600)
        except Exception:
            pass

    def _hash_password(self, password: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt = os.urandom(32)
        kdf  = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000
        )
        key = kdf.derive(password.encode())
        return base64.b64encode(salt + key).decode()

    def add_user(self, callsign: str, password: str):
        self._db[callsign.upper()] = self._hash_password(password)
        self._save()

    def remove_user(self, callsign: str):
        self._db.pop(callsign.upper(), None)
        self._save()

    def verify(self, callsign: str, password: str) -> bool:
        stored = self._db.get(callsign.upper())
        if not stored:
            return False
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            raw        = base64.b64decode(stored)
            salt       = raw[:32]
            stored_key = raw[32:]
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000
            )
            key = kdf.derive(password.encode())
            return key == stored_key
        except Exception:
            return False

    def list_users(self) -> list:
        return list(self._db.keys())
