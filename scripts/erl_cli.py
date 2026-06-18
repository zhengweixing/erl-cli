#!/usr/bin/env python3
"""
erl_cli.py — CLI client for the erl_cli WebSocket debug channel.

Connects to a running Erlang node that has erl_cli loaded, and sends
commands over WebSocket. The server authenticates via ?token=<token>.

Commands:
  eval       Evaluate an Erlang expression and print the result.
  trace      Start a redbug trace and stream formatted trace messages.
  stop       Stop a running redbug trace.
  load       Upload a .beam file and hot-reload it into the remote VM via
             code:load_binary/3. The beam is base64-encoded and sent over
             WebSocket — no file transfer or SSH required. The module is
             live immediately after the server responds with {"result":"ok"}.
  subscribe  Subscribe to a pub/sub topic and stream server-pushed messages
             until Ctrl-C. The server can push to subscribers via
             erl_cli_pubsub:publish(Topic, Data) from application code.
             Useful for receiving alerts, events, or agent triggers.

Usage:
    python erl_cli.py eval "erlang:node()" --host HOST --port PORT --token TOKEN
    python erl_cli.py trace "module:function/arity" --host HOST --port PORT --token TOKEN
    python erl_cli.py stop --host HOST --port PORT --token TOKEN
    python erl_cli.py load path/to/my_module.beam --host HOST --port PORT --token TOKEN
    python erl_cli.py subscribe alerts --host HOST --port PORT --token TOKEN

Hot-reload workflow:
    1. Edit and compile locally:  rebar3 compile
    2. Upload and reload:         python erl_cli.py load _build/default/lib/APP/ebin/MOD.beam ...
    3. Verify:                    python erl_cli.py eval "MOD:module_info(compile)" ...

Protocol (JSON over WebSocket):
  Eval request:      {"id": "<8-hex>", "code": "<erlang expression>"}
  Load request:      {"id": "<8-hex>", "beam": "<module>", "data": "<base64 beam>"}
  Subscribe request: {"id": "<8-hex>", "subscribe": "<topic>"}
  Unsubscribe:       {"id": "<8-hex>", "unsubscribe": "<topic>"}
  Response:          {"id": "<8-hex>", "result": "..."}          -- success
                     {"id": "<8-hex>", "error": "..."}           -- failure
                     {"id": "<8-hex>", "status": "started", ...} -- trace started
                     {"id": "<8-hex>", "trace": "..."}           -- trace event
                     {"id": "<8-hex>", "done": true}             -- trace finished
  Push (no id):      {"topic": "<topic>", "data": ...}           -- pub/sub push
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import uuid
from typing import Iterator, List, Optional

import websocket  # websocket-client


class _Client:
    """WebSocket client that multiplexes requests by id."""

    def __init__(self, host: str, port: int, path: str, token: str,
                 connect_timeout: float = 8.0, default_timeout: float = 10.0):
        # WebSocket URL with token auth via query parameter
        self.url = f"ws://{host}:{port}{path}?token={token}"
        self.connect_timeout = connect_timeout
        self.default_timeout = default_timeout
        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = threading.Event()
        self._connect_error: Optional[Exception] = None
        # per-request response queues, keyed by message id
        self._queues: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *_):
        if self._ws:
            self._ws.close()

    def _connect(self):
        ws = websocket.WebSocketApp(self.url,
            on_open=lambda ws: self._connected.set(),
            on_message=self._on_message,
            on_error=lambda ws, e: (setattr(self, '_connect_error', e), self._connected.set()))
        self._ws = ws
        threading.Thread(target=ws.run_forever, daemon=True).start()
        if not self._connected.wait(timeout=self.connect_timeout):
            raise ConnectionError(f"connect timeout: {self.url}")
        if self._connect_error:
            raise ConnectionError(str(self._connect_error))

    def subscribe(self, topic: str) -> Iterator[str]:
        """Subscribe to a topic and yield pushed messages until interrupted."""
        mid = uuid.uuid4().hex[:8]
        q = self._reg(mid)
        self._ws.send(json.dumps({"id": mid, "subscribe": topic}))
        # Wait for subscription ack
        ack = self._wait(q, self.connect_timeout)
        if "error" in ack:
            self._unreg(mid)
            raise RuntimeError(f"subscribe error: {ack['error']}")
        # Stream incoming push messages for this topic via a separate listener.
        # The server sends {"topic": "<topic>", "data": "..."} with no id,
        # so we use a dedicated topic queue registered under the topic key.
        self._unreg(mid)
        tq: queue.Queue = queue.Queue()
        with self._lock:
            self._queues[f"topic:{topic}"] = tq
        try:
            while True:
                item = tq.get()  # block indefinitely until a message arrives
                yield item
        finally:
            with self._lock:
                self._queues.pop(f"topic:{topic}", None)
            self._ws.send(json.dumps({"id": uuid.uuid4().hex[:8], "unsubscribe": topic}))

    def _on_message(self, ws, message):
        # Route incoming message to the matching per-request queue.
        # Push messages from pubsub have {"topic": "...", "data": "..."}
        # and are routed to a queue keyed by "topic:<name>".
        try:
            item = json.loads(message)
        except Exception:
            return
        if "topic" in item and "id" not in item:
            key = f"topic:{item['topic']}"
            with self._lock:
                q = self._queues.get(key)
            if q:
                q.put(item)
            return
        msg_id = item.get("id")
        with self._lock:
            q = self._queues.get(msg_id)
        if q:
            q.put(item)

    def _send(self, msg_id: str, code: str):
        self._ws.send(json.dumps({"id": msg_id, "code": code}))

    def _wait(self, q: queue.Queue, timeout: float) -> dict:
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no response within {timeout}s")

    def _reg(self, msg_id: str) -> queue.Queue:
        """Register a response queue for a new request id."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues[msg_id] = q
        return q

    def _unreg(self, msg_id: str):
        with self._lock:
            self._queues.pop(msg_id, None)

    def eval(self, code: str, timeout: Optional[float] = None) -> str:
        """Send an Erlang expression and return the result as a string."""
        mid = uuid.uuid4().hex[:8]
        q = self._reg(mid)
        self._send(mid, code)
        item = self._wait(q, timeout or self.default_timeout)
        self._unreg(mid)
        if "error" in item:
            raise RuntimeError(f"eval error: {item['error']}")
        return item.get("result", "")

    def load(self, module: str, beam_path: str, timeout: Optional[float] = None) -> str:
        """Upload a .beam file and hot-reload it into the remote VM."""
        import base64
        with open(beam_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        mid = uuid.uuid4().hex[:8]
        q = self._reg(mid)
        self._ws.send(json.dumps({"id": mid, "beam": module, "data": data}))
        item = self._wait(q, timeout or self.default_timeout)
        self._unreg(mid)
        if "error" in item:
            raise RuntimeError(f"load error: {item['error']}")
        return item.get("result", "")

    def trace(self, pattern: str, opts: Optional[List[tuple]] = None,
              timeout: Optional[float] = None) -> Iterator[str]:
        """Start a redbug trace and yield formatted trace lines until done."""
        # Build redbug options list as an Erlang term string
        opts_str = "[" + ",".join(f"{{{k},{v}}}" for k, v in (opts or [])) + "]"
        mid = uuid.uuid4().hex[:8]
        q = self._reg(mid)
        self._send(mid, f'redbug:start("{pattern}", {opts_str})')
        # Wait for the "started" ack before streaming events
        item = self._wait(q, self.connect_timeout)
        if "error" in item:
            self._unreg(mid)
            raise RuntimeError(f"trace error: {item['error']}")
        yield f"% trace started: {item.get('procs','?')} procs, {item.get('funs','?')} funs\n"
        try:
            while True:
                item = self._wait(q, timeout or self.default_timeout)
                if item.get("done"):
                    yield "% trace done\n"
                    break
                if "error" in item:
                    yield f"% error: {item['error']}\n"
                    break
                if "trace" in item:
                    yield item["trace"]
        finally:
            self._unreg(mid)


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_conn_args(p):
    """Add common connection arguments to a subcommand parser."""
    p.add_argument("--host",  default="127.0.0.1", help="server host (default: 127.0.0.1)")
    p.add_argument("--port",  type=int, default=8080, help="server port (default: 8080)")
    p.add_argument("--path",  default="/debug", help="WebSocket path (default: /debug)")
    p.add_argument("--token", default="", help="auth token")


def cmd_eval(args):
    """Evaluate a single Erlang expression and print the result."""
    with _Client(args.host, args.port, args.path, args.token) as c:
        print(f"{{{args.code}}} => {c.eval(args.code, timeout=args.timeout)}")


def cmd_trace(args):
    """Start a redbug trace and stream events until the trace finishes."""
    opts = [("time", args.time), ("msgs", args.msgs)]
    with _Client(args.host, args.port, args.path, args.token) as c:
        for line in c.trace(args.pattern, opts=opts, timeout=args.time / 1000 + 5):
            print(f"{{{args.pattern}}} => {line}", end="", flush=True)


def cmd_subscribe(args):
    """Subscribe to a topic and print pushed messages until Ctrl-C."""
    print(f"Subscribing to topic '{args.topic}' (Ctrl-C to stop)...")
    with _Client(args.host, args.port, args.path, args.token) as c:
        try:
            for item in c.subscribe(args.topic):
                print(json.dumps(item), flush=True)
        except KeyboardInterrupt:
            pass


def cmd_load(args):
    """Upload a .beam file and hot-reload it on the remote node."""
    import os
    beam_path = args.beam
    module = args.module or os.path.splitext(os.path.basename(beam_path))[0]
    with _Client(args.host, args.port, args.path, args.token) as c:
        print(f"{{code:load_binary({module}, ...)}} => {c.load(module, beam_path, timeout=args.timeout)}")


def cmd_stop(args):
    """Stop the currently running redbug trace."""
    with _Client(args.host, args.port, args.path, args.token) as c:
        print(f"{{redbug:stop()}} => {c.eval('redbug:stop()', timeout=5)}")


def main():
    parser = argparse.ArgumentParser(description="erl_cli CLI — remote Erlang debug over WebSocket")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_eval = sub.add_parser("eval", help="evaluate an Erlang expression")
    add_conn_args(p_eval)
    p_eval.add_argument("code", help="Erlang expression, e.g. erlang:node()")
    p_eval.add_argument("--timeout", type=float, default=10.0, help="response timeout in seconds (default: 10.0)")

    p_trace = sub.add_parser("trace", help="start a redbug trace")
    add_conn_args(p_trace)
    p_trace.add_argument("pattern", help="trace pattern, e.g. module:function/arity")
    p_trace.add_argument("--time",  type=int, default=30000, help="trace duration in ms (default: 30000)")
    p_trace.add_argument("--msgs",  type=int, default=100,   help="max trace messages (default: 100)")

    p_sub = sub.add_parser("subscribe", help="subscribe to a topic and stream pushed messages")
    add_conn_args(p_sub)
    p_sub.add_argument("topic", help="topic name to subscribe to")

    p_stop = sub.add_parser("stop", help="stop the redbug trace")
    add_conn_args(p_stop)

    p_load = sub.add_parser("load", help="hot-reload a .beam file on the remote node")
    add_conn_args(p_load)
    p_load.add_argument("beam", help="path to the .beam file")
    p_load.add_argument("--module", default="", help="module name (inferred from filename if omitted)")
    p_load.add_argument("--timeout", type=float, default=10.0)

    args = parser.parse_args()
    try:
        {"eval": cmd_eval, "trace": cmd_trace, "stop": cmd_stop, "load": cmd_load, "subscribe": cmd_subscribe}[args.cmd](args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
