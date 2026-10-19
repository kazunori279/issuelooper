#!/usr/bin/env python3
"""The monitor host: one long-lived agent session (the monitor) behind a control socket.

    broker.py [--run DIR]

Starts the monitor through ccrole.sh, on the CLI and model agent_cli.py picks for
the monitor role (agy or Claude Code), with the shared checkout as its working
directory, and writes each message to its stdin as stream-json. ccpool.py and
monitorctl.py talk to it over <run>/broker.sock, one JSON request per line:

    {"cmd": "say", "text": ..., "tag": ...}     send a message; returns its turn number
    {"cmd": "wait", "turn": N, "timeout": S}    the turn's result, or how far it got
    {"cmd": "status"} {"cmd": "tail", "n": N} {"cmd": "note", "text": ...} {"cmd": "stop"}

Run dir ($ISSUELOOPER_RUN, default <repo root>/.issuelooper/run):
    broker.sock, broker.pid
    meta.json                  the CLI, the session id, turns done, restarts
    logs/monitor.jsonl         {"t": epoch, "ev": <CLI event>}
    logs/owner.jsonl           every message sent to the monitor, and notes
    logs/broker.jsonl          lifecycle: spawn, exit, restart, stall_kill, quota_resume, stop
    logs/monitor.stderr.log

The Claude Code session (`-p --input-format stream-json --output-format stream-json
--verbose`, one {"type": "user", ...} line per message) is built from its documented
format and has not yet been checked against a live run. agy's is in AGY_PROTOCOL.md.
"""

import argparse
import json
import os
import pathlib
import signal
import socketserver
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_cli  # noqa: E402
import config  # noqa: E402


def now():
    return time.time()


def run_dir() -> pathlib.Path:
    v = os.environ.get("ISSUELOOPER_RUN", "").strip()
    return pathlib.Path(v) if v else config.repo_root() / ".issuelooper" / "run"


