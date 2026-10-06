"""AI 連接器：CLI（Claude / Gemini）與 HTTP（Ollama）。

全部走「訂閱 CLI / 本地服務」，不操作網頁 DOM，不需要 API 金鑰。
每個連接器只做一件事：run(prompt, on_chunk) -> 完整回覆文字。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

OnChunk = Callable[[str], Awaitable[None]]

# 純文字生成情境不需要這些工具；關掉避免 AI 自行讀寫檔案或上網。
CLAUDE_DISALLOWED = "Bash Edit Write Read Glob Grep NotebookEdit WebFetch WebSearch Task TodoWrite"


class ConnectorError(RuntimeError):
    pass


@dataclass
class Connector:
    id: str
    label: str
    kind: str  # claude | gemini | ollama | fake
    model: str = ""
    extra: dict = field(default_factory=dict)

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    async def run(self, prompt: str, on_chunk: OnChunk, timeout: float = 300) -> str:
        raise NotImplementedError


async def _stream_subprocess(cmd: list[str], stdin_text: str, on_chunk: OnChunk, timeout: float) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ConnectorError(f"找不到執行檔：{cmd[0]}") from e

    async def feed() -> None:
        assert proc.stdin
        proc.stdin.write(stdin_text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

    async def read() -> str:
        assert proc.stdout
        parts: list[str] = []
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            parts.append(text)
            await on_chunk(text)
        return "".join(parts)

    try:
        _, out = await asyncio.wait_for(asyncio.gather(feed(), read()), timeout)
        code = await proc.wait()
    except asyncio.TimeoutError as e:
        proc.kill()
        raise ConnectorError(f"逾時（{timeout:.0f}s）") from e
    if code != 0:
        err = (await proc.stderr.read()).decode("utf-8", errors="replace").strip() if proc.stderr else ""
        hint = "（可能尚未登入或用量已滿）" if err else ""
        raise ConnectorError(f"exit {code}{hint}: {err[:300]}")
    return out.strip()


class ClaudeConnector(Connector):
    def available(self):
        exe = shutil.which("claude")
        return (bool(exe), exe or "未安裝 claude CLI")

    async def run(self, prompt, on_chunk, timeout=300):
        cmd = ["claude", "-p", "--model", self.model or "sonnet", "--disallowed-tools", *CLAUDE_DISALLOWED.split()]
        return await _stream_subprocess(cmd, prompt, on_chunk, timeout)


class GeminiConnector(Connector):
    def available(self):
        exe = shutil.which("gemini")
        return (bool(exe), exe or "未安裝 gemini CLI")

    async def run(self, prompt, on_chunk, timeout=300):
        cmd = ["gemini", "-p", prompt] if not self.model else ["gemini", "-m", self.model, "-p", prompt]
        return await _stream_subprocess(cmd, "", on_chunk, timeout)


class OllamaConnector(Connector):
    @property
    def base(self) -> str:
        return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

    def available(self):
        try:
            r = httpx.get(f"{self.base}/api/tags", timeout=1.5)
            names = [m["name"] for m in r.json().get("models", [])]
            if not names:
                return (False, "Ollama 已啟動但沒有任何模型（ollama pull <model>）")
            if self.model not in names:  # 設定的模型沒裝 → 退而用已安裝的第一個
                self.model = names[0]
            return (True, f"{self.base} · {self.model}")
        except Exception:
            return (False, "Ollama 未啟動")

    async def run(self, prompt, on_chunk, timeout=300):
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "stream": True}
        parts: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("POST", f"{self.base}/api/chat", json=body) as r:
                    if r.status_code != 200:
                        raise ConnectorError(f"Ollama HTTP {r.status_code}")
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        piece = json.loads(line).get("message", {}).get("content", "")
                        if piece:
                            parts.append(piece)
                            await on_chunk(piece)
        except httpx.HTTPError as e:
            raise ConnectorError(f"Ollama 連線失敗：{e}") from e
        return "".join(parts).strip()


class FakeConnector(Connector):
    """示範 / 測試用。FACTORY_FAKE=1 時取代全部真實連接器。"""

    def available(self):
        return (True, "模擬節點")

    async def run(self, prompt, on_chunk, timeout=300):
        delay = float(self.extra.get("delay", 0.05))
        reply = self.extra.get("reply") or f"[{self.label}] 已處理：{prompt[:60]}"
        for i in range(0, len(reply), 8):
            await asyncio.sleep(delay)
            await on_chunk(reply[i : i + 8])
        return reply


KINDS = {"claude": ClaudeConnector, "gemini": GeminiConnector, "ollama": OllamaConnector, "fake": FakeConnector}


def build(spec: dict, force_fake: bool = False) -> Connector:
    kind = "fake" if force_fake else spec["kind"]
    return KINDS[kind](id=spec["id"], label=spec["label"], kind=kind, model=spec.get("model", ""), extra=spec.get("extra", {}))
