from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactTooLargeError(ArtifactStoreError):
    pass


class ArtifactStoreUnavailableError(ArtifactStoreError):
    pass


@dataclass(frozen=True)
class ArtifactPointer:
    backend: str
    run_id: str
    sha256: str
    bytes: int
    path: str | None = None
    redis_key: str | None = None
    content_encoding: str | None = None
    extra: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "backend": self.backend,
            "run_id": self.run_id,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }
        if self.path:
            payload["path"] = self.path
        if self.redis_key:
            payload["redis_key"] = self.redis_key
        if self.content_encoding:
            payload["content_encoding"] = self.content_encoding
        if self.extra:
            payload.update(self.extra)
        return payload


def new_run_id(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def sanitize_label(label: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in label.strip())
    safe = safe.strip("_")
    return safe[:80] if safe else "run"


class ArtifactStore:
    """Pluggable persistence for batch artifacts.

    Implementations must be synchronous (callers can run them in a worker thread).
    """

    backend: str = "unknown"

    def write_json(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any],
        label: str,
        run_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ArtifactPointer:
        raise NotImplementedError

    def read_json(self, *, run_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def list(
        self,
        *,
        tool: str | None = None,
        label_contains: str | None = None,
        since_epoch: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class DiskArtifactStore(ArtifactStore):
    backend = "disk"

    def __init__(self, artifact_dir: str):
        self._artifact_dir = os.path.abspath(os.path.expanduser(artifact_dir))

    @property
    def artifact_dir(self) -> str:
        return self._artifact_dir

    def _write_text_atomic(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp.{uuid.uuid4().hex}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)

    def write_json(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any],
        label: str,
        run_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ArtifactPointer:
        if created_at is None:
            created_at = datetime.now(timezone.utc)

        if run_id is None:
            run_id = new_run_id(created_at)
        filename = f"{tool_name}-{sanitize_label(label)}-{run_id}.json"
        artifact_path = os.path.join(self._artifact_dir, filename)

        enriched = {
            "tool": tool_name,
            "run_id": run_id,
            "created_at": created_at.isoformat(),
            **payload,
        }

        encoded = json.dumps(enriched, indent=2, ensure_ascii=False)
        self._write_text_atomic(artifact_path, encoded)

        sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        size_bytes = len(encoded.encode("utf-8"))
        return ArtifactPointer(
            backend="disk",
            run_id=run_id,
            sha256=sha256,
            bytes=size_bytes,
            path=os.path.abspath(artifact_path),
            content_encoding="json",
        )

    def _parse_filename(self, filename: str) -> dict[str, str] | None:
        if not filename.endswith(".json"):
            return None
        import re

        m = re.match(
            r"^(?P<tool>[^-]+)-(?P<label>.+)-(?P<run_id>\d{8}T\d{6}Z-[0-9a-f]{8})\.json$",
            filename,
        )
        if not m:
            return None
        return {"tool": m.group("tool"), "label": m.group("label"), "run_id": m.group("run_id")}

    def read_json(self, *, run_id: str) -> dict[str, Any]:
        run_id = run_id.strip()
        if not run_id:
            raise ArtifactStoreError("empty run_id")

        if not os.path.isdir(self._artifact_dir):
            raise FileNotFoundError(self._artifact_dir)

        candidates: list[str] = []
        for name in os.listdir(self._artifact_dir):
            if run_id in name and name.endswith(".json"):
                candidates.append(os.path.join(self._artifact_dir, name))
        if not candidates:
            raise FileNotFoundError(run_id)
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)

        with open(candidates[0], "r", encoding="utf-8") as f:
            return json.loads(f.read())

    def list(
        self,
        *,
        tool: str | None = None,
        label_contains: str | None = None,
        since_epoch: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not os.path.isdir(self._artifact_dir):
            return []

        out: list[dict[str, Any]] = []
        for name in os.listdir(self._artifact_dir):
            parsed = self._parse_filename(name)
            if parsed is None:
                continue
            if tool and parsed.get("tool") != tool:
                continue
            if label_contains and label_contains not in (parsed.get("label") or ""):
                continue

            full_path = os.path.join(self._artifact_dir, name)
            try:
                st = os.stat(full_path)
            except FileNotFoundError:
                continue

            if since_epoch is not None and st.st_mtime < since_epoch:
                continue

            out.append(
                {
                    **parsed,
                    "backend": "disk",
                    "path": os.path.abspath(full_path),
                    "bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                }
            )

        out.sort(key=lambda a: a.get("mtime", ""), reverse=True)
        return out[:limit]


class RedisArtifactStore(ArtifactStore):
    backend = "redis"

    def __init__(
        self,
        *,
        redis_url: str,
        key_prefix: str = "jmcp:artifact",
        ttl_seconds: int = 0,
        gzip_enabled: bool = False,
        max_bytes: int | None = None,
        reserve_bytes: int = 5_000_000,
    ):
        self._redis_url = redis_url
        self._prefix = key_prefix.rstrip(":")
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._gzip_enabled = bool(gzip_enabled)
        self._max_bytes = None if max_bytes is None else int(max_bytes)
        self._reserve_bytes = max(0, int(reserve_bytes))

        try:
            import redis  # type: ignore

            self._redis = redis.from_url(redis_url, decode_responses=False)
        except Exception as e:  # pragma: no cover
            raise ArtifactStoreUnavailableError(
                f"Redis backend unavailable (install 'redis' and configure JMCP_REDIS_URL): {e}"
            ) from e

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}:{suffix}"

    def _blob_key(self, run_id: str) -> str:
        return self._k(f"blob:{run_id}")

    def _meta_key(self, run_id: str) -> str:
        return self._k(f"meta:{run_id}")

    def _index_time_key(self) -> str:
        return self._k("index:time")

    def _index_tool_key(self, tool: str) -> str:
        return self._k(f"index:tool:{tool}")

    def _maybe_check_limits(self, to_store_bytes: int) -> None:
        if self._max_bytes is not None and to_store_bytes > self._max_bytes:
            raise ArtifactTooLargeError(
                f"Artifact size {to_store_bytes} exceeds configured max_bytes={self._max_bytes}"
            )

        # Best-effort free-space check if maxmemory is configured.
        try:
            info = self._redis.info("memory")
            maxmemory = int(info.get("maxmemory") or 0)
            used_memory = int(info.get("used_memory") or 0)
            if maxmemory > 0:
                free = maxmemory - used_memory
                if free <= 0:
                    raise ArtifactStoreUnavailableError("Redis maxmemory exhausted")
                if to_store_bytes + self._reserve_bytes > free:
                    raise ArtifactStoreUnavailableError(
                        f"Redis free memory too low (free={free}, needed={to_store_bytes}, reserve={self._reserve_bytes})"
                    )
        except ArtifactStoreError:
            raise
        except Exception:
            # If INFO is not permitted, fall back to max_bytes only.
            return

    def write_json(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any],
        label: str,
        run_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ArtifactPointer:
        if created_at is None:
            created_at = datetime.now(timezone.utc)

        if run_id is None:
            run_id = new_run_id(created_at)

        enriched = {
            "tool": tool_name,
            "run_id": run_id,
            "created_at": created_at.isoformat(),
            **payload,
        }

        json_bytes = json.dumps(enriched, indent=2, ensure_ascii=False).encode("utf-8")
        sha256 = hashlib.sha256(json_bytes).hexdigest()

        content_encoding = "json"
        blob = json_bytes
        if self._gzip_enabled:
            blob = gzip.compress(json_bytes)
            content_encoding = "json+gzip"

        self._maybe_check_limits(len(blob))

        blob_key = self._blob_key(run_id)
        meta_key = self._meta_key(run_id)
        now_ms = int(time.time() * 1000)

        pipe = self._redis.pipeline(transaction=True)
        pipe.set(blob_key, blob)
        pipe.hset(
            meta_key,
            mapping={
                b"tool": tool_name.encode("utf-8"),
                b"label": sanitize_label(label).encode("utf-8"),
                b"created_at": created_at.isoformat().encode("utf-8"),
                b"sha256": sha256.encode("utf-8"),
                b"bytes": str(len(blob)).encode("utf-8"),
                b"uncompressed_bytes": str(len(json_bytes)).encode("utf-8"),
                b"content_encoding": content_encoding.encode("utf-8"),
            },
        )
        pipe.zadd(self._index_time_key(), {run_id: now_ms})
        pipe.zadd(self._index_tool_key(tool_name), {run_id: now_ms})

        if self._ttl_seconds > 0:
            pipe.expire(blob_key, self._ttl_seconds)
            pipe.expire(meta_key, self._ttl_seconds)
            # Index TTL is optional; keep it aligned to reduce leak.
            pipe.expire(self._index_time_key(), self._ttl_seconds)
            pipe.expire(self._index_tool_key(tool_name), self._ttl_seconds)

        pipe.execute()

        return ArtifactPointer(
            backend="redis",
            run_id=run_id,
            sha256=sha256,
            bytes=len(blob),
            redis_key=blob_key,
            content_encoding=content_encoding,
        )

    def read_json(self, *, run_id: str) -> dict[str, Any]:
        run_id = run_id.strip()
        if not run_id:
            raise ArtifactStoreError("empty run_id")

        blob = self._redis.get(self._blob_key(run_id))
        if blob is None:
            raise FileNotFoundError(run_id)

        # Determine encoding from meta, but fall back to trying gzip.
        encoding = None
        try:
            meta = self._redis.hget(self._meta_key(run_id), b"content_encoding")
            if isinstance(meta, (bytes, bytearray)):
                encoding = meta.decode("utf-8", errors="ignore")
        except Exception:
            encoding = None

        data = bytes(blob)
        if encoding == "json+gzip":
            data = gzip.decompress(data)
        elif encoding is None and data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)

        return json.loads(data.decode("utf-8"))

    def list(
        self,
        *,
        tool: str | None = None,
        label_contains: str | None = None,
        since_epoch: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, limit)
        max_items = min(limit * 5, 2000)  # give room for label filtering

        zkey = self._index_tool_key(tool) if tool else self._index_time_key()

        min_score = "-inf"
        if since_epoch is not None:
            min_score = int(since_epoch * 1000)

        run_ids: list[bytes] = self._redis.zrevrangebyscore(zkey, "+inf", min_score, start=0, num=max_items)

        out: list[dict[str, Any]] = []
        for rid_b in run_ids:
            rid = rid_b.decode("utf-8") if isinstance(rid_b, (bytes, bytearray)) else str(rid_b)

            meta = self._redis.hgetall(self._meta_key(rid))
            if not meta:
                continue

            tool_b = meta.get(b"tool")
            label_b = meta.get(b"label")
            created_at_b = meta.get(b"created_at")
            bytes_b = meta.get(b"bytes")

            tool_s = tool_b.decode("utf-8") if isinstance(tool_b, (bytes, bytearray)) else None
            label_s = label_b.decode("utf-8") if isinstance(label_b, (bytes, bytearray)) else None
            created_at_s = created_at_b.decode("utf-8") if isinstance(created_at_b, (bytes, bytearray)) else None
            bytes_s = bytes_b.decode("utf-8") if isinstance(bytes_b, (bytes, bytearray)) else None

            if label_contains and (label_s is None or label_contains not in label_s):
                continue

            try:
                bytes_i = int(bytes_s) if bytes_s else None
            except Exception:
                bytes_i = None

            out.append(
                {
                    "backend": "redis",
                    "tool": tool_s,
                    "label": label_s,
                    "run_id": rid,
                    "redis_key": self._blob_key(rid),
                    "bytes": bytes_i,
                    "mtime": created_at_s,  # keep key compatible with disk listing
                    "created_at": created_at_s,
                }
            )

            if len(out) >= limit:
                break

        return out


class DualArtifactStore(ArtifactStore):
    backend = "dual"

    def __init__(self, primary: ArtifactStore, secondary: ArtifactStore):
        self._primary = primary
        self._secondary = secondary

    def write_json(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any],
        label: str,
        run_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ArtifactPointer:
        if created_at is None:
            created_at = datetime.now(timezone.utc)

        if run_id is None:
            run_id = new_run_id(created_at)

        primary_ptr = self._primary.write_json(
            tool_name=tool_name,
            payload=payload,
            label=label,
            run_id=run_id,
            created_at=created_at,
        )
        secondary_ptr = None
        try:
            secondary_ptr = self._secondary.write_json(
                tool_name=tool_name,
                payload=payload,
                label=label,
                run_id=run_id,
                created_at=created_at,
            )
        except Exception:
            secondary_ptr = None

        extra: dict[str, Any] | None = None
        if secondary_ptr is not None:
            extra = {"secondary": secondary_ptr.as_dict()}

        # Return primary pointer fields plus secondary pointer as extra.
        return ArtifactPointer(
            backend="dual",
            run_id=primary_ptr.run_id,
            sha256=primary_ptr.sha256,
            bytes=primary_ptr.bytes,
            path=primary_ptr.path,
            redis_key=primary_ptr.redis_key,
            content_encoding=primary_ptr.content_encoding,
            extra=extra,
        )

    def read_json(self, *, run_id: str) -> dict[str, Any]:
        try:
            return self._primary.read_json(run_id=run_id)
        except Exception:
            return self._secondary.read_json(run_id=run_id)

    def list(
        self,
        *,
        tool: str | None = None,
        label_contains: str | None = None,
        since_epoch: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        primary = self._primary.list(tool=tool, label_contains=label_contains, since_epoch=since_epoch, limit=limit)
        if len(primary) >= limit:
            return primary
        secondary = self._secondary.list(
            tool=tool,
            label_contains=label_contains,
            since_epoch=since_epoch,
            limit=limit,
        )

        seen = {a.get("run_id") for a in primary}
        for item in secondary:
            if item.get("run_id") in seen:
                continue
            primary.append(item)
            if len(primary) >= limit:
                break
        return primary