class Session:
    def __init__(self, run: pathlib.Path, root: pathlib.Path | None = None):
        self.run = run
        self.root = root or config.repo_root()
        self.cli = agent_cli.for_role("monitor")
        self.ccrole = os.environ.get("BROKER_CCROLE", "") or str(HERE / "ccrole.sh")
        self.logs = run / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.meta_path = run / "meta.json"
        self.meta = (
            json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}
        )
        if self.meta.get("cli") != self.cli.name:
            # A session id belongs to one CLI; another one starts a new session.
            self.meta.pop("session_id", None)
        self.meta["cli"] = self.cli.name
        self.lock = threading.Condition()
        self.done = 0  # result events received
        self.results = {}  # turn number -> result payload + timing
        self.turn_started = {}  # turn number -> epoch when the monitor began it
        self.turn_steps = {}  # turn number -> list of condensed finished steps
        self.last_event = now()
        self.last_progress = (
            now()
        )  # a step or a result; the CLI's init line is not progress
        self.proc = None
        self.stopping = False
        # A turn that ended on a model quota error with no response is resumed after a
        # pause, as ccpool resumes its sessions.
        self.quota_resumes = 0  # in a row, reset by a turn that ends cleanly
        self.quota_resume_max = int(os.environ.get("BROKER_QUOTA_RESUMES", "8"))
        self.quota_resume_wait_s = float(os.environ.get("BROKER_QUOTA_WAIT_S", "60"))
        self.quota_resume_wait_max_s = float(
            os.environ.get("BROKER_QUOTA_WAIT_MAX_S", "900")
        )
        # A monitor that takes a message and then writes nothing is restarted and given
        # the message again: a monitor whose CLI lost its sign-in stayed silent for hours
        # while issues waited at stage:owner. Stalled = a pending turn with no event since
        # it was sent for BROKER_STALL_START_S, or no event at all for BROKER_STALL_S
        # (0 turns the watchdog off).
        self.stall_start_s = float(os.environ.get("BROKER_STALL_START_S", "600"))
        self.stall_s = float(os.environ.get("BROKER_STALL_S", "2700"))
        self.stall_check_s = float(os.environ.get("BROKER_STALL_CHECK_S", "60"))
        self.sent_at = {}  # turn -> epoch the message was written
        self.texts = {}  # turn -> (text, tag), to give a lost turn again after a stall
        self.stall_restarts = 0
        self.resend_after_restart = None
        self.quick_exits = 0  # exits in a row within a minute of the spawn
        # Resume numbering if the broker restarts.
        self.base = self.meta.get("turns_done", 0)
        self.sent = self.done = self.base
        self._save_meta()
        self._spawn()
        if self.stall_s > 0:
            threading.Thread(target=self._stall_watch, daemon=True).start()

    # --- logging -------------------------------------------------------------
    def _append(self, name, obj):
        with open(self.logs / name, "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _save_meta(self):
        disk = json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}
        disk.update(self.meta)
        self.meta_path.write_text(json.dumps(disk, indent=2))

    # --- process -------------------------------------------------------------
    def _spawn(self):
        cmd = [self.ccrole, "monitor", "--"] + self.cli.stream_args(
            self.meta.get("session_id"), self.logs
        )
        env = {k: v for k, v in os.environ.items() if k != "CC_ROLE"}
        env.update(
            CCROLE_MONITOR_CLI=self.cli.name,  # ccrole.sh picks the same CLI by the same rule
            SHARED_REPO_PATH=str(self.root),
            ISSUELOOPER_RUN=str(self.run),
        )
        env.setdefault("POOL_LOGDIR", str(self.run / "pool"))
        with open(self.logs / "monitor.stderr.log", "a") as stderr:
            self.proc = subprocess.Popen(
                cmd,
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,  # its own group, so a stall kill reaches every child
            )
        self.spawned_at = now()
        self._append(
            "broker.jsonl",
            {
                "t": now(),
                "kind": "spawn",
                "pid": self.proc.pid,
                "cmd": cmd,
                "cwd": str(self.root),
            },
        )
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            t = now()
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                self._append("monitor.jsonl", {"t": t, "raw": line.rstrip()})
                continue
            self._append("monitor.jsonl", {"t": t, "ev": ev})
            if isinstance(ev, dict):
                self._on_event(t, self.cli.read(ev))
        code = proc.wait()
        self._append(
            "broker.jsonl", {"t": now(), "kind": "exit", "pid": proc.pid, "code": code}
        )
        with self.lock:
            lost = list(range(self.done + 1, self.sent + 1))
            if not self.stopping:
                # Pending turns died with the process; the sender must give them again.
                for n in lost:
                    self.results[n] = {"status": "LOST", "response": "", "t_end": now()}
                self.done = self.sent
                self.meta["turns_done"] = self.done
                self.meta["restarts"] = self.meta.get("restarts", 0) + 1
                self._save_meta()
                self._append(
                    "broker.jsonl", {"t": now(), "kind": "restart", "lost_turns": lost}
                )
                self.lock.notify_all()
        if self.stopping:
            return
        # A CLI that dies right after it starts (not on PATH, signed out) is restarted
        # less and less often instead of in a tight loop.
        self.quick_exits = self.quick_exits + 1 if now() - self.spawned_at < 60 else 0
        time.sleep(min(300, 2 * 2 ** max(0, self.quick_exits - 1)))
        if self.stopping:
            return
        self._spawn()
        with self.lock:
            again, self.resend_after_restart = self.resend_after_restart, None
        if again:
            text, tag = again
            self.say(
                "The broker restarted your session because it stopped responding to "
                "the message below. Handle it now:\n\n" + text,
                tag,
            )

    def _on_event(self, t, n):
        kind = n.get("kind")
        with self.lock:
            self.last_event = t
            if kind in ("step", "result"):
                self.last_progress = t
            if n.get("session_id") and n["session_id"] != self.meta.get("session_id"):
                self.meta["session_id"] = n["session_id"]
                self._save_meta()
            if kind == "step":
                turn = self.done + 1
                self.turn_started.setdefault(turn, t)
                self.turn_steps.setdefault(turn, []).extend(n.get("steps") or [])
            elif kind == "result":
                self.done += 1
                r = {
                    k: n.get(k)
                    for k in ("status", "response", "error", "stale_error")
                    if n.get(k) is not None
                }
                r["t_end"] = t
                r["t_start"] = self.turn_started.get(self.done)
                self.results[self.done] = r
                if self.sent > self.done:
                    self.turn_started.setdefault(
                        self.done + 1, t
                    )  # the next queued turn starts now
                self.meta["turns_done"] = self.done
                self._save_meta()
                self.lock.notify_all()
                if not n.get("quota"):
                    self.quota_resumes = 0
                elif self.quota_resumes < self.quota_resume_max:
                    self.quota_resumes += 1
                    threading.Thread(
                        target=self._quota_resume,
                        args=(self.done, self.quota_resumes),
                        daemon=True,
                    ).start()

    def stalled(self, t=None):
        """Why the session counts as stalled now, or ''."""
        t = now() if t is None else t
        with self.lock:
            if self.sent <= self.done:
                return ""
            pending = self.done + 1
            sent_t = self.sent_at.get(pending)
            if (
                sent_t
                and self.last_progress < sent_t
                and t - sent_t > self.stall_start_s
            ):
                return f"no step since turn {pending} was sent {round((t - sent_t) / 60)} min ago"
            if t - self.last_progress > self.stall_s:
                return f"no step for {round((t - self.last_progress) / 60)} min during turn {pending}"
        return ""

    def _kill(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def _stall_watch(self):
        while not self.stopping:
            time.sleep(self.stall_check_s)
            why = self.stalled()
            if not why or self.stopping:
                continue
            with self.lock:
                self.stall_restarts += 1
                last = self.sent
                self.resend_after_restart = self.texts.get(last)
                proc = self.proc
            self._append(
                "broker.jsonl",
                {
                    "t": now(),
                    "kind": "stall_kill",
                    "why": why,
                    "pid": proc.pid,
                    "resend_turn": last,
                },
            )
            self._kill(proc)

    def _quota_resume(self, turn, k):
        """Resume a turn cut short by a quota error, unless something else was sent
        meanwhile (a feed message or the owner continues the session anyway)."""
        # The wait doubles (60 s, 120 s, ... up to 15 min): a capacity shortage can last
        # hours, and quick retries add to the load the error reports.
        time.sleep(
            min(self.quota_resume_wait_max_s, self.quota_resume_wait_s * 2 ** (k - 1))
        )
        with self.lock:
            idle = self.sent == self.done == turn
        if idle and not self.stopping:
            self._append(
                "broker.jsonl",
                {"t": now(), "kind": "quota_resume", "after_turn": turn, "k": k},
            )
            self.say(
                "Your last turn ended on a model quota error (429) before it finished. "
                "Continue the pass from where you stopped; do not repeat what is already done.",
                "broker_resume",
            )

    # --- commands ------------------------------------------------------------
    def say(self, text, tag):
        with self.lock:
            idle = self.sent == self.done
            self.sent += 1
            turn = self.sent
            queued = not idle
            self.sent_at[turn] = now()
            self.texts[turn] = (text, tag)
            if idle:
                self.turn_started[turn] = now()
        self._append(
            "owner.jsonl",
            {
                "t": now(),
                "kind": "message",
                "turn": turn,
                "tag": tag,
                "text": text,
                "queued_behind_active_turn": queued,
            },
        )
        self.proc.stdin.write(json.dumps(self.cli.user_message(text)) + "\n")
        self.proc.stdin.flush()
        return {"turn": turn, "queued": queued}

    def note(self, text, tag):
        self._append(
            "owner.jsonl", {"t": now(), "kind": "note", "tag": tag, "text": text}
        )
        return {"ok": True}

    def wait(self, turn, timeout):
        deadline = now() + timeout
        with self.lock:
            while self.done < turn and now() < deadline:
                self.lock.wait(timeout=min(5, max(0.1, deadline - now())))
            if self.done < turn:
                return {
                    "turn": turn,
                    "done": False,
                    "busy_for_s": round(
                        now() - self.turn_started.get(self.done + 1, now())
                    ),
                    "last_event_age_s": round(now() - self.last_event, 1),
                    "recent_steps": self.turn_steps.get(self.done + 1, [])[-8:],
                }
            r = self.results[turn]
            steps = self.turn_steps.get(turn, [])
            tools = {}
            for s in steps:
                if s.get("type") == "tool":
                    tools[s.get("tool")] = tools.get(s.get("tool"), 0) + 1
            return {
                "turn": turn,
                "done": True,
                "status": r.get("status"),
                "response": r.get("response", ""),
                "agent_seconds": round(r["t_end"] - r["t_start"], 1)
                if r.get("t_start")
                else None,
                "steps": len(steps),
                "tools": tools,
                "subagents": [s.get("role") for s in steps if s.get("role")],
                "questions": [
                    s.get("text")
                    for s in steps
                    if s.get("type") == "tool"
                    and s.get("tool") in ("ask_question", "AskUserQuestion")
                ],
                "error": r.get("error"),
            }

    def status(self):
        with self.lock:
            return {
                "cli": self.cli.name,
                "session_id": self.meta.get("session_id"),
                "sent": self.sent,
                "done": self.done,
                "busy": self.sent > self.done,
                "last_event_age_s": round(now() - self.last_event, 1),
                "restarts": self.meta.get("restarts", 0),
                "pid": self.proc.pid,
            }

    def tail(self, n):
        with self.lock:
            return {
                "steps": self.turn_steps.get(self.done + 1, [])[-n:]
                or self.turn_steps.get(self.done, [])[-n:]
            }

    def stop(self):
        self.stopping = True
        self._append("broker.jsonl", {"t": now(), "kind": "stop"})
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:  # noqa: BLE001 - stopping either way
            self._kill(self.proc)
        return {"ok": True}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        req = json.loads(self.rfile.readline())
        sess: Session = self.server.session
        cmd = req.pop("cmd")
        try:
            resp = {
                "say": lambda: sess.say(req["text"], req.get("tag") or "owner"),
                "note": lambda: sess.note(req["text"], req.get("tag") or "owner"),
                "wait": lambda: sess.wait(req["turn"], req.get("timeout", 600)),
                "status": sess.status,
                "tail": lambda: sess.tail(req.get("n", 20)),
                "stop": sess.stop,
            }[cmd]()
        except Exception as e:  # noqa: BLE001 - report, keep serving
            resp = {"error": repr(e)}
        self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
        if cmd == "stop":
            threading.Thread(target=self.server.shutdown, daemon=True).start()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    config.apply()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--run",
        help="the run dir (default $ISSUELOOPER_RUN, else <repo>/.issuelooper/run)",
    )
    args = ap.parse_args()
    run = pathlib.Path(args.run).resolve() if args.run else run_dir().resolve()
    run.mkdir(parents=True, exist_ok=True)
    sock = run / "broker.sock"
    if sock.exists():
        sock.unlink()
    server = Server(str(sock), Handler)
    server.session = Session(run)
    (run / "broker.pid").write_text(str(os.getpid()))
    signal.signal(
        signal.SIGTERM,
        lambda *_: threading.Thread(
            target=lambda: (server.session.stop(), server.shutdown())
        ).start(),
    )
    server.serve_forever()
    sock.unlink(missing_ok=True)
    (run / "broker.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
