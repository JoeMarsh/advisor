from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from advisor_common import data_root, default_config, format_usage_report, load_config, load_json, save_json
from advisor_process import process_is_running, terminate_pid_tree


def stop_all_workers(root: Path) -> None:
    sessions = root / "sessions"
    for worker_file in sessions.glob("*/worker.json"):
        state = load_json(worker_file, {})
        try:
            pid = int(state.get("pid", 0)) if isinstance(state, dict) else 0
        except (TypeError, ValueError):
            pid = 0
        queue_file = worker_file.parent / "queue.json"
        queue_state = load_json(queue_file, {})
        if not isinstance(queue_state, dict):
            queue_state = {}
        queue_state["shutdown"] = True
        queue_state["updated_at"] = time.time()
        save_json(queue_file, queue_state)
        if process_is_running(pid):
            terminate_pid_tree(pid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the Codex advisor plugin.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("toggle")
    subparsers.add_parser("on")
    subparsers.add_parser("off")
    subparsers.add_parser("dump")
    subparsers.add_parser("usage")
    subparsers.add_parser("reset")
    configure = subparsers.add_parser("configure")
    configure.add_argument("--model")
    configure.add_argument("--reasoning", choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    options = parser.parse_args()

    root = data_root()
    config_path = root / "config.json"
    config = load_config()
    if options.command in {"toggle", "on", "off"}:
        if options.command == "toggle":
            config["enabled"] = not bool(config.get("enabled", True))
        else:
            config["enabled"] = options.command == "on"
        save_json(config_path, config)
        if not config["enabled"]:
            stop_all_workers(root)
    elif options.command == "configure":
        if options.model:
            config["model"] = options.model
        if options.reasoning:
            config["reasoning_effort"] = options.reasoning
        save_json(config_path, config)
    elif options.command == "reset":
        sessions = root / "sessions"
        stop_all_workers(root)
        if sessions.exists():
            shutil.rmtree(sessions)
        print(f"Reset advisor sessions under {sessions}")
        return 0
    elif options.command == "dump":
        values = []
        for runtime in (root / "sessions").glob("*/runtime.json"):
            values.append({"session": runtime.parent.name, "runtime": json.loads(runtime.read_text(encoding="utf-8"))})
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return 0
    elif options.command == "usage":
        usage_files = sorted(
            (root / "sessions").glob("*/usage.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not usage_files:
            print("No Advisor usage has been recorded yet. Start a new task with the plugin enabled.")
            return 0
        latest = usage_files[0]
        print(format_usage_report(load_json(latest, {}), latest.parent.name))
        return 0

    effective = default_config()
    effective.update(config)
    print(json.dumps({"config_path": str(config_path), **effective}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
