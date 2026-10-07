"""AI 連接器：CLI（Claude / Gemini）與 HTTP（Ollama）。

全部走「訂閱 CLI / 本地服務」，不操作網頁 DOM，不需要 API 金鑰。
每個連接器只做一件事：run(prompt, on_chunk) -> 完整回覆文字。
"""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import httpx

OnChunk = Callable[[str], Awaitable[None]]

# 純文字生成情境不需要工具：主要靠 `--tools ""` 全部關掉；這份清單是保險，
# 萬一某版本把空字串解讀成「預設工具」時仍擋住危險工具。
# PowerShell：Windows 沒裝 Git Bash 時 Claude Code 會改開這個工具。
CLAUDE_DISALLOWED = (
    "Bash PowerShell Edit Write Read Glob Grep NotebookEdit WebFetch WebSearch Task TodoWrite"
)
# 子程序不繼承這些變數，避免 claude -p 改走按量計費的 API 金鑰而不是訂閱。
CLAUDE_API_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
DEFAULT_TIMEOUT = 300.0


class ConnectorError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


def ollama_base(host: str) -> str:
    """OLLAMA_HOST 也是 Ollama 伺服器自己的綁定設定，常見 "0.0.0.0" 或 "127.0.0.1:11434"（無 scheme）。"""
    host = (host or "").strip().rstrip("/") or "127.0.0.1:11434"
    if "://" not in host:
        host = "http://" + host
    scheme, rest = host.split("://", 1)
    if rest.startswith("0.0.0.0"):
        rest = "127.0.0.1" + rest[len("0.0.0.0"):]
    if ":" not in rest.split("/")[0]:
        rest = rest + ":11434"
    return f"{scheme}://{rest}"


@dataclass
class Connector:
    id: str
    label: str
    kind: str  # claude | gemini | ollama | fake
    model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    workdir: Path | None = None  # CLI 的工作目錄：空資料夾，不讓 AI 讀到本專案
    extra: dict = field(default_factory=dict)

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    async def run(self, prompt: str, on_chunk: OnChunk) -> str:
        raise NotImplementedError


def _which(name: str) -> str | None:
    # Windows 上 npm 安裝的 CLI 是 claude.cmd / gemini.cmd；
    # create_subprocess_exec 不會套 PATHEXT，所以一律先解析成完整路徑。
    return shutil.which(name)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """砍掉整棵子程序樹。

    Windows：.cmd 由 cmd.exe 執行，proc 只是 cmd.exe，真正的 node.exe 是孫程序；
    POSIX：gemini 會再 relaunch 一個子 node。只殺 proc 會留下持續消耗額度的孤兒。
    """
    if sys.platform != "win32":
        # 不看 returncode：直接子程序可能已結束，但孫程序仍在同一個 process group 裡。
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.returncode is None:
        if sys.platform == "win32":
            try:
                k = await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(k.wait(), 10)
            except Exception:
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass


async def run_subprocess(
    cmd: list[str],
    stdin_text: str,
    on_text: OnChunk,
    timeout: float,
    env: dict | None = None,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """執行子程序並串流 stdout。回傳 (exit code, stdout, stderr)。"""
    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True  # 讓 _kill_tree 能用 killpg 砍整組
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(cwd) if cwd else None,
            **kwargs,
        )
    except FileNotFoundError as e:
        raise ConnectorError(f"找不到執行檔：{cmd[0]}", retryable=False) from e
    except NotImplementedError as e:  # Windows + SelectorEventLoop（例如 uvicorn --reload）
        raise ConnectorError("目前事件迴圈不支援子程序；請勿用 --reload 啟動", retryable=False) from e

    async def feed() -> None:
        assert proc.stdin
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 子程序提早結束；結束碼與輸出會說明原因

    async def read_out() -> str:
        assert proc.stdout
        # 增量解碼：中文字 3 bytes，固定長度切塊會把字切成兩半。
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts: list[str] = []
        while True:
            chunk = await proc.stdout.read(4096)
            text = dec.decode(chunk, final=not chunk)
            if text:
                parts.append(text)
                await on_text(text)
            if not chunk:
                break
        return "".join(parts)

    async def read_err() -> bytes:
        # 必須同時讀 stderr，否則輸出量大時管線塞滿、子程序卡死。
        assert proc.stderr
        return await proc.stderr.read()

    try:
        _, out, err_b = await asyncio.wait_for(asyncio.gather(feed(), read_out(), read_err()), timeout)
        code = await proc.wait()
    except asyncio.TimeoutError as e:
        await _kill_tree(proc)
        # 逾時不自動重試：同一個長請求再跑一次通常也會逾時，只會重複消耗額度。
        raise ConnectorError(f"逾時（{timeout:.0f}s）", retryable=False) from e
    except BaseException:
        await _kill_tree(proc)  # 被取消（例如伺服器關閉）時也不留孤兒
        raise
    return code, out, err_b.decode("utf-8", errors="replace")


