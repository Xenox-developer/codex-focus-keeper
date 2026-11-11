"""Codex hook for the local macOS focus helper. Never records tool arguments."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
HELPER = HERE / "FocusKeeper"
STATE = Path(os.environ.get("CODEX_FOCUS_KEEPER_STATE_DIR",
                            Path.home() / "Library" / "Caches" / "CodexFocusKeeper"))
TOOL = "mcp__cua_repl__js"
# An abandoned client cannot leave the helper running indefinitely.
MAX_IDLE_SECONDS = 300
POLL_SECONDS = 0.2
# Only a cold start waits this long. The Codex hook timeout is 3 seconds.
READY_TIMEOUT = 1.5


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def shared_clock():
    # Apple's /usr/bin/python3 can be 3.9, whose time.monotonic() uses a
    # per-process origin on macOS. Leases are shared with a separate process.
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def actor_key(event):
    transcript = event.get("transcript_path")
    agent = event.get("agent_id")
    if isinstance(transcript, str) and transcript:
        return "p" + digest(transcript)
    return "a" + digest(agent) if isinstance(agent, str) and agent else ""


def action_for(event):
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    session_key = digest(session) + "/"
    name = event.get("hook_event_name")
    if name == "SessionEnd":
        return "end-session", session_key
    turn = event.get("turn_id")
    if not isinstance(turn, str) or not turn:
        return None
    turn_key = session_key + digest(turn) + "/"
    key = turn_key + actor_key(event)
    if name == "SubagentStop":
        return "end-child", turn_key
    if name in ("Stop", "Interrupt"):
        return "end", key
    if event.get("tool_name") != TOOL:
        return None
    arguments = event.get("tool_input")
    if not isinstance(arguments, dict):
        return None
    if "completion_summary" in arguments:
        return ("end", key) if name == "PostToolUse" else None
    if name == "PreToolUse":
        return "start", key
    if name == "PostToolUse":
        return "refresh", key
    return None


@contextlib.contextmanager
def state_lock():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / "state.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def live_leases():
    try:
        leases = json.loads((STATE / "leases.json").read_bytes())
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(leases, dict):
        return {}
    now = shared_clock()
    # The upper bound discards timestamps from an earlier boot.
    return {key: expiry for key, expiry in leases.items()
            if isinstance(expiry, (int, float)) and now < expiry <= now + MAX_IDLE_SECONDS + 1}


def save_leases(leases):
    if not leases:
        (STATE / "leases.json").unlink(missing_ok=True)
        return
    with tempfile.NamedTemporaryFile(mode="w", dir=STATE, delete=False) as temp:
        try:
            json.dump(leases, temp)
            temp.flush()
            os.replace(temp.name, STATE / "leases.json")
        finally:
            Path(temp.name).unlink(missing_ok=True)


def acquire_owner():
    fd = os.open(STATE / "running.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def start_if_needed():
    if not os.access(HELPER, os.X_OK):
        raise FileNotFoundError("FocusKeeper executable not found.")
    fd = acquire_owner()
    if fd is None:
        return
    try:
        (STATE / "ready").unlink(missing_ok=True)
        subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--supervise", str(fd)],
            pass_fds=(fd,), start_new_session=True, cwd=HERE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        # flock belongs to the shared open file description. Closing this copy
        # leaves ownership with the supervisor; explicitly unlocking would not.
        os.close(fd)


def handle(event):
    action = action_for(event)
    if not action:
        return
    verb, key = action
    if verb != "start" and not STATE.is_dir():
        return
    with state_lock():
        leases = live_leases()
        if verb == "start":
            start_if_needed()
            leases[key] = shared_clock() + MAX_IDLE_SECONDS
        elif verb == "refresh" and key in leases:
            leases[key] = shared_clock() + MAX_IDLE_SECONDS
        elif verb == "end-session":
            leases = {scope: expiry for scope, expiry in leases.items()
                      if not scope.startswith(key)}
        elif verb == "end-child":
            agent = event.get("agent_id")
            transcript = event.get("agent_transcript_path")
            # SubagentStop can refer to the parent's own session and turn.
            # Only retire scopes we can attribute to this particular child.
            if isinstance(agent, str) and agent:
                child_key = digest(agent) + "/" + digest(event["turn_id"]) + "/"
                leases = {scope: expiry for scope, expiry in leases.items()
                          if not scope.startswith(child_key)}
                leases.pop(key + "a" + digest(agent), None)
            if isinstance(transcript, str) and transcript:
                leases.pop(key + "p" + digest(transcript), None)
        elif verb == "end":
            leases.pop(key, None)
        save_leases(leases)
    if verb == "start":
        deadline = time.monotonic() + READY_TIMEOUT
        while not (STATE / "ready").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Focus observer did not confirm startup in time.")
            time.sleep(0.02)


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def supervise(owner_fd):
    child = None
    ready = False
    try:
        while True:
            with state_lock():
                leases = live_leases()
                if not leases or (child is not None and child.poll() is not None):
                    save_leases({})
                    stop_child(child)
                    (STATE / "ready").unlink(missing_ok=True)
                    # Release ownership under the state lock so a concurrent
                    # start cannot lose a new scope during this shutdown.
                    os.close(owner_fd)
                    owner_fd = None
                    return
                if child is None:
                    child = subprocess.Popen([str(HELPER)], cwd=HERE, stdout=subprocess.PIPE)
            if ready:
                time.sleep(POLL_SECONDS)
            else:
                readable, _, _ = select.select([child.stdout], [], [], POLL_SECONDS)
                if readable:
                    if child.stdout.read(1) != b"1":
                        stop_child(child)
                    else:
                        (STATE / "ready").touch(mode=0o600)
                        ready = True
                    child.stdout.close()
    finally:
        stop_child(child)
        if owner_fd is not None:
            os.close(owner_fd)


def status():
    if not STATE.is_dir():
        return {"running": False, "active_turns": 0}
    with state_lock():
        owner_fd = acquire_owner()
        running = owner_fd is None
        if owner_fd is not None:
            os.close(owner_fd)
        return {"running": running, "active_turns": len(live_leases())}


def main():
    args = sys.argv[1:]
    if not args:
        # On all hook types, an empty JSON object lets Codex proceed normally.
        try:
            handle(json.load(sys.stdin))
        except (OSError, ValueError) as error:
            print(json.dumps({"systemMessage": "FocusKeeper: " + str(error)}))
        else:
            print("{}")
    elif args == ["--status"]:
        print(json.dumps(status()))
    elif args == ["--stop"]:
        if STATE.is_dir():
            with state_lock():
                save_leases({})
            deadline = time.monotonic() + 2
            while status()["running"]:
                if time.monotonic() >= deadline:
                    raise SystemExit("Focus observer is still running.")
                time.sleep(0.05)
    elif len(args) == 2 and args[0] == "--supervise":
        supervise(int(args[1]))
    else:
        raise SystemExit("Usage: lifecycle.py [--status|--stop]")


if __name__ == "__main__":
    main()
