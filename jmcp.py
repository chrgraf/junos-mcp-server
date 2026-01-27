###
# Copyright (c) 1999-2025, Juniper Networks Inc.
#
#  All rights reserved.
#
#  License: Apache 2.0
#
#  THIS SOFTWARE IS PROVIDED BY Juniper Networks Inc. ''AS IS'' AND ANY
#  EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL Juniper Networks Inc. BE LIABLE FOR ANY
#  DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#  (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#  ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
###

from __future__ import annotations as _annotations

import argparse
import time
from datetime import datetime, timezone
import hashlib
import logging
import os
import math
import uuid
from jinja2 import Environment, FileSystemLoader, TemplateNotFound
import json
import yaml
import sys
import signal
from typing import Any, Sequence
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import suppress

from typing import Dict, Any, Generic, Literal
from pydantic import BaseModel, Field
from pydantic.networks import AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp.server.elicitation import (
    AcceptedElicitation,
    DeclinedElicitation,
    CancelledElicitation,
)

import anyio
from anyio import CapacityLimiter
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.stdio import stdio_server
from mcp.server.session import ServerSession, ServerSessionT
from mcp.server.elicitation import ElicitationResult, ElicitSchemaModelT, elicit_with_validation

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.lowlevel.server import LifespanResultT
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.lowlevel.server import lifespan as default_lifespan

from mcp.shared.context import LifespanContextT, RequestContext, RequestT
from mcp.types import (
    AnyFunction,
    ContentBlock,
    GetPromptResult,
    ToolAnnotations,
)
from mcp.types import Prompt as MCPPrompt
from mcp.types import PromptArgument as MCPPromptArgument
from mcp.types import Resource as MCPResource
from mcp.types import ResourceTemplate as MCPResourceTemplate
from mcp.types import Tool as MCPTool
from jinja2 import Environment, TemplateError

from jnpr.junos import Device
from jnpr.junos.exception import ConnectError, ConfigLoadError, CommitError, LockError
from jnpr.junos.utils.config import Config

from utils.config import prepare_connection_params, validate_device_config, validate_all_devices
from jmcp_connection_pool import JunosConnectionPool

from artifact_store import (
    ArtifactStore,
    ArtifactStoreError,
    ArtifactStoreUnavailableError,
    ArtifactTooLargeError,
    DiskArtifactStore,
    DualArtifactStore,
    RedisArtifactStore,
)

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger('jmcp-server')

# Global variable for devices (parsed from JSON file)
devices = {}

# Global connection pool (initialized in main())
connection_pool: JunosConnectionPool | None = None

# Junos MCP Server
JUNOS_MCP = 'jmcp-server'

def _default_max_workers() -> int:
        """Compute a conservative default worker count.

    Heuristic: $\\lceil cpu\\_cores \\times 1.5 \\rceil$.

        Notes:
        - This workload is mostly network I/O bound, but excessive concurrency can
            degrade performance (device-side limits, host scheduling, queueing).
        - Apply a small floor for low-core systems and a safety cap for very large
            hosts.
        - Use `--workers-per-core` to scale up/down deterministically.
        """

        cpu_cores = os.cpu_count() or 4
        desired = math.ceil(cpu_cores * 1.5)
        return max(8, min(80, desired))


# Thread pool configuration for I/O-bound SSH operations
default_workers = _default_max_workers()

# Note: MAX_WORKERS and thread_limiter will be initialized in main() after argument parsing
MAX_WORKERS = default_workers
thread_limiter = None

# Operator-facing: the configured CLI value for `--workers-per-core` (or None).
WORKERS_PER_CORE: float | None = None


def _get_batch_response_mode() -> str:
    """Return response mode for batch tools.

    Modes:
        - "full" (default): include full per-router/per-command outputs
        - "summary": return only summary + small router metadata
        - "artifact": persist full results to disk and return only summary + artifact pointer

    Priority:
        1) `JMCP_BATCH_RESPONSE_MODE` env var
        2) `.jmcp_batch_response_mode` file next to this `jmcp.py`
        3) default "full"
    """
    mode = os.getenv("JMCP_BATCH_RESPONSE_MODE")
    if mode:
        mode = mode.strip().lower()
        if mode in {"full", "summary", "artifact"}:
            return mode

    config_path = os.path.join(os.path.dirname(__file__), ".jmcp_batch_response_mode")
    try:
        if os.path.exists(config_path):
            raw = open(config_path, "r", encoding="utf-8").read().strip().lower()
            if raw in {"full", "summary", "artifact"}:
                return raw
    except Exception as e:
        log.warning(f"Failed to read {config_path}: {e}")

    return "full"


