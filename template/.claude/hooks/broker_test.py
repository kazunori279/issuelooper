#!/usr/bin/env python3
"""broker.py, the monitor host, against a fake CLI standing in for ccrole.sh.

    python3 broker_test.py

Every case runs twice: on agy's stream-json, and on Claude Code's documented one
(not recorded from a live run).
"""

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Called as `<fake> monitor -- <args>`, as broker.py calls ccrole.sh. Answers each
# message; a message with HANG in it is taken and never answered, unless this process
# resumes a session (the restart after a stall kill). QUOTA gets one quota error.
FAKE_CLI = r"""#!/usr/bin/env python3
import json, os, sys, time
cli = os.environ["CCROLE_MONITOR_CLI"]
args = sys.argv[1:]
assert args[:2] == ["monitor", "--"], args
resumed = "--conversation" in args or "--resume" in args
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")
def out(o):
    print(json.dumps(o), flush=True)
if cli == "agy":
    out({"event": "init", "conversation_id": "conv-1"})
else:
    out({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "m"})
quota_left = 1
for line in sys.stdin:
    msg = json.loads(line)
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"stdin": msg}) + "\n")
    text = msg["message"]["content"]
    if "HANG" in text and not resumed:
        time.sleep(600)
    if "QUOTA" in text and quota_left:
        quota_left -= 1
        if cli == "agy":
            out({"event": "result", "result": {"status": "ERROR", "response": "",
                 "error": "TPM quota exhausted. No spare capacity is currently available."}})
        else:
            out({"type": "result", "subtype": "success", "is_error": True, "session_id": "sess-1",
                 "result": "API Error: 429 rate_limit_error"})
        continue
    if cli == "agy":
        out({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE",
             "step_type": "tool", "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "gh issue list"}}}})
        out({"event": "result", "result": {"status": "SUCCESS", "response": "done: " + text[:60]}})
    else:
        out({"type": "assistant", "session_id": "sess-1", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh issue list"}}]}})
        out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[]"}]}})
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": "sess-1",
             "result": "done: " + text[:60]})
"""


class AgyMonitor(unittest.TestCase):
    CLI = "agy"
    SID = "conv-1"
    RESUME = "--conversation"

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="broker-test-"))
        fake = self.dir / "fake-ccrole"
        fake.write_text(FAKE_CLI)
        fake.chmod(0o755)
        self.run_dir = self.dir / "run"
        self.root = self.dir / "repo"
        self.root.mkdir()
        self.env = dict(os.environ)
        for k in [k for k in os.environ if k.startswith("CCROLE")]:
            del os.environ[k]
        os.environ.update(
            BROKER_CCROLE=str(fake),
            FAKE_LOG=str(self.dir / "fake.log"),
            CCROLE_CLI=self.CLI,
            BROKER_STALL_START_S="2",
            BROKER_STALL_S="30",
            BROKER_STALL_CHECK_S="0.5",
            BROKER_QUOTA_WAIT_S="0.2",
        )
        self.session = None

    def tearDown(self):
        if self.session:
            self.session.stop()
        os.environ.clear()
        os.environ.update(self.env)

    def start(self):
        import broker

        self.session = broker.Session(self.run_dir, self.root)
        return self.session

    def events(self):
        return [
            json.loads(x)
            for x in (self.run_dir / "logs/broker.jsonl").read_text().splitlines()
        ]

    def fake_log(self):
        return [json.loads(x) for x in (self.dir / "fake.log").read_text().splitlines()]

    def test_messages_reach_the_monitor_and_turns_report_back(self):
        s = self.start()
        r = s.wait(s.say("first pass", "kickoff")["turn"], 10)
        self.assertEqual(
            (r["done"], r["status"], r["response"]),
            (True, "SUCCESS", "done: first pass"),
        )
        self.assertEqual(
            r["tools"], {"run_command" if self.CLI == "agy" else "Bash": 1}
        )
        log = self.fake_log()
        self.assertEqual(
            log[0]["cwd"], str(self.root)
        )  # the monitor works in the shared checkout
        want = (
            {"event": "user", "message": {"content": "first pass"}}
            if self.CLI == "agy"
            else {"type": "user", "message": {"role": "user", "content": "first pass"}}
        )
        self.assertEqual(log[1]["stdin"], want)
        self.assertEqual(s.status()["session_id"], self.SID)
        self.assertEqual(
            json.loads((self.run_dir / "meta.json").read_text())["session_id"], self.SID
        )

    def test_a_silent_monitor_is_restarted_and_given_the_message_again(self):
        s = self.start()
        s.say("first", "pool_feed")
        self.assertTrue(s.wait(1, 10)["done"])
        s.say("HANG on this", "pool_feed")
        deadline = time.time() + 30
        while time.time() < deadline and not (s.done >= 3 and s.results.get(3)):
            time.sleep(0.2)
        kinds = [e["kind"] for e in self.events()]
        self.assertIn("stall_kill", kinds)
        self.assertEqual(s.results[2]["status"], "LOST")
        self.assertEqual(s.results[3]["status"], "SUCCESS")
        self.assertIn("HANG on this", s.texts[3][0])
        self.assertIn("restarted your session", s.texts[3][0])
        self.assertEqual(s.texts[3][1], "pool_feed")
        spawns = [e for e in self.events() if e["kind"] == "spawn"]
        self.assertEqual(
            spawns[-1]["cmd"][-2:], [self.RESUME, self.SID]
        )  # the same session

    def test_a_working_monitor_is_left_alone(self):
        s = self.start()
        for i in range(3):
            s.say(f"message {i}", "pool_feed")
            self.assertTrue(s.wait(i + 1, 10)["done"])
        time.sleep(3)
        self.assertEqual(s.stalled(), "")
        self.assertNotIn("stall_kill", [e["kind"] for e in self.events()])

    def test_a_quota_error_is_resumed(self):
        s = self.start()
        s.say("QUOTA pass", "pool_feed")
        self.assertTrue(s.wait(1, 10)["done"])
        self.assertEqual(s.results[1]["status"], "ERROR")
        self.assertTrue(s.wait(2, 10)["done"])
        self.assertEqual(s.texts[2][1], "broker_resume")
        self.assertEqual(s.results[2]["status"], "SUCCESS")
        self.assertIn("quota_resume", [e["kind"] for e in self.events()])

    def test_the_watchdog_can_be_turned_off(self):
        os.environ["BROKER_STALL_S"] = "0"
        s = self.start()
        s.say("HANG", "owner")
        time.sleep(4)
        self.assertNotIn("stall_kill", [e["kind"] for e in self.events()])
        self.assertTrue(s.stalled())  # it would count, but nothing acts on it

    def test_a_restarted_broker_resumes_the_session_and_its_numbering(self):
        s = self.start()
        self.assertTrue(s.wait(s.say("one", "owner")["turn"], 10)["done"])
        s.stop()
        s2 = self.start()
        self.assertEqual(s2.say("two", "owner")["turn"], 2)
        self.assertTrue(s2.wait(2, 10)["done"])
        spawns = [e for e in self.events() if e["kind"] == "spawn"]
        self.assertEqual(spawns[-1]["cmd"][-2:], [self.RESUME, self.SID])


class ClaudeMonitor(AgyMonitor):
    CLI = "claude"
    SID = "sess-1"
    RESUME = "--resume"


if __name__ == "__main__":
    unittest.main()
