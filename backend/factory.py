"""工廠核心：節點狀態、任務管線、AI 向使用者提問（暫停 / 續跑）、事件廣播。"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .connectors import Connector, ConnectorError, build

Emit = Callable[[dict], Awaitable[None]]

# AI 回覆中出現 [[ASK_USER: 問題]] 就暫停管線，等使用者在 Dashboard 回答。
ASK_RE = re.compile(r"\[\[ASK_USER:\s*(.+?)\]\]", re.S)
ASK_HINT = (
    "\n\n（若你缺少必要資訊而無法繼續，請只輸出 [[ASK_USER: 你的問題]]，系統會立刻轉問使用者。）"
)
MAX_RETRY = 2


class Factory:
    def __init__(self, config_path: Path, data_dir: Path, force_fake: bool = False):
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        self.specs = {n["id"]: n for n in cfg["nodes"]}
        self.pipeline = cfg["pipeline"]
        self.connectors: dict[str, Connector] = {n["id"]: build(n, force_fake) for n in cfg["nodes"]}
        self.nodes: dict[str, dict[str, Any]] = {
            nid: {"id": nid, "label": s["label"], "role": s.get("role", ""), "status": "idle",
                  "detail": "", "last_active": None, "task_id": None}
            for nid, s in self.specs.items()
        }
        self.tasks: dict[str, dict[str, Any]] = {}
        self.questions: dict[str, asyncio.Future] = {}
        self.subscribers: set[Emit] = set()
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = data_dir / "events.jsonl"
        self._lock: dict[str, asyncio.Lock] = {nid: asyncio.Lock() for nid in self.nodes}

    # ---------- 事件 ----------
    async def emit(self, event: dict) -> None:
        event["ts"] = time.time()
        with self._log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        for sub in list(self.subscribers):
            try:
                await sub(event)
            except Exception:
                self.subscribers.discard(sub)

    def snapshot(self) -> dict:
        return {"nodes": list(self.nodes.values()), "tasks": list(self.tasks.values()),
                "pipeline": [{"name": s["name"], "node": s["node"]} for s in self.pipeline]}

    def refresh_availability(self) -> None:
        for nid, c in self.connectors.items():
            ok, detail = c.available()
            n = self.nodes[nid]
            if not ok:
                n["status"], n["detail"] = "offline", detail
            elif n["status"] == "offline":
                n["status"], n["detail"] = "idle", detail
            else:
                n["detail"] = n["detail"] or detail

    async def _set(self, nid: str, status: str, detail: str = "", task_id: str | None = None) -> None:
        n = self.nodes[nid]
        n.update(status=status, detail=detail, last_active=time.time(), task_id=task_id)
        await self.emit({"type": "node", "node": dict(n)})

    # ---------- 單次呼叫（含重試） ----------
    async def call(self, nid: str, prompt: str, task_id: str | None = None, stage: str = "") -> str:
        conn = self.connectors[nid]
        async with self._lock[nid]:  # 同一節點一次只跑一件，避免撞額度
            last_err = ""
            for attempt in range(MAX_RETRY + 1):
                await self._set(nid, "running", f"{stage} 嘗試 {attempt + 1}", task_id)

                async def chunk(text: str) -> None:
                    await self.emit({"type": "chunk", "node": nid, "task_id": task_id, "text": text})

                try:
                    out = await conn.run(prompt, chunk)
                    await self._set(nid, "idle", "完成", None)
                    return out
                except Exception as e:  # 非預期錯誤也要轉成節點失敗，不能讓任務卡在 running
                    last_err = str(e) if isinstance(e, ConnectorError) else f"{type(e).__name__}: {e}"
                    await self.emit({"type": "log", "level": "error", "node": nid, "task_id": task_id,
                                     "text": f"{nid} 失敗（{attempt + 1}/{MAX_RETRY + 1}）：{last_err}"})
                    await asyncio.sleep(0.2 * (attempt + 1))
            await self._set(nid, "failed", last_err, task_id)
            raise ConnectorError(last_err)

    # ---------- 使用者對話 ----------
    async def chat(self, nid: str, message: str) -> str:
        await self.emit({"type": "chat", "role": "user", "node": nid, "text": message})
        try:
            reply = await self.call(nid, message, None, "對話")
        except ConnectorError as e:
            reply = f"（{nid} 無法回應：{e}）"
        await self.emit({"type": "chat", "role": "ai", "node": nid, "text": reply})
        return reply

    # ---------- 任務管線 ----------
    def create_task(self, title: str, prompt: str) -> dict:
        t = {"id": uuid.uuid4().hex[:8], "title": title, "prompt": prompt, "status": "queued",
             "stage": None, "stage_index": -1, "outputs": [], "question": None, "error": None,
             "created_at": time.time()}
        self.tasks[t["id"]] = t
        return t

    async def run_task(self, task_id: str) -> None:
        t = self.tasks[task_id]
        t["status"] = "running"
        await self.emit({"type": "task", "task": dict(t)})
        data = t["prompt"]
        try:
            for i, step in enumerate(self.pipeline):
                t.update(stage=step["name"], stage_index=i)
                await self.emit({"type": "task", "task": dict(t)})
                prompt = step["template"].format(input=data) + ASK_HINT
                out = await self.call(step["node"], prompt, task_id, step["name"])
                while (m := ASK_RE.search(out)):  # AI 提問 → 暫停等人
                    answer = await self._ask_user(t, step["node"], m.group(1).strip())
                    prompt = f"{step['template'].format(input=data)}\n\n補充資訊（使用者回答「{m.group(1).strip()}」）：{answer}"
                    out = await self.call(step["node"], prompt, task_id, step["name"])
                t["outputs"].append({"stage": step["name"], "node": step["node"], "text": out})
                data = out
            t.update(status="done", stage=None)
        except ConnectorError as e:
            t.update(status="blocked", error=str(e))
            await self.emit({"type": "log", "level": "error", "task_id": task_id,
                             "text": f"任務 {task_id} 已阻擋，需要人工介入：{e}"})
        await self.emit({"type": "task", "task": dict(t)})

    async def _ask_user(self, task: dict, nid: str, question: str) -> str:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.questions[task["id"]] = fut
        task.update(status="waiting_user", question={"node": nid, "text": question})
        await self._set(nid, "waiting", "等待你的回答", task["id"])
        await self.emit({"type": "task", "task": dict(task)})
        await self.emit({"type": "ask", "task_id": task["id"], "node": nid, "text": question})
        answer = await fut
        self.questions.pop(task["id"], None)
        task.update(status="running", question=None)
        await self.emit({"type": "task", "task": dict(task)})
        return answer

    def answer(self, task_id: str, text: str) -> bool:
        fut = self.questions.get(task_id)
        if not fut or fut.done():
            return False
        fut.set_result(text)
        return True
