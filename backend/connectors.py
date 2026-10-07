"""AI 連接器：CLI（Claude / ChatGPT Codex / Gemini）與 HTTP（Ollama / OpenAI 相容 API）。

不操作網頁 DOM。登入資訊由帳號提供（extra 裡的設定資料夾或金鑰），不沿用電腦上 CLI 自己的登入。
每個連接器只做一件事：run(prompt, on_chunk) -> 完整回覆文字。
"""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

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
CODEX_API_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")
# Gemini CLI 會從這些變數決定登入方式；用帳號的金鑰時全部拿掉，不沿用電腦上的設定。
GEMINI_AUTH_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_GCA", "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GEMINI_BASE_URL", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "CLOUD_SHELL",
    "GEMINI_CLI_USE_COMPUTE_ADC",
)
# Codex 是會自己執行指令的 agent；這裡只做文字生成，把能動到電腦或網路的工具都關掉。
CODEX_OFF = ("shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use",
             "multi_agent", "image_generation")
# 憑證存在帳號的 CODEX_HOME 資料夾（不放系統金鑰圈）：各帳號互不影響，解除安裝時能一起清掉。
CODEX_FILE_AUTH = ("--disable", "secret_auth_storage")
DEFAULT_TIMEOUT = 300.0
# 桌面版沒有主控台：不加這個旗標，每次呼叫 claude.cmd / gemini.cmd 都會閃出一個黑色視窗。
NO_WINDOW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


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
    models: tuple[str, ...] = ()  # 偵測到可用的模型（本地模型 / OpenAI 相容 API）

    def configure(self, extra: dict) -> None:
        """換帳號或設定時呼叫：更新 extra 並丟掉依賴舊設定的快取。"""
        self.extra = extra

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
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **NO_WINDOW,
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
    kwargs: dict = dict(NO_WINDOW)
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


def claude_env(config_dir: str = "", use_api_key: bool = False) -> dict:
    """claude 子程序的環境變數。config_dir：帳號的設定資料夾（空字串 = 預設帳號）。"""
    env = dict(os.environ)
    if not use_api_key:
        for k in CLAUDE_API_ENV:
            env.pop(k, None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def codex_env(codex_home: str = "") -> dict:
    """codex 子程序的環境變數。codex_home：帳號的設定資料夾（登入資訊存在裡面）。"""
    env = dict(os.environ)
    for k in CODEX_API_ENV:
        env.pop(k, None)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    env["NO_COLOR"] = "1"
    return env


def _fail_detail(code: int, out: str, err: str) -> str:
    # 有些 CLI（例如 claude）把錯誤印在 stdout，stderr 是空的。
    detail = err.strip() or out.strip() or "（沒有輸出）"
    return f"exit {code}: {detail[-300:]}"


class ClaudeConnector(Connector):
    def _config_dir(self) -> str:
        # 多帳號：每個帳號一個 Claude 設定資料夾（登入資訊存在裡面），以 CLAUDE_CONFIG_DIR 指定。
        d = self.extra.get("config_dir") or ""
        return os.path.normpath(os.path.expanduser(os.path.expandvars(d))) if d else ""

    def available(self):
        exe = _which("claude")
        if not exe:
            return (False, "未安裝 claude CLI")
        cfg = self._config_dir()
        if cfg and not os.path.isdir(cfg):
            return (False, f"找不到 config_dir：{cfg}")
        return (True, "")  # 卡片上改顯示帳號，不顯示執行檔路徑

    def _env(self) -> dict:
        return claude_env(self._config_dir(), bool(self.extra.get("use_api_key")))

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
        return (True, "") if exe else (False, "未安裝 gemini CLI（npm install -g @google/gemini-cli）")

    def _env(self) -> dict:
        env = dict(os.environ)
        if self.extra.get("api_key"):
            for k in GEMINI_AUTH_ENV:
                env.pop(k, None)
            env["GEMINI_API_KEY"] = self.extra["api_key"]
        if self.extra.get("home"):  # 帳號自己的 ~/.gemini（設定、信任資料夾、暫存），不讀電腦上的
            env["GEMINI_CLI_HOME"] = self.extra["home"]
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


class CodexConnector(Connector):
    """ChatGPT 訂閱：Codex CLI 的 codex exec（非互動），以 --json 讀事件。"""

    def available(self):
        exe = _which("codex")
        return (True, "") if exe else (False, "未安裝 codex CLI（npm install -g @openai/codex）")

    async def run(self, prompt, on_chunk):
        exe = _which("codex") or "codex"
        cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
               "--color", "never", "--ignore-rules", *CODEX_FILE_AUTH]
        for feature in CODEX_OFF:
            cmd += ["--disable", feature]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append("-")  # prompt 走 stdin
        buf = ""
        messages: list[str] = []
        failed = ""
        last_error = ""

        async def on_text(text: str) -> None:
            nonlocal buf, failed, last_error
            buf += text
            *lines, buf = buf.split("\n")
            for line in lines:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "item.completed":
                    item = ev.get("item") or {}
                    if item.get("type") == "agent_message" and item.get("text"):
                        if messages:
                            await on_chunk("\n\n")
                        messages.append(item["text"])
                        await on_chunk(item["text"])
                elif kind == "turn.failed":
                    failed = str((ev.get("error") or {}).get("message") or "turn.failed")
                elif kind == "error":  # 也用來報「重新連線中」，不一定是最終失敗
                    last_error = str(ev.get("message") or "")

        code, out, err = await run_subprocess(cmd, prompt, on_text, self.timeout, codex_env(self.extra.get("codex_home", "")),
                                              self.workdir)
        if buf.strip():
            await on_text("\n")
        if failed:  # Codex 內部已重試過（例如額度用完）
            raise ConnectorError(failed[-300:], retryable=False)
        if messages:
            return "\n\n".join(messages).strip()
        raise ConnectorError(last_error[-300:] if last_error else _fail_detail(code, "", err))


