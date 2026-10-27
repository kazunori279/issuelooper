"""Starts the app in scripted mode (REQUIREMENTS §9) for browser tests."""

import os
import pathlib
import signal
import socket
import subprocess
import time
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(db_dir: pathlib.Path, own_db: bool = False):
    """Start the app on a free port, its sessions database in db_dir (own_db: whatever
    SESSIONS_DB says); yield its URL."""
    port = _free_port()
    env = dict(os.environ)
    env.setdefault("CONCIERGE_SCRIPT", str(REPO / "spec/fixtures/script.json"))
    if own_db:
        env["SESSIONS_DB"] = str(db_dir / "sessions.db")
    else:
        env.setdefault("SESSIONS_DB", str(db_dir / "sessions.db"))
    # The Maps key accepts only fixed origins, never this random port, so the suite runs
    # the key-less path (R-MAP-3). Set here, not removed: load_dotenv() keeps a value
    # that is already in the environment, even an empty one.
    env["GOOGLE_MAPS_API_KEY"] = ""
    # In a session of its own, stopped as a group: `uv run` starts the server as its child,
    # and stopping uv alone left the server running (109 of them after one six-hour run).
    proc = subprocess.Popen(["uv", "run", "concierge", "--port", str(port)], cwd=REPO, env=env,
                            start_new_session=True)

    def stop(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except OSError:
            time.sleep(0.3)
    else:
        stop(signal.SIGKILL)
        raise RuntimeError("server did not start")
    yield url
    stop(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        stop(signal.SIGKILL)
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    """One app for the whole suite: tests share its conversations (REQUIREMENTS §6)."""
    yield from _serve(tmp_path_factory.mktemp("db"))


@pytest.fixture
def fresh_server_url(tmp_path):
    """An app of this test's own, with an empty sessions database: for a test that needs
    no earlier conversation (an empty feed after a reload, a first visit). The server owns
    conversation identity, so on the shared server a reload resumes whatever the tests
    before it said."""
    yield from _serve(tmp_path, own_db=True)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # No sound server for the browser: Chromium then plays to a silent device whose clock
    # runs, so playback ends on time. Through a real sound server (PipeWire, say) an idle
    # sink can hold the clock and `onended` never fires.
    return {**browser_type_launch_args, "env": {**os.environ, "PULSE_SERVER": "unix:/nonexistent"}}