def _truthy_env(var_name: str) -> bool:
    value = os.getenv(var_name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_artifact_backend(arguments: dict | None = None) -> str:
    """Resolve artifact backend.

    Priority:
      1) tool argument `artifact_backend`
      2) `JMCP_ARTIFACT_BACKEND` env var
      3) `.jmcp_artifact_backend` file next to this `jmcp.py`
      4) default: "disk"

    Values: "disk", "redis", "dual".
    """
    if arguments:
        arg_backend = arguments.get("artifact_backend")
        if isinstance(arg_backend, str) and arg_backend.strip():
            raw = arg_backend.strip().lower()
            if raw in {"disk", "redis", "dual"}:
                return raw

    env_backend = os.getenv("JMCP_ARTIFACT_BACKEND")
    if env_backend and env_backend.strip():
        raw = env_backend.strip().lower()
        if raw in {"disk", "redis", "dual"}:
            return raw

    config_path = os.path.join(os.path.dirname(__file__), ".jmcp_artifact_backend")
    try:
        if os.path.exists(config_path):
            raw = open(config_path, "r", encoding="utf-8").read().strip().lower()
            if raw in {"disk", "redis", "dual"}:
                return raw
    except Exception as e:
        log.warning(f"Failed to read {config_path}: {e}")

    return "disk"


def _int_env(var_name: str, default: int | None = None) -> int | None:
    raw = os.getenv(var_name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _get_redis_url() -> str | None:
    # Artifact-specific env var wins; then generic.
    for name in ("JMCP_ARTIFACT_REDIS_URL", "JMCP_REDIS_URL"):
        val = os.getenv(name)
        if val and val.strip():
            return val.strip()

    # Convenience knobs: build a URL from host/port/(optional) auth.
    host = _get_redis_host()
    port = _get_redis_port()
    db = _get_redis_db()
    if host and port:
        return _build_redis_url(host=host, port=port, db=db, username=_get_redis_username(), password=_get_redis_password())

    return None


def _get_redis_host() -> str:
    # Default local Redis.
    val = os.getenv("JMCP_ARTIFACT_REDIS_HOST") or os.getenv("JMCP_REDIS_HOST")
    return val.strip() if val and val.strip() else "127.0.0.1"


def _get_redis_port() -> int:
    return _int_env("JMCP_ARTIFACT_REDIS_PORT", _int_env("JMCP_REDIS_PORT", 6379)) or 6379


def _get_redis_db() -> int:
    return _int_env("JMCP_ARTIFACT_REDIS_DB", _int_env("JMCP_REDIS_DB", 0)) or 0


def _get_redis_username() -> str | None:
    val = os.getenv("JMCP_ARTIFACT_REDIS_USERNAME") or os.getenv("JMCP_REDIS_USERNAME")
    if val and val.strip():
        return val.strip()
    return None


def _get_redis_password() -> str | None:
    val = os.getenv("JMCP_ARTIFACT_REDIS_PASSWORD") or os.getenv("JMCP_REDIS_PASSWORD")
    if val and val.strip():
        return val.strip()
    return None


def _build_redis_url(*, host: str, port: int, db: int, username: str | None, password: str | None) -> str:
    # Keep URL construction in one place; supports ACL auth when username is provided.
    from urllib.parse import quote

    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"
    elif password and not username:
        # Redis AUTH <password>
        auth = f":{quote(password)}@"
    elif username and not password:
        # Unusual but valid in URL form.
        auth = f"{quote(username)}@"

    return f"redis://{auth}{host}:{int(port)}/{int(db)}"


def _get_redis_prefix() -> str:
    val = os.getenv("JMCP_ARTIFACT_REDIS_PREFIX")
    return val.strip() if val and val.strip() else "jmcp:artifact"


def _get_redis_ttl_seconds() -> int:
    # Default TTL: one week. Set to 0 to disable expiration.
    return _int_env("JMCP_ARTIFACT_REDIS_TTL_SECONDS", 604_800) or 0


def _get_redis_max_bytes() -> int | None:
    v = _int_env("JMCP_ARTIFACT_REDIS_MAX_BYTES", None)
    if v is None or v <= 0:
        return None
    return v


def _get_redis_reserve_bytes() -> int:
    return _int_env("JMCP_ARTIFACT_REDIS_RESERVE_BYTES", 5_000_000) or 5_000_000


def _get_artifact_gzip_enabled() -> bool:
    # Global gzip knob; backend-specific knob supported for convenience.
    if _truthy_env("JMCP_ARTIFACT_GZIP"):
        return True
    if _truthy_env("JMCP_ARTIFACT_REDIS_GZIP"):
        return True
    return False


_ARTIFACT_STORE_CACHE: dict[tuple[Any, ...], ArtifactStore] = {}


def _get_artifact_store(arguments: dict | None = None) -> ArtifactStore:
    backend = _get_artifact_backend(arguments)
    artifact_dir = _get_artifact_dir(arguments or {})

    if backend == "disk":
        cache_key = ("disk", artifact_dir)
        store = _ARTIFACT_STORE_CACHE.get(cache_key)
        if store is None:
            store = DiskArtifactStore(artifact_dir)
            _ARTIFACT_STORE_CACHE[cache_key] = store
        return store

    redis_url = _get_redis_url()
    if not redis_url:
        raise ArtifactStoreUnavailableError(
            "Redis backend selected but no Redis URL configured (set JMCP_REDIS_URL or JMCP_ARTIFACT_REDIS_URL)."
        )

    redis_prefix = _get_redis_prefix()
    ttl_seconds = _get_redis_ttl_seconds()
    gzip_enabled = _get_artifact_gzip_enabled()
    max_bytes = _get_redis_max_bytes()
    reserve_bytes = _get_redis_reserve_bytes()

    if backend == "redis":
        cache_key = ("redis", redis_url, redis_prefix, ttl_seconds, gzip_enabled, max_bytes, reserve_bytes)
        store = _ARTIFACT_STORE_CACHE.get(cache_key)
        if store is None:
            store = RedisArtifactStore(
                redis_url=redis_url,
                key_prefix=redis_prefix,
                ttl_seconds=ttl_seconds,
                gzip_enabled=gzip_enabled,
                max_bytes=max_bytes,
                reserve_bytes=reserve_bytes,
            )
            _ARTIFACT_STORE_CACHE[cache_key] = store
        return store

    if backend == "dual":
        # Default: Redis is primary, disk is secondary.
        cache_key = (
            "dual",
            redis_url,
            redis_prefix,
            ttl_seconds,
            gzip_enabled,
            max_bytes,
            reserve_bytes,
            artifact_dir,
        )
        store = _ARTIFACT_STORE_CACHE.get(cache_key)
        if store is None:
            primary = RedisArtifactStore(
                redis_url=redis_url,
                key_prefix=redis_prefix,
                ttl_seconds=ttl_seconds,
                gzip_enabled=gzip_enabled,
                max_bytes=max_bytes,
                reserve_bytes=reserve_bytes,
            )
            secondary = DiskArtifactStore(artifact_dir)
            store = DualArtifactStore(primary=primary, secondary=secondary)
            _ARTIFACT_STORE_CACHE[cache_key] = store
        return store

    # Defensive fallback
    return DiskArtifactStore(artifact_dir)


def _get_artifact_dir(arguments: dict | None = None) -> str:
    """Resolve artifact directory.

    Priority:
      1) tool argument `artifact_dir`
      2) `JMCP_ARTIFACT_DIR` env var
      3) `.jmcp_artifact_dir` file next to this `jmcp.py`
      4) default: `<jmcp.py dir>/artifacts`
    """
    if arguments:
        arg_dir = arguments.get("artifact_dir")
        if isinstance(arg_dir, str) and arg_dir.strip():
            return os.path.abspath(os.path.expanduser(arg_dir.strip()))

    env_dir = os.getenv("JMCP_ARTIFACT_DIR")
    if env_dir and env_dir.strip():
        return os.path.abspath(os.path.expanduser(env_dir.strip()))

    config_path = os.path.join(os.path.dirname(__file__), ".jmcp_artifact_dir")
    try:
        if os.path.exists(config_path):
            raw = open(config_path, "r", encoding="utf-8").read().strip()
            if raw:
                return os.path.abspath(os.path.expanduser(raw))
    except Exception as e:
        log.warning(f"Failed to read {config_path}: {e}")

    return os.path.abspath(os.path.join(os.path.dirname(__file__), "artifacts"))


def _sanitize_artifact_label(label: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in label.strip())
    safe = safe.strip("_")
    return safe[:80] if safe else "run"


def _write_text_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)


async def _persist_json_artifact(*, tool_name: str, payload: dict[str, Any], arguments: dict | None = None) -> dict[str, Any]:
    """Persist a JSON payload to the configured artifact backend and return a pointer dict."""

    label = "run"
    if arguments:
        arg_label = arguments.get("artifact_label")
        if isinstance(arg_label, str) and arg_label.strip():
            label = _sanitize_artifact_label(arg_label)

    now = datetime.now(timezone.utc)
    store = _get_artifact_store(arguments)

    def _do_write() -> dict[str, Any]:
        ptr = store.write_json(tool_name=tool_name, payload=payload, label=label, created_at=now)
        return ptr.as_dict()

    return await anyio.to_thread.run_sync(_do_write, limiter=thread_limiter)


def _resolve_effective_response_mode(arguments: dict | None = None) -> str:
    """Allow per-call override of batch response mode."""
    if arguments:
        override = arguments.get("response_mode")
        if isinstance(override, str):
            override = override.strip().lower()
            if override in {"full", "summary", "artifact"}:
                return override
    return _get_batch_response_mode()


def _should_persist_to_disk(arguments: dict | None, response_mode: str) -> bool:
    if response_mode == "artifact":
        return True
    if arguments and arguments.get("persist_to_disk") is True:
        return True
    # Backwards/forwards compatible: allow explicit Redis persistence even when response_mode != artifact.
    if arguments and arguments.get("persist_to_redis") is True:
        return True
    if _truthy_env("JMCP_PERSIST_TO_DISK"):
        return True
    if _truthy_env("JMCP_PERSIST_TO_REDIS"):
        return True
    return False


def _find_artifact_by_run_id(run_id: str, artifact_dir: str) -> str | None:
    run_id = run_id.strip()
    if not run_id:
        return None

    try:
        if not os.path.isdir(artifact_dir):
            return None
        candidates: list[str] = []
        for name in os.listdir(artifact_dir):
            if run_id in name and name.endswith(".json"):
                candidates.append(os.path.join(artifact_dir, name))
        if not candidates:
            return None
        # Prefer newest (by mtime).
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return candidates[0]
    except Exception:
        return None


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n...<truncated>...\n" + text[-tail:]


def _render_artifact_view(artifact_obj: dict[str, Any], *, mode: str, max_output_chars: int) -> dict[str, Any]:
    """Create a small, LLM-friendly view of an artifact object."""
    import hashlib
    import difflib

    base = {
        "tool": artifact_obj.get("tool"),
        "run_id": artifact_obj.get("run_id"),
        "created_at": artifact_obj.get("created_at"),
        "request": artifact_obj.get("request"),
        "summary": artifact_obj.get("summary"),
    }

    if mode == "metadata":
        return base

    if mode == "summary":
        # Include failure metadata if present.
        for key in ("failed_routers", "slowest_routers"):
            if key in artifact_obj:
                base[key] = artifact_obj.get(key)
        return base

    if mode == "full":
        return artifact_obj

    if mode == "failures":
        failures: list[dict[str, Any]] = []

        # Tool: execute_junos_command_batch
        if isinstance(artifact_obj.get("results"), list):
            for r in artifact_obj.get("results", []):
                status = r.get("status")
                if status != "success":
                    item = {
                        "router_name": r.get("router_name"),
                        "status": status,
                        "execution_duration": r.get("execution_duration"),
                        "start_time": r.get("start_time"),
                        "end_time": r.get("end_time"),
                    }
                    output = r.get("output")
                    if isinstance(output, str):
                        item["output"] = _truncate_text(output, max_output_chars)
                    failures.append(item)

        # Tool: execute_junos_commands_batch
        if isinstance(artifact_obj.get("routers"), list):
            for rtr in artifact_obj.get("routers", []):
                failed = (rtr.get("failed") or 0) > 0 or bool(rtr.get("error"))
                if not failed:
                    continue
                item = {
                    "router_name": rtr.get("router_name"),
                    "successful": rtr.get("successful"),
                    "failed": rtr.get("failed"),
                    "router_duration": rtr.get("router_duration"),
                    "error": rtr.get("error"),
                    "failed_commands": [],
                }
                for cmdr in rtr.get("results", []) or []:
                    if not cmdr.get("success"):
                        cmd_item = {
                            "command": cmdr.get("command"),
                            "execution_duration": cmdr.get("execution_duration"),
                            "start_time": cmdr.get("start_time"),
                            "end_time": cmdr.get("end_time"),
                            "error": cmdr.get("error"),
                        }
                        output = cmdr.get("output")
                        if isinstance(output, str):
                            cmd_item["output"] = _truncate_text(output, max_output_chars)
                        item["failed_commands"].append(cmd_item)
                failures.append(item)

        base["failures"] = failures
        base["failure_count"] = len(failures)
        return base

    if mode == "diff":
        # A token-safe "what differs" view that avoids dumping full outputs.
        # Strategy:
        # - Group outputs by sha256 hash
        # - Pick baseline as the largest group (prefer success)
        # - For other groups, provide counts + router list (truncated)
        # - Include truncated unified diffs vs baseline for a small number of variants

        # Defaults (can be overridden by read_artifact wrapper by pre-populating artifact_obj['_diff_opts']).
        diff_opts = artifact_obj.get("_diff_opts") if isinstance(artifact_obj.get("_diff_opts"), dict) else {}
        max_routers_per_group = diff_opts.get("max_routers_per_group", 10)
        max_diff_groups = diff_opts.get("max_diff_groups", 3)
        max_diff_chars = diff_opts.get("max_diff_chars", 3000)
        context_lines = diff_opts.get("context_lines", 10)
        # If the caller doesn't explicitly set include_diffs, default to "auto":
        # - Always include grouping (hash clusters)
        # - Only include unified diffs when the variant is sufficiently similar to baseline
        include_diffs = diff_opts.get("include_diffs", "auto")

        # Heuristics for auto diff suppression (designed for commands like
        # `show interfaces extensive` where counters/timestamps can cause massive rewrites).
        auto_diff_similarity_threshold = 0.55
        auto_diff_max_lines = 2000

        if not isinstance(max_routers_per_group, int) or max_routers_per_group <= 0:
            max_routers_per_group = 10
        if not isinstance(max_diff_groups, int) or max_diff_groups < 0:
            max_diff_groups = 3
        if not isinstance(max_diff_chars, int) or max_diff_chars <= 0:
            max_diff_chars = 3000
        if not isinstance(context_lines, int) or context_lines < 0:
            context_lines = 10
        include_diffs_mode: str
        if include_diffs is True:
            include_diffs_mode = "force"
        elif include_diffs is False:
            include_diffs_mode = "off"
        else:
            include_diffs_mode = "auto"

        def _sha256_text(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

        def _limit_router_list(routers: list[str]) -> dict[str, Any]:
            shown = routers[:max_routers_per_group]
            omitted = max(0, len(routers) - len(shown))
            return {
                "routers": shown,
                "routers_omitted": omitted,
            }

        def _unified_diff(a: str, b: str, *, a_label: str, b_label: str, compare_to_len: int) -> tuple[str, bool]:
            a_lines = a.splitlines(keepends=False)
            b_lines = b.splitlines(keepends=False)
            diff_iter = difflib.unified_diff(
                a_lines,
                b_lines,
                fromfile=a_label,
                tofile=b_label,
                n=context_lines,
                lineterm="",
            )

            # Build the diff incrementally to avoid materializing very large diffs.
            # We keep up to max_diff_chars for the returned text, but we also track
            # the total diff size until it exceeds compare_to_len, so we can fall
            # back to returning the full output when the diff is bigger than the raw.
            chunks: list[str] = []
            kept = 0
            total = 0
            kept_limit = max_diff_chars
            exceeded = False
            for line in diff_iter:
                # +1 for the newline we add when joining
                total += len(line) + 1
                if kept < kept_limit:
                    take = min(len(line), max(0, kept_limit - kept))
                    chunks.append(line[:take])
                    kept += take + 1
                if total > compare_to_len:
                    exceeded = True
                    break

            text = "\n".join(chunks)
            if kept >= kept_limit:
                text += "\n...<truncated>..."
            return text, exceeded

        def _similarity_score(a: str, b: str) -> float:
            # Normalize digits to avoid treating per-interface counters/timestamps as rewrites.
            import re

            def _norm_lines(s: str) -> list[str]:
                lines = s.splitlines(keepends=False)
                if len(lines) > auto_diff_max_lines:
                    lines = lines[:auto_diff_max_lines]
                return [re.sub(r"\d+", "0", ln) for ln in lines]

            a_norm = _norm_lines(a)
            b_norm = _norm_lines(b)

            # SequenceMatcher on normalized lines; ratio in [0, 1].
            sm = difflib.SequenceMatcher(None, a_norm, b_norm, autojunk=True)
            return float(sm.ratio())

        def _group_single_command(results: list[dict[str, Any]]) -> dict[str, Any]:
            groups: dict[str, dict[str, Any]] = {}
            for r in results:
                router = r.get("router_name")
                status = r.get("status")
                out = r.get("output")
                if not isinstance(router, str):
                    continue
                if not isinstance(status, str):
                    status = "unknown"
                if not isinstance(out, str):
                    out = ""
                h = _sha256_text(out)
                key = f"{status}:{h}"
                g = groups.get(key)
                if g is None:
                    g = {
                        "status": status,
                        "sha256": h,
                        "routers": [],
                        "count": 0,
                        "sample_output": out,
                    }
                    groups[key] = g
                g["routers"].append(router)
                g["count"] += 1

            group_list = sorted(groups.values(), key=lambda x: (x.get("status") != "success", -(x.get("count") or 0)))

            # Choose baseline: prefer success group with max count.
            baseline = None
            for g in group_list:
                if g.get("status") == "success":
                    baseline = g
                    break
            if baseline is None and group_list:
                baseline = group_list[0]

            baseline_out = baseline.get("sample_output") if baseline else ""
            baseline_id = f"{baseline.get('status')}:{baseline.get('sha256')}" if baseline else "none"

            variants = [g for g in group_list if g is not baseline]
            shown_variants = variants[:max_diff_groups]
            groups_omitted = max(0, len(variants) - len(shown_variants))
            groups_to_show = ([baseline] if baseline is not None else []) + shown_variants

            rendered_groups: list[dict[str, Any]] = []
            diffs: list[dict[str, Any]] = []

            for g in groups_to_show:
                routers_sorted = sorted(g.get("routers") or [])
                rendered = {
                    "status": g.get("status"),
                    "sha256": g.get("sha256"),
                    "count": g.get("count"),
                }
                rendered.update(_limit_router_list(routers_sorted))
                rendered_groups.append(rendered)

            if include_diffs_mode != "off" and baseline is not None:
                for g in shown_variants:
                    other_id = f"{g.get('status')}:{g.get('sha256')}"
                    other_out = g.get("sample_output") or ""
                    similarity = None
                    omitted = False
                    reason = None

                    if include_diffs_mode == "auto":
                        similarity = _similarity_score(baseline_out, other_out)
                        if similarity < auto_diff_similarity_threshold:
                            omitted = True
                            reason = "rewrite_diff_suppressed"

                    entry: dict[str, Any] = {
                        "against_baseline": baseline_id,
                        "variant": other_id,
                    }
                    if similarity is not None:
                        entry["similarity"] = round(similarity, 4)

                    if omitted:
                        entry["omitted"] = True
                        entry["reason"] = reason
                    else:
                        diff_text, exceeded = _unified_diff(
                            baseline_out,
                            other_out,
                            a_label=baseline_id,
                            b_label=other_id,
                            compare_to_len=len(other_out),
                        )

                        # If the unified diff grows larger than the raw output, return the
                        # raw output instead. This preserves correctness while still being
                        # token-aware (raw < diff in this case).
                        if exceeded:
                            entry["fallback"] = "full_output"
                            entry["reason"] = "diff_larger_than_variant"
                            entry["output"] = other_out
                        else:
                            entry["diff"] = diff_text
                    diffs.append(entry)

            return {
                "baseline": {
                    "id": baseline_id,
                    "status": baseline.get("status") if baseline else None,
                    "sha256": baseline.get("sha256") if baseline else None,
                    "count": baseline.get("count") if baseline else 0,
                },
                "total_groups": len(group_list),
                "groups_omitted": groups_omitted,
                "groups": rendered_groups,
                "diffs": diffs,
            }

        def _group_multi_command(routers: list[dict[str, Any]]) -> dict[str, Any]:
            # Build per-command buckets.
            per_cmd: dict[str, list[dict[str, Any]]] = {}
            for rtr in routers:
                rname = rtr.get("router_name")
                if not isinstance(rname, str):
                    continue
                for cmdr in rtr.get("results") or []:
                    cmd = cmdr.get("command")
                    if not isinstance(cmd, str):
                        continue
                    out = cmdr.get("output")
                    if not isinstance(out, str):
                        out = ""
                    status = "success" if cmdr.get("success") else "failed"
                    per_cmd.setdefault(cmd, []).append({
                        "router_name": rname,
                        "status": status,
                        "output": out,
                    })

            commands_sorted = sorted(per_cmd.keys())
            rendered: list[dict[str, Any]] = []
            for cmd in commands_sorted:
                rendered.append({
                    "command": cmd,
                    "diff": _group_single_command(per_cmd[cmd]),
                })
            return {
                "commands": rendered,
            }

        # Tool: execute_junos_command_batch
        if isinstance(artifact_obj.get("results"), list):
            base["diff"] = {
                "type": "grouped_outputs",
                "per_command": False,
                "settings": {
                    "max_routers_per_group": max_routers_per_group,
                    "max_diff_groups": max_diff_groups,
                    "max_diff_chars": max_diff_chars,
                    "context_lines": context_lines,
                    "include_diffs_mode": include_diffs_mode,
                    "auto_diff_similarity_threshold": auto_diff_similarity_threshold,
                    "auto_diff_max_lines": auto_diff_max_lines,
                },
                "result": _group_single_command(artifact_obj.get("results") or []),
            }
            return base

        # Tool: execute_junos_commands_batch
        if isinstance(artifact_obj.get("routers"), list):
            base["diff"] = {
                "type": "grouped_outputs",
                "per_command": True,
                "settings": {
                    "max_routers_per_group": max_routers_per_group,
                    "max_diff_groups": max_diff_groups,
                    "max_diff_chars": max_diff_chars,
                    "context_lines": context_lines,
                    "include_diffs_mode": include_diffs_mode,
                    "auto_diff_similarity_threshold": auto_diff_similarity_threshold,
                    "auto_diff_max_lines": auto_diff_max_lines,
                },
                "result": _group_multi_command(artifact_obj.get("routers") or []),
            }
            return base

        base["note"] = "diff mode is only supported for batch artifacts (execute_junos_command_batch / execute_junos_commands_batch)"
        return base

    # Fallback
    base["note"] = f"Unknown mode '{mode}', returning metadata"
    return base


def _parse_artifact_filename(filename: str) -> dict[str, str] | None:
    """Parse `tool-label-run_id.json` into its components.

    This intentionally relies on the filename convention to avoid reading large
    artifact JSON files just to list them.
    """
    if not filename.endswith(".json"):
        return None

    # Our run_id format is: YYYYmmddTHHMMSSZ-xxxxxxxx
    # Example filename:
    #   execute_junos_command_batch-smoke-20260127T004712Z-3b791dc1.json
    import re

    m = re.match(
        r"^(?P<tool>[^-]+)-(?P<label>.+)-(?P<run_id>\d{8}T\d{6}Z-[0-9a-f]{8})\.json$",
        filename,
    )
    if not m:
        return None

    return {
        "tool": m.group("tool"),
        "label": m.group("label"),
        "run_id": m.group("run_id"),
    }


async def handle_list_artifacts(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """List persisted artifacts from the configured backend.

    For disk, this lists files in the artifact directory. For Redis, this lists
    indexed run_ids without reading full payloads.
    """
    artifact_dir = _get_artifact_dir(arguments)
    tool_filter = arguments.get("tool")
    label_contains = arguments.get("label_contains")
    limit = arguments.get("limit")
    since_ts = arguments.get("since")

    if not isinstance(limit, int) or limit <= 0:
        limit = 50
    limit = min(limit, 500)

    if isinstance(tool_filter, str):
        tool_filter = tool_filter.strip()
    else:
        tool_filter = None

    if isinstance(label_contains, str):
        label_contains = label_contains.strip()
    else:
        label_contains = None

    since_epoch: float | None = None
    if isinstance(since_ts, str) and since_ts.strip():
        try:
            # Accept ISO8601; assume UTC if no tz.
            dt = datetime.fromisoformat(since_ts.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            since_epoch = dt.timestamp()
        except Exception:
            since_epoch = None

    backend = _get_artifact_backend(arguments)
    store = None
    try:
        store = _get_artifact_store(arguments)
    except Exception as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({
                "error": "artifact_store_unavailable",
                "backend": backend,
                "artifact_dir": artifact_dir,
                "message": str(e),
            }, indent=2, ensure_ascii=False),
        )]

    def _do_list() -> list[dict[str, Any]]:
        return store.list(
            tool=tool_filter,
            label_contains=label_contains,
            since_epoch=since_epoch,
            limit=limit,
        )

    try:
        artifacts = await anyio.to_thread.run_sync(_do_list, limiter=thread_limiter)
    except Exception as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({
                "error": "list_failed",
                "backend": backend,
                "artifact_dir": artifact_dir,
                "message": str(e),
            }, indent=2, ensure_ascii=False),
        )]

    return [types.TextContent(
        type="text",
        text=json.dumps({
            "backend": backend,
            "artifact_dir": artifact_dir,
            "count": len(artifacts),
            "artifacts": artifacts,
        }, indent=2, ensure_ascii=False),
    )]


