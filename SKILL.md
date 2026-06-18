---
name: erl-cli
description: Remote Erlang node diagnostics over WebSocket. Use erl_cli_cli.py to eval expressions, run redbug traces, collect recon/observer diagnostics, hot-reload beam files, and subscribe to server-pushed topics.
---

> **Agent instructions**:
> 1. All remote operations MUST use `scripts/erl_cli_cli.py`.
> 2. Before any operation, ask the user for: **host**, **port**, **token**.
> 3. After connecting, always run `eval "erlang:node()"` and `eval "nodes()"` first to confirm topology.

**CLI tool**: `scripts/erl_cli_cli.py`
**Dependency**: `pip install websocket-client`

---

## Commands

```bash
# Eval
python3 scripts/erl_cli_cli.py eval "<expr>" --host HOST --port PORT --token TOKEN

# Trace (streams until done or Ctrl-C)
python3 scripts/erl_cli_cli.py trace "mod:fun/arity" --host HOST --port PORT --token TOKEN --time 30000 --msgs 100

# Stop trace
python3 scripts/erl_cli_cli.py stop --host HOST --port PORT --token TOKEN

# Hot-reload beam (module name inferred from filename)
python3 scripts/erl_cli_cli.py load path/to/mod.beam --host HOST --port PORT --token TOKEN

# Subscribe to a topic (streams until Ctrl-C)
python3 scripts/erl_cli_cli.py subscribe <topic> --host HOST --port PORT --token TOKEN
```

---

## Diagnostics quick reference

### Node overview
```bash
eval "erlang:node()"
eval "nodes()"
eval "erlang:system_info(process_count)"
eval "erlang:system_info(port_count)"
eval "erlang:memory()"
eval "erlang:statistics(run_queue)"
```

### Scheduler utilization (two-sample delta)
```bash
eval "erlang:system_flag(scheduler_wall_time, true)"
eval "erlang:statistics(scheduler_wall_time_all)"
# wait a few seconds, then sample again and compute delta
```

### Process diagnostics
```bash
# Top N by memory / reductions / message queue / binary
eval "recon:proc_count(memory, 10)"
eval "recon:proc_count(reductions, 10)"
eval "recon:proc_count(message_queue_len, 10)"
eval "recon:proc_count(binary, 10)"

# Sliding window (delta over ~1s)
eval "recon:proc_window(memory, 10, 1000)"
eval "recon:proc_window(reductions, 10, 1000)"

# Process details by pid string
eval "erlang:process_info(list_to_pid(\"<0.123.0>\"))"

# Binary leak detection
eval "recon:bin_leak(10)"
```

### Network / ports
```bash
eval "recon:inet_count(oct, 10)"
eval "recon:inet_count(recv_cnt, 10)"
eval "recon:inet_window(oct, 10, 1000)"
```

### Memory allocators
```bash
eval "recon_alloc:memory(usage)"
eval "recon_alloc:memory(allocated)"
eval "recon_alloc:average_block_sizes(current)"
eval "recon_alloc:cache_hit_rates()"
```

### ETS tables
```bash
eval "[{T, ets:info(T, name), ets:info(T, memory), ets:info(T, size)} || T <- ets:all()]"
```

### Applications
```bash
eval "application_controller:info()"
eval "application:which_applications()"
```

### IO / GC / reductions
```bash
eval "erlang:statistics(io)"
eval "erlang:statistics(garbage_collection)"
eval "erlang:statistics(reductions)"
eval "erlang:statistics(context_switches)"
```

### Log level (restore when done)
```bash
eval "logger:set_primary_config(level, debug)"
eval "logger:set_primary_config(level, warning)"
```

### Remote shell commands
```bash
eval "os:cmd(\"tail -50 /app/log/app.log\")"
eval "os:cmd(\"df -h\")"
eval "os:cmd(\"ps aux | grep beam\")"
```

---

## Hot-reload workflow

```bash
# 1. Compile locally
rebar3 compile

# 2. Upload and reload
python3 scripts/erl_cli_cli.py load _build/default/lib/APP/ebin/MOD.beam \
    --host HOST --port PORT --token TOKEN

# 3. Verify
python3 scripts/erl_cli_cli.py eval "MOD:module_info(compile)" \
    --host HOST --port PORT --token TOKEN
```

---

## Pub/Sub

```bash
# Agent subscribes and waits for pushed events
python3 scripts/erl_cli_cli.py subscribe alerts --host HOST --port PORT --token TOKEN
# Output: {"topic": "alerts", "data": {...}}

# Application pushes (Erlang side)
# erl_cli_pubsub:publish(<<"alerts">>, #{<<"msg">> => <<"disk full">>}).
```

---

## Connection troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `401 unauthorized` | wrong token or debug disabled | run `erl_cli:start("token")` on the node |
| connect timeout | wrong host/port | check `erl_cli.port` config |
