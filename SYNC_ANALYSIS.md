# Fork Sync Analysis - Junos MCP Server

Date: 2026-04-16

## Mainline Repository
- URL: https://github.com/Juniper/junos-mcp-server
- Branch: main

## Your Fork Enhancements (TO PRESERVE)

### 1. Connection Pooling System
- **File**: `jmcp_connection_pool.py`
- **Features**:
  - Persistent connections with idle timeout
  - Health check monitoring
  - Automatic reconnection on transport errors
  - Command-per-connection limits

### 2. Artifact Storage System
- **File**: `artifact_store.py`
- **Features**:
  - Disk-based artifact storage
  - Redis-based artifact storage
  - Dual-mode (disk + Redis)
  - Compression support
  - TTL management
  - Tools: `read_artifact`, `list_artifacts`

### 3. Worker Pool Management
- **Features**:
  - CapacityLimiter for concurrency control
  - `--workers-per-core` CLI argument
  - `JMCP_MAX_WORKERS` environment variable
  - Automatic CPU-based sizing

### 4. Enhanced Batch Operations
- **Tool**: `execute_junos_commands_batch` (fork-specific)
  - Multiple commands on multiple routers
  - Connection mode: pooled vs fresh
  - Auto-fallback to fresh on transport errors
  - Barrier sync (preconnect validation)
  
- **Tool**: `execute_junos_command_batch` (enhanced)
  - Added connection pooling support
  - Added barrier sync
  - Added retry/healing statistics
  - Added artifact response mode

### 5. Configuration & Command Blocklisting
- **Functions**: `check_config_blocklist()`, `check_command_blocklist()`
- **Files**: `block.cfg`, `block.cmd`
- **Features**:
  - Regex-based pattern matching
  - Token-based matching for configs
  - Prefix matching for commands

### 6. Server Settings Tool
- **Tool**: `get_server_settings`
- **Features**: Returns worker config, artifact config, process info

### 7. Enhanced CLI Command Runner
- **Function**: `_run_cli_command_with_meta()`
- **Features**:
  - Returns metadata (attempts, connection mode, timing)
  - Auto-fallback to fresh connections
  - Connection mode selection (pooled/fresh)
  - Failure classification

### 8. Response Mode System
- **Environment**: `JMCP_BATCH_RESPONSE_MODE`
- **Modes**: full, summary, artifact
- **Features**: Token-efficient large result handling

### 9. Barrier Sync System
- **Features**:
  - Preconnect validation before command execution
  - Proceed vs strict policy
  - Configurable retries and backoff
  - Per-router preconnect status

## Mainline Features (TO INTEGRATE)

### 1. New Tools
- **`execute_pfe_command`**: PFE command execution (EXISTS in fork already!)
- **`reload_devices`**: Reload device config from JSON (EXISTS in fork already!)

### 2. Stateless Mode Support
- **Function**: `get_stateless_with_fallback()`
- **Environment**: `JMCP_STATELESS`
- **Feature**: Stateless HTTP session mode for StreamableHTTPSessionManager

### 3. Enhanced Validation Functions
- **Already in fork via utils/config.py**

### 4. Version Bump
- **Mainline**: 1.1.0
- **Fork**: 1.0.0

## Differences to Reconcile

### 1. Version Number
- **Action**: Update fork to 1.1.0 to match mainline
- **Risk**: None

### 2. Tool Availability
- **Status**: Fork has ALL mainline tools PLUS extras
- **Action**: None needed

### 3. Stateless Mode
- **Status**: Mainline has stateless HTTP session support
- **Action**: Integrate `get_stateless_with_fallback()` and use it in streamable-http setup
- **Risk**: Low - purely additive

### 4. Code Structure
- **Mainline**: Simpler, no pooling or artifacts
- **Fork**: More complex with pooling and artifacts
- **Action**: Keep fork structure, add mainline's stateless feature

### 5. Connection Handling
- **Mainline**: Always creates fresh connections (via `_run_junos_cli_command`)
- **Fork**: Uses pooling by default (via `_run_cli_command` + `_run_cli_command_with_meta`)
- **Action**: Keep fork's approach - it's superior

## Sync Plan

### Phase 1: Non-Breaking Additions (SAFE)
1. ✅ Add `get_stateless_with_fallback()` function
2. ✅ Update StreamableHTTPSessionManager to use stateless mode
3. ✅ Update version to 1.1.0
4. ✅ Review and align copyright headers

### Phase 2: Testing (VALIDATION)
1. Test with stdio transport
2. Test with streamable-http transport (stateless=false)
3. Test with streamable-http transport (stateless=true)
4. Test connection pooling still works
5. Test artifact storage still works

### Phase 3: Documentation
1. Update README with stateless mode docs
2. Document fork-specific features vs mainline

## Risk Assessment

### Low Risk (Additive Changes Only)
- ✅ Adding stateless mode support
- ✅ Version bump
- ✅ Copyright header alignment

### No Risk (Already in Fork)
- execute_pfe_command
- reload_devices
- All other mainline tools

### High Value (Fork Features to Preserve)
- Connection pooling (15-100x performance improvement)
- Artifact storage (massive token savings)
- Worker pool management
- Barrier sync
- Enhanced error handling
- Blocklisting

## Recommendation

**MINIMAL SYNC APPROACH**: Only integrate the stateless mode feature from mainline, as:
1. Fork has ALL mainline tools already
2. Fork has significant performance/feature enhancements
3. Mainline lacks critical enterprise features (pooling, artifacts, blocklisting)
4. Risk is minimized by only adding stateless mode support

This approach preserves all your valuable enhancements while staying current with mainline's minor improvements.
