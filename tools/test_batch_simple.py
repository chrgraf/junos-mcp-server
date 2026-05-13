#!/usr/bin/env python3
"""
Direct test using HTTP API instead of stdio.
Tests execute_junos_commands_batch with token analysis.
"""

import json
import requests
import time
from datetime import datetime

# Load devices
with open('devices.json', 'r') as f:
    devices = json.load(f)

all_routers = list(devices.keys())

# Test commands
commands = [
    "show version brief",
    "show system uptime",
    "show chassis hardware",
    "show interfaces terse | count",
    "show route summary",
    "show bgp summary",
    "show isis adjacency",
    "show system alarms",
    "show system memory",
    "show system processes summary",
    "show configuration protocols | display set | count",
    "show log messages | last 3"
]

print("="*80)
print("🧪 execute_junos_commands_batch Test - Token Usage Analysis")
print("="*80)
print()

# Test scenarios
scenarios = [
    (5, "Small baseline"),
    (10, "Medium scale"),
    (25, "Large scale"),
    (50, "Full scale - ALL routers")
]

results = []

# Note: Server must be running on http://127.0.0.1:30030
print("📝 NOTE: Please start the server first:")
print("   cd <repo-root> && python3 jmcp.py")
print()
input("Press Enter when server is ready...")
print()

base_url = "http://127.0.0.1:30030"

for num_routers, description in scenarios:
    router_subset = all_routers[:num_routers]
    total_ops = num_routers * len(commands)
    
    print(f"\n{'='*80}")
    print(f"📊 {description}: {num_routers} routers × {len(commands)} commands = {total_ops} ops")
    print(f"{'='*80}")
    
    # Prepare MCP request
    request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": 1,
        "params": {
            "name": "execute_junos_commands_batch",
            "arguments": {
                "router_names": router_subset,
                "commands": commands,
                "timeout": 60
            }
        }
    }
    
    start_time = time.time()
    
    try:
        # Send via HTTP/SSE endpoint with proper headers
        headers = {
            'Accept': 'application/json, text/event-stream',
            'Content-Type': 'application/json'
        }
        response = requests.post(
            f"{base_url}/mcp/v1/sse",
            json=request,
            headers=headers,
            timeout=120
        )
        
        end_time = time.time()
        duration = end_time - start_time
        
        if response.status_code != 200:
            print(f"❌ HTTP {response.status_code}: {response.text[:200]}")
            continue
        
        # Parse SSE response (format: "event: message\ndata: {...}")
        response_text_raw = response.text
        result = None
        
        for line in response_text_raw.split('\n'):
            if line.startswith('data: '):
                data_json = line[6:]  # Remove 'data: ' prefix
                result = json.loads(data_json)
                break
        
        if 'error' in result:
            print(f"❌ MCP Error: {result['error']}")
            continue
        
        if 'result' not in result or 'content' not in result['result']:
            print(f"❌ Invalid response format")
            continue
        
        content = result['result']['content']
        if not content:
            print(f"❌ Empty content")
            continue
        
        response_text = content[0]['text']
        response_data = json.loads(response_text)
        
        # Extract metrics
        total_commands = response_data.get('total_commands_executed', 0)
        successful = response_data.get('total_successful', 0)
        failed = response_data.get('total_failed', 0)
        reported_duration = response_data.get('total_duration', duration)
        
        # Token analysis
        response_size = len(response_text)
        tokens = response_size / 4  # Rough estimate: 1 token ≈ 4 chars
        
        # Router durations
        routers_data = response_data.get('routers', [])
        router_durations = [r.get('router_duration', 0) for r in routers_data]
        max_duration = max(router_durations) if router_durations else 0
        avg_duration = sum(router_durations) / len(router_durations) if router_durations else 0
        
        print(f"✅ Completed in {reported_duration:.2f}s")
        print(f"   Success rate: {successful}/{total_commands} ({successful/total_commands*100:.1f}%)")
        print(f"   Throughput: {total_commands/reported_duration:.1f} cmd/s")
        print(f"   Max router time: {max_duration:.2f}s, Avg: {avg_duration:.2f}s")
        print(f"   📊 Response: {response_size:,} bytes ({response_size/1024:.1f} KB)")
        print(f"   📊 Est. tokens: ~{tokens:,.0f}")
        print(f"   📊 Tokens/router: ~{tokens/num_routers:,.0f}")
        print(f"   📊 Tokens/command: ~{tokens/total_commands:,.0f}")
        
        results.append({
            'routers': num_routers,
            'commands': len(commands),
            'duration': reported_duration,
            'successful': successful,
            'failed': failed,
            'response_size': response_size,
            'tokens': tokens,
            'max_duration': max_duration,
            'throughput': total_commands/reported_duration
        })
        
    except requests.Timeout:
        print(f"❌ Request timed out after 120s")
        break
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        break
    
    time.sleep(1)  # Brief pause between tests