async def handle_get_server_settings(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Return current JMCP server settings (helpful for operators)."""
    global MAX_WORKERS, thread_limiter, default_workers, WORKERS_PER_CORE

    limiter_tokens = None
    limiter_borrowed = None
    try:
        if thread_limiter is not None:
            limiter_tokens = getattr(thread_limiter, "total_tokens", None)
            limiter_borrowed = getattr(thread_limiter, "borrowed_tokens", None)
    except Exception:
        pass

    settings: dict[str, Any] = {
        "workers": {
            "default_workers": default_workers,
            "workers_per_core": WORKERS_PER_CORE,
            "effective_workers": MAX_WORKERS,
            "limiter": {
                "enabled": thread_limiter is not None,
                "total_tokens": limiter_tokens,
                "borrowed_tokens": limiter_borrowed,
            },
        },
        "batch_response": {
            "default_mode": _get_batch_response_mode(),
            "env_default": os.getenv("JMCP_BATCH_RESPONSE_MODE"),
            "file_override_path": os.path.join(os.path.dirname(__file__), ".jmcp_batch_response_mode"),
        },
        "artifacts": {
            "default_dir": _get_artifact_dir({}),
            "env_dir": os.getenv("JMCP_ARTIFACT_DIR"),
            "backend": _get_artifact_backend({}),
            "env_backend": os.getenv("JMCP_ARTIFACT_BACKEND"),
            "persist_env_disk": os.getenv("JMCP_PERSIST_TO_DISK"),
            "persist_env_redis": os.getenv("JMCP_PERSIST_TO_REDIS"),
            "redis": {
                "url_configured": bool(_get_redis_url()),
                "host": _get_redis_host(),
                "port": _get_redis_port(),
                "db": _get_redis_db(),
                "username_configured": bool(_get_redis_username()),
                "password_configured": bool(_get_redis_password()),
                "prefix": _get_redis_prefix(),
                "ttl_seconds": _get_redis_ttl_seconds(),
                "gzip_enabled": _get_artifact_gzip_enabled(),
                "max_bytes": _get_redis_max_bytes(),
                "reserve_bytes": _get_redis_reserve_bytes(),
            },
            "dir_override_path": os.path.join(os.path.dirname(__file__), ".jmcp_artifact_dir"),
            "backend_override_path": os.path.join(os.path.dirname(__file__), ".jmcp_artifact_backend"),
        },
        "process": {
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "jmcp_py": os.path.abspath(__file__),
        },
    }

    try:
        settings["connection_pool"] = {
            "enabled": bool(connection_pool and getattr(connection_pool, "enabled", False)),
        }
    except Exception:
        pass

    return [types.TextContent(type="text", text=json.dumps(settings, indent=2, ensure_ascii=False))]


def _normalize_cli_output(output: Any) -> str:
    if output is None:
        return ""

    if isinstance(output, str):
        return output

    if isinstance(output, (dict, list)):
        try:
            return json.dumps(output, indent=2, ensure_ascii=False)
        except Exception:
            return str(output)

    if isinstance(output, bytes):
        try:
            return output.decode("utf-8", errors="replace")
        except Exception:
            return str(output)

    # PyEZ may return an lxml element for XML format; avoid hard dependency.
    try:
        tag = getattr(output, "tag", None)
        if tag is not None:
            try:
                from lxml import etree  # type: ignore

                return etree.tostring(output, pretty_print=True, encoding="unicode")
            except Exception:
                return str(output)
    except Exception:
        pass

    return str(output)


async def _run_cli_command(router_name: str, command: str, timeout: int = 360, cli_format: str | None = None) -> str:
    """Run a CLI command with optional connection pooling.

    When pooling is enabled (default), reuses a persistent SSH session per router.
    All blocking PyEZ operations are executed in the thread pool.
    """
    if router_name not in devices:
        return f"Router {router_name} not found in the device mapping."

    device_info = devices[router_name]
    try:
        connect_params = prepare_connection_params(device_info, router_name)
    except ValueError as ve:
        return f"Error: {ve}"

    cli_format_normalized: str | None
    if isinstance(cli_format, str):
        cli_format_normalized = cli_format.strip().lower()
    else:
        cli_format_normalized = None

    if cli_format_normalized in {"", "text", "plain"}:
        cli_format_normalized = None

    if cli_format_normalized not in {None, "json", "xml"}:
        cli_format_normalized = None

    pool = connection_pool
    if pool is not None and pool.enabled:
        device = await pool.get_connection(router_name)
        try:
            def _do_cli() -> str:
                device.timeout = timeout
                if cli_format_normalized is None:
                    return device.cli(command, warning=False)
                return device.cli(command, warning=False, format=cli_format_normalized)

            raw = await anyio.to_thread.run_sync(_do_cli, limiter=thread_limiter)
            return _normalize_cli_output(raw)
        except ConnectError as ce:
            await pool.invalidate_connection(router_name)
            return f"Connection error to {router_name}: {ce}"
        except Exception as e:
            await pool.invalidate_connection(router_name)
            return f"An error occurred: {e}"
        finally:
            await pool.release_connection(router_name)

    # Pool disabled: open/run/close within a single thread for safety
    def _direct_cli() -> Any:
        with Device(**connect_params) as junos_device:
            junos_device.timeout = timeout
            if cli_format_normalized is None:
                return junos_device.cli(command, warning=False)
            return junos_device.cli(command, warning=False, format=cli_format_normalized)

    try:
        raw = await anyio.to_thread.run_sync(_direct_cli, limiter=thread_limiter)
        return _normalize_cli_output(raw)
    except ConnectError as ce:
        return f"Connection error to {router_name}: {ce}"
    except Exception as e:
        return f"An error occurred: {e}"


def _looks_like_cli_error(output: Any) -> bool:
    """Best-effort detection of error strings returned by _run_cli_command.

    We intentionally keep this conservative and aligned with existing behavior in
    `handle_execute_junos_command_batch`, which already treats certain prefixes
    as failures.
    """
    if not isinstance(output, str):
        return True

    text = output.lstrip()
    if not text:
        return False

    if text.startswith(("Connection error", "An error occurred", "Error:", "Exception during execution")):
        return True

    # Common internal message when the requested router name isn't configured.
    if "not found in the device mapping" in text:
        return True

    return False


class Context(BaseModel, Generic[ServerSessionT, LifespanContextT, RequestT]):
    """Context object providing access to MCP capabilities.

    This provides a cleaner interface to MCP's RequestContext functionality.
    It gets injected into tool and resource functions that request it via type hints.

    To use context in a tool function, add a parameter with the Context type annotation:

    ```python
    @server.tool()
    def my_tool(x: int, ctx: Context) -> str:
        # Log messages to the client
        ctx.info(f"Processing {x}")
        ctx.debug("Debug info")
        ctx.warning("Warning message")
        ctx.error("Error message")

        # Report progress
        ctx.report_progress(50, 100)

        # Access resources
        data = ctx.read_resource("resource://data")

        # Get request info
        request_id = ctx.request_id
        client_id = ctx.client_id

        return str(x)
    ```

    The context parameter name can be anything as long as it's annotated with Context.
    The context is optional - tools that don't need it can omit the parameter.
    """

    _request_context: RequestContext[ServerSessionT, LifespanContextT, RequestT] | None
    _fastmcp: Server | None

    def __init__(
        self,
        *,
        request_context: (RequestContext[ServerSessionT, LifespanContextT, RequestT] | None) = None,
        fastmcp: Server | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._request_context = request_context
        self._fastmcp = fastmcp

    @property
    def fastmcp(self) -> Server:
        """Access to the FastMCP server."""
        if self._fastmcp is None:
            raise ValueError("Context is not available outside of a request")
        return self._fastmcp

    @property
    def request_context(
        self,
    ) -> RequestContext[ServerSessionT, LifespanContextT, RequestT]:
        """Access to the underlying request context."""
        if self._request_context is None:
            raise ValueError("Context is not available outside of a request")
        return self._request_context

    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        """Report progress for the current operation.

        Args:
            progress: Current progress value e.g. 24
            total: Optional total value e.g. 100
            message: Optional message e.g. Starting render...
        """
        progress_token = self.request_context.meta.progressToken if self.request_context.meta else None

        if progress_token is None:
            return

        await self.request_context.session.send_progress_notification(
            progress_token=progress_token,
            progress=progress,
            total=total,
            message=message,
        )

    async def read_resource(self, uri: str | AnyUrl) -> Iterable[ReadResourceContents]:
        """Read a resource by URI.

        Args:
            uri: Resource URI to read

        Returns:
            The resource content as either text or bytes
        """
        assert self._fastmcp is not None, "Context is not available outside of a request"
        return await self._fastmcp.read_resource(uri)

    async def elicit(
        self,
        message: str,
        schema: type[ElicitSchemaModelT],
    ) -> ElicitationResult[ElicitSchemaModelT]:
        """Elicit information from the client/user.

        This method can be used to interactively ask for additional information from the
        client within a tool's execution. The client might display the message to the
        user and collect a response according to the provided schema. Or in case a
        client is an agent, it might decide how to handle the elicitation -- either by asking
        the user or automatically generating a response.

        Args:
            schema: A Pydantic model class defining the expected response structure, according to the specification,
                    only primive types are allowed.
            message: Optional message to present to the user. If not provided, will use
                    a default message based on the schema

        Returns:
            An ElicitationResult containing the action taken and the data if accepted

        Note:
            Check the result.action to determine if the user accepted, declined, or cancelled.
            The result.data will only be populated if action is "accept" and validation succeeded.
        """

        log.info(f"Calling elicit_with_validation with related_request_id: {self.request_id}")
        return await elicit_with_validation(
            session=self.request_context.session, message=message, schema=schema, related_request_id=self.request_id
        )

    async def log(
        self,
        level: Literal["debug", "info", "warning", "error"],
        message: str,
        *,
        logger_name: str | None = None,
    ) -> None:
        """Send a log message to the client.

        Args:
            level: Log level (debug, info, warning, error)
            message: Log message
            logger_name: Optional logger name
            **extra: Additional structured data to include
        """
        await self.request_context.session.send_log_message(
            level=level,
            data=message,
            logger=logger_name,
            related_request_id=self.request_id,
        )

    @property
    def client_id(self) -> str | None:
        """Get the client ID if available."""
        return getattr(self.request_context.meta, "client_id", None) if self.request_context.meta else None

    @property
    def request_id(self) -> str:
        """Get the unique ID for this request."""
        return str(self.request_context.request_id)

    @property
    def session(self):
        """Access to the underlying session for advanced usage."""
        return self.request_context.session

    # Convenience methods for common log levels
    async def debug(self, message: str, **extra: Any) -> None:
        """Send a debug log message."""
        await self.log("debug", message, **extra)

    async def info(self, message: str, **extra: Any) -> None:
        """Send an info log message."""
        await self.log("info", message, **extra)

    async def warning(self, message: str, **extra: Any) -> None:
        """Send a warning log message."""
        await self.log("warning", message, **extra)

    async def error(self, message: str, **extra: Any) -> None:
        """Send an error log message."""
        await self.log("error", message, **extra)


class ElicitationSchema:
    """Schema definitions for different elicitation types."""
    # Device management schemas
    class GetDeviceName(BaseModel):
        name: str = Field(
            description="Enter the device name (e.g., router1-east)",
            min_length=1,
            max_length=50
        )

    class GetDeviceIP(BaseModel):
        ip: str = Field(
            description="Enter the device IP address (e.g., 192.168.1.1)",
            pattern=r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$"
        )

    class GetDevicePort(BaseModel):
        port: int = Field(
            description="Enter the SSH port (default: 22)",
            ge=1,
            le=65535,
            default=22
        )

    class GetDeviceUsername(BaseModel):
        username: str = Field(
            description="Enter the username for device authentication",
            min_length=1
        )
    
    class GetSSHKeyPath(BaseModel):
        ssh_key_path: str = Field(
            description="Enter the path to the SSH private key file on the MCP server (e.g., /home/user/.ssh/id_rsa)",
            min_length=1
        )
        
    class ConfirmDeviceAdd(BaseModel):
        confirm: bool = Field(description="Confirm adding this device")
        test_connection: bool = Field(
            default=False, 
            description="Test connection to device before adding"
        )

async def elicit_field_value(
    ctx: Context,
    message: str,
    schema_class: type[BaseModel],
    field_name: str | None
) -> str | int | Dict[str, Any] | None:
    """Generic elicitation handler with validation and error handling."""

    try:
        log.info(f"Calling ctx.elicit with schema: {schema_class.__name__}")
        
        # Add timeout to elicitation
        import asyncio
        try:
            result = await asyncio.wait_for(
                ctx.elicit(message=message, schema=schema_class),
                timeout=300.0  # 300 second timeout (5 minutes)
            )
            log.info(f"Elicit returned result of type: {type(result)}")
        except asyncio.TimeoutError:
            log.error("Elicitation timed out after 300 seconds")
            return None

        match result:
            case AcceptedElicitation(data=data):
                # Debug: print what we received
                log.info(f"Elicitation accepted. Data type: {type(data)}, value: {data}")

                # If field_name is None, return the entire data object
                if field_name is None:
                    log.info("Returning full data object")
                    return data
                # Otherwise return the specific field
                if hasattr(data, field_name):
                    field_value = getattr(data, field_name)
                    log.info(f"Returning field '{field_name}' with value: {field_value}")
                    return field_value
                log.warning(f"Field '{field_name}' not found in data object")
                return None
            case DeclinedElicitation():
                log.info("Elicitation was declined")
                return None
            case CancelledElicitation():
                log.info("Elicitation was cancelled")
                return None
    except (anyio.ClosedResourceError, ConnectionError) as e:
        print(f"Client disconnected during elicitation: {e}")
        return None
    except Exception as e:
        log.error(f"Elicitation error: {e}")
        print(f"Elicitation error: {e}")
        return None

async def handle_add_device(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Add a new Junos device with elicitation for missing information."""
    
    # Extract any provided arguments (though we'll elicit missing ones)
    device_name = arguments.get("device_name", "")
    device_ip = arguments.get("device_ip", "")
    device_port = arguments.get("device_port", 0)
    username = arguments.get("username", "")
    ssh_key_path = arguments.get("ssh_key_path", "")
    
    ctx = context
    
    log.info(f"Starting add_device with name='{device_name}', ip='{device_ip}'")
    
    try:
        # Step 1: Get device name
        while not device_name:
            log.info("No device name provided, asking user")
            
            name_result = await elicit_field_value(
                ctx, "Please enter the device name:", 
                ElicitationSchema.GetDeviceName, "name"
            )

            if name_result is None:
                return [types.TextContent(type="text", text="❌ Device name input cancelled.")]
            
            device_name = str(name_result).strip()
            log.info(f"Received device name: '{device_name}'")
            
            # Check if device already exists
            if device_name in devices:
                log.warning(f"Device '{device_name}' already exists")
                await ctx.warning(f"Device '{device_name}' already exists!")
                
                # Ask for a different name
                device_name = ""
                continue

        # Step 2: Get device IP
        while not device_ip:
            log.info("No device IP provided, asking user")
            
            ip_result = await elicit_field_value(
                ctx, f"Please enter the IP address for device '{device_name}':",
                ElicitationSchema.GetDeviceIP, "ip"
            )
            
            if ip_result is None:
                return [types.TextContent(type="text", text="❌ Device IP input cancelled.")]
            
            device_ip = str(ip_result).strip()
            log.info(f"Received device IP: '{device_ip}'")
        
        # Step 3: Get device port (with default)
        while not device_port or device_port <= 0:
            log.info("No valid device port provided, asking user")
            
            port_result = await elicit_field_value(
                ctx, f"Please enter the SSH port for device '{device_name}' (default: 22):",
                ElicitationSchema.GetDevicePort, "port"
            )

            if port_result is None:
                return [types.TextContent(type="text", text="❌ Device port input cancelled.")]

            device_port = int(port_result)
            log.info(f"Received device port: {device_port}")

        # Step 4: Get username
        while not username:
            log.info("Username not provided, asking user")
            
            creds_result = await elicit_field_value(
                ctx, f"Please enter the username for device '{device_name}':",
                ElicitationSchema.GetDeviceUsername, "username"
            )

            if creds_result is None:
                return [types.TextContent(type="text", text="❌ Username input cancelled.")]

            username = str(creds_result).strip()
            log.info(f"Received username: '{username}'")

        # Step 5: Get SSH key path
        while not ssh_key_path:
            log.info("SSH key path not provided, asking user")

            ssh_key_result = await elicit_field_value(
                ctx, f"Please enter the SSH private key file path for device '{device_name}':",
                ElicitationSchema.GetSSHKeyPath, "ssh_key_path"
            )

            if ssh_key_result is None:
                return [types.TextContent(type="text", text="❌ SSH key path input cancelled.")]

            ssh_key_path = str(ssh_key_result).strip()

            # Validate SSH key file exists
            if not os.path.exists(ssh_key_path):
                await ctx.warning(f"SSH key file '{ssh_key_path}' not found. Please enter a valid path.")
                ssh_key_path = ""
                continue

            # Check if file is readable
            if not os.access(ssh_key_path, os.R_OK):
                await ctx.warning(f"SSH key file '{ssh_key_path}' is not readable. Please check permissions.")
                ssh_key_path = ""
                continue

            log.info(f"Received SSH key path: '{ssh_key_path}'")

        # Step 6: Show summary and ask for confirmation
        device_summary = f"""Device Details:
• Name: {device_name}
• IP: {device_ip}
• Port: {device_port}
• Username: {username}
• SSH Key: {ssh_key_path}"""

        confirmation = await elicit_field_value(
            ctx,
            f"Please confirm adding this device:\n\n{device_summary}",
            ElicitationSchema.ConfirmDeviceAdd,
            None
        )

        if confirmation is None or not confirmation.confirm:
            return [types.TextContent(type="text", text="❌ Device addition cancelled.")]

        # Step 7: Optional connection test
        if confirmation.test_connection:
            await ctx.info(f"Testing connection to {device_name}...")

            # Create device configuration for testing
            test_device_info = {
                "ip": device_ip,
                "port": device_port,
                "username": username,
                "auth": {
                    "type": "ssh_key",
                    "private_key_path": ssh_key_path
                }
            }

            test_device = None
            try:
                connect_params = prepare_connection_params(test_device_info, device_name)

                # Create device instance for testing
                test_device = Device(**connect_params)
                test_device.open()
                test_device.timeout = 10

                # Just test the connection, don't run any commands
                await ctx.info(f"✅ Connection test successful!")

            except Exception as e:
                log.error(f"Connection test failed for {device_name}: {e}")
                return [types.TextContent(type="text", text=f"❌ Connection test failed: {str(e)}\nDevice not added.")]
            finally:
                # Ensure test connection is properly closed
                if test_device is not None:
                    try:
                        if test_device.connected:
                            log.debug(f"Explicitly closing test connection to {device_name}")
                            test_device.close()
                    except Exception as close_error:
                        log.warning(f"Error while closing test connection to {device_name}: {close_error}")
                        # Force cleanup of the underlying transport
                        try:
                            if hasattr(test_device, '_conn') and test_device._conn:
                                test_device._conn.close()
                        except Exception as transport_error:
                            log.warning(f"Error while closing test transport to {device_name}: {transport_error}")

        # Step 8: Add device to global devices dictionary
        new_device_config = {
            "ip": device_ip,
            "port": device_port,
            "username": username,
            "auth": {
                "type": "ssh_key",
                "private_key_path": ssh_key_path
            }
        }

        # Validate the new device configuration before adding
        validate_device_config(device_name, new_device_config)
        
        # Add the validated configuration to devices
        devices[device_name] = new_device_config
        
        log.info(f"Successfully added device '{device_name}' to devices dictionary")
        await ctx.info(f"Device '{device_name}' added successfully!")
        
        result_message = f"""✅ Device '{device_name}' added successfully!

Details:
• IP: {device_ip}
• Port: {device_port}
• Username: {username}

The device is now available for use with all Junos MCP tools."""

        return [types.TextContent(type="text", text=result_message)]

    except Exception as e:
        log.error(f"Unexpected error in add_device: {e}")
        return [types.TextContent(type="text", text=f"❌ Failed to add device: {str(e)}")]


def _run_junos_cli_command(router_name: str, command: str, timeout: int = 360) -> str:
    """Internal helper to connect and run a Junos CLI command."""
    log.debug(f"Executing command {command} on router {router_name} with timeout {timeout}s (internal)")
    device_info = devices[router_name]
    try:
        connect_params = prepare_connection_params(device_info, router_name)
    except ValueError as ve:
        return f"Error: {ve}"
    try:
        with Device(**connect_params) as junos_device:            
            junos_device.timeout = timeout
            op = junos_device.cli(command, warning=False)
            return op
    except ConnectError as ce:
        return f"Connection error to {router_name}: {ce}"
    except Exception as e:
        return f"An error occurred: {e}"

def get_timeout_with_fallback(arguments_timeout: int = None) -> int:
    """Get timeout value with fallback priority: arguments -> ENV -> default (360)"""
    if arguments_timeout is not None:
        return arguments_timeout

    env_timeout = os.getenv('JUNOS_TIMEOUT')
    if env_timeout is not None:
        try:
            return int(env_timeout)
        except ValueError:
            log.warning(f"Invalid JUNOS_TIMEOUT environment variable value: {env_timeout}. Using default timeout.")

    return 360

def validate_token_from_file(token: str) -> bool:
    """Validate if a token exists in the .tokens file"""
    try:
        if not os.path.exists(".tokens"):
            return False

        with open(".tokens", 'r') as f:
            tokens = json.load(f)

        for token_data in tokens.values():
            if token_data.get('token') == token:
                return True

        return False
    except (json.JSONDecodeError, FileNotFoundError, KeyError):
        return False


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Middleware to check Bearer token authentication for streamable-http"""

    def __init__(self, app, auth_enabled: bool = True):
        super().__init__(app)
        self.auth_enabled = auth_enabled

    async def dispatch(self, request: Request, call_next):
        # Log all incoming requests during elicitation debugging
        log.info(f"Incoming request: {request.method} {request.url.path} from {request.client.host if request.client else 'unknown'}")

        # Try to read request body for debugging
        if request.method == "POST":
            try:
                body = await request.body()
                if body:
                    import json
                    try:
                        parsed_body = json.loads(body.decode())
                        log.info(f"Request body: {parsed_body}")
                    except:
                        log.info(f"Raw request body: {body[:200]}...")
            except Exception as e:
                log.warning(f"Could not read request body: {e}")

        # Skip auth if disabled (for stdio transport)
        if not self.auth_enabled:
            return await call_next(request)

        auth_header = request.headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            log.warning(f"Missing or invalid auth header for {request.method} {request.url.path}")
            return JSONResponse(
                {"error": "Missing or invalid Authorization header"}, 
                status_code=401
            )

        token = auth_header[7:]  # Remove "Bearer " prefix
        
        # Validate token against .tokens file
        if not validate_token_from_file(token):
            log.warning(f"Invalid token attempt from {request.client.host if request.client else 'unknown'}")
            return JSONResponse(
                {"error": "Invalid token"}, 
                status_code=401
            )
        
        log.debug("Token validation successful")
        return await call_next(request)

async def handle_execute_junos_command(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Handler for execute_junos_command tool"""
    start_time = time.time()
    start_timestamp = datetime.now(timezone.utc).isoformat()
    router_name = arguments.get("router_name", "")
    command = arguments.get("command", "")
    cli_format = arguments.get("format")
    timeout = get_timeout_with_fallback(arguments.get("timeout"))

    log.debug(f"Executing command {command} on router {router_name} with timeout {timeout}s")
    result = await _run_cli_command(router_name, command, timeout, cli_format=cli_format)

    end_time = time.time()
    end_timestamp = datetime.now(timezone.utc).isoformat()
    execution_duration = round(end_time - start_time, 3)
    content_block = types.TextContent(
        type="text",
        text=result,
        annotations={"router_name": router_name,
                     "command": command,
                     "format": cli_format,
                     "metadata": {
                        "execution_duration": execution_duration,
                        "start_time": start_timestamp,
                        "end_time": end_timestamp
                        }
                    })
    log.debug(f"content block: {content_block}")
    return [content_block]


async def handle_execute_junos_command_batch(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """
    Handler for execute_junos_command_batch tool - executes same command on multiple routers in parallel.

    This function demonstrates async/await parallel execution patterns. The "magic" of parallelism
    comes from three key concepts:

    1. ASYNC/AWAIT: Allows cooperative multitasking - while one router is waiting for network I/O,
       other routers can be contacted simultaneously

    2. THREAD POOL: PyEZ's Device.cli() is synchronous (blocking), so we use anyio.to_thread.run_sync()
       to run it in a background thread without blocking the async event loop

    3. ASYNCIO.GATHER: Launches multiple async operations simultaneously and waits for all to complete

    Real-world analogy: Instead of calling 3 restaurants sequentially and waiting on hold for each
    (serial execution = 3 × 2 minutes = 6 minutes), you have 3 friends call simultaneously
    (parallel execution = max(2, 2, 2) = 2 minutes total).
    """
    import asyncio

    batch_start_time = time.time()
    router_names = arguments.get("router_names", [])
    command = arguments.get("command", "")
    cli_format = arguments.get("format")
    timeout = get_timeout_with_fallback(arguments.get("timeout"))

    barrier_sync = bool(arguments.get("barrier_sync"))
    barrier_policy = arguments.get("barrier_policy") or "proceed"
    if isinstance(barrier_policy, str):
        barrier_policy = barrier_policy.strip().lower()
    if barrier_policy not in {"proceed", "strict"}:
        barrier_policy = "proceed"

    preconnect_timeout_raw = arguments.get("preconnect_timeout")
    if isinstance(preconnect_timeout_raw, int) and preconnect_timeout_raw > 0:
        preconnect_timeout = preconnect_timeout_raw
    else:
        preconnect_timeout = 30

    preconnect_retries_raw = arguments.get("preconnect_retries")
    if isinstance(preconnect_retries_raw, int) and preconnect_retries_raw >= 0:
        preconnect_retries = min(preconnect_retries_raw, 10)
    else:
        preconnect_retries = 2

    preconnect_backoff_raw = arguments.get("preconnect_backoff_seconds")
    if isinstance(preconnect_backoff_raw, int) and preconnect_backoff_raw >= 0:
        preconnect_backoff_seconds = min(preconnect_backoff_raw, 60)
    else:
        preconnect_backoff_seconds = 1

    # Concurrency is set once at server startup.

    # ============================================================================
    # STEP 1: Input Validation
    # ============================================================================
    # Fail fast if inputs are invalid - no point starting parallel execution
    # if we know it will fail

    if not router_names:
        return [types.TextContent(
            type="text",
            text="Error: router_names list is required and cannot be empty"
        )]

    if not command:
        return [types.TextContent(
            type="text",
            text="Error: command is required"
        )]

    # Validate all routers exist before executing - prevents partial failures
    invalid_routers = [r for r in router_names if r not in devices]
    if invalid_routers:
        return [types.TextContent(
            type="text",
            text=f"Error: The following routers not found in device mapping: {', '.join(invalid_routers)}"
        )]

    log.info(
        f"Executing batch command on {len(router_names)} routers in parallel: {command} "
        f"(barrier_sync={barrier_sync}, policy={barrier_policy}, preconnect_timeout={preconnect_timeout}s, "
        f"preconnect_retries={preconnect_retries})"
    )
    await context.info(f"Executing command on {len(router_names)} routers in parallel...")

    # Optional barrier sync: preconnect to all routers first, then execute command only on ready routers.
    preconnect_info: dict[str, Any] | None = None
    routers_to_execute = list(router_names)
    preconnect_failed_map: dict[str, str] = {}

    if barrier_sync:
        pool = connection_pool

        async def _preconnect_one(router_name: str) -> dict[str, Any]:
            started = time.time()
            last_error: str | None = None

            for attempt in range(preconnect_retries + 1):
                device = None
                try:
                    if pool is not None and pool.enabled:
                        with anyio.fail_after(preconnect_timeout):
                            device = await pool.get_connection(router_name)
                    else:
                        device_info = devices[router_name]
                        connect_params = prepare_connection_params(device_info, router_name)

                        def _open_and_close() -> None:
                            with Device(**connect_params) as dev:
                                dev.timeout = preconnect_timeout

                        with anyio.fail_after(preconnect_timeout):
                            await anyio.to_thread.run_sync(_open_and_close, limiter=thread_limiter)

                    duration = round(time.time() - started, 3)
                    return {
                        "router_name": router_name,
                        "status": "connected",
                        "attempts": attempt + 1,
                        "duration": duration,
                    }
                except Exception as e:
                    last_error = str(e)
                    if pool is not None and pool.enabled:
                        with suppress(Exception):
                            await pool.invalidate_connection(router_name)
                    if attempt == preconnect_retries:
                        duration = round(time.time() - started, 3)
                        return {
                            "router_name": router_name,
                            "status": "failed",
                            "attempts": attempt + 1,
                            "duration": duration,
                            "error": last_error,
                        }
                    if preconnect_backoff_seconds:
                        await asyncio.sleep(preconnect_backoff_seconds)
                finally:
                    if device is not None and pool is not None and pool.enabled:
                        with suppress(Exception):
                            await pool.release_connection(router_name)

            duration = round(time.time() - started, 3)
            return {
                "router_name": router_name,
                "status": "failed",
                "attempts": preconnect_retries + 1,
                "duration": duration,
                "error": last_error or "unknown preconnect failure",
            }

        preconnect_start = time.time()
        preconnect_results = await asyncio.gather(
            *[_preconnect_one(r) for r in router_names],
            return_exceptions=False,
        )
        preconnect_duration = round(time.time() - preconnect_start, 3)

        ready = [r["router_name"] for r in preconnect_results if r.get("status") == "connected"]
        failed = [r for r in preconnect_results if r.get("status") != "connected"]
        preconnect_failed_map = {
            r.get("router_name"): (r.get("error") or "preconnect failed")
            for r in failed
            if r.get("router_name")
        }

        preconnect_info = {
            "enabled": True,
            "policy": barrier_policy,
            "timeout": preconnect_timeout,
            "retries": preconnect_retries,
            "backoff_seconds": preconnect_backoff_seconds,
            "duration": preconnect_duration,
            "ready_count": len(ready),
            "failed_count": len(failed),
            "failed_routers": failed,
        }

        response_mode = _resolve_effective_response_mode(arguments)

        if barrier_policy == "strict" and preconnect_failed_map:
            batch_end_time = time.time()
            batch_duration = round(batch_end_time - batch_start_time, 3)

            response_data: dict[str, Any] = {
                "error": "preconnect_failed",
                "message": "Barrier preconnect failed for one or more routers; strict policy prevents execution.",
                "preconnect": preconnect_info,
                "summary": {
                    "command": command,
                    "total_routers": len(router_names),
                    "total_routers_executed": 0,
                    "total_routers_preconnect_failed": len(preconnect_failed_map),
                    "successful": 0,
                    "failed": len(router_names),
                    "total_duration": batch_duration,
                },
            }

            if _should_persist_to_disk(arguments, response_mode):
                try:
                    artifact = await _persist_json_artifact(
                        tool_name="execute_junos_command_batch",
                        arguments=arguments,
                        payload={
                            "request": {
                                "router_names": router_names,
                                "command": command,
                                "timeout": timeout,
                                "barrier_sync": True,
                                "barrier_policy": barrier_policy,
                                "preconnect_timeout": preconnect_timeout,
                                "preconnect_retries": preconnect_retries,
                                "preconnect_backoff_seconds": preconnect_backoff_seconds,
                            },
                            "preconnect": preconnect_info,
                            "summary": response_data.get("summary", {}),
                            "results": [],
                        },
                    )
                    response_data["artifact"] = artifact
                except Exception as e:
                    log.warning(f"Failed to persist batch artifact: {e}")

            if response_mode == "artifact":
                response_data.pop("results", None)

            formatted_output = json.dumps(response_data, indent=2)
            return [types.TextContent(type="text", text=formatted_output)]

        routers_to_execute = ready

    # ============================================================================
    # STEP 2: Define Per-Router Async Function
    # ============================================================================
    # This nested async function will be called once per router, and all calls
    # will run in parallel thanks to asyncio.gather() below

    async def execute_on_router(router_name: str) -> dict:
        """
        Execute command on a single router and return structured result.

        This is an ASYNC function, which means it can yield control to the event loop
        while waiting for I/O operations (like network connections to routers).

        KEY INSIGHT: Each call to this function represents one "parallel task".
        When we create 3 tasks, they all run concurrently.
        """
        start_time = time.time()
        start_timestamp = datetime.now(timezone.utc).isoformat()

        try:
            # ----------------------------------------------------------------
            # THE MAGIC: anyio.to_thread.run_sync()
            # ----------------------------------------------------------------
            # Problem: _run_junos_cli_command() is SYNCHRONOUS (blocking)
            # - It uses PyEZ's Device.cli() which blocks the thread while waiting
            # - If we called it directly, it would block the async event loop
            # - This would make everything serial again (defeating parallelism)
            #
            # Solution: anyio.to_thread.run_sync()
            # - Runs the blocking function in a background thread pool
            # - The async event loop remains free to handle other tasks
            # - Multiple threads can run simultaneously (one per router)
            #
            # Result: True parallel execution!
            # - While router1's thread waits for SSH response, router2's thread
            #   can be establishing its connection, and router3's thread can be
            #   sending its command, etc.
            #
            # Think of it like: Each router gets its own phone line (thread),
            # and all phone calls happen at the same time instead of one after another.

            result = await _run_cli_command(router_name, command, timeout, cli_format=cli_format)

            # Determine if this was a success or error based on result content
            # (the _run_junos_cli_command returns error messages as strings)
            is_error = result.startswith("Connection error") or result.startswith("An error occurred") or result.startswith("Error:")
            status = "failed" if is_error else "success"

        except Exception as e:
            # Catch any unexpected exceptions (shouldn't happen normally)
            result = f"Exception during execution: {str(e)}"
            status = "failed"

        end_time = time.time()
        end_timestamp = datetime.now(timezone.utc).isoformat()
        execution_duration = round(end_time - start_time, 3)

        # Return structured data for this single router
        return {
            "router_name": router_name,
            "status": status,
            "output": result,
            "format": cli_format,
            "execution_duration": execution_duration,
            "start_time": start_timestamp,
            "end_time": end_timestamp
        }

    # ============================================================================
    # STEP 3: Launch All Tasks in Parallel with asyncio.gather()
    # ============================================================================
    # This is where the REAL MAGIC happens!
    #
    # asyncio.gather() explanation:
    # -----------------------------
    # 1. List comprehension creates N async tasks (one per router):
    #    [execute_on_router("router1"), execute_on_router("router2"), ...]
    #
    # 2. The * (splat) operator unpacks them as individual arguments:
    #    asyncio.gather(task1, task2, task3, ...)
    #
    # 3. gather() schedules ALL tasks to run CONCURRENTLY on the event loop:
    #    - All tasks start approximately at the same time
    #    - While one task waits for I/O, others continue executing
    #    - The event loop switches between tasks as they yield control (at await points)
    #
    # 4. await gather() waits for ALL tasks to complete and returns results in order:
    #    results = [result1, result2, result3, ...]
    #
    # Timeline visualization (3 routers, each takes ~1.2 seconds):
    #
    # SERIAL EXECUTION (without gather):
    # Router1: [===========]
    # Router2:              [===========]
    # Router3:                           [===========]
    # Total:   |----------------------------------|  (~3.6 seconds)
    #
    # PARALLEL EXECUTION (with gather):
    # Router1: [===========]
    # Router2: [===========]
    # Router3: [===========]
    # Total:   |-----------|                         (~1.2 seconds)
    #
    # Key: Each router runs in its own thread, so they all complete in the time
    # it takes for the slowest one to finish!

    results = await asyncio.gather(
        *[execute_on_router(router_name) for router_name in routers_to_execute],
        return_exceptions=False  # If any task raises an exception, propagate it immediately
    )

    if barrier_sync and preconnect_failed_map:
        now_ts = datetime.now(timezone.utc).isoformat()
        for skipped_router, err in preconnect_failed_map.items():
            results.append({
                "router_name": skipped_router,
                "status": "failed",
                "output": f"preconnect_failed (skipped execution): {err}",
                "execution_duration": 0,
                "start_time": now_ts,
                "end_time": now_ts,
            })

    batch_end_time = time.time()
    batch_duration = round(batch_end_time - batch_start_time, 3)

    # ============================================================================
    # STEP 4: Process and Format Results
    # ============================================================================
    # At this point, ALL routers have completed (or failed), and we have all results

    # Calculate summary statistics
    successful_count = sum(1 for r in results if r["status"] == "success")
    failed_count = len(results) - successful_count

    response_mode = _resolve_effective_response_mode(arguments)

    # Build structured response with summary + (optional) individual results
    response_data: dict[str, Any] = {
        "preconnect": preconnect_info,
        "summary": {
            "command": command,
            "total_routers": len(router_names),
            "total_routers_executed": len(routers_to_execute),
            "total_routers_preconnect_failed": len(preconnect_failed_map) if barrier_sync else 0,
            "successful": successful_count,
            "failed": failed_count,
            "total_duration": batch_duration,
        },
    }

    if response_mode in {"summary", "artifact"}:
        # Keep payload small: include only minimal per-router metadata.
        router_summaries = [
            {
                "router_name": r.get("router_name"),
                "status": r.get("status"),
                "execution_duration": r.get("execution_duration"),
            }
            for r in results
        ]
        # Include only failing routers to keep noise down.
        failed_routers = [r for r in router_summaries if r.get("status") != "success"]
        response_data["failed_routers"] = failed_routers
    else:
        response_data["results"] = results  # Full per-router output

    # Optionally persist full results (even if we're returning only a summary).
    if _should_persist_to_disk(arguments, response_mode):
        try:
            artifact = await _persist_json_artifact(
                tool_name="execute_junos_command_batch",
                arguments=arguments,
                payload={
                    "request": {
                        "router_names": router_names,
                        "command": command,
                        "timeout": timeout,
                        "barrier_sync": barrier_sync,
                        "barrier_policy": barrier_policy,
                        "preconnect_timeout": preconnect_timeout,
                        "preconnect_retries": preconnect_retries,
                        "preconnect_backoff_seconds": preconnect_backoff_seconds,
                    },
                    "preconnect": preconnect_info,
                    "summary": response_data.get("summary", {}),
                    "results": results,
                },
            )
            response_data["artifact"] = artifact
        except Exception as e:
            # Never fail the tool just because persistence failed.
            log.warning(f"Failed to persist batch artifact: {e}")

    # In artifact mode, never include large raw outputs in the tool response.
    if response_mode == "artifact":
        response_data.pop("results", None)

    # Format as pretty JSON for LLM consumption
    # The LLM can easily parse this and identify which output came from which router
    formatted_output = json.dumps(response_data, indent=2)

    log.info(f"Batch command execution completed: {successful_count} successful, {failed_count} failed, {batch_duration}s total")
    await context.info(f"Batch execution complete: {successful_count}/{len(router_names)} successful")

    # Return as MCP TextContent with annotations for structured metadata
    content_block = types.TextContent(
        type="text",
        text=formatted_output,
        annotations={
            "command": command,
            "router_names": router_names,
            "batch_metadata": {
                "total_routers": len(router_names),
                "successful": successful_count,
                "failed": failed_count,
                "total_duration": batch_duration
            }
        }
    )

    return [content_block]


async def handle_get_junos_config(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Handler for get_junos_config tool"""
    router_name = arguments.get("router_name", "")
    
    log.debug(f"Getting configuration from router {router_name}")
    result = await _run_cli_command(
        router_name,
        "show configuration | display inheritance no-comments | display set | no-more"
    )
    
    content_block = types.TextContent(
        type="text",
        text=result,
        annotations={"router_name": router_name}
        )
    log.debug(f"content block: {content_block}")

    return [content_block]


async def handle_junos_config_diff(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Handler for junos_config_diff tool"""
    router_name = arguments.get("router_name", "")
    version = arguments.get("version", 1)
    
    log.debug(f"Getting configuration diff from router {router_name} for version {version}")
    result = await _run_cli_command(router_name, f"show configuration | compare rollback {version}")

    content_block = types.TextContent(
        type="text",
        text=result,
        annotations={"router_name": router_name, "config_diff_version": version}
        )
    log.debug(f"content block: {content_block}")

    return [content_block]

async def handle_render_and_apply_j2_template(arguments: dict, context) -> list[types.ContentBlock]:
    """
    Handler for render_and_apply_j2_template tool
    
    Renders a Jinja2 template with variables and optionally applies it to devices
    
    Args:
        arguments: Dictionary containing:
            - template_content: Jinja2 template content as string
            - vars_content: YAML variables content as string
            - router_name: Router name to apply config to (optional, single router)
            - router_names: List of router names to apply config to (optional, multiple routers)
            - apply_config: Boolean to apply or just render (default: False)
            - commit_comment: Optional commit comment
            - dry_run: Boolean to show diff without committing (default: False)
        context: MCP Context object
    
    Returns:
        List of TextContent blocks with results
    """
    template_content = arguments.get("template_content", "")
    vars_content = arguments.get("vars_content", "")
    router_name = arguments.get("router_name", "")
    router_names = arguments.get("router_names", [])
    apply_config = arguments.get("apply_config", False)
    commit_comment = arguments.get("commit_comment", "Configuration applied via Jinja2 template")
    dry_run = arguments.get("dry_run", False)
    
    results = []
    
    # Step 1: Validate inputs
    if not template_content:
        return [types.TextContent(
            type="text",
            text="❌ Error: template_content is required"
        )]
    
    if not vars_content:
        return [types.TextContent(
            type="text",
            text="❌ Error: vars_content is required"
        )]
    
    # Handle single router_name or list of router_names
    if router_name and not router_names:
        router_names = [router_name]
    
    # Step 2: Load variables from YAML string
    try:
        await context.info("Parsing variables from YAML content...")
        variables = yaml.safe_load(vars_content)
        
        if not variables:
            return [types.TextContent(
                type="text",
                text="❌ Error: Variables content is empty or invalid"
            )]
            
        await context.debug(f"Loaded variables: {variables}")
        
    except yaml.YAMLError as e:
        return [types.TextContent(
            type="text",
            text=f"❌ Error parsing YAML content: {e}"
        )]
    except Exception as e:
        return [types.TextContent(
            type="text",
            text=f"❌ Error loading variables: {e}"
        )]
    
    # Step 3: Setup Jinja2 environment and render template
    try:
        await context.info("Rendering Jinja2 template...")
        
        env = Environment(
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False
        )
        
        template = env.from_string(template_content)
        rendered_config = template.render(variables)
        
        await context.debug(f"Rendered configuration:\n{rendered_config}")
        
    except TemplateError as e:
        return [types.TextContent(
            type="text",
            text=f"❌ Error rendering template: {e}"
        )]
    except Exception as e:
        return [types.TextContent(
            type="text",
            text=f"❌ Error during template rendering: {e}"
        )]
    
    # Step 4: If not applying, just return the rendered config
    if not apply_config:
        result_text = f"""✅ Template rendered successfully!

**Rendered Configuration:**
```
{rendered_config}
```

To apply this configuration to devices, set apply_config=true and provide router_name or router_names.
"""
        return [types.TextContent(
            type="text",
            text=result_text,
            annotations={
                "rendered_config": rendered_config,
                "variables": str(variables)
            }
        )]
    
    # Step 5: Apply configuration to specified routers
    if not router_names:
        return [types.TextContent(
            type="text",
            text="❌ Error: router_name or router_names must be provided when apply_config=true"
        )]

    application_results = []
    
    for rtr_name in router_names:
        if rtr_name not in devices:
            application_results.append(f"❌ {rtr_name}: Router not found in device mapping")
            await context.warning(f"Router {rtr_name} not found")
            continue
        
        try:
            await context.info(f"{'Checking' if dry_run else 'Applying'} configuration on {rtr_name}...")
            
            device_info = devices[rtr_name]
            
            # Use prepare_connection_params to get proper connection parameters
            try:
                connect_params = prepare_connection_params(device_info, rtr_name)
            except ValueError as ve:
                application_results.append(f"❌ {rtr_name}: {ve}")
                await context.error(f"{rtr_name}: {ve}")
                continue
            
            # Connect to device
            def _apply_config_sync(junos_device: Device) -> tuple[str, str]:
                """Apply or dry-run the rendered config on an already-connected device."""
                with Config(junos_device, mode='exclusive') as cu:
                    cu.load(rendered_config, format='set')
                    diff = cu.diff()

                    if not diff:
                        return ("info", "No configuration changes detected")

                    if dry_run:
                        try:
                            check_result = cu.commit_check()
                            if not check_result:
                                return ("error", "Commit check failed - configuration has errors")
                            return ("dry_run", f"Configuration check successful. Changes:\n\n{diff}")
                        finally:
                            with suppress(Exception):
                                cu.rollback()
                    else:
                        check_result = cu.commit_check()
                        if not check_result:
                            with suppress(Exception):
                                cu.rollback()
                            return ("error", "Commit check failed - configuration has errors")
                        cu.commit(comment=commit_comment)
                        return ("success", f"Configuration committed successfully. Changes:\n\n{diff}")

            pool = connection_pool
            if pool is not None and pool.enabled:
                dev = await pool.get_connection(rtr_name)
                try:
                    await context.info(f"Connected to {rtr_name} (pooled)")
                    status, msg = await anyio.to_thread.run_sync(lambda: _apply_config_sync(dev), limiter=thread_limiter)
                except (ConfigLoadError, CommitError, LockError) as e:
                    await pool.invalidate_connection(rtr_name)
                    status, msg = ("error", f"Configuration error: {e}")
                except ConnectError as e:
                    await pool.invalidate_connection(rtr_name)
                    status, msg = ("error", f"Connection failed: {e}")
                except Exception as e:
                    await pool.invalidate_connection(rtr_name)
                    status, msg = ("error", f"Failed to apply configuration: {e}")
                finally:
                    await pool.release_connection(rtr_name)
            else:
                def _direct_apply() -> tuple[str, str]:
                    with Device(**connect_params) as dev:
                        return _apply_config_sync(dev)

                try:
                    status, msg = await anyio.to_thread.run_sync(_direct_apply, limiter=thread_limiter)
                except (ConfigLoadError, CommitError, LockError) as e:
                    status, msg = ("error", f"Configuration error: {e}")
                except ConnectError as e:
                    status, msg = ("error", f"Connection failed: {e}")
                except Exception as e:
                    status, msg = ("error", f"Failed to apply configuration: {e}")

            if status == "success":
                application_results.append(f"✅ {rtr_name}: {msg}")
                await context.info(f"{rtr_name}: Configuration committed successfully")
            elif status == "dry_run":
                application_results.append(f"🔍 {rtr_name}: {msg}")
                await context.info(f"{rtr_name}: Dry-run commit check passed")
            elif status == "info":
                application_results.append(f"ℹ️  {rtr_name}: {msg}")
                await context.info(f"{rtr_name}: {msg}")
            else:
                application_results.append(f"❌ {rtr_name}: {msg}")
                await context.error(f"{rtr_name}: {msg}")

        except Exception as e:
            error_msg = f"Failed to apply configuration: {e}"
            application_results.append(f"❌ {rtr_name}: {error_msg}")
            await context.error(f"{rtr_name}: {error_msg}")
    
    # Step 6: Format final results
    summary = "\n".join(application_results)
    
    final_text = f"""{'🔍 DRY RUN - ' if dry_run else ''}Configuration {'preview' if dry_run else 'application'} complete!

**Routers:** {', '.join(router_names)}

**Rendered Configuration:**
```
{rendered_config}
```

**Results:**
{summary}
"""
    
    return [types.TextContent(
        type="text",
        text=final_text,
        annotations={
            "router_names": router_names,
            "rendered_config": rendered_config,
            "dry_run": dry_run,
            "variables": str(variables)
        }
    )]

async def handle_execute_junos_commands_batch(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """
    Handler for execute_junos_commands_batch tool - executes multiple commands on multiple routers.
    
    This tool allows batching multiple commands to multiple routers in a single MCP request,
    reducing request/response overhead while benefiting from connection pool reuse.
    
    Example: Execute ["show version", "show system uptime"] on ["router1", "router2", "router3"].
    """
    start_time = time.time()
    start_timestamp = datetime.now(timezone.utc).isoformat()
    router_names = arguments.get("router_names", [])
    commands = arguments.get("commands", [])
    cli_format = arguments.get("format")
    timeout = get_timeout_with_fallback(arguments.get("timeout"))

    barrier_sync = bool(arguments.get("barrier_sync"))
    barrier_policy = arguments.get("barrier_policy") or "proceed"
    if isinstance(barrier_policy, str):
        barrier_policy = barrier_policy.strip().lower()
    if barrier_policy not in {"proceed", "strict"}:
        barrier_policy = "proceed"

    preconnect_timeout_raw = arguments.get("preconnect_timeout")
    if isinstance(preconnect_timeout_raw, int) and preconnect_timeout_raw > 0:
        preconnect_timeout = preconnect_timeout_raw
    else:
        preconnect_timeout = 30

    preconnect_retries_raw = arguments.get("preconnect_retries")
    if isinstance(preconnect_retries_raw, int) and preconnect_retries_raw >= 0:
        preconnect_retries = min(preconnect_retries_raw, 10)
    else:
        preconnect_retries = 2

    preconnect_backoff_raw = arguments.get("preconnect_backoff_seconds")
    if isinstance(preconnect_backoff_raw, int) and preconnect_backoff_raw >= 0:
        preconnect_backoff_seconds = min(preconnect_backoff_raw, 60)
    else:
        preconnect_backoff_seconds = 1

    # Concurrency is set once at server startup.
    
    # Validation
    if not router_names or not isinstance(router_names, list):
        return [types.TextContent(
            type="text",
            text="Error: router_names must be a non-empty array of strings"
        )]
    
    if len(router_names) == 0:
        return [types.TextContent(
            type="text",
            text="Error: router_names array cannot be empty"
        )]
    
    if not commands or not isinstance(commands, list):
        return [types.TextContent(
            type="text",
            text="Error: commands must be a non-empty array of strings"
        )]
    
    if len(commands) == 0:
        return [types.TextContent(
            type="text",
            text="Error: commands array cannot be empty"
        )]
    
    # Validate all routers exist
    invalid_routers = [r for r in router_names if r not in devices]
    if invalid_routers:
        return [types.TextContent(
            type="text",
            text=f"Error: Routers not found in device mapping: {', '.join(invalid_routers)}"
        )]
    
    log.info(
        f"Executing {len(commands)} commands on {len(router_names)} routers with timeout {timeout}s "
        f"(barrier_sync={barrier_sync}, policy={barrier_policy}, preconnect_timeout={preconnect_timeout}s, "
        f"preconnect_retries={preconnect_retries})"
    )

    # Optional barrier sync: preconnect to all routers first, then execute commands only on ready routers.
    preconnect_info: dict[str, Any] | None = None
    routers_to_execute = list(router_names)
    preconnect_failed_map: dict[str, str] = {}

    if barrier_sync:
        import asyncio
        pool = connection_pool

        async def _preconnect_one(router_name: str) -> dict[str, Any]:
            started = time.time()
            last_error: str | None = None

            for attempt in range(preconnect_retries + 1):
                device = None
                try:
                    if pool is not None and pool.enabled:
                        with anyio.fail_after(preconnect_timeout):
                            device = await pool.get_connection(router_name)
                    else:
                        device_info = devices[router_name]
                        connect_params = prepare_connection_params(device_info, router_name)

                        def _open_and_close() -> None:
                            with Device(**connect_params) as dev:
                                dev.timeout = preconnect_timeout

                        with anyio.fail_after(preconnect_timeout):
                            await anyio.to_thread.run_sync(_open_and_close, limiter=thread_limiter)

                    duration = round(time.time() - started, 3)
                    return {
                        "router_name": router_name,
                        "status": "connected",
                        "attempts": attempt + 1,
                        "duration": duration,
                    }
                except Exception as e:
                    last_error = str(e)
                    if pool is not None and pool.enabled:
                        with suppress(Exception):
                            await pool.invalidate_connection(router_name)
                    if attempt == preconnect_retries:
                        duration = round(time.time() - started, 3)
                        return {
                            "router_name": router_name,
                            "status": "failed",
                            "attempts": attempt + 1,
                            "duration": duration,
                            "error": last_error,
                        }
                    if preconnect_backoff_seconds:
                        await asyncio.sleep(preconnect_backoff_seconds)
                finally:
                    if device is not None and pool is not None and pool.enabled:
                        with suppress(Exception):
                            await pool.release_connection(router_name)

            duration = round(time.time() - started, 3)
            return {
                "router_name": router_name,
                "status": "failed",
                "attempts": preconnect_retries + 1,
                "duration": duration,
                "error": last_error or "unknown preconnect failure",
            }

        preconnect_start = time.time()
        preconnect_results = await asyncio.gather(
            *[_preconnect_one(r) for r in router_names],
            return_exceptions=False,
        )
        preconnect_duration = round(time.time() - preconnect_start, 3)

        ready = [r["router_name"] for r in preconnect_results if r.get("status") == "connected"]
        failed = [r for r in preconnect_results if r.get("status") != "connected"]
        preconnect_failed_map = {
            r.get("router_name"): (r.get("error") or "preconnect failed")
            for r in failed
            if r.get("router_name")
        }

        preconnect_info = {
            "enabled": True,
            "policy": barrier_policy,
            "timeout": preconnect_timeout,
            "retries": preconnect_retries,
            "backoff_seconds": preconnect_backoff_seconds,
            "duration": preconnect_duration,
            "ready_count": len(ready),
            "failed_count": len(failed),
            "failed_routers": failed,
        }

        if barrier_policy == "strict" and preconnect_failed_map:
            # Fail fast: do not run any commands if not all routers connected.
            end_time = time.time()
            end_timestamp = datetime.now(timezone.utc).isoformat()
            total_duration = round(end_time - start_time, 3)

            response_mode = _resolve_effective_response_mode(arguments)
            response: dict[str, Any] = {
                "error": "preconnect_failed",
                "message": "Barrier preconnect failed for one or more routers; strict policy prevents execution.",
                "total_routers": len(router_names),
                "total_commands_per_router": len(commands),
                "total_commands_executed": 0,
                "total_successful": 0,
                "total_failed": len(router_names) * len(commands),
                "total_duration": total_duration,
                "start_time": start_timestamp,
                "end_time": end_timestamp,
                "preconnect": preconnect_info,
            }

            if _should_persist_to_disk(arguments, response_mode):
                try:
                    artifact = await _persist_json_artifact(
                        tool_name="execute_junos_commands_batch",
                        arguments=arguments,
                        payload={
                            "request": {
                                "router_names": router_names,
                                "commands": commands,
                                "timeout": timeout,
                                "barrier_sync": True,
                                "barrier_policy": barrier_policy,
                                "preconnect_timeout": preconnect_timeout,
                                "preconnect_retries": preconnect_retries,
                                "preconnect_backoff_seconds": preconnect_backoff_seconds,
                            },
                            "preconnect": preconnect_info,
                            "summary": {
                                "total_routers": len(router_names),
                                "total_commands_per_router": len(commands),
                                "total_commands_executed": 0,
                                "total_successful": 0,
                                "total_failed": len(router_names) * len(commands),
                                "total_duration": total_duration,
                                "start_time": start_timestamp,
                                "end_time": end_timestamp,
                            },
                            "routers": [],
                        },
                    )
                    response["artifact"] = artifact
                except Exception as e:
                    log.warning(f"Failed to persist batch artifact: {e}")

            if response_mode == "artifact":
                response.pop("routers", None)

            import json
            return [types.TextContent(type="text", text=json.dumps(response, indent=2))]

        routers_to_execute = ready
    
    
    # Execute commands on all routers in parallel using asyncio.gather
    async def execute_router_commands(router_name: str):
        """Execute all commands on a single router sequentially."""
        router_start = time.time()
        router_results = []
        
        for idx, command in enumerate(commands, 1):
            cmd_start = time.time()
            cmd_start_ts = datetime.now(timezone.utc).isoformat()
            
            try:
                log.debug(f"[{router_name}][{idx}/{len(commands)}] Executing: {command}")
                
                result = await _run_cli_command(router_name, command, timeout, cli_format=cli_format)
                is_error = _looks_like_cli_error(result)
                
                cmd_end = time.time()
                cmd_duration = round(cmd_end - cmd_start, 3)
                
                cmd_result: dict[str, Any] = {
                    "command": command,
                    "format": cli_format,
                    "success": (not is_error),
                    "output": result,
                    "execution_duration": cmd_duration,
                    "start_time": cmd_start_ts,
                    "end_time": datetime.now(timezone.utc).isoformat(),
                }
                if is_error:
                    cmd_result["error"] = result
                router_results.append(cmd_result)
                
                log.debug(f"[{router_name}][{idx}/{len(commands)}] Completed in {cmd_duration}s")
                
            except Exception as e:
                cmd_end = time.time()
                cmd_duration = round(cmd_end - cmd_start, 3)
                error_msg = str(e)
                
                router_results.append({
                    "command": command,
                    "success": False,
                    "error": error_msg,
                    "execution_duration": cmd_duration,
                    "start_time": cmd_start_ts,
                    "end_time": datetime.now(timezone.utc).isoformat()
                })
                
                log.error(f"[{router_name}][{idx}/{len(commands)}] Failed: {error_msg}")
        
        router_end = time.time()
        router_duration = round(router_end - router_start, 3)
        
        successful = sum(1 for r in router_results if r.get("success"))
        failed = len(router_results) - successful
        
        return {
            "router_name": router_name,
            "total_commands": len(commands),
            "executed_commands": len(commands),
            "successful": successful,
            "failed": failed,
            "router_duration": router_duration,
            "results": router_results
        }
    
    # Execute all ready routers in parallel
    import asyncio
    router_responses = await asyncio.gather(
        *[execute_router_commands(router) for router in routers_to_execute],
        return_exceptions=True
    )
    
    # Process results, handling any exceptions
    processed_responses = []
    for idx, response in enumerate(router_responses):
        if isinstance(response, Exception):
            router_name = routers_to_execute[idx]
            processed_responses.append({
                "router_name": router_name,
                "total_commands": len(commands),
                "executed_commands": 0,
                "successful": 0,
                "failed": len(commands),
                "router_duration": 0,
                "error": str(response),
                "results": []
            })
            log.error(f"Router {router_name} execution failed: {response}")
        else:
            processed_responses.append(response)

    # If barrier_sync is enabled and we're proceeding with a subset, record skipped routers.
    if barrier_sync and preconnect_failed_map:
        for skipped_router, err in preconnect_failed_map.items():
            processed_responses.append({
                "router_name": skipped_router,
                "total_commands": len(commands),
                "executed_commands": 0,
                "successful": 0,
                "failed": len(commands),
                "router_duration": 0,
                "error": f"preconnect_failed (skipped execution): {err}",
                "results": [],
            })
    
    # Calculate totals
    end_time = time.time()
    end_timestamp = datetime.now(timezone.utc).isoformat()
    total_duration = round(end_time - start_time, 3)
    
    total_commands_executed = sum(int(r.get("executed_commands") or 0) for r in processed_responses)
    total_successful = sum(r["successful"] for r in processed_responses)
    total_failed = sum(r["failed"] for r in processed_responses)
    
    response_mode = _resolve_effective_response_mode(arguments)

    # Build response
    response: dict[str, Any] = {
        "total_routers": len(router_names),
        "total_routers_executed": len(routers_to_execute),
        "total_routers_preconnect_failed": len(preconnect_failed_map) if barrier_sync else 0,
        "total_commands_per_router": len(commands),
        "total_commands_executed": total_commands_executed,
        "total_successful": total_successful,
        "total_failed": total_failed,
        "total_duration": total_duration,
        "start_time": start_timestamp,
        "end_time": end_timestamp,
    }

    if barrier_sync and preconnect_info is not None:
        response["preconnect"] = preconnect_info

    if response_mode in {"summary", "artifact"}:
        router_summaries = []
        for r in processed_responses:
            router_summaries.append({
                "router_name": r.get("router_name"),
                "executed_commands": r.get("executed_commands"),
                "successful": r.get("successful"),
                "failed": r.get("failed"),
                "router_duration": r.get("router_duration"),
                "error": r.get("error"),
            })

        failed_routers = [r for r in router_summaries if (r.get("failed") or 0) > 0 or r.get("error")]
        slowest_routers = sorted(
            [r for r in router_summaries if isinstance(r.get("router_duration"), (int, float))],
            key=lambda x: x.get("router_duration", 0),
            reverse=True,
        )[:10]

        response["failed_routers"] = failed_routers
        response["slowest_routers"] = slowest_routers
    else:
        response["routers"] = processed_responses

    # Optionally persist full results (even if we're returning only a summary).
    if _should_persist_to_disk(arguments, response_mode):
        try:
            artifact = await _persist_json_artifact(
                tool_name="execute_junos_commands_batch",
                arguments=arguments,
                payload={
                    "request": {
                        "router_names": router_names,
                        "commands": commands,
                        "timeout": timeout,
                        "barrier_sync": barrier_sync,
                        "barrier_policy": barrier_policy,
                        "preconnect_timeout": preconnect_timeout,
                        "preconnect_retries": preconnect_retries,
                        "preconnect_backoff_seconds": preconnect_backoff_seconds,
                    },
                    "preconnect": preconnect_info,
                    "summary": {
                        "total_routers": len(router_names),
                        "total_routers_executed": len(routers_to_execute),
                        "total_routers_preconnect_failed": len(preconnect_failed_map) if barrier_sync else 0,
                        "total_commands_per_router": len(commands),
                        "total_commands_executed": total_commands_executed,
                        "total_successful": total_successful,
                        "total_failed": total_failed,
                        "total_duration": total_duration,
                        "start_time": start_timestamp,
                        "end_time": end_timestamp,
                    },
                    "routers": processed_responses,
                },
            )
            response["artifact"] = artifact
        except Exception as e:
            log.warning(f"Failed to persist batch artifact: {e}")

    # In artifact mode, never include large raw outputs in the tool response.
    if response_mode == "artifact":
        response.pop("routers", None)
    
    import json
    response_text = json.dumps(response, indent=2)
    
    content_block = types.TextContent(
        type="text",
        text=response_text,
        annotations={
            "routers_count": len(router_names),
            "commands_count": len(commands),
            "metadata": {
                "total_duration": total_duration,
                "total_successful": total_successful,
                "total_failed": total_failed
            }
        }
    )
    
    log.info(f"Completed {len(commands)} commands on {len(router_names)} routers: {total_successful} success, {total_failed} failed, {total_duration}s total")
    return [content_block]


async def handle_read_artifact(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Read a previously persisted artifact.

    Supports fetching by absolute/relative path (disk) or by run_id via the
    configured backend (disk/redis/dual).
    """
    artifact_path = arguments.get("artifact_path")
    run_id = arguments.get("run_id")
    # Default to full (legacy behavior): return the full stored payload
    # unless the caller explicitly requests a compact view (diff/summary/etc).
    mode = arguments.get("mode") or "full"
    max_output_chars = arguments.get("max_output_chars")

    # diff mode knobs
    diff_include_diffs = arguments.get("diff_include_diffs")
    diff_context_lines = arguments.get("diff_context_lines")
    diff_max_diff_chars = arguments.get("diff_max_diff_chars")
    diff_max_routers_per_group = arguments.get("diff_max_routers_per_group")
    diff_max_groups = arguments.get("diff_max_groups")

    # Optional: return only a subset of routers from a large artifact.
    # This enables token-safe workflows where you collect once (many routers)
    # and read back in smaller chunks.
    filter_router_names = arguments.get("router_names")
    filter_router_offset = arguments.get("router_offset")
    filter_router_limit = arguments.get("router_limit")

    if isinstance(mode, str):
        mode = mode.strip().lower()
    if mode not in {"metadata", "summary", "failures", "full", "diff"}:
        mode = "full"

    if not isinstance(max_output_chars, int) or max_output_chars <= 0:
        max_output_chars = 4000

    def _filter_artifact_by_router(obj: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Return a shallow-copied artifact with filtered router entries.

        Supports both payload shapes:
        - execute_junos_command_batch artifacts: key "results" (list)
        - execute_junos_commands_batch artifacts: key "routers" (list)

        Returns (filtered_obj, filter_meta_or_none).
        """

        key: str | None = None
        items: list[Any] | None = None
        if isinstance(obj.get("results"), list):
            key = "results"
            items = obj.get("results")
        elif isinstance(obj.get("routers"), list):
            key = "routers"
            items = obj.get("routers")

        if key is None or items is None:
            return obj, None

        # Determine filter mode.
        selected_names: set[str] | None = None
        if isinstance(filter_router_names, list):
            selected_names = {str(x) for x in filter_router_names if isinstance(x, str) and x.strip()}

        offset = filter_router_offset if isinstance(filter_router_offset, int) and filter_router_offset >= 0 else None
        limit = filter_router_limit if isinstance(filter_router_limit, int) and filter_router_limit > 0 else None

        filtered_items = list(items)
        if selected_names is not None:
            filtered_items = [
                it for it in filtered_items
                if isinstance(it, dict) and it.get("router_name") in selected_names
            ]
        elif offset is not None or limit is not None:
            start = offset or 0
            end = (start + limit) if limit is not None else None
            filtered_items = filtered_items[start:end]
        else:
            return obj, None

        filtered_obj: dict[str, Any] = dict(obj)
        filtered_obj[key] = filtered_items

        # Add some light filter metadata (doesn't change the underlying stored artifact).
        filter_meta: dict[str, Any] = {
            "applied": True,
            "key": key,
            "original_count": len(items),
            "returned_count": len(filtered_items),
        }
        if selected_names is not None:
            filter_meta["router_names"] = sorted(selected_names)
        if offset is not None:
            filter_meta["router_offset"] = offset
        if limit is not None:
            filter_meta["router_limit"] = limit

        if isinstance(filtered_obj.get("summary"), dict):
            filtered_obj["summary"] = dict(filtered_obj["summary"])
            filtered_obj["summary"]["filtered_total_routers"] = len(filtered_items)

        return filtered_obj, filter_meta

    backend = _get_artifact_backend(arguments)
    artifact_dir = _get_artifact_dir(arguments)

    resolved_path: str | None = None

    if isinstance(artifact_path, str) and artifact_path.strip():
        # Explicit disk path read (works regardless of backend).
        resolved_path = os.path.abspath(os.path.expanduser(artifact_path.strip()))
        try:
            with open(resolved_path, "r", encoding="utf-8") as f:
                raw = f.read()
            artifact_obj = json.loads(raw)
        except FileNotFoundError:
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": "artifact_not_found",
                    "artifact_path": resolved_path,
                }, indent=2),
            )]
        except json.JSONDecodeError as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": "invalid_artifact_json",
                    "artifact_path": resolved_path,
                    "message": str(e),
                }, indent=2),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": "artifact_read_failed",
                    "artifact_path": resolved_path,
                    "message": str(e),
                }, indent=2),
            )]
    elif isinstance(run_id, str) and run_id.strip():
        rid = run_id.strip()
        try:
            store = _get_artifact_store(arguments)
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": "artifact_store_unavailable",
                    "backend": backend,
                    "artifact_dir": artifact_dir,
                    "run_id": rid,
                    "message": str(e),
                }, indent=2, ensure_ascii=False),
            )]

        def _do_read() -> dict[str, Any]:
            return store.read_json(run_id=rid)

        try:
            artifact_obj = await anyio.to_thread.run_sync(_do_read, limiter=thread_limiter)
        except FileNotFoundError:
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": "artifact_not_found",
                    "backend": backend,
                    "artifact_dir": artifact_dir,
                    "run_id": rid,
                }, indent=2, ensure_ascii=False),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": "artifact_read_failed",
                    "backend": backend,
                    "artifact_dir": artifact_dir,
                    "run_id": rid,
                    "message": str(e),
                }, indent=2, ensure_ascii=False),
            )]
    else:
        return [types.TextContent(
            type="text",
            text=json.dumps({
                "error": "missing_parameters",
                "message": "Provide artifact_path or run_id",
            }, indent=2),
        )]

    # Prepare an artifact object for rendering (optional router filtering, optional diff knobs).
    artifact_for_view: dict[str, Any] | Any = artifact_obj
    filter_meta: dict[str, Any] | None = None

    if isinstance(artifact_obj, dict):
        filtered_artifact_obj, filter_meta = _filter_artifact_by_router(artifact_obj)
        if filter_meta is not None:
            artifact_for_view = filtered_artifact_obj

        if mode == "diff":
            diff_opts: dict[str, Any] = {}
            if diff_include_diffs is not None:
                diff_opts["include_diffs"] = bool(diff_include_diffs)
            if isinstance(diff_context_lines, int):
                diff_opts["context_lines"] = diff_context_lines
            if isinstance(diff_max_diff_chars, int):
                diff_opts["max_diff_chars"] = diff_max_diff_chars
            if isinstance(diff_max_routers_per_group, int):
                diff_opts["max_routers_per_group"] = diff_max_routers_per_group
            if isinstance(diff_max_groups, int):
                diff_opts["max_diff_groups"] = diff_max_groups

            if diff_opts:
                tmp_obj = dict(artifact_for_view)
                tmp_obj["_diff_opts"] = diff_opts
                artifact_for_view = tmp_obj

    view = _render_artifact_view(artifact_for_view, mode=mode, max_output_chars=max_output_chars)
    if filter_meta is not None and isinstance(view, dict):
        view["filter"] = filter_meta

    view["backend"] = backend
    if resolved_path is not None:
        view["artifact_path"] = resolved_path
    view.setdefault("artifact_dir", artifact_dir)
    return [types.TextContent(type="text", text=json.dumps(view, indent=2, ensure_ascii=False))]



