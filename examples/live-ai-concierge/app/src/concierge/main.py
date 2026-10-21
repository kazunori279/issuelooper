"""Live AI Concierge FastAPI server."""

import argparse
import asyncio
import json
import os
import pathlib
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from concierge.scripted import ScriptEngine

STATIC = pathlib.Path(__file__).parent / "static"

app = FastAPI(title="Live AI Concierge")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "status", "state": "ready"})

    script_path = os.environ.get("CONCIERGE_SCRIPT")
    engine = ScriptEngine(script_path) if script_path else None

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if "text" in message:
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue

                if not isinstance(data, dict):
                    continue

                msg_type = data.get("type")
                if msg_type == "text":
                    user_text = data.get("text", "")
                    if engine is not None:
                        events: list[dict[str, Any]] = engine.get_events_for_message(user_text)
                        for event in events:
                            etype = event.get("type")
                            if etype == "transcript":
                                delay = event.get("delay_ms", 100) / 1000.0
                                if delay > 0:
                                    await asyncio.sleep(delay)
                                await websocket.send_json(
                                    {"type": "transcript", "text": event.get("text", "")}
                                )
                            elif etype == "tool_call":
                                await websocket.send_json(
                                    {
                                        "type": "tool_start",
                                        "name": event.get("name", ""),
                                        "args": event.get("args", {}),
                                    }
                                )
                                delay_ms = event.get("delay_ms")
                                if delay_ms:
                                    await asyncio.sleep(delay_ms / 1000.0)
                                await websocket.send_json(
                                    {
                                        "type": "tool_result",
                                        "name": event.get("name", ""),
                                        "result": event.get("result", {}),
                                    }
                                )
                            elif etype == "audio":
                                ms = event.get("ms", 0)
                                await websocket.send_json({"type": "status", "state": "speaking"})
                                await websocket.send_json({"type": "audio", "ms": ms})
                                if ms > 0:
                                    pcm = ScriptEngine.generate_audio_pcm(ms)
                                    await websocket.send_bytes(pcm)
                                    await asyncio.sleep(ms / 1000.0)
                            elif etype == "turn_complete":
                                await websocket.send_json({"type": "turn_complete"})
                                await websocket.send_json({"type": "status", "state": "ready"})
            elif "bytes" in message:
                # Discard binary microphone audio in scripted mode per R-SCRIPT-4
                pass
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass


# Mount /static and root fallback for static assets
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static_root")


def run() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Live AI Concierge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