def _fail_detail(code: int, out: str, err: str) -> str:
    # 有些 CLI（例如 claude）把錯誤印在 stdout，stderr 是空的。
    detail = err.strip() or out.strip() or "（沒有輸出）"
    return f"exit {code}: {detail[-300:]}"


class ClaudeConnector(Connector):
    def available(self):
        exe = _which("claude")
        return (bool(exe), exe or "未安裝 claude CLI")

    def _env(self) -> dict:
        env = dict(os.environ)
        if not self.extra.get("use_api_key"):
            for k in CLAUDE_API_ENV:
                env.pop(k, None)
        return env

    async def run(self, prompt, on_chunk):
        exe = _which("claude") or "claude"
        # stream-json：純文字模式要等整段生成完才一次輸出；這裡逐 token 串流，
        # 並從最後的 result 事件取得完整回覆與 is_error。
        cmd = [
            exe, "-p", "--model", self.model or "sonnet",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--strict-mcp-config",
            # 不給任何工具：有工具時模型可能「寫答案 → 呼叫工具 → 再寫一段」，
            # 而 result 只保留最後一輪，前面的內容會遺失。
            "--tools", "",
            "--disallowed-tools", *CLAUDE_DISALLOWED.split(),
        ]
        buf = ""
        streamed: list[str] = []
        turns: list[str] = []  # 每一輪 assistant 訊息的完整文字
        result: dict | None = None

        async def on_text(text: str) -> None:
            nonlocal buf, result
            buf += text
            *lines, buf = buf.split("\n")  # NDJSON 可能被讀取切成半行
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "stream_event":
                    delta = ev.get("event", {}).get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        streamed.append(delta["text"])
                        await on_chunk(delta["text"])
                elif ev.get("type") == "assistant":
                    blocks = ev.get("message", {}).get("content") or []
                    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                    if text.strip():
                        turns.append(text.strip())
                elif ev.get("type") == "result":
                    result = ev

        code, out, err = await run_subprocess(cmd, prompt, on_text, self.timeout, self._env(), self.workdir)
        if buf.strip():
            await on_text("\n")
        if result is not None:
            text = str(result.get("result") or "")
            if result.get("is_error"):
                # claude 內部已重試過（例如 429）；這類錯誤再重試多半也一樣。
                raise ConnectorError(text[-300:] or _fail_detail(code, out, err), retryable=False)
            if len(turns) > 1:  # 保險：多輪時 result 只有最後一輪，改用全部輪次
                return "\n\n".join(turns)
            return text.strip() or "".join(streamed).strip()
        if code != 0:
            raise ConnectorError(_fail_detail(code, out, err))
        return "".join(streamed).strip()


class GeminiConnector(Connector):
    def available(self):
        exe = _which("gemini")
        return (bool(exe), exe or "未安裝 gemini CLI")

    def _env(self) -> dict:
        env = dict(os.environ)
        env.update(
            # 新版 Gemini CLI 在未信任的資料夾拒絕無頭執行（exit 55）。
            GEMINI_CLI_TRUST_WORKSPACE="true",
            # 不 relaunch 成子 node，逾時時才砍得乾淨。
            GEMINI_CLI_NO_RELAUNCH="true",
            NO_COLOR="1",
        )
        return env

    async def run(self, prompt, on_chunk):
        # prompt 走 stdin（非 TTY → 非互動模式）；Windows 的 .cmd 會吃掉參數裡的換行與特殊字元。
        exe = _which("gemini") or "gemini"
        cmd = [exe, "-m", self.model] if self.model else [exe]
        code, out, err = await run_subprocess(cmd, prompt, on_chunk, self.timeout, self._env(), self.workdir)
        if code != 0:
            raise ConnectorError(_fail_detail(code, out, err))
        return out.strip()