async def handle_gather_device_facts(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Handler for gather_device_facts tool"""
    router_name = arguments.get("router_name", "")
    timeout = get_timeout_with_fallback(arguments.get("timeout"))
    
    if router_name not in devices:
        result = f"Router {router_name} not found in the device mapping."
    else:
        log.debug(f"Getting facts from router {router_name} with timeout {timeout}s")

        # Custom JSON encoder to handle version_info and other complex objects
        def json_serializer(obj):
            if hasattr(obj, '_asdict'):  # Named tuples like version_info
                return obj._asdict()
            elif hasattr(obj, '__dict__'):  # Objects with __dict__
                return obj.__dict__
            else:
                return str(obj)

        async def _facts_to_json() -> str:
            pool = connection_pool
            if pool is not None and pool.enabled:
                device = await pool.get_connection(router_name)
                try:
                    def _do_facts() -> str:
                        device.timeout = timeout
                        facts_dict = dict(device.facts)
                        return json.dumps(facts_dict, indent=2, default=json_serializer)

                    return await anyio.to_thread.run_sync(_do_facts, limiter=thread_limiter)
                except ConnectError as ce:
                    await pool.invalidate_connection(router_name)
                    return f"Connection error to {router_name}: {ce}"
                except Exception as e:
                    await pool.invalidate_connection(router_name)
                    return f"An error occurred: {e}"
                finally:
                    await pool.release_connection(router_name)

            # Pool disabled: open/run/close in one thread
            device_info = devices[router_name]
            connect_params = prepare_connection_params(device_info, router_name)
            connect_params['timeout'] = timeout

            def _direct_facts() -> str:
                with Device(**connect_params) as junos_device:
                    facts_dict = dict(junos_device.facts)
                    return json.dumps(facts_dict, indent=2, default=json_serializer)

            try:
                return await anyio.to_thread.run_sync(_direct_facts, limiter=thread_limiter)
            except ConnectError as ce:
                return f"Connection error to {router_name}: {ce}"
            except Exception as e:
                return f"An error occurred: {e}"

        try:
            result = await _facts_to_json()
        except ValueError as ve:
            result = f"Error: {ve}"

    content_block = types.TextContent(
        type="text",
        text=result,
        annotations={"router_name": router_name}
        )
    log.debug(f"content block: {content_block}")
    
    return [content_block]


async def handle_get_router_list(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Handler for get_router_list tool"""
    log.debug("Getting list of routers")

    # Build structured device information, excluding sensitive data
    router_info = {}
    for router_name, device_config in devices.items():
        # Create a deep copy of device config to avoid modifying original
        import copy
        filtered_config = copy.deepcopy(device_config)

        # Exclude ssh_config (jump host/proxy configuration)
        if "ssh_config" in filtered_config:
            del filtered_config["ssh_config"]

        # Exclude sensitive auth credentials but keep auth type
        if "auth" in filtered_config:
            # Remove password if present
            if "password" in filtered_config["auth"]:
                del filtered_config["auth"]["password"]
            # Remove private key path if present
            if "private_key_path" in filtered_config["auth"]:
                del filtered_config["auth"]["private_key_path"]

        router_info[router_name] = filtered_config

    # Format as pretty JSON
    result = json.dumps(router_info, indent=2)

    content_block = types.TextContent(
        type="text",
        text=result
        )

    log.debug(f"content block: {content_block}")
    return [content_block]


async def handle_load_and_commit_config(arguments: dict, context: Context) -> list[types.ContentBlock]:
    """Handler for load_and_commit_config tool"""
    router_name = arguments.get("router_name", "")
    config_text = arguments.get("config_text", "")
    config_format = arguments.get("config_format", "set")
    commit_comment = arguments.get("commit_comment", "Configuration loaded via MCP")
    timeout = get_timeout_with_fallback(arguments.get("timeout"))
    
    if router_name not in devices:
        result = f"Router {router_name} not found in the device mapping."
    else:
        log.debug(f"Loading and committing config on router {router_name} with format {config_format}")
        try:
            device_info = devices[router_name]
            connect_params = prepare_connection_params(device_info, router_name)
        except ValueError as ve:
            result = f"Error: {ve}"
        else:
            pool = connection_pool

            def _commit_logic(junos_device: Device) -> str:
                config_util = Config(junos_device)
                try:
                    config_util.lock()
                except Exception as e:
                    return f"Failed to lock configuration: {e}"

                try:
                    fmt = config_format.lower()
                    if fmt == "set":
                        config_util.load(config_text, format='set')
                    elif fmt == "text":
                        config_util.load(config_text, format='text')
                    elif fmt == "xml":
                        config_util.load(config_text, format='xml')
                    else:
                        return f"Error: Unsupported config format '{config_format}'. Use 'set', 'text', or 'xml'"

                    diff = config_util.diff()
                    if not diff:
                        return "No configuration changes detected"

                    config_util.commit(comment=commit_comment, timeout=timeout)
                    return f"Configuration successfully loaded and committed on {router_name}. Changes:\n{diff}"
                except Exception as e:
                    with suppress(Exception):
                        config_util.rollback()
                    return f"Failed to load/commit configuration: {e}"
                finally:
                    with suppress(Exception):
                        config_util.unlock()

            if pool is not None and pool.enabled:
                junos_device = await pool.get_connection(router_name)
                try:
                    junos_device.timeout = timeout
                    result = await anyio.to_thread.run_sync(lambda: _commit_logic(junos_device), limiter=thread_limiter)
                except ConnectError as ce:
                    await pool.invalidate_connection(router_name)
                    result = f"Connection error to {router_name}: {ce}"
                except Exception as e:
                    await pool.invalidate_connection(router_name)
                    result = f"An error occurred: {e}"
                finally:
                    await pool.release_connection(router_name)
            else:
                def _direct_commit() -> str:
                    with Device(**connect_params) as junos_device:
                        junos_device.timeout = timeout
                        return _commit_logic(junos_device)

                try:
                    result = await anyio.to_thread.run_sync(_direct_commit, limiter=thread_limiter)
                except ConnectError as ce:
                    result = f"Connection error to {router_name}: {ce}"
                except Exception as e:
                    result = f"An error occurred: {e}"
    
    content_block = types.TextContent(
        type="text",
        text=result,
        annotations={"router_name": router_name, "config_text": config_text,
                     "config_format":config_format,"commit_comment":commit_comment}
        )

    return [content_block]


# Tool registry mapping tool names to their handler functions
# To add a new tool:
# 1. Create an async handler function: async def handle_my_new_tool(arguments: dict) -> list[types.ContentBlock]
# 2. Add it to this registry: "my_new_tool": handle_my_new_tool
# 3. Add the tool definition to list_tools() method
TOOL_HANDLERS = {
    "execute_junos_command": handle_execute_junos_command,
    "execute_junos_command_batch": handle_execute_junos_command_batch,
    "execute_junos_commands_batch": handle_execute_junos_commands_batch,
    "get_junos_config": handle_get_junos_config,
    "junos_config_diff": handle_junos_config_diff,
    "render_and_apply_j2_template": handle_render_and_apply_j2_template,
    "gather_device_facts": handle_gather_device_facts,
    "get_router_list": handle_get_router_list,
    "load_and_commit_config": handle_load_and_commit_config,
    "add_device": handle_add_device,     # Dynamic device management
    "read_artifact": handle_read_artifact,
    "list_artifacts": handle_list_artifacts,
    "get_server_settings": handle_get_server_settings,
}


def create_mcp_server() -> Server:
    """Create and configure the MCP server with all tools"""
    app = Server(JUNOS_MCP, version="1.0.0")
    
    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        """Handle tool calls using the tool registry"""
        handler = TOOL_HANDLERS.get(name)
        if handler:
            try:
                request_context = app.request_context
                log.info(f"Got request_context: {type(request_context)}, session: {type(request_context.session) if request_context else None}")
            except LookupError as e:
                log.warning(f"LookupError getting request_context: {e}")
                request_context = None
            
            context = Context(request_context=request_context, fastmcp=app)
            log.info(f"Created context with request_context: {request_context is not None}")
            
            return await handler(arguments, context=context)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    @app.list_resources()
    async def list_resources() -> list[types.Resource]:
        """List available resources - none for this server"""
        return []
    
    @app.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        """List available prompts - none for this server"""
        return []
    
    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        """List available tools"""
        return [
            types.Tool(
                name="execute_junos_command",
                description="Execute a Junos command on the router",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_name": {"type": "string", "description": "The name of the router"},
                        "command": {"type": "string", "description": "The command to execute on the router"},
                        "format": {
                            "type": "string",
                            "description": "Output format: text (default), json, or xml",
                            "enum": ["text", "json", "xml"]
                        },
                        "timeout": {"type": "integer", "description": "Command timeout in seconds", "default": 360}
                    },
                    "required": ["router_name", "command"]
                }
            ),
            types.Tool(
                name="execute_junos_command_batch",
                description="Execute the same Junos command on multiple routers in parallel. Returns structured JSON output with per-router results, timing, and success/failure status.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of router names to execute the command on"
                        },
                        "command": {"type": "string", "description": "The command to execute on all routers"},
                        "format": {
                            "type": "string",
                            "description": "Output format for the CLI command: text (default), json, or xml",
                            "enum": ["text", "json", "xml"]
                        },
                        "timeout": {"type": "integer", "description": "Command timeout in seconds per router", "default": 360},
                        "barrier_sync": {
                            "type": "boolean",
                            "description": "If true: preconnect to routers first, then (optionally) run the command only on routers with established sessions",
                            "default": False
                        },
                        "barrier_policy": {
                            "type": "string",
                            "description": "Barrier behavior when some routers fail preconnect: proceed (run on connected subset) or strict (abort execution)",
                            "enum": ["proceed", "strict"],
                            "default": "proceed"
                        },
                        "preconnect_timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds for each router preconnect attempt",
                            "default": 30
                        },
                        "preconnect_retries": {
                            "type": "integer",
                            "description": "Number of retries for router preconnect failures (0 means no retry)",
                            "default": 2
                        },
                        "preconnect_backoff_seconds": {
                            "type": "integer",
                            "description": "Seconds to wait between preconnect retries",
                            "default": 1
                        },
                        "response_mode": {
                            "type": "string",
                            "description": "Response size mode: full (include all outputs), summary (only failures/metadata), artifact (persist full results to disk and return only summary+pointer)",
                            "enum": ["full", "summary", "artifact"]
                        },
                        "persist_to_disk": {
                            "type": "boolean",
                            "description": "If true, persist full results to disk and include an artifact pointer in the response",
                            "default": False
                        },
                        "artifact_label": {"type": "string", "description": "Optional short label added to the artifact filename"},
                        "artifact_dir": {"type": "string", "description": "Optional directory to store artifacts (defaults to JMCP_ARTIFACT_DIR or <jmcp.py dir>/artifacts)"}
                    },
                    "required": ["router_names", "command"]
                }
            ),
            types.Tool(
                name="execute_junos_commands_batch",
                description="Execute multiple Junos commands on multiple routers in parallel. This tool batches N commands to M routers in one MCP request, reducing overhead while benefiting from connection pool reuse. Each router executes all commands sequentially, but routers are processed in parallel. Ideal for running the same set of commands across multiple devices.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of router names to execute commands on"
                        },
                        "commands": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of commands to execute on each router"
                        },
                        "format": {
                            "type": "string",
                            "description": "Output format for all CLI commands in this batch: text (default), json, or xml",
                            "enum": ["text", "json", "xml"]
                        },
                        "timeout": {"type": "integer", "description": "Command timeout in seconds per command", "default": 360},
                        "barrier_sync": {
                            "type": "boolean",
                            "description": "If true: preconnect to routers first, then (optionally) run commands only on routers with established sessions",
                            "default": False
                        },
                        "barrier_policy": {
                            "type": "string",
                            "description": "Barrier behavior when some routers fail preconnect: proceed (run on connected subset) or strict (abort execution)",
                            "enum": ["proceed", "strict"],
                            "default": "proceed"
                        },
                        "preconnect_timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds for each router preconnect attempt",
                            "default": 30
                        },
                        "preconnect_retries": {
                            "type": "integer",
                            "description": "Number of retries for router preconnect failures (0 means no retry)",
                            "default": 2
                        },
                        "preconnect_backoff_seconds": {
                            "type": "integer",
                            "description": "Seconds to wait between preconnect retries",
                            "default": 1
                        },
                        "response_mode": {
                            "type": "string",
                            "description": "Response size mode: full (include all outputs), summary (only failures/metadata), artifact (persist full results to configured artifact backend and return only summary+pointer)",
                            "enum": ["full", "summary", "artifact"]
                        },
                        "persist_to_disk": {
                            "type": "boolean",
                            "description": "If true, persist full results to the configured artifact backend and include an artifact pointer in the response",
                            "default": False
                        },
                        "persist_to_redis": {
                            "type": "boolean",
                            "description": "If true, persist full results to the configured artifact backend (use with artifact_backend=redis/dual); kept for convenience",
                            "default": False
                        },
                        "artifact_backend": {
                            "type": "string",
                            "description": "Artifact storage backend: disk (files), redis (Redis), dual (both; Redis primary)",
                            "enum": ["disk", "redis", "dual"]
                        },
                        "artifact_label": {"type": "string", "description": "Optional short label added to the artifact filename"},
                        "artifact_dir": {"type": "string", "description": "Optional directory to store artifacts (defaults to JMCP_ARTIFACT_DIR or <jmcp.py dir>/artifacts)"}
                    },
                    "required": ["router_names", "commands"]
                }
            ),
            types.Tool(
                name="read_artifact",
                description="Read a persisted JMCP artifact by path (disk) or run_id (disk/redis/dual). Supports compact views (metadata/summary/failures) to avoid huge responses.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "artifact_path": {"type": "string", "description": "Absolute or relative path to the artifact JSON"},
                        "run_id": {"type": "string", "description": "Artifact run_id (resolved via configured backend; disk uses artifact_dir lookup)"},
                        "artifact_dir": {"type": "string", "description": "Optional artifact directory for run_id lookup"},
                        "artifact_backend": {
                            "type": "string",
                            "description": "Artifact backend to read from when using run_id",
                            "enum": ["disk", "redis", "dual"]
                        },
                        "router_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: return only these routers from the artifact (useful for token-safe chunked reads)"
                        },
                        "router_offset": {
                            "type": "integer",
                            "description": "Optional: return routers starting at this offset (0-based) when router_names is not provided"
                        },
                        "router_limit": {
                            "type": "integer",
                            "description": "Optional: return at most this many routers when router_names is not provided"
                        },
                        "mode": {
                            "type": "string",
                            "description": "View mode: metadata, summary, failures, diff, full",
                            "enum": ["metadata", "summary", "failures", "diff", "full"],
                            "default": "full"
                        },
                        "max_output_chars": {"type": "integer", "description": "Max characters to include per output snippet in failures mode", "default": 4000},
                        "diff_include_diffs": {"type": "boolean", "description": "diff mode: if true, force unified diffs; if false, disable diffs; if omitted, server uses auto heuristics (may suppress rewrite diffs or oversized diffs)"},
                        "diff_context_lines": {"type": "integer", "description": "diff mode: unified diff context lines", "default": 10},
                        "diff_max_diff_chars": {"type": "integer", "description": "diff mode: max characters for each diff text", "default": 3000},
                        "diff_max_routers_per_group": {"type": "integer", "description": "diff mode: max router names to list per variant group", "default": 10},
                        "diff_max_groups": {"type": "integer", "description": "diff mode: max variant groups to include (and optionally diff) vs baseline", "default": 3}
                    },
                    "required": []
                }
            ),
            types.Tool(
                name="list_artifacts",
                description="List persisted JMCP artifacts from the configured backend (disk/redis/dual), optionally filtered by tool/label and time.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "artifact_dir": {"type": "string", "description": "Optional artifact directory (defaults to JMCP_ARTIFACT_DIR or <jmcp.py dir>/artifacts)"},
                        "artifact_backend": {
                            "type": "string",
                            "description": "Artifact backend to list from",
                            "enum": ["disk", "redis", "dual"]
                        },
                        "tool": {"type": "string", "description": "Filter by tool name, e.g. execute_junos_commands_batch"},
                        "label_contains": {"type": "string", "description": "Filter by substring match on artifact label"},
                        "since": {"type": "string", "description": "Only include artifacts modified since this ISO8601 timestamp (UTC assumed if no timezone)"},
                        "limit": {"type": "integer", "description": "Max artifacts to return (max 500)", "default": 50}
                    },
                    "required": []
                }
            ),
            types.Tool(
                name="get_server_settings",
                description="Return current JMCP server settings (workers, response mode, artifact backend/dir, pool status).",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            types.Tool(
                name="get_junos_config",
                description="Get the configuration of the router",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_name": {"type": "string", "description": "The name of the router"}
                    },
                    "required": ["router_name"]
                }
            ),
            types.Tool(
                name="junos_config_diff",
                description="Get the configuration diff against a rollback version",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_name": {"type": "string", "description": "The name of the router"},
                        "version": {"type": "integer", "description": "Rollback version to compare against (1-49)", "default": 1}
                    },
                    "required": ["router_name"]
                }
            ),
            types.Tool(
                name="render_and_apply_j2_template",
                description="Render a Jinja2 template and apply it to the router",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_name": {"type": "string", "description": "The name of the router"},
                        "template_content": {"type": "string", "description": "Jinja2 template to load"},
                        "vars_content": {"type": "string", "description": "YAML variables to load"},
                        "apply_config": {"type": "boolean", "description": "Boolean to apply or just render (default: False)"},
                        "dry_run": {"type": "boolean", "description": "Boolean to show diff without committing (default: False)"},
                        "commit_comment": {"type": "string", "description": "Commit comment", "default": "Configuration loaded via MCP"}
                    },
                    "required": ["template_content", "vars_content"]
                }
            ),
            types.Tool(
                name="gather_device_facts",
                description="Gather Junos device facts from the router",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_name": {"type": "string", "description": "The name of the router"},
                        "timeout": {"type": "integer", "description": "Connection timeout in seconds", "default": 360}
                    },
                    "required": ["router_name"]
                }
            ),
            types.Tool(
                name="get_router_list",
                description="Get list of available Junos routers",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            types.Tool(
                name="load_and_commit_config",
                description="Load and commit configuration on a Junos router",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "router_name": {"type": "string", "description": "The name of the router"},
                        "config_text": {"type": "string", "description": "The configuration text to load"},
                        "config_format": {"type": "string", "description": "Format: set, text, or xml", "default": "set"},
                        "commit_comment": {"type": "string", "description": "Commit comment", "default": "Configuration loaded via MCP"}
                    },
                    "required": ["router_name", "config_text"]
                }
            ),
            types.Tool(
                name="add_device",
                description="Add a new Junos device with interactive elicitation for device details",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "device_name": {"type": "string", "description": "Device name/identifier", "default": ""},
                        "device_ip": {"type": "string", "description": "Device IP address", "default": ""},
                        "device_port": {"type": "integer", "description": "SSH port (default: 22)", "default": 0},
                        "username": {"type": "string", "description": "Username for authentication", "default": ""},
                        "ssh_key_path": {"type": "string", "description": "Path to SSH private key file", "default": ""}
                    },
                    "required": []
                }
            )
        ]

    return app


