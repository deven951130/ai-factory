"""工廠核心：節點與生產線設定、任務管線、AI 向使用者提問（暫停 / 續跑）、事件廣播。

節點與生產線都由使用者在 Dashboard 新增 / 修改，存回設定檔（nodes.json）。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .accounts import Accounts, ClaudeAuth, CodexAuth, FakeAuth
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

# 每種 AI 用哪一類帳號；optional：可以不指定帳號（例如本機的 OpenAI 相容伺服器不需要金鑰）。
KINDS = {
    "claude": {"provider": "claude", "timeout": 900},
    "codex": {"provider": "codex", "timeout": 900},
    "gemini": {"provider": "gemini", "timeout": 600},
    "ollama": {"provider": None, "timeout": 300},
    "openai": {"provider": "openai", "timeout": 300, "optional": True},
}
LABEL_MAX, ROLE_MAX, MODEL_MAX, ADDR_MAX = 40, 60, 100, 300
STAGE_NAME_MAX, TEMPLATE_MAX, STAGES_MAX = 30, 8000, 20
TIMEOUT_RANGE = (30, 3600)
SPEC_FIELDS = ("label", "model", "role", "timeout", "account", "host", "base_url")


def find_question(out: str, prompt: str) -> str | None:
    """找出 AI 真正的提問。忽略照抄 prompt 的標記（提示文字本身、使用者輸入裡的標記）。"""
    for m in ASK_RE.finditer(out):
        q = m.group(1).strip()
        if q and q != ASK_PLACEHOLDER and m.group(0) not in prompt:
            return q
    return None


def _text(value: Any, limit: int, what: str, required: bool = False) -> str:
    s = str(value or "").strip()
    if required and not s:
        raise ValueError(f"{what}是空的")
    if len(s) > limit:
        raise ValueError(f"{what}太長（最多 {limit} 字）")
    return s


class Factory:
    def __init__(self, config_path: Path, data_dir: Path, force_fake: bool = False):
        self.config_path = config_path
        self.force_fake = force_fake
        # utf-8-sig：Windows 記事本 / PowerShell 5.1 存檔常帶 BOM。
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        self.specs: dict[str, dict] = {n["id"]: n for n in cfg.get("nodes", []) if n.get("kind") in KINDS}
        self.pipeline: list[dict] = [s for s in cfg.get("pipeline", []) if s.get("node") in self.specs]
        data_dir.mkdir(parents=True, exist_ok=True)
        self.workdir = data_dir / "workspace"  # CLI 在空資料夾執行，讀不到本專案與事件紀錄
        self.workdir.mkdir(exist_ok=True)
        self.connectors: dict[str, Connector] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self._lock: dict[str, asyncio.Lock] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self._task_steps: dict[str, list[dict]] = {}
        self.questions: dict[str, asyncio.Future] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.subscribers: set[Emit] = set()
        self.data_dir = data_dir
        self._log_file = data_dir / "events.jsonl"
        if force_fake:  # 模擬模式不碰真正的 CLI、帳號清單與家目錄
            fake = FakeAuth()
            self.accounts = Accounts(data_dir / "fake-accounts.json", {"claude": fake, "codex": fake},
                                     data_dir / "fake-accounts")
        else:
            self.accounts = Accounts(data_dir / "accounts.json",
                                     {"claude": ClaudeAuth(self.workdir), "codex": CodexAuth(self.workdir)},
                                     data_dir / "accounts")
        self.accounts.on_change = self.accounts_changed
        if self._migrate_accounts():
            self._write_config(self.specs, self.pipeline)
        for nid in self.specs:
            self._mount(nid)
        self._apply_accounts()

    # ---------- 設定 ----------
    def _migrate_accounts(self) -> bool:
        """舊版把 Claude 節點的帳號分配存在 accounts.json；搬進節點設定。"""
        moved = False
        for nid, spec in self.specs.items():
            if "account" not in spec and nid in self.accounts.legacy_nodes:
                spec["account"] = self.accounts.legacy_nodes[nid]
                moved = True
        return moved

    def _write_config(self, specs: dict[str, dict], pipeline: list[dict]) -> None:
        data = {"nodes": list(specs.values()), "pipeline": pipeline}
        tmp = self.config_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.config_path)
        except OSError as e:
            raise ValueError(f"設定檔寫入失敗：{e}") from e

    def _mount(self, nid: str) -> None:
        spec = self.specs[nid]
        self.connectors[nid] = build(spec, self.force_fake, self.workdir)
        self._lock.setdefault(nid, asyncio.Lock())
        self.nodes.setdefault(nid, {"id": nid, "status": "idle", "detail": "", "last_active": None, "task_id": None})
        self._sync(nid)

    def _sync(self, nid: str) -> None:
        """把設定與偵測結果寫進節點卡片的資料。"""
        spec, conn = self.specs[nid], self.connectors[nid]
        self.nodes[nid].update(
            label=spec["label"], kind=spec["kind"], model=spec.get("model", ""), role=spec.get("role", ""),
            timeout=spec.get("timeout", KINDS[spec["kind"]]["timeout"]), account=spec.get("account"),
            host=spec.get("host", ""), base_url=spec.get("base_url", ""), models=list(conn.models),
            effective_model=getattr(conn, "effective_model", ""),
        )

    def _public_nodes(self) -> list[dict]:
        return [dict(self.nodes[nid]) for nid in self.specs]

    def snapshot(self) -> dict:
        return {"nodes": self._public_nodes(), "tasks": list(self.tasks.values()), "pipeline": self.pipeline,
                "accounts": self.accounts.public()}

    async def _config_changed(self) -> None:
        await self.emit({"type": "config", "nodes": self._public_nodes(), "pipeline": self.pipeline}, persist=False)

    def _validate_node(self, kind: str, data: dict, nid: str | None) -> dict:
        meta = KINDS.get(kind)
        if not meta:
            raise ValueError("不支援的 AI 類型")
        label = _text(data.get("label"), LABEL_MAX, "名稱", required=True)
        if any(s["label"] == label for i, s in self.specs.items() if i != nid):
            raise ValueError("已經有同名的 AI")
        spec: dict[str, Any] = {"id": nid, "label": label, "kind": kind,
                                "model": _text(data.get("model"), MODEL_MAX, "模型名稱", required=kind == "openai"),
                                "role": _text(data.get("role"), ROLE_MAX, "說明")}
        try:
            timeout = int(data.get("timeout") or meta["timeout"])
        except (TypeError, ValueError) as e:
            raise ValueError("逾時要是整數秒數") from e
        if not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
            raise ValueError(f"逾時要在 {TIMEOUT_RANGE[0]}–{TIMEOUT_RANGE[1]} 秒之間")
        spec["timeout"] = timeout
        aid = data.get("account") or None
        if aid is not None:
            a = self.accounts.items.get(aid)
            if not a:
                raise ValueError("找不到這個帳號")
            if a["provider"] != meta["provider"]:
                raise ValueError("帳號類型和 AI 類型不符")
        spec["account"] = aid
        if kind == "ollama":
            host = _text(data.get("host"), ADDR_MAX, "Ollama 位址")
            if host:
                spec["host"] = host
        if kind == "openai":
            url = _text(data.get("base_url"), ADDR_MAX, "API 網址", required=True)
            if not url.startswith(("http://", "https://")):
                raise ValueError("API 網址要以 http:// 或 https:// 開頭")
            spec["base_url"] = url.rstrip("/")
        return spec

    async def add_node(self, data: dict) -> dict:
        kind = data.get("kind", "")
        nid = f"{kind}-{uuid.uuid4().hex[:6]}"
        spec = self._validate_node(kind, data, nid)
        specs = {**self.specs, nid: spec}
        self._write_config(specs, self.pipeline)
        self.specs = specs
        self._mount(nid)
        self._apply_accounts()
        await self._config_changed()
        await self.refresh(nid)
        return dict(self.nodes[nid])

    async def update_node(self, nid: str, data: dict) -> dict:
        old = self.specs[nid]  # 不存在 → KeyError
        merged = {**{k: old.get(k) for k in SPEC_FIELDS}, **{k: v for k, v in data.items() if k in SPEC_FIELDS}}
        spec = self._validate_node(old["kind"], merged, nid)
        if "extra" in old:  # 手動寫在設定檔裡的進階選項
            spec["extra"] = old["extra"]
        specs = {**self.specs, nid: spec}
        self._write_config(specs, self.pipeline)
        self.specs = specs
        self._mount(nid)  # 換新的連接器；執行中的呼叫繼續用舊的，鎖沿用同一把
        self._apply_accounts()
        await self._config_changed()
        await self.refresh(nid)
        return dict(self.nodes[nid])

    async def remove_node(self, nid: str) -> None:
        self.specs[nid]  # 不存在 → KeyError
        used = [s["name"] for s in self.pipeline if s["node"] == nid]
        if used:
            raise ValueError(f"生產線的「{used[0]}」階段正在使用這個 AI，請先修改生產線")
        busy = self.nodes[nid]["status"] in ("running", "waiting") or self._lock[nid].locked() or any(
            nid in (s["node"] for s in steps) for tid, steps in self._task_steps.items() if tid in self.jobs)
        if busy:
            raise ValueError("這個 AI 還有任務在用，請等任務結束或取消後再刪除")
        specs = {i: s for i, s in self.specs.items() if i != nid}
        self._write_config(specs, self.pipeline)
        self.specs = specs
        for d in (self.connectors, self.nodes, self._lock):
            d.pop(nid, None)
        await self._config_changed()

    async def set_pipeline(self, steps: list[dict]) -> list[dict]:
        if len(steps) > STAGES_MAX:
            raise ValueError(f"最多 {STAGES_MAX} 個階段")
        clean = []
        for i, s in enumerate(steps, 1):
            name = _text(s.get("name"), STAGE_NAME_MAX, f"第 {i} 階段的名稱", required=True)
            if s.get("node") not in self.specs:
                raise ValueError(f"第 {i} 階段「{name}」沒有選擇 AI")
            template = str(s.get("template") or "")
            if not template.strip():
                raise ValueError(f"第 {i} 階段「{name}」的指令是空的")
            if len(template) > TEMPLATE_MAX:
                raise ValueError(f"第 {i} 階段「{name}」的指令太長（最多 {TEMPLATE_MAX} 字）")
            if "{input}" not in template:
                raise ValueError(f"第 {i} 階段「{name}」的指令要包含 {{input}}（代表上一階段的輸出）")
            clean.append({"name": name, "node": s["node"], "template": template})
        self._write_config(self.specs, clean)
        self.pipeline = clean
        await self._config_changed()
        return clean

    # ---------- 事件 ----------
    async def emit(self, event: dict, persist: bool = True) -> None:
        event["ts"] = time.time()
        try:
            if persist:
                with self._log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except (OSError, UnicodeError) as e:  # 寫 log 失敗不能影響任務與節點狀態
            print(f"[factory] events.jsonl 寫入失敗：{e}", file=sys.stderr)
        for sub in list(self.subscribers):
            try:
                await sub(event)
            except Exception:
                self.subscribers.discard(sub)

    # ---------- 帳號 ----------
    def _apply_accounts(self) -> None:
        """把帳號的設定資料夾 / 金鑰交給連接器。"""
        for nid, spec in self.specs.items():
            extra = {k: v for k, v in spec.get("extra", {}).items() if k not in ("config_dir", "api_key")}
            for k in ("host", "base_url"):
                if spec.get(k):
                    extra[k] = spec[k]
            a = self.accounts.items.get(spec.get("account") or "")
            if a and a["provider"] == KINDS[spec["kind"]]["provider"]:
                if spec["kind"] == "claude":
                    extra["config_dir"] = a["config_dir"]
                elif spec["kind"] == "codex":
                    extra["codex_home"] = a["config_dir"]
                elif spec["kind"] == "gemini":
                    extra.update(home=a["config_dir"], api_key=a["key"])
                elif spec["kind"] == "openai" and a["key"]:
                    extra["api_key"] = a["key"]
            conn = self.connectors[nid]
            if conn.extra != extra:
                conn.configure(extra)

    def _account_problem(self, nid: str) -> str:
        spec = self.specs[nid]
        meta = KINDS[spec["kind"]]
        if not meta["provider"]:
            return ""
        aid = spec.get("account")
        if not aid:
            # 模擬模式免登入示範；真實模式一律要在軟體裡登入，不沿用電腦上 CLI 的登入。
            return "" if meta.get("optional") or self.force_fake else "尚未指定帳號（按「設定」選擇）"
        a = self.accounts.items.get(aid)
        if not a or a["provider"] != meta["provider"]:
            return "指定的帳號已不存在（按「設定」重新選擇）"
        if aid in self.accounts.logins:
            return f"帳號「{a['name']}」登入中"
        if (self.accounts.status.get(aid) or {}).get("logged_in") is False:
            if a["provider"] in ("gemini", "openai"):
                return f"帳號「{a['name']}」還沒有輸入 API 金鑰"
            return f"帳號「{a['name']}」未登入"
        return ""

    async def accounts_changed(self) -> None:
        self._apply_accounts()
        await self.refresh()
        await self.emit({"type": "accounts", "accounts": self.accounts.public()}, persist=False)

    async def remove_account(self, aid: str) -> None:
        await self.accounts.remove(aid)
        used = [nid for nid, s in self.specs.items() if s.get("account") == aid]
        if used:
            specs = {nid: ({**s, "account": None} if nid in used else s) for nid, s in self.specs.items()}
            self._write_config(specs, self.pipeline)
            self.specs = specs
            for nid in used:
                self._sync(nid)
            await self._config_changed()
        await self.accounts_changed()

    async def refresh(self, only: str | None = None) -> None:
        """重新偵測節點是否可用。available() 會做阻塞 I/O，放到 thread 跑。"""
        targets = {only: self.connectors[only]} if only else dict(self.connectors)
        results = await asyncio.to_thread(lambda: {nid: c.available() for nid, c in targets.items()})
        for nid, (ok, detail) in results.items():
            if nid not in self.specs or self.connectors.get(nid) is not targets[nid]:
                continue  # 偵測期間節點被刪除或換了設定
            if ok and (problem := self._account_problem(nid)):
                ok, detail = False, problem
            n = self.nodes[nid]
            before = (n["status"], n["detail"], n["models"], n["effective_model"])
            self._sync(nid)
            if not ok:
                n["status"], n["detail"] = "offline", detail
            elif n["status"] in ("offline", "idle"):
                n["status"], n["detail"] = "idle", detail
                waiting = self._waiting_task_for(nid)
                if waiting:  # 恢復上線時，若有任務在等這個節點的問題，維持「等你回答」
                    n.update(status="waiting", detail="等待你的回答", task_id=waiting)
            if (n["status"], n["detail"], n["models"], n["effective_model"]) != before:
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
        if nid not in self.specs:
            raise ConnectorError("這個 AI 已被刪除", retryable=False)
        if problem := self._account_problem(nid):  # 沒有帳號時不能退回電腦上 CLI 的登入
            await self._set(nid, "offline", problem)
            raise ConnectorError(problem, retryable=False)
        conn = self.connectors[nid]
        async with self._lock[nid]:  # 同一節點一次只跑一件，避免撞額度
            # 取消可能發生在任何 await（執行中、重試等待、寫事件），一律在這裡結算節點狀態。
            # 放在鎖裡面：還在等鎖時被取消，不能去改別人正在用的節點。
            try:
                return await self._attempts(nid, conn, prompt, task_id, stage)
            except asyncio.CancelledError:
                await self._settle(nid, "已取消")
                raise

    async def _attempts(self, nid: str, conn: Connector, prompt: str, task_id: str | None, stage: str) -> str:
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
            except Exception as e:  # 非預期錯誤也要轉成節點失敗，不能讓任務卡在 running
                last_err = str(e) if isinstance(e, ConnectorError) else f"{type(e).__name__}: {e}"
                retryable = getattr(e, "retryable", True)
            await self.emit({"type": "log", "level": "error", "node": nid, "task_id": task_id,
                             "text": f"{self.nodes[nid]['label']} 失敗（{attempt + 1}/{MAX_RETRY + 1}）：{last_err}"})
            if not retryable or attempt == MAX_RETRY:
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
            label = self.nodes[nid]["label"] if nid in self.nodes else nid
            ok, reply = False, f"（{label} 無法回應：{e}）"
        await self.emit({"type": "chat", "role": "ai", "node": nid, "text": reply, "ok": ok})
        return reply

    # ---------- 任務管線 ----------
    def create_task(self, title: str, prompt: str) -> dict:
        if not self.pipeline:
            raise ValueError("生產線還沒有任何階段，請先在「生產線」按「編輯」設定")
        steps = [dict(s) for s in self.pipeline]  # 任務照建立當下的生產線跑，之後改設定不影響它
        t = {"id": uuid.uuid4().hex[:8], "title": title, "prompt": prompt, "status": "queued",
             "stage": None, "stage_index": -1, "steps": [{"name": s["name"], "node": s["node"]} for s in steps],
             "outputs": [], "question": None, "error": None, "created_at": time.time()}
        self.tasks[t["id"]] = t
        self._task_steps[t["id"]] = steps
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
            for i, step in enumerate(self._task_steps[task_id]):
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
            self._task_steps.pop(task_id, None)
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
            if nid in self.nodes and self.nodes[nid]["status"] == "waiting" and self.nodes[nid]["task_id"] == task["id"]:
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
        await self.accounts.shutdown()
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
