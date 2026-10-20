#!/usr/bin/env python3
"""
erl_cli.py — CLI client for the erl_cli WebSocket debug channel.

Connects to a running Erlang node that has erl_cli loaded, and sends
commands over WebSocket. The server authenticates via ?token=<token>.

Commands:
  eval          Evaluate an Erlang expression and print the result.
  trace         Start a redbug trace and stream formatted trace messages.
                Supports multiple patterns and an --async mode that runs the
                collection in a detached background worker writing to a file.
  trace-result  Print what an --async trace has collected so far.
  stop          Stop a running redbug trace.
  load          Upload a .beam file and hot-reload it into the remote VM via
                code:load_binary/3. The beam is base64-encoded and sent over
                WebSocket — no file transfer or SSH required.
  download      Download a file from the remote node to the local machine
                (log/config/dump files) via file_download:read/1.
  subscribe     Subscribe to a pub/sub topic and stream server-pushed messages
                until Ctrl-C. The server can push to subscribers via
                erl_cli_pubsub:publish(Topic, Data) from application code.

Usage:
    python erl_cli.py eval "erlang:node()" --host HOST --port PORT --token TOKEN
    python erl_cli.py trace "module:function/arity" --host HOST --port PORT --token TOKEN
    python erl_cli.py trace "m1:f1/2" "m2:f2/3" --async -o /tmp/trace.log --host HOST --port PORT --token TOKEN
    python erl_cli.py trace-result -f /tmp/trace.log
    python erl_cli.py stop --host HOST --port PORT --token TOKEN
    python erl_cli.py load path/to/my_module.beam --host HOST --port PORT --token TOKEN
    python erl_cli.py download /app/log/app.log -o ./app.log --host HOST --port PORT --token TOKEN
    python erl_cli.py subscribe alerts --host HOST --port PORT --token TOKEN

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
                     {"id": "<8-hex>", "type": "file_download", "path", "size", "data"}
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

    def download(self, remote_path: str,
                 timeout: Optional[float] = None) -> "tuple[str, int, bytes]":
        """Download a file from the remote node. Returns (path, size, bytes)."""
        import base64
        code = f'file_download:read("{remote_path}")'
        mid = uuid.uuid4().hex[:8]
        q = self._reg(mid)
        self._send(mid, code)
        item = self._wait(q, timeout or self.default_timeout)
        self._unreg(mid)
        if "error" in item:
            raise RuntimeError(f"download error: {item['error']}")
        if item.get("type") != "file_download":
            raise RuntimeError(f"unexpected response: {item}")
        content = base64.b64decode(item["data"])
        return item["path"], item["size"], content

    def trace(self, pattern: "str | List[str]", opts: Optional[List[tuple]] = None,
              timeout: Optional[float] = None) -> Iterator[str]:
        """Start a redbug trace and yield formatted trace lines until done.

        `pattern` may be a single pattern string or a list of patterns; a list
        is sent to the server as an Erlang list so multiple functions are
        traced in one session.
        """
        # Build redbug options list as an Erlang term string
        opts_str = "[" + ",".join(f"{{{k},{v}}}" for k, v in (opts or [])) + "]"
        mid = uuid.uuid4().hex[:8]
        q = self._reg(mid)
        if isinstance(pattern, list):
            pat_str = "[" + ",".join(f'"{p}"' for p in pattern) + "]"
        else:
            pat_str = f'"{pattern}"'
        self._send(mid, f'redbug:start({pat_str}, {opts_str})')
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


def _ensure_return(pattern: str) -> str:
    """Append ->return unless the pattern already carries an action, so trace
    output includes return values. A pattern that already has ->return (e.g.
    "mod:fun/2->return;stack") is left untouched."""
    if "->return" in pattern:
        return pattern
    return pattern + "->return"


def cmd_trace(args):
    """Start a redbug trace. Synchronous by default; --async detaches a worker."""
    opts = [("time", args.time), ("msgs", args.msgs)]
    patterns = [_ensure_return(p) for p in args.pattern]
    pattern = patterns if len(patterns) > 1 else patterns[0]
    label = ", ".join(args.pattern)

    if not args.async_mode:
        # ── Synchronous (default): block until done. The "% trace started"
        #    line and any error print straight to stdout, visible to caller. ──
        with _Client(args.host, args.port, args.path, args.token) as c:
            for line in c.trace(pattern, opts=opts, timeout=args.time / 1000 + 5):
                print(f"{{{label}}} => {line}", end="", flush=True)
        return

    # ── Async: synchronously confirm attach succeeded, then hand collection
    #    to a detached background worker that writes to a file. ──
    import os, re, time, tempfile, subprocess

    out_path = args.output
    if not out_path:
        fd, out_path = tempfile.mkstemp(prefix="erl_cli_trace_", suffix=".log")
        os.close(fd)

    # Re-invoke self as a hidden worker running the synchronous trace to file.
    worker_cmd = [
        sys.executable, os.path.abspath(__file__), "_trace_worker",
        "--host", args.host, "--port", str(args.port),
        "--path", args.path, "--token", args.token,
        "--time", str(args.time), "--msgs", str(args.msgs),
        "--out", out_path,
    ] + list(args.pattern)

    with open(out_path, "w") as f:
        f.write(f"% trace pattern: {label}\n")
        f.write(f"% time={args.time}ms msgs={args.msgs}\n")

    # Detach: don't inherit parent stdio, survive parent exit.
    proc = subprocess.Popen(
        worker_cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Synchronously wait for the attach result by polling the output file:
    # success -> "% trace started: N procs, M funs"; failure -> "% worker error:".
    # This makes "command succeeded" == "server confirmed attach".
    attach_timeout = 20.0
    deadline = time.time() + attach_timeout
    started = None
    worker_err = None
    while time.time() < deadline:
        try:
            content = open(out_path).read()
        except FileNotFoundError:
            content = ""
        m = re.search(r"% trace started: (\S+) procs, (\S+) funs", content)
        if m:
            started = m
            break
        em = re.search(r"% worker error: (.*)", content)
        if em:
            worker_err = em.group(1)
            break
        if proc.poll() is not None:
            content = open(out_path).read() if os.path.exists(out_path) else ""
            em = re.search(r"% worker error: (.*)", content)
            worker_err = em.group(1) if em else f"worker exited early (exit={proc.returncode}), no output"
            break
        time.sleep(0.3)

    if worker_err is not None:
        print(f"% async trace attach failed: {worker_err}", file=sys.stderr)
        print(f"% output file: {out_path}", file=sys.stderr)
        try:
            proc.terminate()
        except Exception:
            pass
        sys.exit(1)

    if started is None:
        try:
            proc.terminate()
        except Exception:
            pass
        print(f"% async trace attach timed out (no server confirmation within {attach_timeout}s)", file=sys.stderr)
        print(f"% likely causes: node unreachable / wrong token / redbug not responding. output file: {out_path}", file=sys.stderr)
        sys.exit(1)

    procs, funs = started.group(1), started.group(2)
    if not funs.isdigit() or int(funs) == 0:
        # funs is not a positive integer: 0 funs / no_matching_functions / etc.
        # — wrong mfa/arity or function does not exist; nothing will be captured.
        try:
            proc.terminate()
        except Exception:
            pass
        print(f"% async trace attach failed: {procs} procs, {funs} funs (wrong mfa/arity or function not found)", file=sys.stderr)
        print(f"% stop first, confirm the function exists, then re-attach:", file=sys.stderr)
        print(f"%   python3 {os.path.basename(__file__)} stop --host {args.host} --port {args.port} --token <token>", file=sys.stderr)
        sys.exit(2)

    # ── Attach confirmed: background worker keeps collecting to the file. ──
    print(f"% async trace started (worker pid={proc.pid})")
    print(f"% trace started: {procs} procs, {funs} funs  <- server confirmed attach")
    print(f"% output file: {out_path}")
    print(f"% trace is attached and collecting in the background — now go trigger the")
    print(f"%   target behaviour (reconnect device / send message / reproduce the issue).")
    print(f"% no need to wait the full {args.time}ms; inspect collected data any time:")
    print(f"%   python3 {os.path.basename(__file__)} trace-result -f {out_path}")
    print(f"% finish collection: python3 {os.path.basename(__file__)} stop --host {args.host} --port {args.port} --token <token>")


def cmd_trace_worker(args):
    """Hidden command: run a synchronous trace in the background, appending
    output to --out. Not intended for direct use."""
    patterns = [_ensure_return(p) for p in args.pattern]
    pattern = patterns if len(patterns) > 1 else patterns[0]
    opts = [("time", args.time), ("msgs", args.msgs)]
    with open(args.out, "a", buffering=1) as f:  # line-buffered for live reads
        try:
            with _Client(args.host, args.port, args.path, args.token) as c:
                for line in c.trace(pattern, opts=opts, timeout=args.time / 1000 + 5):
                    f.write(line)
                    f.flush()
        except Exception as e:
            f.write(f"% worker error: {e}\n")


def cmd_trace_result(args):
    """Print what an --async trace has collected so far (safe to call mid-trace)."""
    try:
        with open(args.file, "r") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"error: trace output file not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    print(content, end="")
    if "% trace done" in content or "% worker error" in content:
        print("% [collection finished]")
    else:
        print("% [collection in progress — keep triggering or check again later]")


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


def cmd_download(args):
    """Download a file from the remote node to the local machine."""
    import os
    remote_path = args.remote_path
    local_path = args.output
    if not local_path:
        local_path = os.path.basename(remote_path) or "downloaded_file"
    with _Client(args.host, args.port, args.path, args.token,
                 connect_timeout=8.0, default_timeout=args.timeout) as c:
        path, size, content = c.download(remote_path, timeout=args.timeout)
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(content)
        print(f"  ✓ downloaded: {path} ({size} bytes) → {local_path}")


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

    p_trace = sub.add_parser("trace", help="start a redbug trace (one or more patterns)")
    add_conn_args(p_trace)
    p_trace.add_argument("pattern", nargs="+",
                         help="trace pattern(s), e.g. mod:fun/arity (multiple allowed)")
    p_trace.add_argument("--time",  type=int, default=30000, help="trace duration in ms (default: 30000)")
    p_trace.add_argument("--msgs",  type=int, default=100,   help="max trace messages (default: 100)")
    p_trace.add_argument("--async", dest="async_mode", action="store_true",
                         help="async mode: detached worker collects to a file, command returns once attach is confirmed")
    p_trace.add_argument("-o", "--output", default="",
                         help="output file for async mode (default: auto temp file)")

    # Hidden worker command for async trace; not for direct use.
    p_worker = sub.add_parser("_trace_worker")
    add_conn_args(p_worker)
    p_worker.add_argument("pattern", nargs="+")
    p_worker.add_argument("--time", type=int, default=30000)
    p_worker.add_argument("--msgs", type=int, default=100)
    p_worker.add_argument("--out", required=True)

    p_tresult = sub.add_parser("trace-result", help="print collected output of an async trace")
    p_tresult.add_argument("-f", "--file", required=True, help="async trace output file path")

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

    p_download = sub.add_parser("download", help="download a file from the remote node")
    add_conn_args(p_download)
    p_download.add_argument("remote_path", help="remote file path, e.g. /app/log/app.log")
    p_download.add_argument("-o", "--output", default="",
                            help="local save path (default: remote filename in cwd)")
    p_download.add_argument("--timeout", type=float, default=30.0,
                            help="download timeout in seconds (default: 30)")

    args = parser.parse_args()
    try:
        {"eval": cmd_eval, "trace": cmd_trace, "stop": cmd_stop, "load": cmd_load,
         "subscribe": cmd_subscribe, "download": cmd_download,
         "_trace_worker": cmd_trace_worker, "trace-result": cmd_trace_result}[args.cmd](args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