def _ollama_name(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def ollama_models(host: str = "") -> list[str]:
    """已安裝、可對話的模型（略過 embedding 模型）。Ollama 沒開時丟 ConnectorError。"""
    base = ollama_base(host or os.environ.get("OLLAMA_HOST", ""))
    try:
        # trust_env=False：Windows 系統 proxy 不會略過 127.0.0.1，本機請求會被送去 proxy。
        r = httpx.get(f"{base}/api/tags", timeout=1.5, trust_env=False)
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        raise ConnectorError(f"Ollama 未啟動（{base}）") from e
    return [n for n in names if "embed" not in n.lower()]


class OllamaConnector(Connector):
    effective_model: str = ""

    @property
    def base(self) -> str:
        return ollama_base(self.extra.get("host") or os.environ.get("OLLAMA_HOST", ""))

    def pick_model(self, installed: list[str]) -> str | None:
        """設定的模型有裝就用它；沒裝就退而用第一個已安裝的對話模型。"""
        if self.model and _ollama_name(self.model) in installed:
            return _ollama_name(self.model)
        return installed[0] if installed else None

    def available(self):
        try:
            installed = ollama_models(self.base)
        except ConnectorError:
            self.models = ()
            return (False, "Ollama 未啟動")
        self.models = tuple(installed)
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


def _api_error(status: int, raw: str) -> str:
    try:
        err = json.loads(raw).get("error", raw)
        raw = err.get("message", err) if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        pass
    return f"HTTP {status}: {str(raw)[:300]}"


class OpenAIConnector(Connector):
    """OpenAI 相容的 Chat Completions API（OpenAI、DeepSeek、xAI、OpenRouter、LM Studio…）。"""

    CHECK_EVERY = 60.0  # 秒；遠端 API 不必每 15 秒都查一次 /models
    _checked: tuple[float, tuple[bool, str]] | None = None

    def configure(self, extra: dict) -> None:
        self.extra = extra
        self._checked = None

    @property
    def base(self) -> str:
        return (self.extra.get("base_url") or "").rstrip("/")

    def _client_kwargs(self) -> dict:
        # 本機伺服器不能走系統 proxy；遠端 API 則可能需要 proxy。
        local = urlsplit(self.base).hostname in ("localhost", "127.0.0.1", "::1")
        key = self.extra.get("api_key")
        return {"trust_env": not local, "headers": {"Authorization": f"Bearer {key}"} if key else {}}

    def available(self):
        if not self.base:
            return (False, "沒有設定 API 網址")
        now = time.monotonic()
        if self._checked and now - self._checked[0] < self.CHECK_EVERY:
            return self._checked[1]
        try:
            r = httpx.get(f"{self.base}/models", timeout=5, **self._client_kwargs())
        except httpx.HTTPError as e:
            result = (False, f"無法連線：{e}")
        else:
            if r.status_code in (401, 403):
                result = (False, "API 金鑰無效或沒有權限")
            else:  # 有些服務沒有 /models，仍可呼叫
                try:
                    self.models = tuple(m["id"] for m in r.json().get("data", []) if m.get("id"))
                except (ValueError, AttributeError, TypeError):
                    pass
                result = (True, f"{self.base} · {self.model}")
        self._checked = (now, result)
        return result

    async def run(self, prompt, on_chunk):
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "stream": True}
        parts: list[str] = []

        async def stream() -> None:
            finished = False
            async with httpx.AsyncClient(timeout=self.timeout, **self._client_kwargs()) as c:
                async with c.stream("POST", f"{self.base}/chat/completions", json=body) as r:
                    if r.status_code != 200:
                        raw = (await r.aread()).decode("utf-8", errors="replace")
                        # 4xx（金鑰、模型名稱、額度）重試也一樣；5xx 才值得再試。
                        raise ConnectorError(_api_error(r.status_code, raw), retryable=r.status_code >= 500)
                    async for line in r.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            finished = True
                            break
                        obj = json.loads(data)
                        if obj.get("error"):
                            raise ConnectorError(_api_error(200, json.dumps(obj)))
                        for choice in obj.get("choices") or []:
                            piece = (choice.get("delta") or {}).get("content") or ""
                            if piece:
                                parts.append(piece)
                                await on_chunk(piece)
                            if choice.get("finish_reason"):
                                finished = True
            if not finished:
                raise ConnectorError("API 串流在完成前中斷")

        try:
            await asyncio.wait_for(stream(), self.timeout)
        except asyncio.TimeoutError as e:
            raise ConnectorError(f"逾時（{self.timeout:.0f}s）", retryable=False) from e
        except httpx.HTTPError as e:
            raise ConnectorError(f"API 連線失敗：{e}") from e
        except ValueError as e:
            raise ConnectorError(f"API 回應格式錯誤：{e}") from e
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


KINDS = {"claude": ClaudeConnector, "codex": CodexConnector, "gemini": GeminiConnector, "ollama": OllamaConnector,
         "openai": OpenAIConnector, "fake": FakeConnector}


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
