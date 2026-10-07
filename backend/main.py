"""FastAPI 進入點：REST + WebSocket + 靜態前端。"""
from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .factory import Factory

ROOT = Path(__file__).resolve().parent.parent
REFRESH_EVERY = 15  # 秒；Ollama 可能在 Dashboard 啟動之後才起來
WS_QUEUE_MAX = 5000


class _In(BaseModel):
    # 前端截字可能切出半個 emoji（lone surrogate），無法編成 UTF-8；在入口換成 "?"。
    @field_validator("*", mode="after")
    @classmethod
    def _clean(cls, v):
        return v.encode("utf-8", "replace").decode("utf-8") if isinstance(v, str) else v


class ChatIn(_In):
    node: str
    message: str


class TaskIn(_In):
    title: str
    prompt: str


class AnswerIn(_In):
    text: str


def create_app(force_fake: bool | None = None, data_dir: Path | None = None) -> FastAPI:
    if force_fake is None:
        force_fake = os.environ.get("FACTORY_FAKE") == "1"
    factory = Factory(ROOT / "nodes.json", data_dir or ROOT / "data", force_fake)

    async def refresher() -> None:
        while True:
            await asyncio.sleep(REFRESH_EVERY)
            with contextlib.suppress(Exception):
                await factory.refresh()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await factory.refresh()
        loop_task = asyncio.create_task(refresher())
        yield
        loop_task.cancel()
        await factory.shutdown()

    app = FastAPI(title="AI Factory", lifespan=lifespan)
    app.state.factory = factory
    background: set[asyncio.Task] = set()

    @app.get("/api/state")
    async def state():
        await factory.refresh()
        return factory.snapshot()

    @app.post("/api/chat")
    async def chat(body: ChatIn):
        if body.node not in factory.nodes:
            raise HTTPException(404, "未知節點")
        if not body.message.strip():
            raise HTTPException(422, "訊息是空的")
        if factory.nodes[body.node]["status"] == "offline":
            await factory.refresh(body.node)  # 也許剛啟動，先重新偵測一次
            if factory.nodes[body.node]["status"] == "offline":
                raise HTTPException(409, f"節點離線：{factory.nodes[body.node]['detail']}")
        t = asyncio.create_task(factory.chat(body.node, body.message))
        background.add(t)
        t.add_done_callback(background.discard)
        return {"ok": True}

    @app.post("/api/tasks")
    async def create_task(body: TaskIn):
        if not body.prompt.strip():
            raise HTTPException(422, "任務內容是空的")
        return factory.start_task(body.title.strip() or body.prompt[:20], body.prompt)

    @app.post("/api/tasks/{task_id}/answer")
    async def answer(task_id: str, body: AnswerIn):
        if not factory.answer(task_id, body.text):
            raise HTTPException(409, "此任務目前沒有待回答的問題")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        if not factory.cancel(task_id):
            raise HTTPException(409, "此任務不在執行中")
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE_MAX)
        overflow = asyncio.Event()

        async def sub(ev: dict) -> None:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:  # 用戶端跟不上：斷線讓它重連拿新快照
                overflow.set()
                raise

        async def sender() -> None:
            while True:
                await sock.send_json(await q.get())

        async def receiver() -> None:
            # 必須讀 socket 才看得到關閉；否則 Ctrl+C 時會卡在 "Waiting for background tasks"。
            while (await sock.receive())["type"] != "websocket.disconnect":
                pass

        factory.subscribers.add(sub)
        try:
            await factory.refresh()
            await sock.send_json({"type": "snapshot", **factory.snapshot()})
            jobs = [asyncio.create_task(c) for c in (sender(), receiver(), overflow.wait())]
            done, pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for d in done:
                d.exception()  # 取出例外，避免 "Task exception was never retrieved"
            if overflow.is_set():
                with contextlib.suppress(Exception):
                    await sock.close(1013)
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
