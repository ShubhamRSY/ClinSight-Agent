"""In-memory TTL cache for identical visualization query responses."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Optional

from app.schemas.input import QueryRequest
from app.schemas.output import VisualizationResponse


class QueryResponseCache:
    """Simple process-local cache keyed by normalized QueryRequest JSON."""

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 128):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, VisualizationResponse]] = {}

    @staticmethod
    def cache_key(request: QueryRequest) -> str:
        payload = request.model_dump(mode="json", exclude_none=True)
        # Stable key independent of dict insertion order.
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, request: QueryRequest) -> Optional[VisualizationResponse]:
        if self.ttl_seconds <= 0:
            return None
        key = self.cache_key(request)
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at < now:
                self._store.pop(key, None)
                return None
            return value

    def set(self, request: QueryRequest, response: VisualizationResponse) -> None:
        if self.ttl_seconds <= 0:
            return
        key = self.cache_key(request)
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            if len(self._store) >= self.max_entries:
                # Drop expired first, then oldest insertion.
                now = time.monotonic()
                expired = [k for k, (exp, _) in self._store.items() if exp < now]
                for k in expired:
                    self._store.pop(k, None)
                while len(self._store) >= self.max_entries:
                    self._store.pop(next(iter(self._store)))
            self._store[key] = (expires_at, response)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"entries": len(self._store), "ttl_seconds": self.ttl_seconds}
