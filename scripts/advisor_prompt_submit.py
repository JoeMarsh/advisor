from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_LINK = re.compile(
    r"^\s*\[\$advisor:advisor\]\([^\r\n)]*[/\\]skills[/\\]advisor[/\\]SKILL\.md\)"
    r"(?:\s+(.*?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
TEXT_COMMAND = re.compile(r"^\s*[/$]advisor(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)
REASONING_LEVELS = {"low", "medium", "high", "xhigh", "max", "ultra"}


def parse_advisor_invocation(prompt: str) -> str | None:
    for pattern in (SKILL_LINK, TEXT_COMMAND):
        match = pattern.fullmatch(prompt)
        if match:
            return (match.group(1) or "").strip()
    return None


def control_arguments(remainder: str) -> tuple[list[str] | None, str | None]:
    if not remainder:
        return ["toggle"], None
    try:
        parts = shlex.split(remainder, posix=True)
    except ValueError as error:
        return None, f"Invalid Advisor command: {error}"
    if not parts:
        return ["toggle"], None
    command = parts[0].lower()
    if command in {"on", "off", "status", "dump", "usage", "reset"} and len(parts) == 1:
        return [command], None
    if command in {"model", "configure"} and 2 <= len(parts) <= 3:
        arguments = ["configure", "--model", parts[1]]
        if len(parts) == 3:
            reasoning = parts[2].lower()
            if reasoning not in REASONING_LEVELS:
                return None, f"Invalid Advisor reasoning level: {parts[2]}"
            arguments.extend(["--reasoning", reasoning])
        return arguments, None
    return None, (
        "Unknown Advisor command. Use /advisor to toggle, or add on, off, status, "
        "dump, usage, feed, reset, or model <model> [reasoning]."
    )


def hook_output(result: str) -> dict[str, Any]:
    return {
        "systemMessage": f"Advisor control\n{result}",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "The Advisor UserPromptSubmit hook already consumed and executed this control "
                "invocation. Do not run advisor_ctl.py and do not perform any other task from "
                "this prompt. Reply concisely with the control result below.\n\n" + result
            ),
        },
    }


def feed_hook_output(session_id: str) -> dict[str, Any]:
    return {
        "systemMessage": "Opening the read-only Advisor feed.",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "The user requested the Advisor feed. Call the `show_advisor_feed` tool from "
                "the `advisor-feed` MCP server with the session_id below, then reply only with "
                "a concise confirmation. Do not run advisor_ctl.py or do unrelated work.\n\n"
                f"session_id: {session_id}"
            ),
        },
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return 0
    remainder = parse_advisor_invocation(prompt)
    if remainder is None:
        return 0
    if remainder.strip().lower() == "feed":
        session_id = str(
            payload.get("session_id")
            or payload.get("sessionId")
            or payload.get("thread_id")
            or ""
        )
        json.dump(feed_hook_output(session_id), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    arguments, error = control_arguments(remainder)
    if error:
        result = error
    else:
        assert arguments is not None
        script = Path(__file__).with_name("advisor_ctl.py")
        completed = subprocess.run(
            [sys.executable, str(script), *arguments],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        result = (completed.stdout if completed.returncode == 0 else completed.stderr).strip()
        if not result:
            result = f"Advisor control exited with code {completed.returncode}."
    json.dump(hook_output(result), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
