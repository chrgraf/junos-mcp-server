#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


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


def _post_sse(url: str, payload: dict, timeout: int) -> tuple[dict, dict[str, str]]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        headers = {k.lower(): v for k, v in resp.headers.items()}

    return _sse_extract_last_jsonrpc(raw), headers


def _tool_props(tools: list[dict], tool_name: str) -> set[str]:
    for tool in tools:
        if tool.get("name") != tool_name:
            continue
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        if isinstance(props, dict):
            return set(props.keys())
    return set()


def _tool_exists(tools: list[dict], tool_name: str) -> bool:
    return any((t.get("name") == tool_name) for t in tools)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="JMCP streamable-http SSE smoke test")
    ap.add_argument(
        "--base-url",
        default="http://127.0.0.1:30031/mcp/v1/sse",
        help="JMCP SSE endpoint URL (default: %(default)s)",
    )
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--artifact-label", default="copilot-regression-smoke")
    ap.add_argument("--artifact-backend", default="redis")
    ap.add_argument("--router", default="")
    ap.add_argument("--skip-cli", action="store_true")

    ns = ap.parse_args(argv)

    base = ns.base_url
    timeout = ns.timeout

    def post(payload: dict, timeout_override: int | None = None) -> tuple[dict, dict[str, str]]:
        return _post_sse(base, payload, timeout_override or timeout)

    t0 = time.time()

    # initialize (best-effort; some servers don't require sessions)
    try:
        msg, hdr = post(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "http_smoke_test", "version": "0"},
                    "capabilities": {},
                },
            },
            timeout_override=30,
        )
        if "error" in msg:
            raise RuntimeError(msg["error"])
        sid = hdr.get("mcp-session-id")
    except Exception as e:
        print(f"initialize: skipped ({e})")
        sid = None

    print(f"session_id={sid!r}")

    # list tools
    msg, _ = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout_override=30)
    if "error" in msg:
        raise RuntimeError(msg["error"])
    tools = (msg.get("result") or {}).get("tools") or []
    tool_names = sorted([t.get("name") for t in tools if t.get("name")])
    print(f"tools={len(tool_names)}")

    # router list
    msg, _ = post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_router_list", "arguments": {}},
        },
        timeout_override=30,
    )
    if "error" in msg:
        raise RuntimeError(msg["error"])
    text = (((msg.get("result") or {}).get("content") or [{}])[0].get("text")) or "{}"
    routers = json.loads(text)
    router_names = sorted(list(routers.keys()))
    print(f"routers={len(router_names)}")

    router = ns.router or (router_names[0] if router_names else "")
    if not router:
        raise RuntimeError("No routers found")
    print(f"selected_router={router}")

    # execute_junos_command_batch in artifact mode
    if ns.skip_cli:
        print("execute_junos_command_batch: skipped")
    elif not _tool_exists(tools, "execute_junos_command_batch"):
        print("execute_junos_command_batch: tool not exposed")
    else:
        supported = _tool_props(tools, "execute_junos_command_batch")
        args: dict = {"router_names": [router], "command": "show version"}

        # Add optional args only if schema supports them
        optional = {
            "timeout": 60,
            "format": "text",
            "response_mode": "artifact",
            "persist_to_disk": False,
            "persist_to_redis": True,
            "artifact_backend": ns.artifact_backend,
            "artifact_label": ns.artifact_label,
            "connection_mode": "pooled",
            "auto_fallback_to_fresh": True,
        }
        args.update({k: v for k, v in optional.items() if k in supported})

        msg, _ = post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "execute_junos_command_batch", "arguments": args},
            },
            timeout_override=180,
        )
        if "error" in msg:
            raise RuntimeError(msg["error"])
        out = (((msg.get("result") or {}).get("content") or [{}])[0].get("text")) or ""
        print(f"execute_junos_command_batch: ok (chars={len(out)})")

    # artifact tools (optional)
    if _tool_exists(tools, "list_artifacts"):
        supported = _tool_props(tools, "list_artifacts")
        list_args = {
            k: v
            for k, v in {
                "artifact_backend": ns.artifact_backend,
                "label_contains": ns.artifact_label,
                "limit": 5,
            }.items()
            if k in supported
        }

        msg, _ = post(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "list_artifacts", "arguments": list_args},
            },
            timeout_override=30,
        )
        if "error" in msg:
            raise RuntimeError(msg["error"])

        txt = (((msg.get("result") or {}).get("content") or [{}])[0].get("text")) or "[]"
        parsed = json.loads(txt)
        if isinstance(parsed, dict):
            artifacts = parsed.get("artifacts") or []
        elif isinstance(parsed, list):
            artifacts = parsed
        else:
            artifacts = []

        print(f"list_artifacts: ok (found={len(artifacts)})")

        if artifacts and _tool_exists(tools, "read_artifact"):
            supported = _tool_props(tools, "read_artifact")
            run_id = artifacts[0].get("run_id")
            if run_id and "run_id" in supported:
                read_args = {"run_id": run_id}
                if "artifact_backend" in supported:
                    read_args["artifact_backend"] = ns.artifact_backend
                if "mode" in supported:
                    read_args["mode"] = "summary"

                msg, _ = post(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "read_artifact", "arguments": read_args},
                    },
                    timeout_override=30,
                )
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                txt = (((msg.get("result") or {}).get("content") or [{}])[0].get("text")) or ""
                print(f"read_artifact: ok (chars={len(txt)})")
            else:
                print("read_artifact: skipped (no run_id)")
    else:
        print("artifact tools: not exposed")

    print(f"elapsed_s={time.time() - t0:.2f}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except urllib.error.URLError as e:
        print(f"HTTP error: {e}")
        raise
