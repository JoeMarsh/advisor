from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from advisor_process import terminate_process_tree


DEFAULT_IDLE_TIMEOUT_SECONDS = 3600.0
_STARTED_BROKERS: dict[int, subprocess.Popen[Any]] = {}


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _save_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _send_json(stream: Any, value: dict[str, Any]) -> None:
    stream.write((json.dumps(value, ensure_ascii=True) + "\n").encode("utf-8"))
    stream.flush()


def _connect(state: dict[str, Any], timeout: float = 5.0) -> socket.socket:
    connection = socket.create_connection(
        (str(state.get("host", "127.0.0.1")), int(state["port"])),
        timeout=timeout,
    )
    connection.settimeout(timeout)
    return connection


def broker_is_healthy(state_path: Path, expected_cwd: Path | None = None, expected_script: Path | None = None) -> bool:
    state = _load_state(state_path)
    if not state or not state.get("token") or not state.get("port"):
        return False
    if expected_cwd and os.path.normcase(str(expected_cwd.resolve())) != os.path.normcase(str(state.get("cwd", ""))):
        return False
    if expected_script and os.path.normcase(str(expected_script.resolve())) != os.path.normcase(str(state.get("server_script", ""))):
        return False
    try:
        with _connect(state) as connection:
            stream = connection.makefile("rwb", buffering=0)
            _send_json(stream, {"token": state["token"], "mode": "health"})
            reply = json.loads(stream.readline().decode("utf-8"))
            return bool(reply.get("ok"))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def shutdown_broker(state_path: Path) -> bool:
    state = _load_state(state_path)
    if not state or not state.get("token") or not state.get("port"):
        return False
    try:
        with _connect(state) as connection:
            stream = connection.makefile("rwb", buffering=0)
            _send_json(stream, {"token": state["token"], "mode": "shutdown"})
            reply = json.loads(stream.readline().decode("utf-8"))
            accepted = bool(reply.get("ok"))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
    if not accepted:
        return False
    pid = int(state.get("pid", 0))
    process = _STARTED_BROKERS.pop(pid, None)
    deadline = time.monotonic() + 10.0
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process is not None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    return not _pid_exists(pid)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _detached_process_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def ensure_broker(
    state_path: Path,
    cwd: Path,
    server_script: Path,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> Path:
    state_path = state_path.resolve()
    cwd = cwd.resolve()
    server_script = server_script.resolve()
    if broker_is_healthy(state_path, cwd, server_script):
        return state_path

    state_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    log_path = state_path.with_name("devtools-broker.log")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--broker",
        "--state-file",
        str(state_path),
        "--cwd",
        str(cwd),
        "--server-script",
        str(server_script),
        "--token",
        token,
        "--idle-timeout",
        str(idle_timeout),
    ]
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=cwd,
            **_detached_process_kwargs(),
        )
    _STARTED_BROKERS[process.pid] = process

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        state = _load_state(state_path)
        if state and state.get("token") == token and broker_is_healthy(state_path, cwd, server_script):
            return state_path
        time.sleep(0.05)
    raise RuntimeError(f"Timed out starting Advisor devtools broker; see {log_path}")


def _close_node(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if process.stdin:
            process.stdin.close()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        terminate_process_tree(process)


def _remove_owned_state(state_path: Path, token: str) -> None:
    state = _load_state(state_path)
    if state and state.get("token") == token:
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass


def _handle_broker_client(
    connection: socket.socket,
    node: subprocess.Popen[str],
    token: str,
) -> tuple[bool, bool]:
    """Return (activity, shutdown_requested)."""
    connection.settimeout(15.0)
    stream = connection.makefile("rwb", buffering=0)
    try:
        line = stream.readline()
        if not line:
            return False, False
        hello = json.loads(line.decode("utf-8"))
        if hello.get("token") != token:
            _send_json(stream, {"ok": False, "error": "unauthorized"})
            return False, False
        mode = hello.get("mode")
        if mode == "health":
            _send_json(stream, {"ok": node.poll() is None, "pid": node.pid})
            return True, False
        if mode == "shutdown":
            _send_json(stream, {"ok": True})
            return True, True
        if mode != "proxy":
            _send_json(stream, {"ok": False, "error": "unknown mode"})
            return False, False

        _send_json(stream, {"ok": True, "pid": node.pid})
        connection.settimeout(None)
        while True:
            raw = stream.readline()
            if not raw:
                return True, False
            request = json.loads(raw.decode("utf-8"))
            if node.poll() is not None or not node.stdin or not node.stdout:
                raise RuntimeError("Persistent devtools MCP process exited")
            node.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
            node.stdin.flush()
            if "id" not in request:
                continue
            response = node.stdout.readline()
            if not response:
                raise RuntimeError("Persistent devtools MCP process closed stdout")
            stream.write(response.encode("utf-8"))
            stream.flush()
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        try:
            _send_json(stream, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(error)}})
        except OSError:
            pass
        return True, False


def broker_main(options: argparse.Namespace) -> int:
    state_path = Path(options.state_file).resolve()
    cwd = Path(options.cwd).resolve()
    server_script = Path(options.server_script).resolve()
    node = subprocess.Popen(
        ["node", str(server_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(1.0)
    host, port = server.getsockname()
    _save_state(
        state_path,
        {
            "host": host,
            "port": port,
            "token": options.token,
            "pid": os.getpid(),
            "node_pid": node.pid,
            "cwd": str(cwd),
            "server_script": str(server_script),
            "started_at": time.time(),
        },
    )
    last_activity = time.monotonic()
    try:
        while node.poll() is None and time.monotonic() - last_activity < options.idle_timeout:
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            with connection:
                activity, shutdown = _handle_broker_client(connection, node, options.token)
            if activity:
                last_activity = time.monotonic()
            if shutdown:
                break
    finally:
        server.close()
        _close_node(node)
        _remove_owned_state(state_path, options.token)
    return 0


def proxy_main(state_path: Path) -> int:
    state = _load_state(state_path)
    if not state:
        print(f"Advisor devtools broker state is unavailable: {state_path}", file=sys.stderr)
        return 1
    try:
        with _connect(state, timeout=15.0) as connection:
            stream = connection.makefile("rwb", buffering=0)
            _send_json(stream, {"token": state["token"], "mode": "proxy"})
            hello = json.loads(stream.readline().decode("utf-8"))
            if not hello.get("ok"):
                raise RuntimeError(str(hello.get("error", "broker rejected connection")))
            connection.settimeout(None)
            for raw in sys.stdin.buffer:
                if not raw.strip():
                    continue
                stream.write(raw if raw.endswith(b"\n") else raw + b"\n")
                stream.flush()
                request = json.loads(raw.decode("utf-8"))
                if "id" not in request:
                    continue
                response = stream.readline()
                if not response:
                    raise RuntimeError("Advisor devtools broker closed the connection")
                sys.stdout.buffer.write(response)
                sys.stdout.buffer.flush()
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as error:
        print(f"Advisor devtools proxy failed: {error}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--broker", action="store_true")
    parser.add_argument("--cwd")
    parser.add_argument("--server-script")
    parser.add_argument("--token")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    options = parser.parse_args()
    if options.broker:
        if not options.cwd or not options.server_script or not options.token:
            parser.error("--broker requires --cwd, --server-script, and --token")
        return broker_main(options)
    return proxy_main(Path(options.state_file).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
