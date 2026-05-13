# JMCP Enhanced_CG - beta

Dear all,

This JMCP is based on [junos-mcp-server](https://github.com/Juniper/junos-mcp-server).
It is strongly encouraged to read the security aspects of the upstream project in the [README](https://github.com/Juniper/junos-mcp-server/blob/main/README.md).

**Why**
- Improves scale and throughput via configurable workers (good defaults + easy tuning).
- Extends upstream batch execution: adds multi-command batching (`execute_junos_commands_batch`) and enhances `execute_junos_command_batch` for large-scale runs.
- Enables connection pooling (default) to reuse 1 persistent session per router (optional per-call override via `connection_mode="fresh"`, plus `auto_fallback_to_fresh` for self-healing stale sessions).
- Supports barrier-synced execution (`barrier_sync`) for a near-simultaneous snapshot across routers (useful for LSDB/ISIS troubleshooting).
- Adds a first-class `format=text|json|xml` knob for more reliable structured output than CLI pipes.
- Adds token-safe `response_mode=artifact`: JMCP stores full results out-of-band (disk / Redis / dual) and returns a small summary + pointer (`run_id`).
- Includes artifact inspection tools (`list_artifacts`, `read_artifact`) with token-safe views (`mode=failures|diff|full`) and chunked reads (`router_offset/router_limit`).

External Python remains best for maximum performance and custom post-processing, but enhanced JMCP can reduce the need for extra scripts in many operational scenarios.

**Delta beyond upstream `junos-mcp-server`**

If you already use the upstream repo and want to cherry-pick only what this fork adds/changes, these are the *extra or replaced* files to bring over:
- jmcp.py (enhanced server; replaces upstream jmcp.py)
- jmcp_connection_pool.py (connection pooling + health checks)
- artifact_store.py (artifact persistence; required for `response_mode=artifact` and for the `list_artifacts` / `read_artifact` tools)
- regression_selftest/ (optional: HTTP/SSE smoke + regression matrix scripts)

Notes on what changed vs upstream behavior:
- Upstream already includes `execute_junos_command_batch`; this fork primarily enhances batch execution with pooling/barrier controls, token-safe response modes, and artifact inspection.
- This fork adds `execute_junos_commands_batch` (N routers × M commands) and keeps per-call compatibility knobs like `connection_mode="fresh"` alongside pooled defaults.

Optional helper:
- merge_devices_json.py (only needed if you want to merge multiple device inventories)

**Still required from upstream (in addition)**

If you copy just the files above into an upstream checkout, you still need the upstream project layout and dependencies, especially:
- utils/config.py (device parsing/validation helpers imported by jmcp.py)
- Your device inventory file (e.g. devices.json) in the upstream format
- Python deps from upstream; plus `redis>=5` if you use `artifact_backend=redis|dual` (and a running Redis)

## overall advantages
The bullets in **Why** are the short version; this section adds a bit more “why it matters” context.

- Runs much larger fleets faster via configurable workers + batching.
- Avoids reconnect-per-command overhead via connection pooling (default; override per call via `connection_mode="fresh"`).
- Supports a preconnect barrier (`barrier_sync`) for near-simultaneous snapshots.
- Protects the LLM context window via artifacts + token-safe inspection (`response_mode=artifact`, `read_artifact`).

Artifacts are especially useful when a single large batch (e.g., `show isis database` on 250 routers) would flood the LLM context window. JMCP can collect data once in parallel, persist the full payload out-of-band, and then you can pull back only what you need in smaller chunks.

### ASCII overview: JMCP as data-broker (artifacts)

This diagram shows what gets stored (artifact payloads) vs what gets sent back to the LLM.

```
                (1) tool call (batch / multi-command)

   +--------------------+      MCP request      +---------------------+      SSH/NETCONF      +------------------+
   | LLM / MCP client   |  ------------------>  | JMCP (data broker)  |  ------------------>  | Junos routers     |
   | (ChatGPT, etc.)    |  <------------------  | + connection pool   |  <------------------  | (crpd*, mx*, ...) |
   +--------------------+      MCP response     | + workers           |                       +------------------+
                                                 | + batching tools    |
                                                 | + response_mode     |
                                                 | + format=json/xml   |
                                                 | + barrier_sync      |
                                                 +----------+----------+
                                                            |
                                                            | (2) response_mode=artifact stores full payloads
                                                            v
                                         +----------------------------------------------+
                                         | Artifact storage (out-of-band)               |
                                         | - Redis (run_id -> JSON blob)                |
                                         | - disk (artifacts/*.json)                    |
                                         | - dual (Redis + disk)                        |
                                         +----------------------------------------------+


What JMCP sends back to the LLM (same router execution, different shapes):

  response_mode=full
    -> summary + per-router outputs inline (largest, token-heavy)

  response_mode=summary
    -> only summary/metadata (smallest, but no data to analyze)

  response_mode=artifact
    -> summary + pointer (run_id)  [full payload is stored in Redis/disk]


Token-safe “collect once, inspect subsets later” loop:

  LLM calls execute_* with response_mode=artifact
    -> gets run_id
  LLM calls read_artifact(run_id, mode=diff|failures|full, router_names/offset/limit)
    -> fetches only the slice/representation needed for analysis
```


## Usage and understanding of the knobs
This enhanced MCP has a couple of knobs, e.g. **artifact_backend** and **response_mode**

The `artifact_backend` can be configured to disk, redis, or both. By default it is set to disk. Whether data is persisted depends on the `response_mode` knob.

## response_mode
**response_mode=full**
That's the same behavior as in today's junos-mcp-server: for any query, JMCP returns the full output to the LLM.

**response_mode=summary**
Only a brief summary (how many failures, successes, time taken) is signaled back to the LLM. Might be good for testing and prototyping, and makes most sense if it is known up front that the retrieved data might trash the context window. Summary is the default mode.

**response_mode=artifact**
Triggers sending summary-report and JMCP persists the full payload to the configured artifact backend and returns only a summary + `run_id`.


## Examples

**response_mode summary**
Prompt: `get the version from all routers crpd1 and crpd2. tool execute_junos_command_batch`
This is today's default MCP behavior.

**response_mode artifact**
Now let's persist out-of-band and protect the LLM context window from being overwhelmed by 250 routers sending their `show isis database` output.
Prompt: `get isis database from all routers starting with crpd. tool execute_junos_command_batch. response_mode = artifact. artifact_backend = redis`
This is the summary sent back by the JMCP:
```
Executed execute_junos_command_batch for show isis database on all crpd* routers with response_mode=artifact and artifact_backend=redis.

Summary: total_routers=48, successful=48, failed=0, duration=3.319s
New Redis artifact run_id: 20260127T180529Z-7f5a55c7
```

As a next step, two options exist: JMCP can send full data to the LLM (which consumes context window), or it can send a compact diff/grouping view. For some use cases the author observed a factor of 13 in token savings when using the diff. But let's start by retrieving full data from the JMCP:

Example 1 - retrieve full CLI output
```markdown
“Using JMCP, we will analyze ISIS LSDB sync token-safely. retrieve most recent run_id

**Rules:**

Always read data in chunks of 20 routers using read_artifact(mode="full").
Do NOT keep raw outputs in memory; immediately reduce each chunk to a compact summary.
Maintain a single rolling report called LSDB_SYNC_REPORT with only:
baseline LSP inventory (LSP-ID → seq/checksum if present)
per-LSP min/max lifetime/age across routers (track min_router/max_router)
routers flagged as outliers (missing LSPs, seq mismatch, lifetime outside threshold)
After each chunk, output only:
updated LSDB_SYNC_REPORT (compact)
next read_artifact call parameters.
Start now:
Call read_artifact with:

use tool list_artifacts  to get  most actual run_id
mode="full"
router_offset=0
router_limit=20
artifact_backend="redis"
Then parse each router’s show isis database text and update LSDB_SYNC_REPORT.
Heuristics:

Pick baseline as the first router in the first chunk.
LSDB sync OK if: LSP set identical and sequence numbers identical (ignore volatile columns).
Lifetime check: flag if lifetime differs by more than 120s from the fleet median for that LSP (or if lifetime < 300s).
Proceed until all routers are processed.”
```

Example 2 - getting just a diff back from the JMCP, not the full output

Massive token savings can be achieved when JMCP returns only a diff/grouping view, instead of the full per-router output. This works especially well for outputs like `show isis database`.

```markdown
Using JMCP, we will analyze ISIS LSDB sync token-safely (diff-first). Retrieve the most recent run_id and then request a diff view.

Rules:

- Prefer `read_artifact(mode="diff")` first.
- Build a compact LSDB_SYNC_REPORT from grouping + diffs (do not store raw outputs).
- Treat LSDB sync OK if LSP set is identical and sequence/checksum are identical (ignore volatile columns like Lifetime).
- If you need lifetime checks, do a small targeted follow-up with `read_artifact(mode="full")` for only a few routers.

Start now:

1) Call `list_artifacts` to get the newest run_id (do not load payloads):
  - tool: "execute_junos_command_batch"
  - label_contains: "isis"
  - limit: 1
  - artifact_backend: "redis"

2) Call `read_artifact` for that run_id:
  - mode: "diff"
  - artifact_backend: "redis"
  - diff_max_groups: 10
  - diff_max_routers_per_group: 20
  - diff_max_diff_chars: 2000
  - (omit diff_include_diffs to let auto heuristics decide)

3) Output only:
  - LSDB_SYNC_REPORT (compact)
  - next suggested read_artifact(...) parameters (only if needed)
```

Note: not every CLI output benefits from diff mode. Commands with lots of volatile counters/timestamps (e.g. `show interfaces extensive`) may produce large diffs or diffs that are auto-suppressed.


## format
JMCP supports an explicit `format` knob for the Junos execution tools:

- `format=text` (default): returns plain CLI text
- `format=json`: requests JSON output from the device and returns it as JSON text
- `format=xml`: requests XML output from the device and returns it as text

Supported tools:
- `execute_junos_command`
- `execute_junos_command_batch`
- `execute_junos_commands_batch`

Why this exists:
- Relying on CLI pipes like `| display json` can be inconsistent depending on platform and how the command is executed.
- With `format=json`, JMCP requests structured output directly and normalizes it into a stable JSON string for downstream processing/token counting.

Example prompt:
`Using JMCP: run execute_junos_command_batch on routers [crpd1] with command: show isis database, format=json, timeout=120, response_mode=full. Summarize the number of LSPs and highlight any anomalies.`



## Quickstart: one-liner prompts (no artifacts)

Copy/paste these as-is into your LLM chat after JMCP is connected (adjust commands/timeouts as needed). These prompts explicitly use `get_router_list` so you don’t have to manually maintain router lists. The first examples do not use artifacts, meaning JMCP returns results inline.

- "Using JMCP: call get_router_list, then call execute_junos_command_batch with router_names=<that list>, command='show version brief', timeout=60, response_mode='summary'."
- "Using JMCP: run execute_junos_command_batch on routers [acx7100, mx204] with command: show interfaces terse, timeout=60, response_mode=summary."
- "Using JMCP: run execute_junos_command_batch on routers [crpd1] with command: show isis database, format=json, timeout=120, response_mode=full. Then summarize the LSP count and highlight any anomalies."
- "Using JMCP: call get_router_list, then run execute_junos_commands_batch with commands: [show isis database, show isis adjacency], timeout=120, response_mode=summary; summarize results and show only failures."
- "Using JMCP: run execute_junos_commands_batch on routers [crpd1] with commands: [show isis adjacency, show isis database], format=json, timeout=120, response_mode=full. Summarize adjacency state and any LSDB deltas." 
- "Repeat the previous batch, but enable barrier_sync=true (barrier_policy=proceed, preconnect_timeout=30, preconnect_retries=2, preconnect_backoff_seconds=1) for a near-simultaneous snapshot."
- "Run the same batch with barrier_sync=true and barrier_policy=strict so the operation aborts if any router cannot preconnect."

## Quickstart: one-liner prompts (with artifacts)

Artifacts are for large outputs: JMCP stores full results and returns only a pointer (`run_id`) so the LLM context window stays small.

### Option A (per call): no config changes

- "Using JMCP: call get_router_list, then run execute_junos_commands_batch with commands: [show isis database, show isis adjacency], timeout=120, response_mode=artifact, artifact_label=isis-smoke. Then immediately call read_artifact on the returned run_id in mode=failures (max_output_chars=2000)."
- "Using JMCP: call get_router_list, then run execute_junos_commands_batch with commands: [show isis database, show isis adjacency], format=json, timeout=120, response_mode=artifact, artifact_label=isis-json. Then call read_artifact on the returned run_id in mode=diff (diff_include_diffs=true)."
- "List the last 10 artifacts for tool=execute_junos_commands_batch with label_contains=isis (do not load full payloads)."

### Option B (defaults via mcp.json): recommended for daily use

If you want artifacts by default (so you don’t have to set `response_mode=artifact` every time), add these env vars to your JMCP server entry in your VS Code `mcp.json`.

Minimal (disk artifacts) – snippet to merge into your JMCP server entry:

```json
{
  "env": {
    "JMCP_BATCH_RESPONSE_MODE": "artifact",
    "JMCP_ARTIFACT_BACKEND": "disk"
  }
}
```

Redis artifacts (requires a reachable Redis; defaults work for local Homebrew `brew services start redis`) – snippet to merge into your JMCP server entry:

```json
{
  "env": {
    "JMCP_BATCH_RESPONSE_MODE": "artifact",
    "JMCP_ARTIFACT_BACKEND": "redis",
    "JMCP_ARTIFACT_REDIS_HOST": "127.0.0.1",
    "JMCP_ARTIFACT_REDIS_PORT": "6379",
    "JMCP_ARTIFACT_REDIS_DB": "0"
  }
}
```

Optional knobs you might add later:
- `JMCP_ARTIFACT_GZIP=1` (compress artifact payloads)
- `JMCP_ARTIFACT_REDIS_TTL_SECONDS=604800` (expire after 1 week)
- `JMCP_ARTIFACT_REDIS_MAX_BYTES=50000000` (hard cap per artifact)

Once configured, you can use the same batching prompts as above; JMCP will return summaries + an artifact pointer by default.

### Full mcp.json example (stdio)

Paste a complete server entry.

Note: depending on your VS Code version/extension, the top-level key may be `servers` (newer) or `mcpServers` (older). Keep whatever your existing `mcp.json` uses.

```json
{
  "mcpServers": {
    "jmcp": {
      "command": "${workspaceFolder}/.venv/bin/python",
      "args": ["${workspaceFolder}/jmcp.py", "-t", "stdio", "-f", "${workspaceFolder}/devices.json"],
      "env": {
        "JMCP_BATCH_RESPONSE_MODE": "summary",
        "JMCP_ARTIFACT_BACKEND": "disk"
      }
    }
  }
}
```

To enable artifacts by default, change:
- `JMCP_BATCH_RESPONSE_MODE` to `artifact`
- `JMCP_ARTIFACT_BACKEND` to `disk` or `redis`

Redis example (defaults for local Homebrew Redis):

```json
{
  "env": {
    "JMCP_BATCH_RESPONSE_MODE": "artifact",
    "JMCP_ARTIFACT_BACKEND": "redis",
    "JMCP_ARTIFACT_REDIS_HOST": "127.0.0.1",
    "JMCP_ARTIFACT_REDIS_PORT": "6379",
    "JMCP_ARTIFACT_REDIS_DB": "0"
  }
}
```

After changing `mcp.json`, restart the MCP server in VS Code so the new environment is applied.

Note: if you set `JMCP_ARTIFACT_BACKEND=redis|dual`, the Python environment used to run JMCP must have the `redis` package installed.


---

## Latest Configuration

### **Connection Pool: ENABLED BY DEFAULT** ✓
- **Default behavior:** Connection pool ON for all operations
- **Performance:** 15-100x faster than non-pooled
- **TCP sessions:** 1 per router (reused across all commands)
- **Override:** Use `--disable-connection-pool` to disable (not recommended)

Per-request override (batch tools):
- `connection_mode="pooled"|"fresh"` lets you bypass pooling for a single call without changing global server flags.
- `auto_fallback_to_fresh=true` (when `connection_mode="pooled"`) retries once with a fresh session if the pooled session hits a likely-stale transport/timeout error.

### **Workers-Per-Core Model** ✓
- **Replaced:** `--max-workers` → `--workers-per-core`
- **Default (if omitted):** `ceil(cpu_cores × 1.5)` workers (floor=8, cap=80)
- **Calculation (if set):** `total_workers = ceil(cpu_cores × workers_per_core)`
- **Override:** `JMCP_MAX_WORKERS` env var for absolute count

### **CLI Arguments**
```bash
python jmcp.py -h

Options:
  -f, --device-mapping        Device configuration file (default: devices.json)
  -H, --host                  Server host (default: 127.0.0.1)
  -t, --transport             Protocol: streamable-http or stdio
  -p, --port                  Server port (default: 30030)
  --workers-per-core          Workers per CPU core (float, optional; default uses heuristic ceil(cpu*1.5), floor=8, cap=80)
  --disable-connection-pool   Disable connection pool (not recommended)
  --idle-timeout              Pool idle timeout in seconds (default: 300)
  --health-check-interval     Pool health check interval (default: 30)
```

### Device Inventory Setup

- Copy the template: `cp devices.example.json devices.json`
- Edit `devices.json` with your real routers and credentials
- `devices.json` is intentionally ignored by git

---

## Regression testing (recommended)

This repo uses lightweight Python scripts (not `pytest`) to validate common flows:
- `tools/` contains local/unit-style checks.
- `regression_selftest/` contains the recommended HTTP/SSE integration & regression runners.
  - Compatibility wrappers exist under `tools/` so older docs/paths keep working.

### 1) Local/unit-style checks (no server required)

If you have `uv` installed, `make test` works out of the box. The Makefile uses `uv run ...`.

Without `uv`, run the scripts directly:

```bash
cd /path/to/junos-mcp-server-cg
PYTHONPATH=$PWD /path/to/python tools/test_config_validation.py
PYTHONPATH=$PWD /path/to/python tools/test_get_router_list.py
```

### 2) Integration smoke test (HTTP SSE)

Start the server (use a non-default port if another instance is already running):

```bash
cd /path/to/junos-mcp-server-cg
JMCP_ARTIFACT_BACKEND=redis JMCP_BATCH_RESPONSE_MODE=artifact \
JMCP_ARTIFACT_REDIS_HOST=127.0.0.1 JMCP_ARTIFACT_REDIS_PORT=6379 JMCP_ARTIFACT_REDIS_DB=0 \
/path/to/python jmcp.py -t streamable-http -H 127.0.0.1 -p 30031 -f devices.json
```

Then run the smoke test:

```bash
cd /path/to/junos-mcp-server-cg
/path/to/python regression_selftest/http_smoke_test.py --base-url http://127.0.0.1:30031/mcp/v1/sse
```

What it validates:
- `tools/list` and `get_router_list`
- `execute_junos_command_batch` with `response_mode=artifact`
- artifact persistence + retrieval via `list_artifacts` and `read_artifact` (when those tools are exposed over the same transport)

### 3) Stability/permutation testing (HTTP regression matrix)

For broader stability validation across many permutations (pooled vs fresh sessions, fallback behavior, response_mode variants, and negative cases), run:

```bash
cd /path/to/junos-mcp-server-cg
/path/to/python regression_selftest/regression_matrix_http.py --max-routers 2 --max-cases 25
```

It starts a temporary JMCP server on a free port and writes a JSON report under `artifacts/`.

Example long-run (about 1 hour), stopping on first runner-level error:

```bash
cd /path/to/junos-mcp-server-cg
/path/to/python regression_selftest/regression_matrix_http.py \
  --duration-seconds 3600 \
  --max-routers 2 \
  --max-cases 800 \
  --include-barrier \
  --include-artifact-backends \
  --stop-on-failure \
  --report artifacts/regression-matrix-longrun.json
```

---

## Stress Test Results

**Test Date:** January 22, 2026  
**Test Evidence:** See docs (links below)  
**Result:** ✅ ALL TESTS PASSED

### Test Coverage

| Test | Result | Details |
|------|--------|---------|
| **Startup Verification** | ✅ PASS | All 5 configurations start successfully |
| **Device Configuration** | ✅ PASS | 50 devices loaded from devices.json (stress-test inventory) |
| **Connection Pool Module** | ✅ PASS | JunosConnectionPool class available |
| **Help Output** | ✅ PASS | All 7 sections + 4 arguments documented |

### Startup Configurations Tested

1. **Default (if omitted):** ceil(14 cores × 1.5) = 21 workers ✓
2. **Workers-per-core 20:** ceil(14 cores × 20) = 280 workers ✓
3. **Workers-per-core 5:** ceil(14 cores × 5) = 70 workers ✓
4. **Custom idle timeout:** --idle-timeout 600 ✓
5. **Pool disabled:** --disable-connection-pool ✓

---

## TCP Session Behavior

### Example: 10 routers × 20 commands

**WITH CONNECTION POOL (default):**
- **TCP sessions:** 10 total (1 per router)
- **Reuse:** Each router's 20 commands use same session
- **TIME_WAIT:** Only 10 sockets when sessions expire
- **Speed:** 15-100x faster

**WITHOUT CONNECTION POOL:**
- **TCP sessions:** 200 total (20 per router)
- **Reconnections:** New connection per command
- **TIME_WAIT:** 200 sockets created immediately
- **Speed:** Very slow (1-2 seconds reconnection per command)

**EXECUTION MODEL:**
- **Parallel:** All 10 routers execute simultaneously
- **Sequential:** Each router's 20 commands run one-by-one
- **Total time:** ≈ time for 20 commands on slowest router (NOT 10×20)

**SUCCESS / FAILURE SEMANTICS (Batch Tools):**
- For `execute_junos_command_batch` and `execute_junos_commands_batch`, a command is counted as **failed** if the returned output looks like an error string (e.g. starts with `Connection error`, `An error occurred`, or `Error:`).
- This matters for platforms like cRPD where unsupported commands can return an RPC error message; those are now reported as failures (instead of being counted as successful just because a string was returned).

---

## ⚠️ System Resource Caveats

When running JMCP with many concurrent connections (e.g., 100+ routers), you may encounter **"too many open files"** errors if your system's file descriptor limit is too low.

### Check Current Limit
```bash
ulimit -n              # Current shell limit
sysctl kern.maxfiles   # macOS system-wide limit
sysctl fs.file-max     # Linux system-wide limit
```

### Temporary Solution (Current Session Only)
These changes only last until reboot:

**macOS:**
```bash
ulimit -n 65536
sudo sysctl -w kern.maxfiles=1048576
sudo sysctl -w kern.maxfilesperproc=1048576
```

**Linux:**
```bash
ulimit -n 65536
sudo sysctl -w fs.file-max=1048576
sudo sysctl -w fs.nr_open=1048576
```

### Persistent Solution (Survives Reboot)

**macOS:**
```bash
# Create or edit /etc/sysctl.conf
sudo nano /etc/sysctl.conf

# Add these lines:
kern.maxfiles=1048576
kern.maxfilesperproc=1048576

# Apply immediately
sudo sysctl -p
```

**Linux:**
```bash
# Create /etc/sysctl.d/99-jmcp.conf
sudo nano /etc/sysctl.d/99-jmcp.conf

# Add these lines:
fs.file-max=1048576
fs.nr_open=1048576

# Apply immediately
sudo sysctl -p /etc/sysctl.d/99-jmcp.conf

# For per-user limits, edit /etc/security/limits.conf
sudo nano /etc/security/limits.conf

# Add these lines:
* soft nofile 65536
* hard nofile 1048576
```

**Note:** The connection pool mitigates this issue by reusing persistent sessions (1 per router) rather than creating new connections per command. Systems with limits ≥ 65536 are generally sufficient for most deployments.

---

## Artifacts + Response Modes (Token-Safe Batch Runs)

JMCP supports **response modes** for the batch tools so you can avoid returning huge raw outputs into the client/LLM context.

### Supported Modes

Applies to:
- `execute_junos_command_batch` (N routers × 1 command)
- `execute_junos_commands_batch` (N routers × M commands)

Modes:
- `full` (default): include full outputs in the tool response
- `summary`: return a compact summary + metadata (and failures only)
- `artifact`: **persist full results to the configured artifact backend** and return only summary + an `artifact` pointer

Supported artifact backends:
- `disk`: write JSON files under the artifact directory
- `redis`: write JSON blobs + metadata to Redis
- `dual`: write to both (Redis primary; disk best-effort secondary)

### Where Artifacts Are Written

Artifact backend selection (priority order is tool arg → env var → file → default):
1. tool argument `artifact_backend`
2. env var `JMCP_ARTIFACT_BACKEND`
3. file `.jmcp_artifact_backend` (next to `jmcp.py`)
4. default: `disk`

Note: if you see `redis` being used “by default”, that’s coming from an override (most commonly `JMCP_ARTIFACT_BACKEND=redis` in your environment, or a `.jmcp_artifact_backend` file next to `jmcp.py`). `jmcp.py` itself falls back to `disk` when no override is present.

### Redis-down self-healing (recommended default)

If you request `artifact_backend=redis` but Redis is unreachable, JMCP will try to self-heal:
- **Persist:** for batch `response_mode=artifact`, JMCP falls back to writing the artifact to disk and returns an `artifact` pointer with `backend="disk"` plus a `warnings` list.
- **Inspect:** `list_artifacts` / `read_artifact` called with `artifact_backend=redis` will fall back to disk (when possible) and return `backend="disk"`, `requested_backend="redis"`, and `warnings: [...]`.

If you want to fail closed (no Redis→disk fallback for persistence), set:
- `JMCP_ARTIFACT_FAIL_CLOSED=true`

Artifact directory resolution order:
1. tool argument `artifact_dir`
2. env var `JMCP_ARTIFACT_DIR`
3. file `.jmcp_artifact_dir` (next to `jmcp.py`)
4. default: `<jmcp.py dir>/artifacts`

Use case: huge outputs (e.g. `show isis database`) across many routers without flooding the LLM context window
- Example math: 250 routers × ~1400 tokens/router ≈ ~350k tokens (this can exceed the context window quickly)
- Solution: run the batch with `response_mode=artifact`
  - JMCP stores the full results to an artifact backend (disk / Redis / dual)
  - the LLM receives only a small summary + a pointer (`run_id`)
  - later, the LLM can query the stored data in a controlled/token-safe way
- Artifact tools:
  - `list_artifacts` (find artifacts without loading payloads)
  - `read_artifact` (read one artifact by `run_id`)
- `read_artifact` modes (what gets sent back to the LLM):
  - **DIFF** (`mode=diff`): token-safe triage (groups identical outputs and shows a few diffs vs a baseline)
    - diffs are auto-suppressed when they would explode tokens (rewrite-like diffs or diffs larger than the raw output)
    - note: DIFF is great to spot structural LSDB differences (missing LSPs / different sequence/checksum). It is *not* a good way to compute fleet-wide min/max lifetime deviation.
  - **FULL** (`mode=full`): returns the full stored payload for the selected routers (can be large)
- Phased loading (token-safe): use `router_offset` + `router_limit` (or `router_names`) to read the artifact in chunks

If you specifically want lifetime deviation stats, use chunked FULL reads and aggregate in the LLM (or an external script):

```text
read_artifact(mode='full', router_offset=0, router_limit=5)
```

Have the LLM maintain a rolling table like:

`lsp-id -> {min_lifetime, max_lifetime, min_router, max_router}`

Then increment `router_offset` and repeat until done.

### Artifact Tools

JMCP exposes tools to work with artifacts (useful when running in `artifact` mode):

- `get_server_settings`: shows effective batch mode + artifact dir + worker/pool status
- `list_artifacts`: lists artifacts from the configured backend without loading full payloads
- `read_artifact`: loads one artifact by `run_id` (backend) or `artifact_path` (disk) and returns a compact view

`read_artifact` view modes:
- `auto`: chooses `full` vs `diff` based on size/router count (good default)
- `metadata`: minimal header info
- `summary`: summary + failure metadata (LLM-friendly)
- `failures`: only failing routers/commands (truncated outputs)
- `diff`: token-safe grouping of outputs + truncated diffs against a baseline (spot anomalies without dumping all output)
- `full`: entire artifact JSON (can be large)

`diff` mode knobs (optional):
- `diff_include_diffs`: if true, force unified diffs; if false, disable diffs; if omitted, server may auto-suppress rewrite-level diffs
- `diff_max_groups` (default 3): how many variant groups to include (and optionally diff) vs baseline
- `diff_max_diff_chars` (default 3000): truncation limit per diff text
- `diff_max_routers_per_group` (default 10): how many router names to list per variant
- `diff_context_lines` (default 10): unified diff context lines

Notes on `diff_include_diffs` auto mode:
- Some commands (notably `show interfaces extensive`) include volatile counters/timestamps that can make unified diffs *bigger* than the raw output (“rewrite diffs”).
- If you omit `diff_include_diffs`, JMCP runs in **auto** mode: it always returns grouping, but only includes unified diffs when the variant is sufficiently similar to the baseline.
- When a diff is suppressed you’ll still get a diff entry with `omitted: true`, a `reason` (e.g. `rewrite_diff_suppressed` or `diff_larger_than_variant`), and a `similarity` score when relevant.

### Example: Run in `artifact` Mode + Fetch Failures

1) Run a large batch but keep the response small:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 1,
  "params": {
    "name": "execute_junos_commands_batch",
    "arguments": {
      "router_names": ["r1", "r2", "r3"],
      "commands": ["show version brief", "show system uptime"],
      "timeout": 60,
      "response_mode": "artifact",
      "artifact_label": "smoke"
    }
  }
}
```

The response includes a compact summary plus an `artifact` pointer (not the full outputs).
Capture `artifact.run_id` (and optionally `artifact.path`).

2) List recent artifacts:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 2,
  "params": {
    "name": "list_artifacts",
    "arguments": {
      "tool": "execute_junos_commands_batch",
      "label_contains": "smoke",
      "limit": 10
    }
  }
}
```

