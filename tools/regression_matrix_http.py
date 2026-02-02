#!/usr/bin/env python3

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _pick_free_port() -> int:
    # Bind to port 0 to let OS pick a free port, then close.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _sse_extract_last_jsonrpc(raw: str) -> dict:
    lines = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ")]
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            return obj
    raise RuntimeError("No JSON-RPC result/error found in SSE response")


def _post_sse(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return _sse_extract_last_jsonrpc(raw)


def _tool_props(tools: list[dict], tool_name: str) -> set[str]:
    for tool in tools:
        if tool.get("name") != tool_name:
            continue
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        if isinstance(props, dict):
            return set(props.keys())
    return set()


def _text_from_tool_result(msg: dict) -> str:
    # JMCP uses MCP JSON-RPC content list with text payload.
    result = msg.get("result") or {}
    content = result.get("content") or []
    if not content:
        return ""
    item0 = content[0]
    if isinstance(item0, dict):
        return item0.get("text") or ""
    return ""


@dataclass(frozen=True)
class Case:
    tool: str
    name: str
    args: dict[str, Any]


def _build_cases(
    tools: list[dict],
    router_names: list[str],
    *,
    max_routers: int,
    max_cases: int,
    include_barrier: bool,
    include_artifact_backends: bool,
) -> list[Case]:
    supported_tools = {t.get("name") for t in tools if t.get("name")}

    selected = router_names[: max(1, max_routers)]
    # Make some negative cases by adding a clearly invalid router name.
    negative_router = f"__nonexistent_router_{random.randint(1000,9999)}__"

    cases: list[Case] = []

    def add(tool: str, name: str, args: dict[str, Any]) -> None:
        if tool not in supported_tools:
            return
        props = _tool_props(tools, tool)
        filtered = {k: v for k, v in args.items() if k in props}
        cases.append(Case(tool=tool, name=name, args=filtered))

    # Common permutations
    connection_modes = ["pooled", "fresh"]
    auto_fallback_vals = [None, False, True]
    response_modes = ["summary", "full", "artifact"]
    artifact_backends = ["disk", "redis", "dual"] if include_artifact_backends else ["disk"]

    # 1) execute_junos_command_batch
    base_args_single = {
        "router_names": selected,
        "command": "show version",
        "timeout": 60,
        "format": "text",
    }

    for connection_mode, auto_fallback, response_mode in itertools.product(
        connection_modes, auto_fallback_vals, response_modes
    ):
        args = dict(base_args_single)
        args["connection_mode"] = connection_mode
        if auto_fallback is not None:
            args["auto_fallback_to_fresh"] = auto_fallback
        args["response_mode"] = response_mode
        if response_mode == "artifact":
            for backend in artifact_backends:
                a2 = dict(args)
                a2["artifact_backend"] = backend
                a2["artifact_label"] = f"reg-matrix-{_utc_stamp()}"
                a2["persist_to_disk"] = backend in ("disk", "dual")
                a2["persist_to_redis"] = backend in ("redis", "dual")
                add(
                    "execute_junos_command_batch",
                    f"cmd_batch/{connection_mode}/fallback={auto_fallback}/artifact/{backend}",
                    a2,
                )
        else:
            add(
                "execute_junos_command_batch",
                f"cmd_batch/{connection_mode}/fallback={auto_fallback}/{response_mode}",
                args,
            )

    # 2) execute_junos_commands_batch
    base_args_multi = {
        "router_names": selected,
        "commands": ["show version", "show system uptime"],
        "timeout": 60,
        "format": "text",
    }

    for connection_mode, auto_fallback, response_mode in itertools.product(
        connection_modes, auto_fallback_vals, response_modes
    ):
        args = dict(base_args_multi)
        args["connection_mode"] = connection_mode
        if auto_fallback is not None:
            args["auto_fallback_to_fresh"] = auto_fallback
        args["response_mode"] = response_mode
        if response_mode == "artifact":
            for backend in artifact_backends:
                a2 = dict(args)
                a2["artifact_backend"] = backend
                a2["artifact_label"] = f"reg-matrix-{_utc_stamp()}"
                a2["persist_to_disk"] = backend in ("disk", "dual")
                a2["persist_to_redis"] = backend in ("redis", "dual")
                add(
                    "execute_junos_commands_batch",
                    f"cmds_batch/{connection_mode}/fallback={auto_fallback}/artifact/{backend}",
                    a2,
                )
        else:
            add(
                "execute_junos_commands_batch",
                f"cmds_batch/{connection_mode}/fallback={auto_fallback}/{response_mode}",
                args,
            )

    # 3) Barrier-sync (only makes sense for pooled mode)
    if include_barrier:
        barrier_args = {
            "router_names": selected,
            "command": "show system uptime",
            "timeout": 60,
            "format": "text",
            "connection_mode": "pooled",
            "barrier_sync": True,
            "barrier_policy": "proceed",
            "preconnect_timeout": 20,
            "preconnect_retries": 1,
            "preconnect_backoff_seconds": 1,
            "response_mode": "summary",
        }
        add("execute_junos_command_batch", "cmd_batch/barrier_sync/proceed", barrier_args)

        barrier_args_strict = dict(barrier_args)
        barrier_args_strict["barrier_policy"] = "strict"
        add("execute_junos_command_batch", "cmd_batch/barrier_sync/strict", barrier_args_strict)

    # 4) Negative cases (validation + failure categorization)
    add(
        "execute_junos_command_batch",
        "cmd_batch/negative/unknown_router",
        {
            "router_names": [negative_router],
            "command": "show version",
            "timeout": 20,
            "response_mode": "summary",
            "connection_mode": "fresh",
        },
    )
    add(
        "execute_junos_commands_batch",
        "cmds_batch/negative/unknown_router",
        {
            "router_names": [negative_router],
            "commands": ["show version"],
            "timeout": 20,
            "response_mode": "summary",
            "connection_mode": "fresh",
        },
    )

    # Trim to max_cases but keep variety by shuffling first
    random.shuffle(cases)
    return cases[: max_cases]


def _wait_ready(url: str, timeout_s: int) -> list[dict]:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            msg = _post_sse(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=10)
            if "result" in msg:
                tools = (msg.get("result") or {}).get("tools") or []
                if isinstance(tools, list):
                    return tools
        except Exception as e:
            last_err = e
        time.sleep(0.25)
    raise RuntimeError(f"Server not ready within {timeout_s}s: {last_err}")


def _get_router_list(url: str) -> list[str]:
    msg = _post_sse(
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_router_list", "arguments": {}}},
        timeout=30,
    )
    if "error" in msg:
        raise RuntimeError(msg["error"])
    text = _text_from_tool_result(msg)
    routers = json.loads(text) if text else {}
    return sorted(list(routers.keys()))


def _maybe_read_artifact(
    url: str,
    tools: list[dict],
    text: str,
    *,
    artifact_backend_hint: str | None,
) -> tuple[str | None, dict | None]:
    # Best-effort: pull run_id from summary JSON text.
    # The server may return a human-readable summary; try to parse JSON first.
    run_id: str | None = None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            run_id = parsed.get("run_id") or (parsed.get("artifact") or {}).get("run_id")
    except Exception:
        pass

    if not run_id:
        # fallback: look for a token like 20260127T180529Z-7f5a55c7
        import re

        m = re.search(r"\b\d{8}T\d{6}Z-[0-9a-f]{8}\b", text)
        if m:
            run_id = m.group(0)

    tool_names = {t.get("name") for t in tools if t.get("name")}
    if not run_id or "read_artifact" not in tool_names:
        return run_id, None

    props = _tool_props(tools, "read_artifact")
    args: dict[str, Any] = {"run_id": run_id}
    if "mode" in props:
        args["mode"] = "summary"
    if "artifact_backend" in props:
        # Prefer the backend used by the case when provided.
        if artifact_backend_hint:
            args["artifact_backend"] = artifact_backend_hint

    msg = _post_sse(
        url,
        {"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "read_artifact", "arguments": args}},
        timeout=30,
    )
    if "error" in msg:
        return run_id, {"error": msg["error"]}

    summary_text = _text_from_tool_result(msg)
    try:
        return run_id, json.loads(summary_text)
    except Exception:
        return run_id, {"raw": summary_text}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Run a JMCP HTTP SSE regression matrix (many permutations).")
    ap.add_argument("--python", default=sys.executable, help="Python interpreter to use")
    ap.add_argument("--jmcp", default="jmcp.py", help="Path to jmcp.py")
    ap.add_argument("--devices", default="devices.json", help="Path to devices.json")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="Port for temporary server (0=auto)")
    ap.add_argument("--startup-timeout", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=180)

    ap.add_argument(
        "--default-artifact-backend",
        default="redis",
        choices=["disk", "redis", "dual"],
        help="Default backend for the temporary server (can be overridden by environment)",
    )
    ap.add_argument("--redis-host", default="127.0.0.1")
    ap.add_argument("--redis-port", default="6379")
    ap.add_argument("--redis-db", default="0")

    ap.add_argument("--max-routers", type=int, default=2, help="Routers per case (keep small for stability)")
    ap.add_argument("--max-cases", type=int, default=30, help="Maximum number of cases to run")
    ap.add_argument("--include-barrier", action="store_true")
    ap.add_argument("--include-artifact-backends", action="store_true")

    ap.add_argument(
        "--report",
        default="",
        help="Write JSON report to this path (default: artifacts/regression-matrix-<ts>.json)",
    )

    ns = ap.parse_args(argv)

    port = ns.port or _pick_free_port()
    base_url = f"http://{ns.host}:{port}/mcp/v1/sse"

    env = dict(os.environ)
    # Provide sensible defaults, but allow caller to override via environment.
    env.setdefault("JMCP_BATCH_RESPONSE_MODE", "artifact")
    env.setdefault("JMCP_ARTIFACT_BACKEND", ns.default_artifact_backend)

    # If Redis is involved, default its connection settings unless user already provided them.
    if env.get("JMCP_ARTIFACT_BACKEND") in ("redis", "dual"):
        env.setdefault("JMCP_ARTIFACT_REDIS_HOST", ns.redis_host)
        env.setdefault("JMCP_ARTIFACT_REDIS_PORT", str(ns.redis_port))
        env.setdefault("JMCP_ARTIFACT_REDIS_DB", str(ns.redis_db))

    cmd = [
        ns.python,
        ns.jmcp,
        "-t",
        "streamable-http",
        "-H",
        ns.host,
        "-p",
        str(port),
        "-f",
        ns.devices,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=os.getcwd(),
    )

    started = time.time()
    report: dict[str, Any] = {
        "started_utc": _utc_stamp(),
        "base_url": base_url,
        "server_cmd": cmd,
        "env_effective": {
            "JMCP_BATCH_RESPONSE_MODE": env.get("JMCP_BATCH_RESPONSE_MODE"),
            "JMCP_ARTIFACT_BACKEND": env.get("JMCP_ARTIFACT_BACKEND"),
            "JMCP_ARTIFACT_REDIS_HOST": env.get("JMCP_ARTIFACT_REDIS_HOST"),
            "JMCP_ARTIFACT_REDIS_PORT": env.get("JMCP_ARTIFACT_REDIS_PORT"),
            "JMCP_ARTIFACT_REDIS_DB": env.get("JMCP_ARTIFACT_REDIS_DB"),
        },
        "cases": [],
    }

    try:
        tools = _wait_ready(base_url, ns.startup_timeout)
        routers = _get_router_list(base_url)

        report["tool_count"] = len(tools)
        report["tools"] = sorted([t.get("name") for t in tools if t.get("name")])
        report["router_count"] = len(routers)

        cases = _build_cases(
            tools,
            routers,
            max_routers=ns.max_routers,
            max_cases=ns.max_cases,
            include_barrier=ns.include_barrier,
            include_artifact_backends=ns.include_artifact_backends,
        )

        ok = 0
        failed = 0

        for idx, case in enumerate(cases, start=1):
            t0 = time.time()
            row: dict[str, Any] = {
                "i": idx,
                "tool": case.tool,
                "name": case.name,
                "args": case.args,
            }
            try:
                msg = _post_sse(
                    base_url,
                    {"jsonrpc": "2.0", "id": idx + 10, "method": "tools/call", "params": {"name": case.tool, "arguments": case.args}},
                    timeout=ns.timeout,
                )
                if "error" in msg:
                    raise RuntimeError(msg["error"])

                text = _text_from_tool_result(msg)
                row["result_chars"] = len(text)

                # Best-effort artifact follow-up for artifact mode.
                if case.args.get("response_mode") == "artifact":
                    run_id, artifact_summary = _maybe_read_artifact(
                        base_url,
                        tools,
                        text,
                        artifact_backend_hint=case.args.get("artifact_backend"),
                    )
                    row["run_id"] = run_id
                    if artifact_summary is not None:
                        row["artifact_summary"] = artifact_summary

                row["status"] = "ok"
                ok += 1
            except Exception as e:
                row["status"] = "error"
                row["error"] = str(e)
                failed += 1
            finally:
                row["duration_s"] = round(time.time() - t0, 3)
                report["cases"].append(row)

        report["summary"] = {
            "ok": ok,
            "failed": failed,
            "total": ok + failed,
            "elapsed_s": round(time.time() - started, 3),
        }

    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    report_path = ns.report
    if not report_path:
        os.makedirs("artifacts", exist_ok=True)
        report_path = os.path.join("artifacts", f"regression-matrix-{_utc_stamp()}.json")

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=False)

    print(f"JMCP regression matrix complete: ok={report['summary']['ok']} failed={report['summary']['failed']} total={report['summary']['total']}")
    print(f"Report: {report_path}")

    return 0 if report.get("summary", {}).get("failed") == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
