# JMCP Connection Pool Design

**Date:** January 22, 2026  
**Objective:** Implement persistent connection pooling in JMCP to eliminate SSH handshake overhead and connection exhaustion

## Architecture Overview

### Current (Inefficient) Model
```
Command 1: Open SSH → Authenticate → Execute → Close
Command 2: Open SSH → Authenticate → Execute → Close
Command 3: Open SSH → Authenticate → Execute → Close

Time per command: ~2-3 seconds (mostly handshake overhead)
```

### Proposed (Connection Pool) Model
```
First Command:  Open SSH → Authenticate → Execute → Keep Alive
Second Command: [Reuse existing] → Execute (instant)
Third Command:  [Reuse existing] → Execute (instant)

Time per command after first: ~0.2-0.5 seconds (only command execution)
```

## Key Components

### 1. Connection Pool Manager

**Class: `JunosConnectionPool`**
- **Purpose:** Maintain persistent PyEZ Device connections per router
- **Lifecycle:** Created on JMCP server startup, closed on shutdown
- **Thread-safe:** Multiple async tasks can request same connection

**Core Methods:**
```python
class JunosConnectionPool:
    async def start() -> None
    async def stop() -> None
    async def get_connection(router_name: str) -> Device
    async def release_connection(router_name: str) -> None
    async def invalidate_connection(router_name: str) -> None
    async def close_all() -> None
```

### 2. Connection States

```
DISCONNECTED → CONNECTING → CONNECTED → IDLE → STALE → DISCONNECTED
     ↑______________________________________________|
```

**States:**
- **DISCONNECTED:** No connection exists
- **CONNECTING:** Connection being established (protects against duplicate opens)
- **CONNECTED:** Active connection in use
- **IDLE:** Connection not in use but alive (can be reused)
- **STALE:** Connection lost (detected by health check), needs reconnect

### 3. Health Monitoring

**Background Task:** Runs every 30 seconds
```python
async def health_check_loop():
    while True:
        await asyncio.sleep(30)
        for router_name, conn in pool.items():
            if conn.idle_time > 300:  # 5 minutes idle
                await pool.close_connection(router_name)
            elif not conn.is_alive():  # Probe failed
                await pool.reconnect(router_name)
```

**Probe Command:** `show version | display xml | no-more` (fast, harmless)

### 4. Concurrency Control

**Problem:** Multiple tasks requesting same router connection
```python
Task 1: await pool.get_connection("crpd0")  # Opens connection
Task 2: await pool.get_connection("crpd0")  # Should wait or reuse?
```

**Solution:** Connection-level async locks
```python
class ConnectionWrapper:
    def __init__(self):
        self.device = None
        self.lock = asyncio.Lock()  # Serialize access
        self.in_use = False
        self.last_used = time.time()
```

## Implementation Details

### Connection Pool Storage

```python
class JunosConnectionPool:
    def __init__(self, devices_map: dict, prepare_connection_params_func: Callable[[dict, str], dict] | None = None):
        self.connections: Dict[str, ConnectionWrapper] = {}
        self.global_lock = asyncio.Lock()  # Protects pool dict
        self.health_check_task = None
        self.max_idle_time = 300  # 5 minutes
        self.health_check_interval = 30  # seconds
```

### Connection Acquisition Flow

```python
async def get_connection(self, router_name: str) -> Device:
    """Get or create connection for router"""
    
    # Step 1: Check if connection exists
    async with self.global_lock:
        if router_name not in self.connections:
            self.connections[router_name] = ConnectionWrapper(router_name)
        conn_wrapper = self.connections[router_name]
    
    # Step 2: Acquire connection-level lock (serialize per-router access)
    await conn_wrapper.lock.acquire()
    
    try:
        # Step 3: Check if device is connected
        if conn_wrapper.device is None or not conn_wrapper.is_connected():
            # Create new connection
            device_info = devices[router_name]
            connect_params = prepare_connection_params(device_info, router_name)
            
            conn_wrapper.device = Device(**connect_params)
            await anyio.to_thread.run_sync(conn_wrapper.device.open)
            log.info(f"Opened new connection to {router_name}")
        else:
            log.debug(f"Reusing existing connection to {router_name}")
        
        # Step 4: Mark as in-use and return device
        conn_wrapper.in_use = True
        conn_wrapper.last_used = time.time()
        return conn_wrapper.device
    
    except Exception as e:
        conn_wrapper.lock.release()
        raise
```