3) Read only failures (LLM-friendly, truncated outputs):

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 3,
  "params": {
    "name": "read_artifact",
    "arguments": {
      "run_id": "20260127T004712Z-3b791dc1",
      "mode": "failures",
      "max_output_chars": 2000
    }
  }
}
```

Tip: `get_server_settings` shows the effective default response mode and artifact directory.

### Example: Token-safe "what differs" (diff mode)

Instead of loading all outputs, ask JMCP to group routers by identical output and show diffs for the variants:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 4,
  "params": {
    "name": "read_artifact",
    "arguments": {
      "run_id": "20260127T004712Z-3b791dc1",
      "mode": "diff",
      "diff_max_groups": 3,
      "diff_max_diff_chars": 3000,
      "diff_max_routers_per_group": 10
    }
  }
}
```

Force diffs (even if they’re rewrite-like):

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 5,
  "params": {
    "name": "read_artifact",
    "arguments": {
      "run_id": "20260127T004712Z-3b791dc1",
      "mode": "diff",
      "diff_include_diffs": true,
      "diff_max_groups": 1,
      "diff_max_diff_chars": 1200,
      "diff_context_lines": 10
    }
  }
}
```

Disable diffs entirely (grouping only):

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 6,
  "params": {
    "name": "read_artifact",
    "arguments": {
      "run_id": "20260127T004712Z-3b791dc1",
      "mode": "diff",
      "diff_include_diffs": false
    }
  }
}
```

