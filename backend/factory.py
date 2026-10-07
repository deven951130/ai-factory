"""工廠核心：節點狀態、任務管線、AI 向使用者提問（暫停 / 續跑）、事件廣播。"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .connectors import Connector, ConnectorError, build

Emit = Callable[[dict], Awaitable[None]]

# AI 回覆中出現 [[ASK_USER: 問題]] 就暫停管線，等使用者在 Dashboard 回答。
ASK_RE = re.compile(r"\[\[ASK_USER:\s*(.+?)\]\]", re.S)
ASK_PLACEHOLDER = "你的問題"
ASK_HINT = (
    f"\n\n（若你缺少必要資訊而無法繼續，請只輸出 [[ASK_USER: {ASK_PLACEHOLDER}]]，系統會立刻轉問使用者。）"
)
ASK_FINAL = "\n\n（已無法再提問，請依現有資訊直接完成。）"
MAX_RETRY = 2
MAX_ASKS = 3  # 每個階段最多問使用者幾次，避免無限追問


def find_question(out: str, prompt: str) -> str | None:
    """找出 AI 真正的提問。忽略照抄 prompt 的標記（提示文字本身、使用者輸入裡的標記）。"""
    for m in ASK_RE.finditer(out):
        q = m.group(1).strip()
        if q and q != ASK_PLACEHOLDER and m.group(0) not in prompt:
            return q
    return None


class Factory:
    def __init__(self, config_path: Path, data_dir: Path, force_fake: bool = False):
        # utf-8-sig：Windows 記事本 / PowerShell 5.1 存檔常帶 BOM。
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        self.specs = {n["id"]: n for n in cfg["nodes"]}
        self.pipeline = cfg["pipeline"]
        data_dir.mkdir(parents=True, exist_ok=True)
        workdir = data_dir / "workspace"  # CLI 在空資料夾執行，讀不到本專案與事件紀錄
        workdir.mkdir(exist_ok=True)
        self.connectors: dict[str, Connector] = {
            n["id"]: build(n, force_fake, workdir) for n in cfg["nodes"]
        }
        self.nodes: dict[str, dict[str, Any]] = {
            nid: {"id": nid, "label": s["label"], "role": s.get("role", ""), "status": "idle",
                  "detail": "", "last_active": None, "task_id": None}
            for nid, s in self.specs.items()
        }
        self.tasks: dict[str, dict[str, Any]] = {}
        self.questions: dict[str, asyncio.Future] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.subscribers: set[Emit] = set()
        self.data_dir = data_dir
        self._log_file = data_dir / "events.jsonl"
        self._lock: dict[str, asyncio.Lock] = {nid: asyncio.Lock() for nid in self.nodes}

    # ---------- 事件 ----------
    async def emit(self, event: dict) -> None:
        event["ts"] = time.time()
        try:
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except (OSError, UnicodeError) as e:  # 寫 log 失敗不能影響任務與節點狀態
            print(f"[factory] events.jsonl 寫入失敗：{e}", file=sys.stderr)
        for sub in list(self.subscribers):
            try:
                await sub(event)
            except Exception:
                self.subscribers.discard(sub)

    def snapshot(self) -> dict:
        return {"nodes": list(self.nodes.values()), "tasks": list(self.tasks.values()),
                "pipeline": [{"name": s["name"], "node": s["node"]} for s in self.pipeline]}

    async def refresh(self, only: str | None = None) -> None:
        """重新偵測節點是否可用。available() 會做阻塞 I/O，放到 thread 跑。"""
        targets = {only: self.connectors[only]} if only else self.connectors
        results = await asyncio.to_thread(lambda: {nid: c.available() for nid, c in targets.items()})
        for nid, (ok, detail) in results.items():
            n = self.nodes[nid]
            before = (n["status"], n["detail"])
            if not ok:
                n["status"], n["detail"] = "offline", detail
            elif n["status"] in ("offline", "idle"):
                n["status"], n["detail"] = "idle", detail
            if (n["status"], n["detail"]) != before:
                await self.emit({"type": "node", "node": dict(n)})

    def _waiting_task_for(self, nid: str) -> str | None:
        for tid, t in self.tasks.items():
            if t.get("question") and t["question"]["node"] == nid:
                return tid
        return None

    async def _set(self, nid: str, status: str, detail: str = "", task_id: str | None = None) -> None:
        n = self.nodes[nid]
        n.update(status=status, detail=detail, last_active=time.time(), task_id=task_id)
        await self.emit({"type": "node", "node": dict(n)})

    async def _settle(self, nid: str, detail: str) -> None:
        # 節點空下來時，若還有任務在等使用者回答這個節點的問題，維持「等你回答」。
        waiting = self._waiting_task_for(nid)
        if waiting:
            await self._set(nid, "waiting", "等待你的回答", waiting)
        else:
            await self._set(nid, "idle", detail, None)

    # ---------- 單次呼叫（含重試） ----------
    async def call(self, nid: str, prompt: str, task_id: str | None = None, stage: str = "") -> str:
        conn = self.connectors[nid]
        async with self._lock[nid]:  # 同一節點一次只跑一件，避免撞額度
            last_err = ""
            for attempt in range(MAX_RETRY + 1):
                await self._set(nid, "running", f"{stage} 嘗試 {attempt + 1}", task_id)
                await self.emit({"type": "attempt", "node": nid, "task_id": task_id, "attempt": attempt + 1})

                async def chunk(text: str) -> None:
                    await self.emit({"type": "chunk", "node": nid, "task_id": task_id, "text": text})

                try:
                    out = await conn.run(prompt, chunk)
                    await self._settle(nid, "完成")
                    return out
                except asyncio.CancelledError:
                    await self._settle(nid, "已取消")
                    raise
                except Exception as e:  # 非預期錯誤也要轉成節點失敗，不能讓任務卡在 running
                    last_err = str(e) if isinstance(e, ConnectorError) else f"{type(e).__name__}: {e}"
                    retryable = getattr(e, "retryable", True)
                    await self.emit({"type": "log", "level": "error", "node": nid, "task_id": task_id,
                                     "text": f"{nid} 失敗（{attempt + 1}/{MAX_RETRY + 1}）：{last_err}"})
                    if not retryable:
                        break
                    await asyncio.sleep(0.2 * (attempt + 1))
            await self._set(nid, "failed", last_err, task_id)
            raise ConnectorError(last_err, retryable=False)

    # ---------- 使用者對話 ----------
    async def chat(self, nid: str, message: str) -> str:
        await self.emit({"type": "chat", "role": "user", "node": nid, "text": message})
        ok = True
        try:
            reply = await self.call(nid, message, None, "對話")
        except Exception as e:
            ok, reply = False, f"（{self.nodes[nid]['label']} 無法回應：{e}）"
        await self.emit({"type": "chat", "role": "ai", "node": nid, "text": reply, "ok": ok})
        return reply

    # ---------- 任務管線 ----------
    def create_task(self, title: str, prompt: str) -> dict:
        t = {"id": uuid.uuid4().hex[:8], "title": title, "prompt": prompt, "status": "queued",
             "stage": None, "stage_index": -1, "outputs": [], "question": None, "error": None,
             "created_at": time.time()}
        self.tasks[t["id"]] = t
        return t

    def start_task(self, title: str, prompt: str) -> dict:
        t = self.create_task(title, prompt)
        job = asyncio.create_task(self.run_task(t["id"]))
        self.jobs[t["id"]] = job
        job.add_done_callback(lambda _: self.jobs.pop(t["id"], None))
        return t

    async def _run_stage(self, t: dict, step: dict, data: str) -> str:
        # 用 replace 而不是 str.format：模板裡的 JSON 大括號不會被當成欄位。
        base = step["template"].replace("{input}", data)
        qa: list[tuple[str, str]] = []
        while True:
            extra = "".join(f"\n\n補充資訊（你問「{q}」，使用者回答）：{a}" for q, a in qa)
            final = len(qa) >= MAX_ASKS
            prompt = base + extra + (ASK_FINAL if final else ASK_HINT)
            out = await self.call(step["node"], prompt, t["id"], step["name"])
            q = None if final else find_question(out, prompt)
            if q is None:
                return out
            qa.append((q, await self._ask_user(t, step["node"], q)))

    async def run_task(self, task_id: str) -> None:
        t = self.tasks[task_id]
        try:
            t["status"] = "running"
            await self.emit({"type": "task", "task": dict(t)})
            data = t["prompt"]
            for i, step in enumerate(self.pipeline):
                t.update(stage=step["name"], stage_index=i)
                await self.emit({"type": "task", "task": dict(t)})
                out = await self._run_stage(t, step, data)
                t["outputs"].append({"stage": step["name"], "node": step["node"], "text": out})
                data = out
            t.update(status="done", stage=None)
        except asyncio.CancelledError:
            t.update(status="cancelled", question=None)
        except Exception as e:  # 任何錯誤都要讓任務落在終止狀態，不能卡在 running
            err = str(e) if isinstance(e, ConnectorError) else f"{type(e).__name__}: {e}"
            t.update(status="blocked", error=err, question=None)
            await self.emit({"type": "log", "level": "error", "task_id": task_id,
                             "text": f"任務 {task_id} 已阻擋，需要人工介入：{err}"})
        finally:
            self.questions.pop(task_id, None)
            await self.emit({"type": "task", "task": dict(t)})

    async def _ask_user(self, task: dict, nid: str, question: str) -> str:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.questions[task["id"]] = fut
        task.update(status="waiting_user", question={"node": nid, "text": question})
        await self._set(nid, "waiting", "等待你的回答", task["id"])
        await self.emit({"type": "task", "task": dict(task)})
        await self.emit({"type": "ask", "task_id": task["id"], "node": nid, "text": question})
        try:
            answer = await fut
        finally:
            self.questions.pop(task["id"], None)
            task.update(question=None)
            if self.nodes[nid]["status"] == "waiting" and self.nodes[nid]["task_id"] == task["id"]:
                await self._settle(nid, "")
        task.update(status="running")
        await self.emit({"type": "task", "task": dict(task)})
        return answer

    def answer(self, task_id: str, text: str) -> bool:
        fut = self.questions.get(task_id)
        if not fut or fut.done():
            return False
        fut.set_result(text)
        return True

    def cancel(self, task_id: str) -> bool:
        job = self.jobs.get(task_id)
        if not job or job.done():
            return False
        job.cancel()  # 進行中的 CLI 子程序會連同子孫一起被砍掉
        return True

    async def shutdown(self) -> None:
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
