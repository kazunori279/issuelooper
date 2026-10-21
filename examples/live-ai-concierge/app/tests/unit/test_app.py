import json

from fastapi.testclient import TestClient

from concierge.main import app


def test_index_served():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Live AI Concierge" in response.text
    assert 'data-testid="status"' in response.text
    assert 'data-testid="feed"' in response.text
    assert 'data-testid="cards"' in response.text
    assert 'data-testid="starters"' in response.text


def test_websocket_ready_on_connect():
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg == {"type": "status", "state": "ready"}


def test_websocket_scripted_replay(tmp_path, monkeypatch):
    script_data = {
        "turns": [
            {
                "match": "hello",
                "events": [
                    {"type": "transcript", "text": "Hi there!", "delay_ms": 0},
                    {"type": "turn_complete"},
                ],
            }
        ],
        "fallback": [],
    }
    script_file = tmp_path / "script.json"
    script_file.write_text(json.dumps(script_data))
    monkeypatch.setenv("CONCIERGE_SCRIPT", str(script_file))

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        init_msg = ws.receive_json()
        assert init_msg == {"type": "status", "state": "ready"}

        ws.send_json({"type": "text", "text": "hello concierge"})

        transcript_msg = ws.receive_json()
        assert transcript_msg == {"type": "transcript", "text": "Hi there!"}

        turn_msg = ws.receive_json()
        assert turn_msg == {"type": "turn_complete"}

        status_msg = ws.receive_json()
        assert status_msg == {"type": "status", "state": "ready"}