# Final analysis
if results:
    print("\n" + "="*80)
    print("📊 COMPREHENSIVE SCALING ANALYSIS")
    print("="*80)
    print()
    print("  Routers | Cmds | Total | Duration | Tokens     | Tok/Rtr | Tok/Cmd | Cmd/sec")
    print("  " + "-"*79)
    
    for r in results:
        print(f"  {r['routers']:4d}    | {r['commands']:2d}   | {r['routers']*r['commands']:4d}  | "
              f"{r['duration']:>6.1f}s  | {r['tokens']:>9,.0f}  | {r['tokens']/r['routers']:>6,.0f}  | "
              f"{r['tokens']/(r['routers']*r['commands']):>6,.0f}  | {r['throughput']:>5.1f}")
    
    print()
    print("="*80)
    print("💡 TOKEN USAGE PROJECTIONS FOR SCALING")
    print("="*80)
    
    # Use last successful test as baseline
    baseline = results[-1]
    tokens_per_op = baseline['tokens'] / (baseline['routers'] * baseline['commands'])
    
    projections = [
        (100, 10, "2x routers"),
        (100, 20, "2x routers, 2x commands"),
        (200, 10, "4x routers"),
        (500, 10, "10x routers"),
        (1000, 10, "20x routers"),
        (2000, 10, "40x routers"),
    ]
    
    print(f"\n  Baseline: {baseline['routers']} routers × {baseline['commands']} commands")
    print(f"  Tokens per operation: ~{tokens_per_op:.0f}")
    print()
    print("  Scale           | Routers | Cmds | Total Ops | Est. Tokens  | Time  | Warning")
    print("  " + "-"*82)
    
    for routers, cmds, desc in projections:
        total_ops = routers * cmds
        proj_tokens = tokens_per_op * total_ops
        proj_time = baseline['max_duration'] * (cmds / baseline['commands'])
        
        warning = "✅ OK"
        if proj_tokens > 2_000_000:
            warning = "🔴 >2M (CRITICAL)"
        elif proj_tokens > 1_000_000:
            warning = "🔴 >1M (TOO HIGH)"
        elif proj_tokens > 500_000:
            warning = "🟡 >500K (HIGH)"
        elif proj_tokens > 200_000:
            warning = "🟠 >200K (MODERATE)"
        elif proj_tokens > 100_000:
            warning = "🟡 >100K"
        
        print(f"  {desc:15s} | {routers:4d}    | {cmds:2d}   | {total_ops:5d}     | "
              f"{proj_tokens:>12,.0f} | {proj_time:>5.1f}s | {warning}")
    
    print()
    print("="*80)
    print("💡 KEY RECOMMENDATIONS")
    print("="*80)
    print()
    
    final_tokens = baseline['tokens']
    
    if final_tokens < 50_000:
        print("  ✅ EXCELLENT - Very efficient token usage")
        print("     • Current scale is ideal for real-time LLM processing")
        print("     • Can safely scale to 500+ routers with 10 commands")
        print("     • No special considerations needed")
    elif final_tokens < 100_000:
        print("  ✅ GOOD - Reasonable token usage")
        print("     • Can scale to 200-300 routers")
        print("     • Consider output filtering for 500+ routers")
        print("     • Fine for most LLM context windows")
    elif final_tokens < 200_000:
        print("  🟠 MODERATE - Approaching limits")
        print("     • Recommend output filtering: | count, | brief, | except")
        print("     • Consider chunking for 200+ routers")
        print("     • May hit limits on smaller context windows")
    else:
        print("  🔴 HIGH - Significant token usage")
        print("     • REQUIRED: Use output filtering or pagination")
        print("     • Break into smaller batches (e.g., 25 routers at a time)")
        print("     • Stream results instead of single response")
    
    print()
    print("  🎯 Performance:")
    final_throughput = baseline['throughput']
    
    if final_throughput > 50:
        print(f"     🚀 EXCELLENT - {final_throughput:.0f} cmd/s (connection pool optimal)")
    elif final_throughput > 20:
        print(f"     ✅ GOOD - {final_throughput:.0f} cmd/s (solid performance)")
    elif final_throughput > 10:
        print(f"     🟡 MODERATE - {final_throughput:.0f} cmd/s (room for improvement)")
    else:
        print(f"     🔴 LOW - {final_throughput:.0f} cmd/s (check connection pool/network)")
    
    print()
    print("  💾 Output Filtering Strategies:")
    print("     • Use | count instead of full output")
    print("     • Use | display set for configs")
    print("     • Use | match <pattern> to filter specific lines")
    print("     • Use | brief for summary views")
    print("     • Use | except <pattern> to exclude noise")
    
    print()
else:
    print("\n❌ No tests completed successfully")
    print("   • Ensure server is running: python3 jmcp.py")
    print("   • Check server logs for errors")
