# Testing execute_junos_commands_batch

## Issue Found

The MCP server runs in **HTTP mode** by default (`streamable-http`), not `stdio` mode. The tests were trying to communicate via stdin/stdout which doesn't work with HTTP transport.

## Solution: Two-Step Testing Process

### Step 1: Start the Server

```bash
cd /Users/behn66googlemail.com/Library/CloudStorage/OneDrive-HewlettPackardEnterprise/github/mcp_server/junos-mcp-server-cg
source /Users/behn66googlemail.com/Library/CloudStorage/OneDrive-HewlettPackardEnterprise/github/.venv/bin/activate
python3 jmcp.py
```

Server will start on: `http://127.0.0.1:30030`

### Step 2: Run the Test (in another terminal)

```bash
cd /Users/behn66googlemail.com/Library/CloudStorage/OneDrive-HewlettPackardEnterprise/github/mcp_server/junos-mcp-server-cg
source /Users/behn66googlemail.com/Library/CloudStorage/OneDrive-HewlettPackardEnterprise/github/.venv/bin/activate
python3 tools/test_batch_simple.py
```

## Extended stability testing: regression matrix runner

For higher confidence, run the HTTP SSE regression matrix. It starts a temporary JMCP server on a free port, runs many permutations (pooled/fresh, fallback on/off, response_mode variants, negative cases), then writes a JSON report under `artifacts/`.

```bash
cd /Users/behn66googlemail.com/Library/CloudStorage/OneDrive-HewlettPackardEnterprise/github/mcp_server/junos-mcp-server-cg
source /Users/behn66googlemail.com/Library/CloudStorage/OneDrive-HewlettPackardEnterprise/github/.venv/bin/activate

# Quick but meaningful coverage (recommended default)
python3 tools/regression_matrix_http.py --max-routers 2 --max-cases 25

# More permutations (slower)
python3 tools/regression_matrix_http.py --max-routers 3 --max-cases 60 --include-barrier --include-artifact-backends

# Long stability run (example: ~1 hour), stop immediately on first failure
python3 tools/regression_matrix_http.py --duration-seconds 3600 --max-routers 2 --max-cases 500 --include-barrier --include-artifact-backends --stop-on-failure
```

This will test progressively:
- 5 routers × 12 commands = 60 operations
- 10 routers × 12 commands = 120 operations
- 25 routers × 12 commands = 300 operations
- 50 routers × 12 commands = 600 operations

## What the Test Analyzes

1. **Execution Performance**
   - Duration, throughput (commands/second)
   - Parallelization efficiency
   
2. **Token Usage**
   - Response size in bytes/KB/MB
   - Estimated tokens (1 token ≈ 4 characters)
   - Tokens per router, tokens per command
   
3. **Scaling Projections**
   - Estimates for 100, 200, 500, 1000, 2000 routers
   - Identifies when token limits become a concern
   - Recommends output filtering strategies

4. **Success Rate**
   - Failed vs successful commands
   - Per-router failure analysis

## Expected Results

Based on typical Junos command outputs:
- **5 routers**: ~10-20K tokens (✅ excellent)
- **10 routers**: ~20-40K tokens (✅ excellent)  
- **25 routers**: ~50-100K tokens (✅ good)
- **50 routers**: ~100-200K tokens (🟠 moderate, may need filtering)

## Alternative: Manual cURL Test

If Python test has issues, test directly with cURL:

```bash
curl -X POST http://127.0.0.1:30030/mcp/v1/sse \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 1,
    "params": {
      "name": "execute_junos_commands_batch",
      "arguments": {
        "router_names": ["mx204", "acx7100", "crpd0"],
        "commands": ["show version brief", "show system uptime"],
        "timeout": 30
      }
    }
  }'
```

## Token Concerns?

**When to worry:**
- **<100K tokens**: No concerns for most LLMs
- **100-200K tokens**: Fine for GPT-4, Claude 3 (large contexts)
- **200-500K tokens**: May hit limits on smaller models
- **>500K tokens**: Requires chunking/filtering/streaming

**Solutions for high token usage:**
1. **Output Filtering**: Use `| count`, `| brief`, `| display set`
2. **Command Selection**: Only run essential commands
3. **Batching**: Split into multiple smaller requests
4. **Streaming**: Process results incrementally (future enhancement)
