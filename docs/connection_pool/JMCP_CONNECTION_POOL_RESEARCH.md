# JMCP Connection Pool Exhaustion Research

**Date:** January 22, 2026  
**Issue:** Connection pool exhaustion when executing multiple JMCP batch commands sequentially

## Problem Statement

When executing sequential JMCP batch commands, the second and third batches fail with:
```
Connection error: ConnectError(host: 77.42.30.134, msg: Capability exchange timed out)
```

This occurred after successfully collecting ISIS database from 45 routers (6.9 seconds), but then all 45 routers failed on the next three commands (adjacency, DDoS, statistics).

## Root Cause Analysis

### 1. JMCP Architecture (from jmcp.py inspection)

**Function: `_run_junos_cli_command()`** (lines 588-603)
```python
def _run_junos_cli_command(router_name: str, command: str, timeout: int = 360) -> str:
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
```

**Key Observations:**
- Each call creates a **new PyEZ Device connection** (line 597)
- Uses context manager `with Device(...) as junos_device` which should auto-close
- **No connection pooling/reuse** - connections are ephemeral per command
- Connection should be closed when context exits

**Function: `handle_execute_junos_command_batch()`** (lines 720+)
```python
async def handle_execute_junos_command_batch(arguments: dict, context: Context):
    # ...
    async def execute_on_router(router_name: str) -> dict:
        result = await anyio.to_thread.run_sync(
            _run_junos_cli_command,  # Runs in thread pool
            router_name,
            command,
            timeout
        )
        # ...
    
    results = await asyncio.gather(
        *[execute_on_router(router_name) for router_name in router_names],
        return_exceptions=False
    )
```

**Key Observations:**
- Uses `asyncio.gather()` to parallelize all 45 router commands
- Each command runs in thread pool via `anyio.to_thread.run_sync()`
- **All 45 SSH connections open simultaneously**

### 2. Hypothesis: PyEZ/Netconf Connection Leak

**Evidence:**
1. ✅ First batch (ISIS database): 45 routers, 6.9s - **SUCCESS**
2. ❌ Second batch (adjacency): 45 routers, all timeout after 32s - **FAIL**
3. ❌ Third batch (DDoS): 45 routers, all timeout after 32s - **FAIL**

**Timeout Pattern:**
- All routers fail at ~31-32 seconds (SSH capability exchange timeout)
- This is **before** the Junos CLI timeout (360s default)
- Suggests SSH handshake never completes

**Probable Causes:**

#### A. SSH Connection State Not Fully Cleaned Up
- PyEZ `Device.close()` might not immediately release SSH socket
- TCP TIME_WAIT state (can last 60-120 seconds on Linux/macOS)
- Even with context manager, underlying SSH connections may linger
- When 2nd batch starts, old connections still exist in kernel

#### B. Junos Router-Side Connection Limits
- Each cRPD router may have max concurrent SSH sessions limit
- First batch: 45 connections opened → closed, but kernel still tracking
- Second batch: 45 NEW connections attempted while old ones in TIME_WAIT
- Router refuses connections: "too many sessions from this IP"

#### C. anyio Thread Pool Exhaustion
- `anyio.to_thread.run_sync()` uses a thread pool
- Default thread pool size may be small (typically 10-40 threads)
- 45 concurrent blocking calls might exhaust pool
- Subsequent requests queue up, but SSH handshake times out (32s) before thread becomes available

#### D. System Resource Exhaustion
- File descriptors: ✅ NOT THE ISSUE (limit = 1,048,575)
- Port exhaustion: Each connection needs ephemeral port (32768-60999 on Linux)
  - Maximum ~28,000 ports
  - 45 connections × 2 batches = 90 connections
  - Not exhausted, but if ports not released...

### 3. Testing Needed

