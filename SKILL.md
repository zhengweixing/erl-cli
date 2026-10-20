---
name: erl-cli
description: Remote Erlang node diagnostics over WebSocket. Use erl_cli.py to eval expressions, run redbug traces (sync or async-to-file), collect recon/observer diagnostics, hot-reload beam files, download files, and subscribe to server-pushed topics.
---

> **Agent instructions**:
> 1. All remote operations MUST use `scripts/erl_cli.py`.
> 2. Before any operation, ask the user for: **host**, **port**, **token** (and `--path` if the deployment is not the default `/debug`). Optionally ask for a **source/docs path** to aid analysis; the user may answer "none".
> 3. After connecting, run the `Node overview` checks first to confirm the current node and cluster topology.

**CLI tool**: `scripts/erl_cli.py`
**Dependency**: `pip install websocket-client`

To avoid repetition, set shell variables once per session; `...` below stands for `--host $HOST --port $PORT --token $TOKEN`:

```bash
HOST=127.0.0.1; PORT=8888; TOKEN=your_secret_token
```

---

## Capabilities

| Capability | Subcommand | Purpose |
|---|---|---|
| **eval** | `eval` | Evaluate any Erlang expression (state / logic / log level) |
| **trace** | `trace` / `trace-result` / `stop` | redbug tracing (real args / return values / call stack) |
| **hot-reload** | `load` | Upload a `.beam` and hot-load it, no restart |
| **download** | `download` | Pull remote log / config / dump files to local |
| **pub/sub** | `subscribe` | Stream server-pushed topic messages |

---

## Commands

```bash
# Eval
python3 scripts/erl_cli.py eval "<expr>" --host $HOST --port $PORT --token $TOKEN

# Trace (sync, single function) — streams until done or Ctrl-C
python3 scripts/erl_cli.py trace "mod:fun/arity" ... --time 30000 --msgs 100

# Trace multiple functions in one session
python3 scripts/erl_cli.py trace "mod1:fun1/2" "mod2:fun2/3" ... --time 20000

# Async trace (non-blocking: background worker collects to a file, returns once attach confirmed)
python3 scripts/erl_cli.py trace "mod:fun/arity" --async ... --time 60000
python3 scripts/erl_cli.py trace "mod:fun/arity" --async -o /tmp/trace.log ... --time 60000

# Inspect what an async trace has collected so far (safe mid-trace)
python3 scripts/erl_cli.py trace-result -f /tmp/trace.log

# Stop trace
python3 scripts/erl_cli.py stop ...

# Hot-reload beam (module name inferred from filename)
python3 scripts/erl_cli.py load path/to/mod.beam ...

# Download a remote file
python3 scripts/erl_cli.py download /app/log/app.log -o ./app.log ...

# Subscribe to a topic (streams until Ctrl-C)
python3 scripts/erl_cli.py subscribe <topic> ...
```

> **Defaults**: `--time` defaults to `30000` ms, `--msgs` to `100`. redbug stops when **either** condition is met first (timeout or message cap). For rare/intermittent issues raise `--msgs` (e.g. 500) so it isn't truncated right after triggering.
> redbug is a **global singleton** — only one trace session at a time. Always `stop` before starting a new trace.

---

## Tracing

redbug is a safe wrapper over Erlang trace BIFs: it self-monitors and stops itself on overload, so it is safe in production.

### Confirm the function exists first

A wrong module/arity or a non-existent function matches **0 functions** and silently captures nothing (`% trace started: N procs, 0 funs`). Verify first:

```bash
# Confirm a function (incl. private ones, which are not in exports -> check functions) exists with its arity
python3 scripts/erl_cli.py eval "lists:member({handle,2}, my_mod:module_info(functions))" ...
```

If `funs` is `0` in `% trace started`, the mfa is almost certainly wrong.

### RTP match patterns (the core of redbug)

A pattern is a **Restricted Trace Pattern**: `"<mfa> when <guards> -> <actions>"` (the `when` and `->` parts are optional).

**① `<mfa>` — which function / which args**, coarse to fine:

| Form | Meaning |
|---|---|
| `"my_mod"` | **all functions** of `my_mod` |
| `"my_mod:open_session"` | **all arities** of that function |
| `"my_mod:open_session/4"` | a specific arity |
| `"my_mod:open_session(_, _, _, true)"` | **match by argument value**: only when the 4th arg is `true` |