---

## Barrier Sync (Preconnect + Retry) for Batch Tools

Applies to:
- `execute_junos_command_batch` (N routers × 1 command)
- `execute_junos_commands_batch` (N routers × M commands)

By default, the batch tools start per-router tasks immediately: routers with faster SSH/TCP establishment begin executing while slower routers are still connecting.

If you want a **"connect barrier"** (connect first, then execute), enable `barrier_sync`.

### What `barrier_sync` Does

- **Phase 1 (preconnect):** concurrently attempts to establish a session to every router (using the connection pool if enabled).
- **Phase 2 (execute):** runs the command list in parallel **only on routers that preconnected successfully**.
- Routers that fail preconnect are marked as **skipped** with an error like `preconnect_failed (skipped execution): ...`.

### Retry Behavior

Preconnect supports retries (per router):

- `preconnect_retries` (default `2`) → total attempts = `preconnect_retries + 1`
- `preconnect_backoff_seconds` (default `1`) between attempts
- `preconnect_timeout` (default `30`) seconds per attempt

### Barrier Policy

- `barrier_policy: "proceed"` (default): run commands on the connected subset, and report preconnect failures as skipped.
- `barrier_policy: "strict"`: abort execution if any router fails preconnect.

### Example

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 42,
  "params": {
    "name": "execute_junos_commands_batch",
    "arguments": {
      "router_names": ["r1", "r2", "r3"],
      "commands": ["show version brief", "show system uptime"],
      "timeout": 60,
      "barrier_sync": true,
      "barrier_policy": "proceed",
      "preconnect_timeout": 20,
      "preconnect_retries": 2,
      "preconnect_backoff_seconds": 1,
      "response_mode": "summary"
    }
  }
}
```

The response includes `preconnect` metadata and counts like `total_routers_executed` and `total_routers_preconnect_failed`.

### Example (Single Command)

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 43,
  "params": {
    "name": "execute_junos_command_batch",
    "arguments": {
      "router_names": ["r1", "r2", "r3"],
      "command": "show version brief",
      "timeout": 60,
      "barrier_sync": true,
      "barrier_policy": "proceed",
      "preconnect_timeout": 20,
      "preconnect_retries": 2,
      "preconnect_backoff_seconds": 1,
      "response_mode": "summary"
    }
  }
}
```