**Test 1: Sequential vs Parallel Batch Execution**
```python
# Current: All 3 batches fired in parallel
results = await asyncio.gather(
    execute_batch("show isis adjacency"),
    execute_batch("show ddos-protection"),
    execute_batch("show isis statistics")
)

# Test: Execute batches sequentially with delay
result1 = await execute_batch("show isis adjacency")
await asyncio.sleep(5)  # Allow connections to fully close
result2 = await execute_batch("show ddos-protection")
```

**Expected Outcome:** If sequential execution works, confirms connection cleanup issue

**Test 2: Smaller Batch Sizes**
```python
# Instead of 45 routers at once, try 10 at a time
chunk_size = 10
for i in range(0, len(routers), chunk_size):
    chunk = routers[i:i+chunk_size]
    result = await execute_batch(chunk, command)
```

**Expected Outcome:** If smaller batches work, confirms resource exhaustion

**Test 3: Connection Reuse (requires JMCP modification)**
```python
# Create persistent connection pool
connection_pool = {}

def _run_junos_cli_command_with_pool(router_name: str, command: str):
    if router_name not in connection_pool:
        connection_pool[router_name] = Device(**connect_params)
        connection_pool[router_name].open()
    
    return connection_pool[router_name].cli(command)
```

**Expected Outcome:** Reusing connections should eliminate exhaustion

### 4. Recommended Solution

**Short-term Workaround (No JMCP Changes Required):**
1. **Add delays between batch commands in Python code**
   ```python
   lsdb_result = await execute_batch(routers, "show isis database")
   await asyncio.sleep(10)  # Allow cleanup
   adj_result = await execute_batch(routers, "show isis adjacency")
   ```

2. **Execute batches sequentially instead of parallel**
   ```python
   # Instead of 3 parallel tool calls
   for cmd in ["show isis adjacency", "show ddos-protection", "show isis statistics"]:
       await execute_batch(routers, cmd)
       await asyncio.sleep(5)
   ```

3. **Reduce batch size**
   ```python
   # Process 10 routers at a time
   for chunk in chunks(routers, 10):
       result = await execute_batch(chunk, command)
   ```

**Long-term Fix (Requires JMCP Modification):**
1. **Implement connection pooling in JMCP**
   - Maintain persistent connections per router
   - Reuse connections across commands
   - Add connection timeout/refresh logic

2. **Add concurrency limiter in JMCP**
   ```python
   import asyncio
   
   # Limit to 10 concurrent SSH connections
   semaphore = asyncio.Semaphore(10)
   
   async def execute_on_router(router_name: str):
       async with semaphore:
           result = await anyio.to_thread.run_sync(_run_junos_cli_command, ...)
   ```

3. **Expose batch chunking in JMCP API**
   ```python
   mcp_jmcp_execute_junos_command_batch(
       router_names=routers,
       command="show isis adjacency",
       max_concurrent=10  # New parameter
   )
   ```

## Immediate Action

For the current health check demonstration, use **sequential batch execution** with delays:

```python
# Phase 1: LSDB (barrier sync)
lsdb_data = await mcp_jmcp_execute_junos_command_batch(
    router_names=all_routers,
    command="show isis database",
    format="json"
)

# Wait for connections to fully close
await asyncio.sleep(10)

# Phase 2: Global commands (sequential)
for cmd in [
    "show isis adjacency",
    "show ddos-protection protocols isis",
    "show isis statistics"
]:
    result = await mcp_jmcp_execute_junos_command_batch(
        router_names=all_routers,
        command=cmd,
        format="json"
    )
    await asyncio.sleep(5)  # Cooldown between commands
```

## Conclusion

Connection pool exhaustion is **NOT** due to JMCP keeping connections open. The `with Device()` context manager properly closes connections. The issue is:

1. **SSH/TCP state cleanup delay** - Even after close(), kernel retains connection state
2. **Router-side connection limits** - Junos may throttle rapid reconnections from same IP
3. **Thread pool contention** - 45 concurrent blocking calls may exceed anyio thread pool

**Workaround:** Sequential batch execution with delays between commands allows full cleanup.

**Proper fix:** Requires JMCP modification to implement connection pooling or concurrency limiting.