Argument positions may be literals (`true`, `<<"abc">>`, `{ok,_}`), wildcard `_`, or uppercase variables (`X`, usable in a guard).

**② `when <guards>` — conditions on bound variables** (same guards as match specs: `==`, `=/=`, `>`, `<`, `is_atom/1`, `is_integer/1`, `is_tuple/1`, `andalso`, `orelse`, ...):

```bash
python3 scripts/erl_cli.py eval ...   # (example patterns)
python3 scripts/erl_cli.py trace "ets:lookup(ac_tab, X) when not is_tuple(X)" ...
python3 scripts/erl_cli.py trace "my_keepalive:interval(alive_timer, K) when K > 0" ...
```

**③ `-> <actions>` — what to do on a match** (two actions, separated by `;`):

| action | effect |
|---|---|
| `return` | print the **return value** (the CLI appends this automatically) |
| `stack` | print the **call stack** — answers "who called this function" |

> **⚠️ Writing `stack` correctly**: because the CLI auto-appends `->return` to any pattern lacking an action, write stack as **`->return;stack`** (no spaces around `->`):
> - ✅ `"my_mod:discard_session/1->return;stack"`
> - ❌ `"...->stack"` (becomes `->stack->return`, syntax error)
> - ❌ `"... -> return, stack"` (spaced `->` gets a second `->return` appended, syntax error)

**Combined example** (arg match + guard + return + stack):

```bash
python3 scripts/erl_cli.py trace "my_mod:open_session(_, _, _, true)->return;stack" ... --time 60000
```

### Sync vs async — and ALWAYS remind the user to trigger

> **⚠️ Agent must know**: after a trace attaches successfully it has only **installed the probe** — it captures nothing until the traced calls actually happen. Once attach is confirmed you **must** tell the user to go trigger the behaviour (reconnect the device / send a message / reproduce the issue), and show them a table of the attached functions. For async traces also show the trace file path.
> If nothing is triggered within the `--time` / `--msgs` window, the trace runs empty to timeout.

- **Sync (default)**: the command blocks until `--time` ends. Use for short `--time` (≤10s) when you can trigger immediately. The `% trace started` line and any error print to stdout on the spot.
- **`--async`**: for long `--time` or when the user must trigger manually. The command does **not** blindly return — it **synchronously waits until the server confirms attach** (`% trace started: N procs, M funs` with **funs > 0**) before printing success and returning; collection then continues in a background worker writing to a file, not blocking the agent. Therefore:
  - **Exit code is trustworthy**: `exit 0` = really attached; attach failure returns a **non-zero** exit synchronously, no need to re-read the file to confirm:
    - `exit 1`: connection failed / wrong token / node unreachable
    - `exit 2`: wrong mfa/arity or function not found (`funs=0`)
  - Only on `exit 0` should you remind the user to trigger.
  - Once triggered, **no need to wait the full `--time`** — use `trace-result -f <file>` any time to inspect and analyse what was collected, then `stop` when done.

Example output of `trace-result` (`->return;stack`, one hit = call line + stack + return line):

```
% 11:00:11 <0.18319.0>({my_conn,init,4})               <- call: process + trigger point
% my_mod:delete(<<"child101">>)                         <- function + actual args
%   my_sub_mgt:process_child_offline/1                  ┐
%   my_sub_mgt:process_child_offline/3                  │ stack: who called in (top-down)
%   my_sub_mgt:handle/2                                 ┘
% 11:00:11 <0.18319.0>({my_conn,init,4})
% my_mod:delete/1 -> ok                                 <- return value
```

With only `->return` (default) there is no middle `stack` section — just the call line and the `-> return` line.

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

# Binary leak detection (triggers GC — low-peak only, do not call repeatedly)
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

> **⚠️ eval expressions must be single-line.** Join statements with `,`; use list comprehensions (`[X || X <- L]`) instead of multi-line `lists:foreach`.

---

## Hot-reload workflow

> **⚠️ Before hot-reloading**: list the modules to reload and assess the risk; ask the user to confirm before reloading — never reload automatically. After reload, verify the new code is live.

