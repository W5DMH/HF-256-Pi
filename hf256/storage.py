"""
HF-256 Message Store
Persistent store-and-forward message storage for hub stations.
Messages stored as JSON files in configdir/hub_messages/
"""

import os
import json
import time
import uuid
import logging
from pathlib import Path

log = logging.getLogger("hf256.storage")


class MessageStore:
    """
    File-based message store for hub stations.
    Each message stored as a separate JSON file named by UUID.
    """

    def __init__(self, storage_dir: str):
        self.storage_dir = storage_dir
        Path(storage_dir).mkdir(parents=True, exist_ok=True)
        log.info("MessageStore initialized at %s", storage_dir)

    def store_message(self, recipient: str, encrypted_data: bytes,
                      sender: str = "") -> str:
        """
        Store a message for a recipient.
        Returns message ID (UUID) or empty string on failure.
        """
        recipient = recipient.upper().strip()
        msg_id    = str(uuid.uuid4())

        msg = {
            "id":        msg_id,
            "recipient": recipient,
            "sender":    sender,
            "received":  time.time(),
            "data":      list(encrypted_data)  # bytes -> list for JSON
        }

        msg_dir = os.path.join(self.storage_dir, recipient)
        os.makedirs(msg_dir, exist_ok=True)

        msg_path = os.path.join(msg_dir, f"{msg_id}.json")
        try:
            with open(msg_path, "w") as f:
                json.dump(msg, f)
            log.info("Stored message %s for %s from %s",
                     msg_id, recipient, sender)
            return msg_id
        except Exception as e:
            log.error("Failed to store message: %s", e)
            return ""

    def get_message_count(self, callsign: str) -> int:
        """Return number of pending messages for a callsign."""
        msg_dir = os.path.join(self.storage_dir, callsign.upper())
        if not os.path.exists(msg_dir):
            return 0
        return len([f for f in os.listdir(msg_dir)
                    if f.endswith(".json")])

    def retrieve_messages(self, callsign: str,
                          delete: bool = False) -> list:
        """
        Retrieve all messages for a callsign.
        If delete=True, removes message files after reading.
        Returns list of message dicts with 'data' as bytes.
        """
        callsign = callsign.upper()
        msg_dir  = os.path.join(self.storage_dir, callsign)

        if not os.path.exists(msg_dir):
            return []

        messages = []
        files    = sorted(
            [f for f in os.listdir(msg_dir) if f.endswith(".json")]
        )

        for fname in files:
            fpath = os.path.join(msg_dir, fname)
            try:
                with open(fpath) as f:
                    msg = json.load(f)
                # Convert data list back to bytes
                msg["data"] = bytes(msg["data"])
                messages.append(msg)

                if delete:
                    os.remove(fpath)
                    log.debug("Deleted message file %s", fname)

            except Exception as e:
                log.error("Error reading message %s: %s", fname, e)

        log.info("Retrieved %d messages for %s (delete=%s)",
                 len(messages), callsign, delete)
        return messages

    def cleanup_old_messages(self, max_age_days: int = 30):
        """Remove messages older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0

        for callsign_dir in os.listdir(self.storage_dir):
            msg_dir = os.path.join(self.storage_dir, callsign_dir)
            if not os.path.isdir(msg_dir):
                continue

            for fname in os.listdir(msg_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(msg_dir, fname)
                try:
                    with open(fpath) as f:
                        msg = json.load(f)
                    if msg.get("received", 0) < cutoff:
                        os.remove(fpath)
                        removed += 1
                except Exception:
                    pass

        if removed:
            log.info("Cleanup removed %d old messages", removed)

    def get_stats(self) -> dict:
        """Return storage statistics."""
        stats = {"total": 0, "by_callsign": {}}
        if not os.path.exists(self.storage_dir):
            return stats

        for callsign_dir in os.listdir(self.storage_dir):
            msg_dir = os.path.join(self.storage_dir, callsign_dir)
            if not os.path.isdir(msg_dir):
                continue
            count = len([f for f in os.listdir(msg_dir)
                         if f.endswith(".json")])
            if count > 0:
                stats["by_callsign"][callsign_dir] = count
                stats["total"] += count

        return stats
