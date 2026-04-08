"""
HF-256 Session Manager
=======================
Central registry for ALL connected client sessions regardless of transport.

Design principles
-----------------
* Thread-safe: called from asyncio tasks (TCP) and OS threads (ARDOP, FreeDV).
* Minimal coupling: knows nothing about protocol; HubCore drives all logic.
* Watchdog: evicts idle sessions so dead radio links don't block session slots.
* Max-sessions cap prevents resource exhaustion.

Session lifecycle
-----------------
    create_session() → ClientSession  →  close_session()

Each transport registers a ``send_func`` that knows how to write bytes back
to its specific underlying socket/channel.  HubCore calls session.send(data)
without caring what transport is underneath.
"""

import logging
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

log = logging.getLogger("hf256.sessions")

# ── Tuneable constants ──────────────────────────────────────────────────────
MAX_SESSIONS      = 10     # hard cap across ALL transports
IDLE_TIMEOUT_SEC  = 300    # 5 min silence → evict
WATCHDOG_INTERVAL = 30     # watchdog tick interval (seconds)
AUTH_TIMEOUT_SEC  = 120    # seconds before unauthenticated session is evicted


class ClientSession:
    """
    Represents one connected remote client regardless of transport type.

    Thread-safe for use from asyncio tasks AND blocking threads.

    Attributes
    ----------
    session_id     : unique string ID (e.g. ``TCP-W1ABC-1714000000``)
    transport_type : ``"TCP"`` | ``"VHF_AX25"`` | ``"HF_AX25"`` |
                     ``"ARDOP_HF"`` | ``"ARDOP_FM"`` | ``"FREEDV"``
    callsign       : remote station callsign; set at handshake
    authenticated  : True after /auth succeeds
    download_lock  : per-session mutex so concurrent downloads don't interleave
    rx_buffer      : reassembly buffer for framed-stream transports
    created_at     : monotonic clock at creation
    last_active    : monotonic clock of last received byte
    """

    __slots__ = (
        "session_id", "transport_type", "_send_func",
        "callsign", "authenticated",
        "rx_buffer", "download_lock",
        "created_at", "last_active", "_lock",
    )

    def __init__(
        self,
        session_id: str,
        transport_type: str,
        send_func: Callable[[bytes], bool],
    ) -> None:
        self.session_id     = session_id
        self.transport_type = transport_type
        self._send_func     = send_func

        self.callsign       = ""       # populated from handshake/auth
        self.authenticated  = False

        self.rx_buffer      = bytearray()
        self.download_lock  = threading.Lock()

        self.created_at     = time.monotonic()
        self.last_active    = time.monotonic()
        self._lock          = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def touch(self) -> None:
        """Update last-active timestamp (call on every received byte)."""
        self.last_active = time.monotonic()

    def idle_seconds(self) -> float:
        """Seconds since last received data."""
        return time.monotonic() - self.last_active

    def send(self, data: bytes) -> bool:
        """
        Send raw bytes to this client.

        Thread-safe – may be called from any thread or asyncio task.
        Returns True on success, False if the transport rejected the write.
        Never raises; logs errors internally.
        """
        try:
            result = self._send_func(data)
            return bool(result)
        except Exception as exc:
            log.error(
                "Session %s [%s] send error: %s",
                self.session_id, self.transport_type, exc,
            )
            return False

    def replace_send_func(self, new_func: Callable[[bytes], bool]) -> None:
        """
        Swap the underlying send function (e.g. after transport handoff).
        Thread-safe.
        """
        with self._lock:
            self._send_func = new_func

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return JSON-serialisable status snapshot."""
        return {
            "id":        self.session_id,
            "transport": self.transport_type,
            "callsign":  self.callsign,
            "auth":      self.authenticated,
            "idle_s":    round(self.idle_seconds(), 1),
            "age_s":     round(time.monotonic() - self.created_at, 1),
        }

    def __repr__(self) -> str:
        call = self.callsign or "UNAUTH"
        return f"<Session {self.session_id} [{self.transport_type}] {call}>"


class SessionManager:
    """
    Central registry for all active ``ClientSession`` objects.

    All transports call ``create_session()`` on connect and
    ``close_session()`` on disconnect.  HubCore queries this for routing.

    Thread-safety
    -------------
    Every public method acquires ``self._lock`` for the minimum required
    duration.  Callbacks (``on_session_open`` / ``on_session_close``) are
    called *outside* the lock so they can safely call back into the manager.
    """

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        idle_timeout: int = IDLE_TIMEOUT_SEC,
        auth_timeout: int = AUTH_TIMEOUT_SEC,
    ) -> None:
        self._sessions: Dict[str, ClientSession] = {}
        self._lock      = threading.Lock()

        self.max_sessions  = max_sessions
        self.idle_timeout  = idle_timeout
        self.auth_timeout  = auth_timeout

        # Optional hooks — set by HubCore
        self.on_session_open:  Optional[Callable[[ClientSession], None]] = None
        self.on_session_close: Optional[Callable[[ClientSession], None]] = None

        self._watchdog_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(
        self,
        transport_type: str,
        send_func: Callable[[bytes], bool],
        callsign: str = "",
        session_id: Optional[str] = None,
    ) -> Optional[ClientSession]:
        """
        Register a new session.

        Returns ``None`` if the hub is at session capacity; the caller must
        then send a rejection to the remote client and close the socket.
        """
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                log.warning(
                    "Session limit (%d) reached — rejecting %s [%s]",
                    self.max_sessions, callsign or "?", transport_type,
                )
                return None

            sid = session_id or (
                f"{transport_type}-{callsign or uuid.uuid4().hex[:6]}"
                f"-{int(time.time())}"
            )
            # Guard against duplicate IDs (shouldn't happen but be safe)
            while sid in self._sessions:
                sid = f"{sid}-{uuid.uuid4().hex[:4]}"

            session              = ClientSession(sid, transport_type, send_func)
            session.callsign     = callsign.upper() if callsign else ""
            self._sessions[sid]  = session

        log.info(
            "Session created: %s [%s] call=%s  total=%d",
            sid, transport_type, callsign or "?", len(self._sessions),
        )

        if self.on_session_open:
            try:
                self.on_session_open(session)
            except Exception as exc:
                log.error("on_session_open error: %s", exc, exc_info=True)

        return session

    def close_session(self, session_id: str) -> None:
        """Deregister a session by ID.  No-op if already removed."""
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            return

        log.info(
            "Session closed: %s [%s] call=%s  total=%d",
            session_id, session.transport_type,
            session.callsign or "UNAUTH",
            len(self._sessions),
        )

        if self.on_session_close:
            try:
                self.on_session_close(session)
            except Exception as exc:
                log.error("on_session_close error: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> Optional[ClientSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def by_callsign(self, callsign: str) -> Optional[ClientSession]:
        """First authenticated session matching *callsign* (case-insensitive)."""
        callsign = callsign.upper()
        with self._lock:
            for s in self._sessions.values():
                if s.callsign == callsign and s.authenticated:
                    return s
        return None

    def all(self) -> List[ClientSession]:
        """Snapshot of all sessions (safe to iterate after return)."""
        with self._lock:
            return list(self._sessions.values())

    def authenticated(self) -> List[ClientSession]:
        with self._lock:
            return [s for s in self._sessions.values() if s.authenticated]

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def status_list(self) -> List[dict]:
        """Serialisable status for the web API."""
        with self._lock:
            return [s.to_dict() for s in self._sessions.values()]

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def start_watchdog(self, watchdog_interval: int = WATCHDOG_INTERVAL) -> None:
        """Start the background eviction thread (idempotent)."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_interval = watchdog_interval
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="session-watchdog",
        )
        self._watchdog_thread.start()
        log.info(
            "Session watchdog started "
            "(idle=%ds auth_timeout=%ds interval=%ds)",
            self.idle_timeout, self.auth_timeout, watchdog_interval,
        )

    def _watchdog_loop(self) -> None:
        interval = getattr(self, "_watchdog_interval", WATCHDOG_INTERVAL)
        while True:
            time.sleep(interval)
            try:
                self._evict_idle()
                self._evict_unauthenticated()
            except Exception as exc:
                log.error("Watchdog tick error: %s", exc, exc_info=True)

    def _evict_idle(self) -> None:
        with self._lock:
            idle = [
                sid
                for sid, s in self._sessions.items()
                if s.authenticated and s.idle_seconds() > self.idle_timeout
            ]
        for sid in idle:
            log.warning("Evicting idle session: %s", sid)
            self.close_session(sid)

    def _evict_unauthenticated(self) -> None:
        """Remove sessions that never authenticated within auth_timeout."""
        with self._lock:
            unauth = [
                sid
                for sid, s in self._sessions.items()
                if not s.authenticated
                and (time.monotonic() - s.created_at) > self.auth_timeout
            ]
        for sid in unauth:
            log.warning("Evicting unauthenticated session: %s", sid)
            self.close_session(sid)
