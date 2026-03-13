"""
HF-256 File Transfer
File server (hub) and file receiver (spoke) implementations.
"""

import os
import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Optional, Callable

log = logging.getLogger("hf256.filetransfer")

# FreeDV file size and chunk limits
FREEDV_MAX_FILE_SIZE = 150 * 1024   # 150KB max over FreeDV HF
CHUNK_SIZE_FREEDV    = 4096          # 4KB chunks for FreeDV
CHUNK_SIZE_TCP       = 65536         # 64KB chunks for TCP


@dataclass
class FileInfo:
    """Metadata for a file available on the hub."""
    name:        str
    size:        int
    sha256:      str
    description: str = ""


class FileServer:
    """
    Hub-side file server.
    Serves files from files_dir to authenticated spokes.
    """

    def __init__(self, files_dir: str):
        self.files_dir = files_dir
        Path(files_dir).mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, FileInfo] = {}
        self.scan_files()

    def scan_files(self):
        """Scan files_dir and update the file cache."""
        self._cache.clear()
        if not os.path.exists(self.files_dir):
            return

        for fname in sorted(os.listdir(self.files_dir)):
            fpath = os.path.join(self.files_dir, fname)
            if not os.path.isfile(fpath):
                continue
            if fname.startswith("."):
                continue

            try:
                size   = os.path.getsize(fpath)
                sha256 = self._hash_file(fpath)
                self._cache[fname] = FileInfo(
                    name=fname,
                    size=size,
                    sha256=sha256
                )
            except Exception as e:
                log.error("Error scanning %s: %s", fname, e)

        log.info("FileServer: %d files available", len(self._cache))

    def get_file_list(self) -> dict:
        """Return file list as dict suitable for FileListResponse."""
        return {
            fname: {
                "size":        info.size,
                "description": info.description
            }
            for fname, info in self._cache.items()
        }

    def get_file_info(self, filename: str) -> Optional[FileInfo]:
        """Return FileInfo for a named file, or None if not found."""
        return self._cache.get(filename)

    def _hash_file(self, path: str) -> str:
        """Compute SHA-256 of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()


class FileReceiver:
    """
    Spoke-side file receiver.
    Accumulates chunks and writes to download_dir on completion.
    """

    def __init__(self, download_dir: str):
        self.download_dir   = download_dir
        Path(download_dir).mkdir(parents=True, exist_ok=True)

        self._receiving      = False
        self._filename       = None
        self._total_chunks   = 0
        self._expected_hash  = None
        self._chunks: Dict[int, bytes] = {}
        self._progress_cb:  Optional[Callable] = None

    @property
    def current_filename(self) -> Optional[str]:
        return self._filename

    def is_receiving(self) -> bool:
        return self._receiving

    def start_receive(self, filename: str, total_chunks: int,
                      file_hash: str,
                      progress_callback: Optional[Callable] = None):
        """Initialize receiver for a new file transfer."""
        self._receiving     = True
        self._filename      = filename
        self._total_chunks  = total_chunks
        self._expected_hash = file_hash
        self._chunks        = {}
        self._progress_cb   = progress_callback
        log.info("Starting receive: %s (%d chunks)", filename, total_chunks)

    def receive_chunk(self, chunk_num: int,
                      chunk_data: bytes) -> bool:
        """
        Store a received chunk.
        Returns True if all chunks received and file saved successfully.
        """
        if not self._receiving:
            log.warning("receive_chunk called but not receiving")
            return False

        self._chunks[chunk_num] = chunk_data
        received = len(self._chunks)

        if self._progress_cb:
            try:
                progress = received / self._total_chunks
                self._progress_cb(
                    self._filename, progress,
                    received, self._total_chunks
                )
            except Exception:
                pass

        log.debug("Chunk %d/%d received for %s",
                  chunk_num + 1, self._total_chunks, self._filename)

        if received >= self._total_chunks:
            return self._finalize()

        return False

    def _finalize(self) -> bool:
        """Assemble chunks and write file to download_dir."""
        try:
            # Assemble in order
            data = b"".join(
                self._chunks[i]
                for i in sorted(self._chunks.keys())
            )

            # Verify hash
            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != self._expected_hash:
                log.error("Hash mismatch for %s: expected %s got %s",
                          self._filename,
                          self._expected_hash[:16],
                          actual_hash[:16])
                self.cancel()
                return False

            # Write file
            outpath = os.path.join(self.download_dir, self._filename)
            with open(outpath, "wb") as f:
                f.write(data)

            log.info("File saved: %s (%d bytes)", outpath, len(data))
            self._receiving = False
            return True

        except Exception as e:
            log.error("File finalization error: %s", e, exc_info=True)
            self.cancel()
            return False

    def cancel(self):
        """Cancel current file transfer."""
        if self._receiving:
            log.info("File transfer cancelled: %s", self._filename)
        self._receiving    = False
        self._filename     = None
        self._total_chunks = 0
        self._chunks       = {}
