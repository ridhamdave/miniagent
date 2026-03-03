"""
Session storage: append-only JSONL files, one per session key.

File format — one JSON object per line:
  {"id":"msg_abc","role":"user","content":"Hello","created_at":"2026-03-02T12:00:00Z"}
  {"id":"msg_def","role":"assistant","content":"Hi!","created_at":"...","run_id":"run-1"}

JSONL chosen because:
- Append-only → no file rewrite → crash-safe
- Each line independently parseable → easy to tail/grep/stream
- Same format OpenClaw uses for Pi transcripts (session-utils.fs.ts)

Storage path: {sessions_dir}/{sanitized_session_key}.jsonl
OpenClaw: resolveSessionFilePath() in src/config/sessions.ts
"""

import json
from pathlib import Path

from miniagent.config import get_config

try:
    import aiofiles  # type: ignore[import-untyped]
    _AIOFILES_AVAILABLE = True
except ImportError:
    _AIOFILES_AVAILABLE = False


class SessionStore:
    """
    Append-only JSONL conversation history store.

    Uses aiofiles for async I/O when available; falls back to sync I/O otherwise.
    The sessions directory is created on first write if it does not exist.
    """

    def __init__(self, sessions_dir: str | None = None) -> None:
        if sessions_dir is None:
            sessions_dir = get_config().sessions_dir
        self.sessions_dir: Path = Path(sessions_dir).expanduser()

    def _session_path(self, session_key: str) -> Path:
        """Return the path for a session file, sanitizing the key."""
        safe = "".join(c for c in session_key if c.isalnum() or c in "-_")
        return self.sessions_dir / f"{safe}.jsonl"

    def _ensure_dir(self) -> None:
        """Create the sessions directory if it does not exist."""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    async def append(self, session_key: str, entry: dict) -> None:
        """
        Serialize entry as JSON and append it as a new line to the session file.
        Creates the sessions directory and file if they do not exist.
        """
        self._ensure_dir()
        path = self._session_path(session_key)
        line = json.dumps(entry) + "\n"
        if _AIOFILES_AVAILABLE:
            async with aiofiles.open(path, "a") as f:
                await f.write(line)
        else:
            with path.open("a") as f:
                f.write(line)

    async def load(self, session_key: str, limit: int = 50) -> list[dict]:
        """
        Read the last `limit` entries from the session file.
        Returns an empty list if the session file does not exist.
        Each line is parsed as a JSON object.
        """
        path = self._session_path(session_key)
        if not path.exists():
            return []

        records: list[dict] = []
        if _AIOFILES_AVAILABLE:
            async with aiofiles.open(path, "r") as f:
                async for line in f:
                    stripped = line.strip()
                    if stripped:
                        records.append(json.loads(stripped))
        else:
            with path.open("r") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        records.append(json.loads(stripped))

        return records[-limit:] if limit > 0 else records

    async def clear(self, session_key: str) -> None:
        """
        Delete the session file if it exists.
        Does not raise if the file does not exist.
        """
        path = self._session_path(session_key)
        if path.exists():
            path.unlink()
