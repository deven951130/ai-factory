"""FastAPI 進入點：REST + WebSocket + 靜態前端。"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .factory import Factory

ROOT = Path(__file__).resolve().parent.parent


class ChatIn(BaseModel):
    node: str
    message: str


class TaskIn(BaseModel):
    title: str
    prompt: str


class AnswerIn(BaseModel):
    text: str


def create_app(force_fake: bool | None = None, data_dir: Path | None = None) -> FastAPI:
    if force_fake is None:
        force_fake = os.environ.get("FACTORY_FAKE") == "1"
    factory = Factory(ROOT / "nodes.json", data_dir or ROOT / "data", force_fake)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        factory.refresh_availability()
        yield

    app = FastAPI(title="AI Factory", lifespan=lifespan)
    app.state.factory = factory
    background: set[asyncio.Task] = set()

    @app.get("/api/state")
    async def state():
        factory.refresh_availability()
        return factory.snapshot()

    @app.post("/api/chat")
    async def chat(body: ChatIn):
        if body.node not in factory.nodes:
            raise HTTPException(404, "未知節點")
        if factory.nodes[body.node]["status"] == "offline":
            raise HTTPException(409, f"節點離線：{factory.nodes[body.node]['detail']}")
        t = asyncio.create_task(factory.chat(body.node, body.message))
        background.add(t)
        t.add_done_callback(background.discard)
        return {"ok": True}

    @app.post("/api/tasks")
    async def create_task(body: TaskIn):
        task = factory.create_task(body.title, body.prompt)
        t = asyncio.create_task(factory.run_task(task["id"]))
        background.add(t)
        t.add_done_callback(background.discard)
        return task

    @app.post("/api/tasks/{task_id}/answer")
    async def answer(task_id: str, body: AnswerIn):
        if not factory.answer(task_id, body.text):
            raise HTTPException(409, "此任務目前沒有待回答的問題")
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue()

        async def sub(ev: dict) -> None:
            q.put_nowait(ev)

        factory.subscribers.add(sub)
        await sock.send_json({"type": "snapshot", **factory.snapshot()})
        try:
            while True:
                await sock.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            factory.subscribers.discard(sub)

    app.mount("/assets", StaticFiles(directory=ROOT / "frontend"), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "frontend" / "index.html")

    return app


app = create_app()
