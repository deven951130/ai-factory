"""FastAPI 進入點：REST + WebSocket + 靜態前端。"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .connectors import ConnectorError, ollama_models
from .factory import Factory

ROOT = Path(__file__).resolve().parent.parent
REFRESH_EVERY = 15  # 秒；Ollama 可能在 Dashboard 啟動之後才起來
WS_QUEUE_MAX = 5000
DEFAULT_HOSTS = ("127.0.0.1", "localhost", "::1")


def _hostname(value: str) -> str | None:
    try:
        return urlsplit(value if "//" in value else "//" + value).hostname
    except ValueError:
        return None


def normalize_host(entry: str) -> str | None:
    """把設定的主機名稱正規化成和請求比對時相同的形式：小寫、去掉 port / scheme / 結尾的點。"""
    h = entry.strip().lower()
    if not h:
        return None
    try:
        return str(ipaddress.ip_address(h.strip("[]")))  # 純 IPv6（例如 fe80::1）不能交給 urlsplit
    except ValueError:
        pass
    name = _hostname(h)
    return name.rstrip(".") or None if name else None


class LocalOnly:
    """只接受本機來源的請求。

    - Host 必須是本機名稱：擋 DNS rebinding（惡意網域解析到 127.0.0.1 後以同源身分讀資料）。
    - 有 Origin 時（瀏覽器的 POST 與 WebSocket 一定會帶），來源也必須是本機：
      瀏覽器不對 WebSocket 套用 CORS，不檢查的話任何網頁都能連 /ws 讀走所有 prompt 與輸出。
    """

    def __init__(self, app, allowed: set[str]):
        self.app, self.allowed = app, {h.lower() for h in allowed}

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            ok = _hostname(headers.get("host", "")) in self.allowed
            origin = headers.get("origin")
            checks_origin = scope["type"] == "websocket" or scope.get("method") not in ("GET", "HEAD")
            if ok and origin is not None and checks_origin:
                ok = _hostname(origin) in self.allowed
            if not ok:
                if scope["type"] == "websocket":
                    await receive()  # websocket.connect
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                    await send({"type": "http.response.body", "body": "只接受本機存取".encode()})
                return
        await self.app(scope, receive, send)


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


class AccountIn(_In):
    name: str
    provider: str = "claude"


class CodeIn(_In):
    code: str


class KeyIn(_In):
    key: str


class NodeIn(_In):
    kind: str
    label: str
    model: str = ""
    role: str = ""
    account: str | None = None
    timeout: int | None = None
    host: str = ""
    base_url: str = ""


class NodePatch(_In):  # 只改有送來的欄位
    label: str | None = None
    model: str | None = None
    role: str | None = None
    account: str | None = None
    timeout: int | None = None
    host: str | None = None
    base_url: str | None = None


class StageIn(_In):
    name: str
    node: str
    template: str


class PipelineIn(_In):
    steps: list[StageIn]


@contextlib.contextmanager
def account_errors():
    try:
        yield
    except KeyError as e:
        raise HTTPException(404, "未知的帳號或節點") from e
    except (ValueError, ConnectorError) as e:
        raise HTTPException(409, str(e)) from e


def create_app(
    force_fake: bool | None = None, data_dir: Path | None = None, allowed_hosts: set[str] | None = None,
    config: Path | None = None,
) -> FastAPI:
    if force_fake is None:
        force_fake = os.environ.get("FACTORY_FAKE") == "1"
    if allowed_hosts is None:  # 例如要從區網其他電腦開：FACTORY_ALLOWED_HOSTS=192.168.1.10
        extra = os.environ.get("FACTORY_ALLOWED_HOSTS", "")
        allowed_hosts = set(DEFAULT_HOSTS) | {n for n in map(normalize_host, extra.split(",")) if n}
    # 桌面版把設定與資料放在 %LOCALAPPDATA%\AIFactory（安裝資料夾可能不能寫），用環境變數指定。
    data_dir = data_dir or Path(os.environ.get("FACTORY_DATA_DIR") or ROOT / "data")
    # Dashboard 會把節點與生產線存回設定檔；預設放在資料夾裡，repo 的 nodes.json 只當全新安裝的起點。
    config = config or Path(os.environ.get("FACTORY_CONFIG") or data_dir / "nodes.json")
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "nodes.json", config)
    factory = Factory(config, data_dir, force_fake)

    async def refresher() -> None:
        while True:
            await asyncio.sleep(REFRESH_EVERY)
            with contextlib.suppress(Exception):
                await factory.refresh()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await factory.accounts.refresh()
        await factory.refresh()
        loop_task = asyncio.create_task(refresher())
        yield
        loop_task.cancel()
        await factory.shutdown()

    app = FastAPI(title="AI Factory", lifespan=lifespan)
    app.add_middleware(LocalOnly, allowed=allowed_hosts)
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
        with account_errors():
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

    # ---------- 節點與生產線設定 ----------
    @app.post("/api/nodes")
    async def add_node(body: NodeIn):
        with account_errors():
            return await factory.add_node(body.model_dump())

    @app.patch("/api/nodes/{nid}")
    async def update_node(nid: str, body: NodePatch):
        with account_errors():
            return await factory.update_node(nid, body.model_dump(exclude_unset=True))

    @app.delete("/api/nodes/{nid}")
    async def remove_node(nid: str):
        with account_errors():
            await factory.remove_node(nid)
        return {"ok": True}

    @app.put("/api/pipeline")
    async def set_pipeline(body: PipelineIn):
        with account_errors():
            return await factory.set_pipeline([s.model_dump() for s in body.steps])

    @app.get("/api/ollama/models")
    async def list_ollama_models(host: str = ""):
        with account_errors():
            return {"models": await asyncio.to_thread(ollama_models, host)}

    # ---------- AI 帳號 ----------
    @app.get("/api/accounts")
    async def accounts():
        await factory.accounts.refresh()
        await factory.accounts_changed()
        return factory.accounts.public()

    @app.post("/api/accounts/refresh")
    async def refresh_accounts():
        await factory.accounts.refresh()
        await factory.accounts_changed()
        return {"ok": True}

    @app.post("/api/accounts")
    async def add_account(body: AccountIn):
        with account_errors():
            a = factory.accounts.add(body.name, body.provider)
        await factory.accounts_changed()
        return a

    @app.post("/api/accounts/{aid}/key")
    async def set_key(aid: str, body: KeyIn):
        with account_errors():
            factory.accounts.set_key(aid, body.key)
        await factory.accounts_changed()
        return {"ok": True}

    @app.post("/api/accounts/{aid}/login")
    async def login(aid: str):
        with account_errors():
            factory.accounts.start_login(aid)
        await factory.accounts_changed()
        return {"ok": True}

    @app.post("/api/accounts/{aid}/code")
    async def login_code(aid: str, body: CodeIn):
        with account_errors():
            factory.accounts.submit_code(aid, body.code)
        return {"ok": True}

    @app.post("/api/accounts/{aid}/cancel")
    async def cancel_login(aid: str):
        with account_errors():
            await factory.accounts.cancel_login(aid)
        await factory.accounts_changed()
        return {"ok": True}

    @app.post("/api/accounts/{aid}/logout")
    async def logout(aid: str):
        with account_errors():
            await factory.accounts.logout(aid)
        await factory.accounts_changed()
        return {"ok": True}

    @app.delete("/api/accounts/{aid}")
    async def remove_account(aid: str):
        with account_errors():
            await factory.remove_account(aid)
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

        try:
            await factory.refresh()
            # 訂閱與取快照之間不能有 await：否則快照之前的事件會在快照之後重播，
            # 前端會把上一階段的串流接到新階段後面。
            factory.subscribers.add(sub)
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
