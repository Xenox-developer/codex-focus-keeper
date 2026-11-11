#!/usr/bin/python3
"""Install or remove only the user-level Codex hooks belonging to this tool."""

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


SOURCE = Path(__file__).resolve().parent
MANIFEST = "focus-keeper-install.json"
APP_ID = "local.codex.focus-keeper"
EVENTS = ("PreToolUse", "PostToolUse", "Stop", "Interrupt", "SessionEnd", "SubagentStop")
MATCHER = "^mcp__cua_repl__js$"
SOURCES = ("FocusKeeper.swift", "lifecycle.py")


def read_hooks(path):
    raw = path.read_bytes() if path.exists() else None
    config = json.loads(raw) if raw is not None else {}
    if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
        raise ValueError("Unrecognized hooks.json format. No changes made.")
    for event in EVENTS:
        groups = config.get("hooks", {}).get(event, [])
        if not isinstance(groups, list) or any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            for group in groups
        ):
            raise ValueError(f"Unrecognized hook configuration for {event}.")
    return raw, config


def handler_command(directory):
    return "/usr/bin/python3 -I " + shlex.quote(str(directory / "lifecycle.py"))


def change_hooks(config, directory, installing):
    config = copy.deepcopy(config)
    command = handler_command(directory)
    all_hooks = config.setdefault("hooks", {})
    for event in EVENTS:
        groups = all_hooks.get(event, [])
        result = []
        for group in groups:
            handlers = [
                h for h in group["hooks"]
                if not (isinstance(h, dict) and h.get("type") == "command"
                        and h.get("command") == command)
            ]
            if len(handlers) == len(group["hooks"]):
                result.append(group)
            elif handlers:
                result.append(dict(group, hooks=handlers))
        if installing:
            handler = {"type": "command", "command": command, "timeout": 3}
            group = {"hooks": [handler]}
            if event in ("PreToolUse", "PostToolUse"):
                group["matcher"] = MATCHER
            result.append(group)
        if result:
            all_hooks[event] = result
        elif event in all_hooks:
            del all_hooks[event]
    return config


def atomic_write(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        name = file.name
        try:
            os.fchmod(file.fileno(), mode)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            os.unlink(name)
            raise
    try:
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_hooks(path, raw, old, new, remove_empty=False):
    if (path.read_bytes() if path.exists() else None) != raw:
        raise ValueError("Codex hooks changed during this operation. Run the command again.")
    if raw is not None and remove_empty and dict(new) == {"hooks": {}}:
        path.unlink()
    elif new != old:
        data = (json.dumps(new, ensure_ascii=False, indent=2) + "\n").encode()
        atomic_write(path, data, (path.stat().st_mode & 0o777) if path.exists() else 0o600)


def digest(paths):
    digestor = hashlib.sha256()
    for path in paths:
        digestor.update(path.read_bytes())
        digestor.update(b"\0")
    return digestor.hexdigest()


def read_manifest(directory):
    if not directory.exists():
        return None
    marker = directory / MANIFEST
    if not marker.is_file() or directory.is_symlink():
        raise ValueError(f"Directory already exists and is not owned by this installer: {directory}")
    data = json.loads(marker.read_bytes())
    if data.get("id") != APP_ID:
        raise ValueError("Installation directory belongs to another tool.")
    return data


def stop_helper(directory):
    process = subprocess.run(
        ["/usr/bin/python3", "-I", str(directory / "lifecycle.py"), "--stop"],
        timeout=10, stdout=subprocess.DEVNULL,
    )
    if process.returncode:
        raise ValueError("Could not stop the active helper. Try again later.")


def install(directory, hooks_path, raw, old, previous):
    new = change_hooks(old, directory, True)
    fingerprint = digest(SOURCE / n for n in SOURCES)
    if (previous and previous["sourceHash"] == fingerprint
            and os.access(directory / "FocusKeeper", os.X_OK)
            and all((directory / n).is_file() for n in SOURCES)
            and digest(directory / n for n in SOURCES) == fingerprint):
        save_hooks(hooks_path, raw, old, new)
        print("Already installed. Review hook trust in Codex.")
        return
    with tempfile.TemporaryDirectory(dir=directory.parent) as build:
        staged = Path(build) / "focus-keeper"
        staged.mkdir(mode=0o700)
        for name in SOURCES:
            shutil.copyfile(SOURCE / name, staged / name)
        binary = staged / "FocusKeeper"
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", str(staged / "FocusKeeper.swift"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary), "--self-test"], check=True)
        source_config = old.get("hooks", None)
        record = dict(previous) if previous else {
            "id": APP_ID, "createdHooksFile": raw is None, "createdHooksObject": source_config is None,
            "originalEmptyEvents": [e for e in EVENTS if old.get("hooks", {}).get(e) == []],
        }
        record["sourceHash"] = digest(staged / n for n in SOURCES)
        (staged / MANIFEST).write_text(json.dumps(record) + "\n")
        old_directory = Path(build) / "previous"
        if previous:
            known = {*SOURCES, "FocusKeeper", MANIFEST}
            if any(item.name not in known for item in directory.iterdir()):
                raise ValueError(f"Installation directory contains extra files. Move them before updating: {directory}")
            if (directory / "lifecycle.py").is_file():
                stop_helper(directory)
            directory.rename(old_directory)
        try:
            staged.rename(directory)
            save_hooks(hooks_path, raw, old, new)
        except BaseException:
            if directory.exists():
                shutil.rmtree(directory)
            if old_directory.exists():
                old_directory.rename(directory)
            raise
    print("Installed. Review the new user hooks in Codex.")
    print("Allow the focus-keeper/lifecycle.py entries. Use a new chat for the first test.")


def uninstall(directory, hooks_path, raw, old, previous):
    if previous is None:
        print("Not installed.")
        return
    new = change_hooks(old, directory, False)
    for event in previous.get("originalEmptyEvents", []):
        new["hooks"].setdefault(event, [])
    if previous.get("createdHooksObject") and new.get("hooks") == {} and not previous["createdHooksFile"]:
        new.pop("hooks")
    stop_helper(directory)
    save_hooks(hooks_path, raw, old, new,
               remove_empty=previous["createdHooksFile"] and not hooks_path.is_symlink())
    for name in (*SOURCES, "FocusKeeper", MANIFEST):
        (directory / name).unlink(missing_ok=True)
    if not any(directory.iterdir()):
        directory.rmdir()
    print("Uninstalled. Helper files and hooks removed.")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("install", "uninstall"):
        raise ValueError("Run install.command or uninstall.command.")
    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    config.mkdir(parents=True, exist_ok=True)
    # Lock on the stable directory descriptor; leave no lock file in CODEX_HOME.
    fd = os.open(config, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        directory = config / "focus-keeper"
        previous = read_manifest(directory)
        path = (config / "hooks.json").resolve()
        raw, hooks = read_hooks(path)
        if sys.argv[1] == "install":
            install(directory, path, raw, hooks, previous)
        else:
            uninstall(directory, path, raw, hooks, previous)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Operation failed. {error}", file=sys.stderr)
        sys.exit(1)