def main():
    global MAX_WORKERS, thread_limiter, WORKERS_PER_CORE
    
    # Create the parser
    parser = argparse.ArgumentParser(
        description="Junos MCP Server - Model Context Protocol server for Junos automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
BASIC USAGE:
    python jmcp.py                              # Start with defaults (ceil(cpu*1.5) workers, connection pool ON)
  python jmcp.py -p 8080                      # Use custom port
  python jmcp.py -f my_routers.json           # Use different device file

WORKER POOL TUNING:
    python jmcp.py                              # Default: ceil(cpu_cores*1.5) workers (floor=8, cap=80)
    python jmcp.py --workers-per-core 2         # Scale with CPU cores (cores × factor)

CONNECTION POOL OPTIONS:
  python jmcp.py --idle-timeout 600           # Keep connections alive 10 minutes
  python jmcp.py --health-check-interval 60   # Health check every 60 seconds
  python jmcp.py --disable-connection-pool    # Disable pooling (NOT RECOMMENDED - 100x slower!)

PRODUCTION EXAMPLES:
  # Small deployment (20-30 routers)
    python jmcp.py -p 30030                     # Start with defaults
    python jmcp.py -p 30030 --workers-per-core 2
  
  # Medium deployment (45-75 routers)
    python jmcp.py -p 30030 --workers-per-core 3 --idle-timeout 600
  
  # Large deployment (100+ routers)
    python jmcp.py -p 30030 --workers-per-core 4 --idle-timeout 900 --health-check-interval 120

TROUBLESHOOTING:
  python jmcp.py --disable-connection-pool    # If seeing connection issues
    python jmcp.py --workers-per-core 2         # Lower concurrency if system constrained

CONFIGURATION:
        Thread Pool: Defaults to ceil(cpu_cores*1.5) workers (floor=8, cap=80) to avoid overload on shared hosts
  Connection Pool: ENABLED by default (15-100x faster batch operations)
    Priority: --workers-per-core CLI > default (ceil(cpu*1.5))

TCP SESSION BEHAVIOR:
  
  EXAMPLE PROMPT: "on all routers get show version and show system uptime"
  
  BATCH TOOL OPTIONS:
    • execute_junos_command_batch: N routers, 1 command (all get same command)
    • execute_junos_commands_batch: N routers, N commands (all get same command set)
  
  WITH CONNECTION POOL (default - RECOMMENDED):
    50 routers, 12 commands each (execute_junos_commands_batch):
    
    Router 1-50: All execute in PARALLEL
      Each router: TCP Session opens → authenticate
                   ├─ cmd1 (uses session)
                   ├─ cmd2 (REUSES session)
                   ...
                   └─ cmd12 (REUSES session)
                   Session kept alive for 300 seconds
    
    Result:
    • Total TCP sessions: 50 (1 per router)
    • Sessions reused: YES (12 times per router)
    • TIME_WAIT sockets: Only 50 (when idle timeout expires)
    • Total time: 2.3 seconds (parallel execution!)
    • Throughput: 262 commands/second
    • Token usage: ~53K tokens for 600 operations
  
  WITHOUT CONNECTION POOL (--disable-connection-pool):
    50 routers, 12 commands each:
    
    Each router: 12 separate sessions (connect, auth, command, close)
    
    Result:
    • Total TCP sessions: 600 (new connection per command!)
    • Reconnection overhead: ~1-2 seconds × 600
    • TIME_WAIT sockets: 600 immediately
    • Speed: SLOW (reconnect between every command)
  
  KEY INSIGHTS:
    • Pool works PER ROUTER, not per batch
    • 1 router + 20 commands = 1 TCP session (reused 20 times)
    • 50 routers + 12 commands = 50 sessions (1 per router, each reused 12 times)
    • Session stays alive until idle timeout (default: 300s)
    • Batch processing: All routers execute in parallel (not sequential)
  
  EXECUTION MODEL:
    • PARALLEL across routers: All 50 routers execute simultaneously
    • SEQUENTIAL per router: Each router's commands run one-by-one
    • Total time ≈ time for commands on slowest router (NOT 50×12×time)
    • Pool eliminates reconnections between commands (15-100x faster!)
        """
    )
    
    # Device configuration
    parser.add_argument(
        '-f', '--device-mapping',
        default="devices.json",
        type=str,
        help='JSON file containing device mappings (default: devices.json)'
    )
    
    # Server configuration
    parser.add_argument(
        '-H', '--host',
        default="127.0.0.1",
        type=str,
        help='Server host address (default: 127.0.0.1)'
    )
    
    parser.add_argument(
        '-t', '--transport',
        default="streamable-http",
        type=str,
        choices=['streamable-http', 'stdio'],
        help='Transport protocol (default: streamable-http)'
    )
    
    parser.add_argument(
        '-p', '--port',
        default=30030,
        type=int,
        help='Server port number (default: 30030)'
    )
    
    # Thread pool configuration
    parser.add_argument(
        '--workers-per-core',
        type=float,
        default=None,
        help='Workers per CPU core (optional). If set, total workers = ceil(cores * factor). If omitted, default is heuristic ceil(cpu*1.5) (floor=8, cap=80).'
    )
    
    # Connection pool configuration
    parser.add_argument(
        '--disable-connection-pool',
        action='store_true',
        help='Disable persistent connection pool (not recommended - 15-100x slower)'
    )
    
    parser.add_argument(
        '--idle-timeout',
        type=int,
        default=300,
        help='Connection pool idle timeout in seconds (default: 300). Pool enabled by default'
    )
    
    parser.add_argument(
        '--health-check-interval',
        type=int,
        default=30,
        help='Connection pool health check interval in seconds (default: 30). Pool enabled by default'
    )

    
    # Parse the arguments
    args = parser.parse_args()
    global devices
    global connection_pool
    
    # Initialize thread pool.
    # Priority: JMCP_MAX_WORKERS env var (absolute) > --workers-per-core CLI arg > default heuristic (ceil(cpu*1.5), floor=8, cap=80)
    cpu_cores = os.cpu_count() or 4
    env_max_workers = os.getenv("JMCP_MAX_WORKERS")
    if env_max_workers is not None and env_max_workers.strip():
        if args.workers_per_core is not None:
            log.warning(
                "JMCP_MAX_WORKERS is set; ignoring --workers-per-core=%s",
                args.workers_per_core,
            )
        try:
            MAX_WORKERS = int(env_max_workers.strip())
        except ValueError as e:
            raise ValueError("JMCP_MAX_WORKERS must be an integer") from e
        if MAX_WORKERS <= 0:
            raise ValueError("JMCP_MAX_WORKERS must be > 0")
        WORKERS_PER_CORE = None
        log.info(f"Thread pool configured via JMCP_MAX_WORKERS absolute override: MAX_WORKERS={MAX_WORKERS}")
    else:
        WORKERS_PER_CORE = args.workers_per_core
        if args.workers_per_core is None:
            MAX_WORKERS = _default_max_workers()
            log.info(f"Thread pool defaulting to heuristic limit: MAX_WORKERS={MAX_WORKERS}")
        else:
            if args.workers_per_core <= 0:
                raise ValueError("--workers-per-core must be > 0")
            MAX_WORKERS = int(math.ceil(cpu_cores * args.workers_per_core))
            log.info(
                f"Thread pool configured: {cpu_cores} cores × {args.workers_per_core} workers/core = {MAX_WORKERS} total workers (ceil)"
            )
    
    thread_limiter = CapacityLimiter(MAX_WORKERS)
    
    # Check if authentication should be enabled
    auth_enabled = False
    if args.transport != 'stdio':
        # For non-stdio transports, check if we have tokens configured
        if os.path.exists(".tokens"):
            try:
                with open(".tokens", 'r') as f:
                    tokens = json.load(f)
                    if tokens:  # If tokens exist, enable auth
                        auth_enabled = True
                        log.info("Token-based authentication enabled")
                        log.info("Clients must send 'Authorization: Bearer <token>' header")
                        log.info("Use jmcp_token_manager.py to manage tokens")
                    else:
                        log.warning("Empty .tokens file found - server is open to all clients")
            except (json.JSONDecodeError, FileNotFoundError):
                log.warning("Invalid .tokens file - server is open to all clients")
        else:
            log.warning("No .tokens file found - server is open to all clients")
            log.info("Create tokens using: python jmcp_token_manager.py generate --id <token-id>")
    else:
        log.info("stdio transport - no authentication required")
    
    try:
        with open(args.device_mapping, 'r') as f:
            devices = json.load(f)
            # Validate all device configurations
            validate_all_devices(devices)
            log.info(f"Successfully loaded and validated {len(devices)} device(s)")
    except FileNotFoundError:
        print(f"File {args.device_mapping} not found.")
        devices = {}
        raise
    except json.JSONDecodeError:
        print(f"File {args.device_mapping} is not a valid JSON file.")
        devices = {}
        raise
    except ValueError as e:
        print(f"Device configuration validation failed: {e}")
        sys.exit(1)

    # Initialize global connection pool (enabled by default)
    connection_pool = JunosConnectionPool(
        devices_map=devices,
        prepare_connection_params_func=prepare_connection_params,
        max_idle_time=args.idle_timeout,
        health_check_interval=args.health_check_interval,
        enabled=not args.disable_connection_pool
    )

    # Set up signal handler for clean shutdown
    def signal_handler(sig, frame):
        print("\nShutting down MCP server...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Create MCP server
    mcp_server = create_mcp_server()

    # Run with the specified transport
    try:
        if args.transport == 'stdio':
            
            async def run_stdio():
                if connection_pool is not None:
                    await connection_pool.start()
                async with stdio_server() as (read_stream, write_stream):
                    try:
                        await mcp_server.run(
                            read_stream,
                            write_stream,
                            mcp_server.create_initialization_options()
                        )
                    finally:
                        if connection_pool is not None:
                            await connection_pool.stop()
            
            anyio.run(run_stdio)
        elif args.transport == 'streamable-http':
            # For streamable-http, create Starlette app with session manager
            async def run_streamable_http():
                session_manager = StreamableHTTPSessionManager(
                    app=mcp_server,
                    event_store=None,  # No persistence
                    stateless=True  # Stateless mode for VS Code compatibility
                )
                
                # ASGI handler
                async def handle_streamable_http(scope, receive, send):
                    await session_manager.handle_request(scope, receive, send)
                
                # Create middleware stack
                middleware = []
                if auth_enabled:
                    middleware.append(Middleware(BearerTokenMiddleware, auth_enabled=True))
                
                # Create Starlette app
                async def lifespan(app):
                    async with session_manager.run():
                        if connection_pool is not None:
                            await connection_pool.start()
                        try:
                            log.info(f"Streamable HTTP server started on http://{args.host}:{args.port}")
                            yield
                            log.info("Server shutting down...")
                        finally:
                            if connection_pool is not None:
                                await connection_pool.stop()
                
                starlette_app = Starlette(
                    routes=[Mount("/mcp", app=handle_streamable_http)],
                    middleware=middleware,
                    lifespan=lifespan
                )
                
                # Run with uvicorn
                import uvicorn
                config = uvicorn.Config(
                    starlette_app,
                    host=args.host,
                    port=args.port,
                    log_level="info"
                )
                server = uvicorn.Server(config)
                await server.serve()
            
            anyio.run(run_streamable_http)
        else:
            log.error(f"Unsupported transport: {args.transport}")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\nServer stopped by user")
        sys.exit(0)

if __name__ == '__main__':
    main()