### Connection Release Flow

```python
async def release_connection(self, router_name: str) -> None:
    """Release connection back to pool"""
    
    async with self.global_lock:
        if router_name in self.connections:
            conn_wrapper = self.connections[router_name]
            conn_wrapper.in_use = False
            conn_wrapper.lock.release()
            log.debug(f"Released connection to {router_name}")
```

### Modified Command Execution

```python
# OLD: Create connection per command
def _run_junos_cli_command(router_name: str, command: str, timeout: int):
    with Device(**connect_params) as junos_device:
        return junos_device.cli(command)

# NEW: Use pooled connection
async def _run_junos_cli_command_pooled(router_name: str, command: str, timeout: int):
    device = await connection_pool.get_connection(router_name)
    try:
        # Execute in thread pool (PyEZ is synchronous)
        result = await anyio.to_thread.run_sync(
            lambda: device.cli(command, warning=False)
        )
        return result
    finally:
        await connection_pool.release_connection(router_name)
```

## Configuration Options

**Environment Variables:**
```bash
# Connection pool settings
JMCP_POOL_MAX_IDLE_TIME=300        # Close idle connections after 5 minutes
JMCP_POOL_HEALTH_CHECK_INTERVAL=30 # Check connection health every 30s
JMCP_POOL_ENABLED=true              # Enable/disable pooling
JMCP_POOL_MAX_RETRIES=3            # Reconnection attempts
```

**devices.json per-router settings:**
```json
{
  "crpd0": {
    "host": "77.42.30.134",
    "port": 2200,
    "username": "lab",
    "pool_enabled": true,        // Override: disable pooling for this router
    "pool_max_idle_time": 600    // Override: custom idle timeout
  }
}
```

## Error Handling

### Connection Lost During Command
```python
async def _run_junos_cli_command_pooled(router_name: str, command: str, timeout: int):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            device = await connection_pool.get_connection(router_name)
            result = await anyio.to_thread.run_sync(
                lambda: device.cli(command, warning=False)
            )
            await connection_pool.release_connection(router_name)
            return result
        
        except (RpcError, ConnectError) as e:
            log.warning(f"Connection lost to {router_name}: {e}")
            await connection_pool.invalidate_connection(router_name)
            
            if attempt == max_retries - 1:
                raise
            
            await asyncio.sleep(1)  # Brief delay before retry
```

### Health Check Failure
```python
async def health_check(self) -> None:
    """Check all connections and reconnect stale ones"""
    for router_name, conn_wrapper in list(self.connections.items()):
        if conn_wrapper.in_use:
            continue  # Skip connections in use
        
        try:
            # Quick probe
            if conn_wrapper.device and conn_wrapper.device.connected:
                result = await anyio.to_thread.run_sync(
                    lambda: conn_wrapper.device.cli("show version | no-more", warning=False)
                )
                log.debug(f"Health check passed for {router_name}")
            else:
                raise ConnectError("Device not connected")
        
        except Exception as e:
            log.warning(f"Health check failed for {router_name}: {e}")
            await self.close_connection(router_name)
```

## Migration Strategy

### Phase 1: Add Pool (Opt-in)
```python
# Add pool support but keep old path available
if POOL_ENABLED:
    result = await _run_junos_cli_command_pooled(router_name, command, timeout)
else:
    result = await anyio.to_thread.run_sync(_run_junos_cli_command, ...)
```

### Phase 2: Default Enable (Can Disable)
```python
# Pool enabled by default, opt-out via config
pool_enabled = get_config("JMCP_POOL_ENABLED", default=True)
```

