# erl_cli

> **For AI agents**: This library provides a remote Erlang debug channel over WebSocket. Use `scripts/erl_cli_cli.py` to interact with a running Erlang node — evaluate expressions, run redbug traces, collect recon diagnostics, hot-reload beam files, and subscribe to server-pushed topics. Always ask the user for `host`, `port`, and `token` before connecting. Start with `eval "erlang:node()"` and `eval "nodes()"` to confirm connectivity and deployment topology.

Remote Erlang debug channel over WebSocket. Supports `eval`, `redbug` tracing, `recon` diagnostics, beam hot-reload, and topic pub/sub.

## Dependencies

- [cowboy](https://github.com/ninenines/cowboy) — HTTP/WebSocket server
- [jiffy](https://github.com/davisp/jiffy) — JSON encode/decode
- [redbug](https://github.com/massemanet/redbug) — safe production tracing
- [recon](https://github.com/ferd/recon) — runtime diagnostics

## Build

```bash
rebar3 compile
```

## Server Setup

### Option A — Built-in listener

Configure `port` and the application starts its own HTTP listener automatically.

**sys.config:**
```erlang
{erl_cli, [
    {debug, true},
    {token,  "your_secret_token"},
    {port,   8888},
    {path,   "/debug"}          %% optional, default: "/debug"
]}
```

**Or at runtime:**
```erlang
erl_cli:start("your_secret_token", 8888).
```

### Option B — Mount into existing cowboy router

```erlang
erl_cli:start("your_secret_token").
```

Add to your cowboy dispatch routes:
```erlang
{"/debug", erl_cli_ws_handler, []}
```

### Stop

```erlang
erl_cli:stop().
```

## Authentication

By default, auth checks `debug = true` and matches the `token` query parameter against the configured token.

To use a custom auth module, set `auth_mod`:

```erlang
{erl_cli, [{auth_mod, your_auth_module}]}
%% or at runtime:
application:set_env(erl_cli, auth_mod, your_auth_module).
```

The module must export:

```erlang
-spec check_auth(cowboy_req:req(), term()) -> boolean().
```

## CLI

**Install dependency:**
```bash
pip install websocket-client
```

**Usage:**
```bash
# Evaluate an Erlang expression
python3 scripts/erl_cli_cli.py eval "erlang:node()" \
    --host 127.0.0.1 --port 8888 --token your_secret_token

# Start a redbug trace
python3 scripts/erl_cli_cli.py trace "module:function/arity" \
    --host 127.0.0.1 --port 8888 --token your_secret_token \
    --time 30000 --msgs 100

# Stop the trace
python3 scripts/erl_cli_cli.py stop \
    --host 127.0.0.1 --port 8888 --token your_secret_token

# Hot-reload a beam file (module name inferred from filename)
python3 scripts/erl_cli_cli.py load _build/default/lib/myapp/ebin/my_module.beam \
    --host 127.0.0.1 --port 8888 --token your_secret_token

# Subscribe to a topic and stream pushed messages (Ctrl-C to stop)
python3 scripts/erl_cli_cli.py subscribe alerts \
    --host 127.0.0.1 --port 8888 --token your_secret_token
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Server host |
| `--port` | `8080` | Server port |
| `--path` | `/debug` | WebSocket path |
| `--token` | `` | Auth token |
| `--timeout` | `10.0` | Response timeout in seconds (eval/load) |
| `--time` | `30000` | Trace duration in ms (trace only) |
| `--msgs` | `100` | Max trace messages (trace only) |
| `--module` | _(filename)_ | Module name override (load only) |

## Hot-reload

Upload and reload a compiled beam file without SSH or node restart:

```bash
# 1. Edit and compile locally
rebar3 compile

# 2. Upload and hot-reload
python3 scripts/erl_cli_cli.py load _build/default/lib/myapp/ebin/my_module.beam \
    --host 127.0.0.1 --port 8888 --token your_secret_token

# 3. Verify
python3 scripts/erl_cli_cli.py eval "my_module:module_info(compile)" \
    --host 127.0.0.1 --port 8888 --token your_secret_token
```

The beam is base64-encoded and sent over WebSocket. The server calls `code:load_binary/3` — no disk write required.

## Pub/Sub

The server can push messages to subscribed clients. Useful for streaming alerts, events, or agent triggers from application code.

**Subscribe from CLI (agent side):**
```bash
python3 scripts/erl_cli_cli.py subscribe alerts \
    --host 127.0.0.1 --port 8888 --token your_secret_token
# outputs: {"topic": "alerts", "data": {...}}
```

**Publish from Erlang (application side):**
```erlang
erl_cli_pubsub:publish(<<"alerts">>, #{<<"level">> => <<"error">>, <<"msg">> => <<"disk full">>}).
```

Multiple clients can subscribe to the same topic. Subscriptions are automatically cleaned up when the WebSocket connection closes.

## Common eval examples

```bash
# Node info
eval "erlang:node()"
eval "nodes()"
eval "erlang:system_info(process_count)"
eval "erlang:memory()"

# recon diagnostics
eval "recon:proc_count(memory, 5)"
eval "recon:proc_count(reductions, 5)"
eval "recon_alloc:memory(usage)"
eval "recon:bin_leak(5)"

# Log level (remember to restore)
eval "logger:set_primary_config(level, debug)"
eval "logger:set_primary_config(level, warning)"

# Shell commands on the remote node
eval "os:cmd(\"tail -50 /app/log/app.log\")"
```

## Configuration reference

| Key | Default | Description |
|-----|---------|-------------|
| `erl_cli.debug` | `false` | Enable the debug channel |
| `erl_cli.token` | `"changeme"` | Auth token |
| `erl_cli.port` | — | If set, starts a dedicated HTTP listener |
| `erl_cli.path` | `"/debug"` | WebSocket route path |
| `erl_cli.auth_mod` | — | Custom auth module (optional) |

## WebSocket Protocol

All messages are JSON. Requests include an `id` field for response correlation.

| Direction | Format | Description |
|-----------|--------|-------------|
| client → server | `{"id":"<hex>","code":"<expr>"}` | Eval expression |
| client → server | `{"id":"<hex>","beam":"<mod>","data":"<base64>"}` | Hot-reload beam |
| client → server | `{"id":"<hex>","subscribe":"<topic>"}` | Subscribe to topic |
| client → server | `{"id":"<hex>","unsubscribe":"<topic>"}` | Unsubscribe from topic |
| server → client | `{"id":"<hex>","result":"..."}` | Eval/load success |
| server → client | `{"id":"<hex>","error":"..."}` | Error response |
| server → client | `{"id":"<hex>","trace":"..."}` | Trace event |
| server → client | `{"id":"<hex>","done":true}` | Trace finished |
| server → client | `{"topic":"<topic>","data":...}` | Pub/sub push (no id) |
