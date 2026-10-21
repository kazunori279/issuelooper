"""Starts the app in scripted mode (REQUIREMENTS §9) for browser tests."""

import os
import pathlib
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


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    port = _free_port()
    env = dict(os.environ)
    env.setdefault("CONCIERGE_SCRIPT", str(REPO / "spec/fixtures/script.json"))
    env.setdefault("SESSIONS_DB", str(tmp_path_factory.mktemp("db") / "sessions.db"))
    # The Maps key accepts only fixed origins, never this random port, so the suite runs
    # the key-less path (R-MAP-3). Set here, not removed: load_dotenv() keeps a value
    # that is already in the environment, even an empty one.
    env["GOOGLE_MAPS_API_KEY"] = ""
    proc = subprocess.Popen(["uv", "run", "concierge", "--port", str(port)], cwd=REPO, env=env)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except OSError:
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # No sound server for the browser: Chromium then plays to a silent device whose clock
    # runs, so playback ends on time. Through a real sound server (PipeWire, say) an idle
    # sink can hold the clock and `onended` never fires.
    return {**browser_type_launch_args, "env": {**os.environ, "PULSE_SERVER": "unix:/nonexistent"}}