### Phase 3: Remove Old Code Path
```python
# Delete _run_junos_cli_command, only use pooled version
```

## Performance Impact

### Before (No Pooling)
```
45 routers × 4 commands = 180 connections
Each connection: ~2 seconds overhead
Total overhead: 360 seconds (6 minutes)
Actual command time: ~90 seconds
Total time: 450 seconds (7.5 minutes)
```

### After (With Pooling)
```
45 routers × 4 commands = 45 connections (reused)
First command: ~2 seconds overhead per router
Subsequent commands: ~0 seconds overhead
Total overhead: 90 seconds (1.5 minutes)
Actual command time: ~90 seconds
Total time: 180 seconds (3 minutes)
```

**Improvement: 60% faster (7.5min → 3min)**

## Testing Strategy

### Unit Tests
```python
async def test_connection_reuse():
    pool = JunosConnectionPool(devices_map=devices_map)
    
    # First connection opens
    conn1 = await pool.get_connection("crpd0")
    await pool.release_connection("crpd0")
    
    # Second connection reuses
    conn2 = await pool.get_connection("crpd0")
    assert conn1 is conn2  # Same object
    
async def test_concurrent_access():
    pool = JunosConnectionPool(devices_map=devices_map)
    
    # Two tasks accessing same router
    async def task1():
        conn = await pool.get_connection("crpd0")
        await asyncio.sleep(0.1)
        await pool.release_connection("crpd0")
    
    async def task2():
        conn = await pool.get_connection("crpd0")
        await pool.release_connection("crpd0")
    
    # Should serialize, not fail
    await asyncio.gather(task1(), task2())
```

### Integration Tests
```python
async def test_batch_commands_with_pooling():
    # Execute 4 commands on 45 routers
    commands = [
        "show isis database",
        "show isis adjacency",
        "show ddos-protection protocols isis",
        "show isis statistics"
    ]
    
    for cmd in commands:
        result = await execute_junos_command_batch(
            router_names=all_routers,
            command=cmd
        )
        assert result["summary"]["successful"] == 45
```

## Security Considerations

1. **Credential Storage:** Connection objects hold authentication credentials in memory
   - Risk: Memory dump could expose passwords
   - Mitigation: Use SSH keys instead of passwords when possible

2. **Stale Sessions:** Long-lived connections could be hijacked
   - Risk: Session fixation if router compromised
   - Mitigation: Periodic health checks detect anomalies, max idle timeout

3. **Connection Limits:** Router may have max session limits
   - Risk: Pool holds connections, preventing other users
   - Mitigation: Close idle connections after 5 minutes

## Monitoring & Observability

**Metrics to Track:**
```python
class PoolMetrics:
    total_connections: int      # Currently open
    connection_hits: int        # Reused connections
    connection_misses: int      # New connections created
    health_check_failures: int
    reconnection_attempts: int
    average_connection_age: float
```

**Logging:**
```
[INFO] Connection pool initialized
[DEBUG] Reusing connection to crpd0 (age: 45s)
[INFO] Opened new connection to crpd1
[WARN] Health check failed for crpd2, reconnecting...
[INFO] Closed idle connection to crpd3 (idle: 301s)
```

## Rollback Plan

If connection pooling causes issues:

1. **Disable via environment variable:** `JMCP_POOL_ENABLED=false`
2. **Per-router disable:** Set `pool_enabled: false` in devices.json
3. **Graceful degradation:** Code falls back to old path automatically

## Future Enhancements

1. **Connection limits per router:** Max N concurrent commands per router
2. **Circuit breaker:** Temporarily disable router if too many failures
3. **Adaptive idle timeout:** Adjust based on usage patterns
4. **Pool statistics API:** Expose metrics via MCP tool
5. **Connection pre-warming:** Open connections to frequently-used routers on startup

## References

- PyEZ Documentation: https://www.juniper.net/documentation/us/en/software/junos-pyez/
- asyncio Locks: https://docs.python.org/3/library/asyncio-sync.html
- Connection Pool Patterns: https://en.wikipedia.org/wiki/Connection_pool
