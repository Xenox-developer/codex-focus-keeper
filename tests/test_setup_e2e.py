"""Local integration: compile, discover using the installed Codex, and uninstall."""
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("QA_FOCUS_PACKAGE", TESTS.parent)).resolve()
events = {"preToolUse", "postToolUse", "stop", "interrupt", "sessionEnd", "subagentStop"}


def discover(env, cwd):
    p = subprocess.Popen(
        [shutil.which("codex"), "app-server", "--listen", "stdio://"],
        cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0,
    )
    try:
        def send(msg):
            p.stdin.write(json.dumps(msg).encode() + b"\n")
        def receive(id_):
            deadline = time.monotonic() + 12
            with selectors.DefaultSelector() as sel:
                sel.register(p.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline and sel.select(deadline - time.monotonic()):
                    line = p.stdout.readline()
                    if not line:
                        break
                    message = json.loads(line)
                    if message.get("id") == id_:
                        assert "error" not in message, message
                        return message["result"]
            raise TimeoutError("App server did not respond")
        send(dict(id=1, method="initialize", params={"clientInfo": {"name": "focus-keeper-qa", "version": "1"},
                                                      "capabilities": {"experimentalApi": True}}))
        receive(1)
        send(dict(method="initialized"))
        send(dict(id=2, method="hooks/list", params={"cwds": [str(cwd)]}))
        return receive(2)
    finally:
        p.stdin.close()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.terminate()
            p.wait(timeout=3)


def run():
    with tempfile.TemporaryDirectory(prefix="qa 'quoted path' ",dir=TESTS) as tmp:
        sandbox = Path(tmp)
        config = sandbox / "codex home"
        config.mkdir()
        env = dict(os.environ, CODEX_HOME=str(config), CODEX_FOCUS_KEEPER_STATE_DIR=str(sandbox/"leases"))
        initial = {"description": "user data", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type":"command", "command":"/usr/bin/true"}]}], "SessionStart": [], "Stop": []}}
        hooks = config/"hooks.json"
        hooks.write_text(json.dumps(initial))
        hooks.chmod(0o600)
        for _ in range(2):
            subprocess.run([str(SOURCE/"install.command")],env=env,check=True)
        managed = json.loads(hooks.read_text())
        assert sum(len(g["hooks"]) for groups in managed["hooks"].values() for g in groups) == 7
        assert (hooks.stat().st_mode & 0o777) == 0o600
        print("Installer passed merging and idempotent reinstall.")
        listing = discover(env,sandbox)
        def items(value):
            if isinstance(value,dict):
                if "eventName" in value:
                    yield value
                else:
                    for child in value.values(): yield from items(child)
            elif isinstance(value,list):
                for child in value: yield from items(child)
        ours = [x for x in items(listing) if "focus-keeper/lifecycle.py" in json.dumps(x) or
                ("lifecycle.py" in json.dumps(x) and "focus-keeper" in json.dumps(x))]
        assert len(ours) == 6, [(x.get("eventName"),x.get("trustStatus"),list(x.keys())) for x in items(listing)]
        print("Installed Codex discovered",[(x['eventName'],x['trustStatus']) for x in ours])
        assert len({x['eventName'] for x in ours})==6
        # Simulate an unrelated hook added by the user after the install.
        current = json.loads(hooks.read_bytes())
        later = [{"hooks":[{"type":"command","command":"/usr/bin/true"}]}]
        current["hooks"]["PostCompact"] = later
        hooks.write_text(json.dumps(current))
        initial["hooks"]["PostCompact"] = later
        for _ in range(2):
            subprocess.run([str(SOURCE/"uninstall.command")],env=env,check=True)
        assert json.loads(hooks.read_text()) == initial and not (config/"focus-keeper").exists()
        print("Uninstall passed, unrelated config preserved.")
        hooks.unlink()
        subprocess.run([str(SOURCE/"install.command")],env=env,check=True)
        subprocess.run([str(SOURCE/"uninstall.command")],env=env,check=True)
        assert not hooks.exists()
        print("Clean install and removal passed.")


if __name__ == "__main__": run()
