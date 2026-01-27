# JMCP Connection Pool Implementation Guide

**Date:** January 22, 2026  
**Status:** Production Ready ✅  
**Version:** 1.1 (aligned to current code)

This guide documents the current API in `jmcp_connection_pool.py` and the device schema used by `devices.example.json`.

---

## Quick Start

### Basic usage (standalone)

```python
import json

from jmcp_connection_pool import JunosConnectionPool
from utils.config import prepare_connection_params

with open("devices.json", "r", encoding="utf-8") as f:
    devices_map = json.load(f)

pool = JunosConnectionPool(
    devices_map=devices_map,
    prepare_connection_params_func=prepare_connection_params,
    max_idle_time=300,
    health_check_interval=30,
    enabled=True,
)

await pool.start()

device = await pool.get_connection("router1")
try:
    # Note: PyEZ is blocking; keep it in a worker thread if you’re inside asyncio.
    output = device.cli("show version", warning=False)
finally:
    await pool.release_connection("router1")

await pool.stop()
```

---

## Prerequisites

- **Python**: 3.8+
- **PyEZ**: `junos-eznc`
- **Network**: NETCONF over SSH enabled and reachable from the JMCP host

Install:

```bash
pip install junos-eznc
```

---

## Device configuration schema

The pool expects a `devices_map` dict (see `devices.example.json`). Example:

```json
{
  "router1": {
    "ip": "192.0.2.10",
    "port": 22,
    "username": "netops",
    "auth": {
      "type": "ssh_key",
      "private_key_path": "~/.ssh/id_rsa"
    }
  }
}
```

If your `devices.json` uses a different schema, pass a custom `prepare_connection_params_func` that converts your device record into PyEZ `Device(**kwargs)` parameters.

---

## Configuration knobs

Constructor:

```python
JunosConnectionPool(
    devices_map: dict,
    prepare_connection_params_func: Callable[[dict, str], dict] | None = None,
    max_idle_time: int = 300,
    health_check_interval: int = 30,
    enabled: bool = True,

    # Backward-compatible aliases:
    idle_timeout: int | None = None,
    health_check: int | None = None,
)
```

Notes:
- Prefer `max_idle_time` / `health_check_interval` in new code.
- Logging is standard Python logging; configure handlers/levels in your app.

---

## Integration with the JMCP server

In this repository, the JMCP server already initializes and uses the pool:
- It builds the pool in `jmcp.py` at startup.
- CLI execution goes through `_run_cli_command()`, which acquires/releases pooled connections when enabled.

If you are integrating into another server, follow the standalone pattern above and ensure all PyEZ calls run in a worker thread (PyEZ is blocking).

---

## API reference

- `await pool.start()`
  - Starts background health checks (when `enabled=True`).
- `await pool.stop()`
  - Stops health checks and closes active connections.
- `device = await pool.get_connection(router_name)`
  - Returns a connected PyEZ `Device` for the router.
- `await pool.release_connection(router_name)`
  - Releases the per-router lock and marks the connection idle.
- `await pool.invalidate_connection(router_name)`
  - Marks a connection as bad so the next user forces a reconnect.
- `await pool.close_all()`
  - Closes all connections (alias-style helper).

---

## Best practices

- Always `release_connection()` in a `finally` block.
- Avoid long-held connections: do your CLI work, then release.
- Keep health checks enabled unless you are debugging.
- If you run high fan-out batches, also rate-limit your own concurrency upstream.

---

## Troubleshooting

- **Connect errors / auth failures**
  - Validate `devices.json` (or your `prepare_connection_params_func`).
  - Confirm SSH reachability and NETCONF is enabled.

- **Commands that work in SSH but fail here**
  - PyEZ runs commands via NETCONF; some CLI pipe constructs can behave differently.
  - Prefer server-side structured output (`format="json"` / `format="xml"`) when available.
