import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "lifecycle.py"
TOOL = "mcp__cua_repl__js"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = self.directory / "state"
        self.env = dict(os.environ, CODEX_FOCUS_KEEPER_STATE_DIR=str(self.state))
        self.command = self.directory / "lifecycle.py"
        shutil.copyfile(SOURCE, self.command)
        helper = self.directory / "FocusKeeper"
        helper.write_text(
            '#!/bin/sh\nprintf "start\\n" >> starts\nprintf 1\nexec /bin/sleep 60\n'
        )
        helper.chmod(0o700)
        self.addCleanup(self.stop)

    def run_command(self, *args, data=None):
        proc = subprocess.run([sys.executable, "-I", str(self.command), *args],
                              input=data, capture_output=True, text=True, env=self.env,
                              check=True, timeout=5)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def hook(self, name, session, turn, tool=TOOL, **fields):
        event = dict(hook_event_name=name, session_id=session, turn_id=turn,
                     tool_name=tool, tool_input={"code": "test"})
        event.update(fields)
        self.assertEqual(self.run_command(data=json.dumps(event)), {})

    def wait(self, condition):
        start = time.monotonic()
        while not condition():
            if time.monotonic() - start > 4:
                self.fail("Timed out waiting for the test helper")
            time.sleep(0.05)

    def stop(self):
        self.run_command("--stop")
        self.wait(lambda: not self.run_command("--status")["running"])

    def test_hooks_preserve_concurrent_scopes(self):
        self.hook("PreToolUse", "s1", "t1", "Bash")
        self.assertFalse(self.state.exists())
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.hook, "PreToolUse", s, t) for s, t in
                       [("s1", "t1"), ("s2", "t2"), ("s3", "t3"), ("s3", "t3")]]
            for result in futures:
                result.result()
        self.wait(lambda: (self.directory / "starts").exists())
        self.assertEqual(self.run_command("--status"), {"running": True, "active_turns": 3})
        self.assertEqual((self.directory / "starts").read_text(), "start\n")
        self.hook("PostToolUse", "s1", "t1", tool_input={"completion_summary": "Done"})
        self.hook("PostToolUse", "s1", "t1")  # late response must not restart
        self.hook("Interrupt", "s2", "t2")
        self.assertEqual(self.run_command("--status")["active_turns"], 1)
        self.hook("SubagentStop", "parent", "t3", agent_id="s3")
        self.wait(lambda: not self.run_command("--status")["running"])

    def test_session_end_and_abandoned_scopes(self):
        for s, t in [("one", "A"), ("one", "B"), ("other", "C")]:
            self.hook("PreToolUse", s, t)
        self.hook("SessionEnd", "one", None)
        self.assertEqual(self.run_command("--status")["active_turns"], 1)
        lease_file = self.state / "leases.json"
        leases = json.loads(lease_file.read_text())
        key = next(iter(leases))
        boot_clock = time.clock_gettime(time.CLOCK_MONOTONIC)
        self.assertGreater(leases[key], boot_clock + 290)
        temp = self.state / "expired-for-test"
        temp.write_text(json.dumps({key: boot_clock - 30}))
        os.replace(temp, lease_file)
        self.wait(lambda: not self.run_command("--status")["running"])
        self.assertFalse(lease_file.exists())

    def test_regular_stop_ignores_other_turn_and_completion_only(self):
        self.hook("PreToolUse", "parent", "old", tool_input={"completion_summary": "Done"})
        self.assertFalse(self.state.exists())
        self.hook("PreToolUse", "parent", "new")
        self.hook("Stop", "parent", "old")
        self.hook("SubagentStop", "parent", "other-turn", agent_id="child")
        self.assertEqual(self.run_command("--status")["active_turns"], 1)
        self.hook("Stop", "parent", "new")
        self.wait(lambda: not self.run_command("--status")["running"])

    def test_subagent_stop_does_not_close_parent_with_same_turn(self):
        self.hook("PreToolUse", "parent", "turn", transcript_path="/main")
        self.hook("PreToolUse", "parent", "turn", transcript_path="/child")
        self.hook("SubagentStop", "parent", "turn", agent_id="child-id",
                  agent_transcript_path="/child", transcript_path="/main")
        self.assertEqual(self.run_command("--status"), {"running": True, "active_turns": 1})
        self.hook("Stop", "parent", "turn", transcript_path="/main")
        self.wait(lambda: not self.run_command("--status")["running"])

    def test_cold_pre_hook_waits_for_native_observer(self):
        (self.directory / "FocusKeeper").write_text(
            '#!/bin/sh\n/bin/sleep 0.2\n/usr/bin/touch observed\nprintf 1\nexec /bin/sleep 60\n'
        )
        self.hook("PreToolUse", "session", "turn")
        self.assertTrue((self.directory / "observed").exists())


if __name__ == "__main__":
    unittest.main()