---

## Usage Examples

### Basic Usage
```bash
# Start with defaults (heuristic workers, connection pool ON)
python jmcp.py

# Custom port
python jmcp.py -p 8080

# Different device file
python jmcp.py -f my_routers.json
```

### Worker Pool Tuning
```bash
# Small deployment (20-30 routers)
python jmcp.py --workers-per-core 1.5    # total_workers = ceil(cpu_cores * 1.5)

# Medium deployment (45-75 routers)
python jmcp.py --workers-per-core 2.5    # total_workers = ceil(cpu_cores * 2.5)

# Large deployment (100+ routers)
python jmcp.py --workers-per-core 4      # total_workers = ceil(cpu_cores * 4)

# Absolute override
JMCP_MAX_WORKERS=300 python jmcp.py      # Exactly 300 workers
```

### Connection Pool Options
```bash
# Keep connections alive 10 minutes
python jmcp.py --idle-timeout 600

# Health check every 60 seconds
python jmcp.py --health-check-interval 60

# Disable pool (not recommended)
python jmcp.py --disable-connection-pool
```

### Production Examples
```bash
# Small: 20-30 routers
python jmcp.py -p 30030 --workers-per-core 1.5

# Medium: 45-75 routers
python jmcp.py -p 30030 --workers-per-core 2.5 --idle-timeout 600

# Large: 100+ routers
python jmcp.py -p 30030 --workers-per-core 4 --idle-timeout 900 --health-check-interval 120
```

