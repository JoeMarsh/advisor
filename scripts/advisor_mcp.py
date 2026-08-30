from __future__ import annotations

import argparse
import glob as glob_module
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from advisor_common import plugin_root, record_advice


def emit(message: dict[str, Any]) -> None:
    # MCP stdio is JSON. ASCII escapes keep the wire encoding deterministic on
    # Windows hosts whose redirected stdout otherwise defaults to a legacy code page.
    sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def safe_path(cwd: Path, raw: str) -> Path:
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError as error:
        raise ValueError("Path is outside the watched session cwd") from error
    return resolved


def tool_specs() -> list[dict[str, Any]]:
    advise_description = (plugin_root() / "prompts" / "advise-tool.md").read_text(encoding="utf-8").strip()
    return [
        {
            "name": "advise",
            "description": advise_description,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "One concrete piece of advice for the agent you are watching. Terse, specific, actionable.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["nit", "concern", "blocker"],
                        "description": "How strongly to weigh this. Omit for a plain nit.",
                    },
                },
                "required": ["note"],
                "additionalProperties": False,
            },
        },
        {
            "name": "read",
            "description": "Read a UTF-8 text file in the watched session cwd.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "limit": {"type": "integer"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "grep",
            "description": "Search UTF-8 text files in the watched session cwd with a regular expression.",
            "inputSchema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
        {
            "name": "glob",
            "description": "List paths in the watched session cwd matching a glob.",
            "inputSchema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    ]


def text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def call_tool(name: str, args: dict[str, Any], options: argparse.Namespace) -> dict[str, Any]:
    cwd = Path(options.cwd).resolve()
    if name == "advise":
        update_id = options.update_id
        in_progress = options.in_progress
        if options.runtime_file:
            runtime_path = Path(options.runtime_file)
            try:
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return text_result("Advisor update is no longer active; advice was not delivered.", is_error=True)
            update_id = str(runtime.get("update_id") or "")
            in_progress = bool(runtime.get("in_progress"))
        if not update_id:
            return text_result("Advisor update is no longer active; advice was not delivered.", is_error=True)
        response = record_advice(
            Path(options.session_dir), update_id, str(args.get("note", "")),
            args.get("severity"), in_progress,
        )
        return text_result(response)
    if name == "read":
        path = safe_path(cwd, str(args["path"]))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("start", 1)))
        limit = min(2000, max(1, int(args.get("limit", 400))))
        selected = lines[start - 1:start - 1 + limit]
        return text_result("\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start)))
    if name == "glob":
        pattern = str(args["pattern"])
        limit = min(1000, max(1, int(args.get("limit", 200))))
        matches = []
        for raw in glob_module.iglob(str(cwd / pattern), recursive=True):
            path = safe_path(cwd, raw)
            matches.append(path.relative_to(cwd).as_posix())
            if len(matches) >= limit:
                break
        return text_result("\n".join(matches))
    if name == "grep":
        expression = re.compile(str(args["pattern"]))
        file_glob = str(args.get("glob", "**/*"))
        limit = min(1000, max(1, int(args.get("limit", 200))))
        matches: list[str] = []
        for raw in glob_module.iglob(str(cwd / file_glob), recursive=True):
            path = safe_path(cwd, raw)
            if not path.is_file():
                continue
            try:
                for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                    if expression.search(line):
                        matches.append(f"{path.relative_to(cwd).as_posix()}:{line_number}:{line}")
                        if len(matches) >= limit:
                            return text_result("\n".join(matches))
            except OSError:
                continue
        return text_result("\n".join(matches))
    return text_result(f"Unknown tool: {name}", is_error=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--update-id")
    parser.add_argument("--runtime-file")
    parser.add_argument("--in-progress", action="store_true")
    options = parser.parse_args()
    if not options.update_id and not options.runtime_file:
        parser.error("one of --update-id or --runtime-file is required")

    for line in sys.stdin:
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codex-advisor", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": tool_specs()}
            elif method == "tools/call":
                params = request.get("params", {})
                result = call_tool(params.get("name", ""), params.get("arguments", {}), options)
            elif method == "ping":
                result = {}
            elif method in {"notifications/initialized", "notifications/cancelled"}:
                continue
            else:
                if request_id is None:
                    continue
                emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
                continue
            if request_id is not None:
                emit({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as error:
            request_id = locals().get("request", {}).get("id") if isinstance(locals().get("request"), dict) else None
            if request_id is not None:
                emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(error)}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