def _ollama_name(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


class OllamaConnector(Connector):
    effective_model: str = ""

    @property
    def base(self) -> str:
        return ollama_base(os.environ.get("OLLAMA_HOST", ""))

    def pick_model(self, installed: list[str]) -> str | None:
        """設定的模型有裝就用它；沒裝就退而用第一個非 embedding 的已安裝模型。"""
        if self.model and _ollama_name(self.model) in installed:
            return _ollama_name(self.model)
        chat = [n for n in installed if "embed" not in n.lower()]
        return chat[0] if chat else None

    def available(self):
        try:
            # trust_env=False：Windows 系統 proxy 不會略過 127.0.0.1，本機請求會被送去 proxy。
            r = httpx.get(f"{self.base}/api/tags", timeout=1.5, trust_env=False)
            installed = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return (False, "Ollama 未啟動")
        picked = self.pick_model(installed)
        if not picked:
            return (False, "Ollama 已啟動但沒有可對話的模型（ollama pull <model>）")
        self.effective_model = picked
        if self.model and picked != _ollama_name(self.model):
            return (True, f"未安裝 {self.model}，改用 {picked}")
        return (True, f"{self.base} · {picked}")

    async def run(self, prompt, on_chunk):
        model = self.effective_model or self.model
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": True}
        parts: list[str] = []

        async def stream() -> None:
            done = False
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as c:
                async with c.stream("POST", f"{self.base}/api/chat", json=body) as r:
                    if r.status_code != 200:
                        raw = (await r.aread()).decode("utf-8", errors="replace")
                        try:
                            raw = json.loads(raw).get("error", raw)
                        except ValueError:
                            pass
                        raise ConnectorError(f"Ollama HTTP {r.status_code}: {str(raw)[:300]}")
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        if obj.get("error"):  # 串流中途出錯仍是 HTTP 200
                            raise ConnectorError(f"Ollama：{obj['error']}")
                        piece = obj.get("message", {}).get("content", "")
                        if piece:
                            parts.append(piece)
                            await on_chunk(piece)
                        if obj.get("done"):
                            done = True
            if not done:
                raise ConnectorError("Ollama 串流在完成前中斷")

        try:
            await asyncio.wait_for(stream(), self.timeout)
        except asyncio.TimeoutError as e:
            raise ConnectorError(f"逾時（{self.timeout:.0f}s）", retryable=False) from e
        except httpx.HTTPError as e:
            raise ConnectorError(f"Ollama 連線失敗：{e}") from e
        except ValueError as e:
            raise ConnectorError(f"Ollama 回應格式錯誤：{e}") from e
        return "".join(parts).strip()


class FakeConnector(Connector):
    """示範 / 測試用。FACTORY_FAKE=1 時取代全部真實連接器。"""

    def available(self):
        return (True, "模擬節點")

    async def run(self, prompt, on_chunk):
        delay = float(self.extra.get("delay", 0.05))
        reply = self.extra.get("reply") or f"[{self.label}] 已處理：{prompt[:60]}"
        for i in range(0, len(reply), 8):
            await asyncio.sleep(delay)
            await on_chunk(reply[i : i + 8])
        return reply


KINDS = {"claude": ClaudeConnector, "gemini": GeminiConnector, "ollama": OllamaConnector, "fake": FakeConnector}


def build(spec: dict, force_fake: bool = False, workdir: Path | None = None) -> Connector:
    kind = "fake" if force_fake else spec["kind"]
    return KINDS[kind](
        id=spec["id"],
        label=spec["label"],
        kind=kind,
        model=spec.get("model", ""),
        timeout=float(spec.get("timeout", DEFAULT_TIMEOUT)),
        workdir=workdir,
        extra=spec.get("extra", {}),
    )