---

## File Inventory

### Core Implementation (Updated)
- **jmcp.py** (75 KB) - Main server with all updates ✓
- **jmcp_connection_pool.py** (19 KB) - Connection pool implementation ✓
- **devices.example.json** - Example device inventory template ✓
- **devices.json** (local/ignored) - Router inventory (do not commit) ✓
- **utils/** - Configuration utilities ✓

### Testing
- See [docs/FINAL_TEST_RESULTS.md](docs/FINAL_TEST_RESULTS.md) and [docs/connection_pool/JMCP_CONNECTION_POOL_STRESS_TEST_RESULTS.md](docs/connection_pool/JMCP_CONNECTION_POOL_STRESS_TEST_RESULTS.md)

### Documentation (see separate docs)
- [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) - One-page overview
- [docs/connection_pool/JMCP_CONNECTION_POOL_SUMMARY.md](docs/connection_pool/JMCP_CONNECTION_POOL_SUMMARY.md) - Executive summary
- [docs/connection_pool/JMCP_CONNECTION_POOL_IMPLEMENTATION_GUIDE.md](docs/connection_pool/JMCP_CONNECTION_POOL_IMPLEMENTATION_GUIDE.md) - Integration guide
- [docs/connection_pool/JMCP_CONNECTION_POOL_DESIGN.md](docs/connection_pool/JMCP_CONNECTION_POOL_DESIGN.md) - Architecture
- [docs/connection_pool/DEVELOPER_HANDOFF_CHECKLIST.md](docs/connection_pool/DEVELOPER_HANDOFF_CHECKLIST.md) - Action items

---

## Key Changes from Original

| Feature | Original | Patched |
|---------|----------|---------|
| **Connection Pool** | Manual opt-in | **ENABLED by default** |
| **Worker Config** | `--max-workers` (absolute) | `--workers-per-core` (scalable; float supported) |
| **Batch Tools** | Single-router execution focus | **`execute_junos_command_batch` + `execute_junos_commands_batch` (N routers, 1 or M commands)** |
| **Artifacts (Token Safety)** | None | **`response_mode=artifact` stores full payload + returns `run_id`** |
| **Artifact Inspection** | N/A | **`list_artifacts` + `read_artifact(mode=failures|diff|full|...)`** |
| **Barrier Sync** | None | **Preconnect barrier + retry (`barrier_sync`) for near-simultaneous snapshots** |
| **Output Format** | CLI pipes only / inconsistent | **First-class `format=text|json|xml` knob** |
| **TCP Sessions** | New session per command (no reuse) | **Persistent 1 session per router (reused across commands)** |
| **Performance** | Slow (reconnect overhead) | **15-100x faster** |
| **Help Text** | Basic | **Comprehensive with examples** |
| **TCP Explanation** | Not documented | **Fully explained in -h** |

---

## Configuration Priority

### Workers
1. `JMCP_MAX_WORKERS` environment variable (absolute override; integer > 0)
  - If set, it takes precedence and `--workers-per-core` is ignored.
2. `--workers-per-core` CLI argument
3. Default heuristic: `ceil(cpu_cores × 1.5)` workers (floor=8, cap=80)

### Connection Pool
- **Default:** ENABLED (recommended)
- **Override:** `--disable-connection-pool` (not recommended)

---

## Support

For detailed implementation guidance, see:
- [docs/connection_pool/JMCP_CONNECTION_POOL_IMPLEMENTATION_GUIDE.md](docs/connection_pool/JMCP_CONNECTION_POOL_IMPLEMENTATION_GUIDE.md) - Complete integration guide
- [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) - Quick start overview

For performance analysis, see:
- [docs/connection_pool/JMCP_CONNECTION_POOL_STRESS_TEST_RESULTS.md](docs/connection_pool/JMCP_CONNECTION_POOL_STRESS_TEST_RESULTS.md) - Original validation results

**All systems tested and validated. Ready to deploy.**

## Artifact tool knobs (quick reference)

### list_artifacts knobs (find/select what to read)

- `artifact_backend`: where to list from (`disk` / `redis` / `dual`)
- `artifact_dir`: (disk) override the directory
- `tool`: filter by tool name (e.g. only `execute_junos_commands_batch`)
- `label_contains`: substring filter on the artifact label
- `since`: only show artifacts newer than a timestamp
- `limit`: cap how many results you get back

### read_artifact knobs (how much/how shaped you read back)

Select which artifact:
- `run_id` (recommended) or `artifact_path` (disk)
- `artifact_backend`, `artifact_dir` (where to read from)

Select which routers (token-safe slicing):
- `router_names`: explicit subset
- `router_offset` + `router_limit`: paginate through routers

Select representation (mode):
- `mode="full"` (default), `metadata`, `summary`, `failures`, `diff`

Failures view sizing:
- `max_output_chars`: truncate per-router output snippets in failures

Diff view controls:
- `diff_include_diffs`: true/false (or omit and let auto heuristics decide)
- `diff_context_lines`: unified diff context lines
- `diff_max_diff_chars`: truncate each diff text
- `diff_max_routers_per_group`: cap router names listed per hash-group
- `diff_max_groups`: cap how many variant groups are included (and optionally diffed)
