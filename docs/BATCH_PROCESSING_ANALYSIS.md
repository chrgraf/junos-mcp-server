================================================================================
BATCH PROCESSING ANALYSIS & RECOMMENDATIONS
================================================================================

Current Tool Architecture:
--------------------------

1. execute_junos_command(router_name, command)
   - Single router, single command
   - Clients might call this 20 times for 20 commands
   - Each call reuses connection pool (if within timeout)
   
2. execute_junos_command_batch(router_names[], command)
   - Multiple routers, single command
   - Good for running same command on many routers

3. execute_junos_commands_batch(router_names[], commands[])
  - One or more routers, multiple commands
  - Commands run sequentially per router; routers run in parallel
  - Covers the “single router, many commands” case via router_names=["router1"]

================================================================================
PROBLEM SCENARIO
================================================================================

Client might do:
  execute_junos_command(router1, "show version")
  execute_junos_command(router1, "show system uptime")
  execute_junos_command(router1, "show interfaces terse")
  ... (20 separate MCP requests)

Current behavior:
  ✓ Connection pool helps (reuses session)
  ✗ But still 20 separate MCP calls (overhead)
  ✗ Not truly batched

================================================================================
SOLUTION OPTIONS
================================================================================

OPTION 1: Add Multi-Command Batch Tool (RECOMMENDED)
-----------------------------------------------------
Create new tool: execute_junos_commands_batch(router_names[], commands[])

Tool definition:
  execute_junos_commands_batch:
    router_names: array of strings (use a 1-element array for a single router)
    commands: array of strings
    timeout: integer

Example usage:
  execute_junos_commands_batch(
    router_names=["router1"],
    commands=["show version", "show system uptime", "show interfaces"]
  )

Benefits:
  ✓ Explicit batching
  ✓ Client can optimize if they want
  ✓ Backward compatible (keep old tools)
  ✓ Clear API

Implementation:
  - Add new tool to jmcp.py
  - Execute commands sequentially on same connection
  - Return structured results array


OPTION 2: Auto-Batching Queue (COMPLEX)
----------------------------------------
Server-side command queuing with delay:
  - Hold commands for 100ms per router
  - If more commands arrive for same router, batch them
  - Execute batch after delay

Benefits:
  ✓ Transparent to client
  ✓ Works with existing tools

Drawbacks:
  ✗ Added latency (100ms delay)
  ✗ Complex implementation
  ✗ Hard to tune delay correctly
  ✗ Potential race conditions


OPTION 3: Deprecate Single-Command Tool (BREAKING)
--------------------------------------------------
Force clients to use arrays:
  - Remove execute_junos_command
  - Only expose execute_junos_commands_batch(router, commands[])
  - Single command = array of 1

Benefits:
  ✓ Forces best practice
  ✓ Simpler API

Drawbacks:
  ✗ Breaking change
  ✗ Harder for simple use cases
  ✗ Client friction


OPTION 4: Wrapper Pattern (COMPROMISE)
--------------------------------------
Keep both tools, but:
  - Single-command wraps batch: execute_junos_command → calls batch with [command]
  - Encourages batch in documentation
  - Log warning when single tool used repeatedly

Benefits:
  ✓ Backward compatible
  ✓ Single code path (DRY)
  ✓ Can track usage patterns

Drawbacks:
  ~ Doesn't force batching
  ~ Relies on documentation/education

================================================================================
RECOMMENDATION
================================================================================

IMPLEMENT OPTION 1: Add Multi-Command Batch Tool

Why:
1. Gives clients the ABILITY to batch multiple commands on one router
2. Backward compatible
3. Clear, explicit API
4. Connection pool already handles session reuse
5. Simple to implement

Tool set becomes:
  execute_junos_command(router, command)              - 1 router, 1 command
  execute_junos_command_batch(routers[], command)     - N routers, 1 command  
  execute_junos_commands_batch(routers[], commands[]) - N routers, N commands

Eventually could add:
  execute_junos_commands_multi_batch(routers[], commands[]) - N routers, N commands

================================================================================
CURRENT STATE
================================================================================

Connection pooling already provides:
✓ Session reuse across sequential calls
✓ Works even with non-batched usage
✓ 15-100x speedup vs non-pooled

What we DON'T have:
✓ Multi-command batching for single router (via router_names=["router1"])
✓ Reduced MCP request overhead for multiple commands

Workaround:
- Use execute_junos_commands_batch with a 1-element router_names list

================================================================================
NEXT STEPS
================================================================================

If you want to implement Option 1:

1. Add new tool definition in jmcp.py
2. Implement handler: handle_execute_junos_commands_batch()
3. Execute commands sequentially on same router
4. Return structured JSON with per-command results
5. Update documentation
6. Update help text to recommend batching

Estimated effort: ~30 minutes

================================================================================