The server runs `code:purge(Module)` then `code:load_binary/3` — no disk write.

```bash
# 1. Compile locally
rebar3 compile

# 2. Upload and reload
python3 scripts/erl_cli.py load _build/default/lib/APP/ebin/MOD.beam ...
# Expected: {code:load_binary(MOD, ...)} => hot reload success: MOD (12345 bytes)

# 3. Verify
python3 scripts/erl_cli.py eval "MOD:module_info(compile)" ...
```

> Only suitable for **pure function changes**. Changing `-record` definitions or `-export` signatures may require updating process state. `code:purge` immediately kills processes still running old code — confirm no long-lived process depends on the old version.

---

## File download

Pull a remote file (log / config / dump) to the local machine. Transferred base64-encoded over WebSocket — **not for very large files** (keep < ~50MB).

```bash
# Optional: confirm it exists and its size first
python3 scripts/erl_cli.py eval "filelib:is_regular(\"/app/log/app.log\")" ...
python3 scripts/erl_cli.py eval "filelib:file_size(\"/app/log/app.log\")" ...

# Download (-o local path; default: remote filename in cwd)
python3 scripts/erl_cli.py download /app/log/app.log -o ./app.log ...
# Expected: ✓ downloaded: /app/log/app.log (234567 bytes) → ./app.log

# Large files: raise the timeout (seconds, default 30)
python3 scripts/erl_cli.py download /app/log/large.log --timeout 60 ...
```

---

## Server-side file trace (RPC-launched, no WebSocket client)

When you reach the node directly (local shell, `./bin/<app> rpc`, or a container), you can start a trace that writes to a file on the node instead of streaming over WebSocket:

```erlang
%% Start; stops when time or msgs is reached first
erl_cli:start_trace(["my_worker:handle/2->return;stack"],
                    "/tmp/erl_cli_trace.log",
                    [{time, 60000}, {msgs, 100}]).

%% ... trigger the behaviour, then read the file:  tail -100 /tmp/erl_cli_trace.log

%% Stop early and close the file
erl_cli:stop_trace().
```

`erl_cli:start_trace/3` takes the trace pattern(s), a node-local file path and redbug options. On success it returns `{ok, Procs, Funs, File}`; it errors if a trace is already running, the file can't be opened, or the pattern is invalid.

---

## Pub/Sub

```bash
# Agent subscribes and waits for pushed events
python3 scripts/erl_cli.py subscribe alerts ...
# Output: {"topic": "alerts", "data": {...}}

# Application pushes (Erlang side)
# erl_cli_pubsub:publish(<<"alerts">>, #{<<"msg">> => <<"disk full">>}).
```

---

## Troubleshooting flow

```
1. Clarify the fault: symptom, device/interface/time involved.
2. Locate the logic in source: read code, map the call chain and branches.
3. Can conclude -> state it. Can't -> combine sources: redbug trace a few key
   functions (remind the user to trigger after attach), download & inspect logs,
   raise the debug log level and reproduce.
4. Analyse the trace result; if inconclusive, re-trace (other functions / arg
   match / add stack).
5. Use eval any time to verify logic (feed real args from the trace back into
   the function); trace and eval can cross-check -> root cause.
```

After root cause: report symptom + root cause + involved code, ask whether to fix; on agreement, fix, then ask whether to hot-reload; compile locally, `load`, verify with eval (re-trace if needed).

---

## Troubleshooting (tool itself)

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: websocket` | `pip install websocket-client` |
| connect timeout / refused | wrong host/port, or debug channel not open; confirm `erl_cli` started and the port is reachable (`--path` defaults to `/debug`, pass explicitly if non-default) |
| `401 unauthorized` / no response | wrong token; set via `erl_cli.token` config or `erl_cli:start("token")` |
| `% trace started: N procs, 0 funs` | wrong mfa/arity or function missing (see "Confirm the function exists first") |
| async trace attach failed | CLI already checked attach synchronously: `exit 1` = connection/token/network, `exit 2` = function missing / `0 funs`. Follow stderr: `stop` first, fix, re-attach |
| re-trace errors / captures nothing | redbug is a global singleton; a previous trace is still running — `stop` first |
| eval `syntax error` | expression was multi-line: make it single-line / use a list comprehension |
