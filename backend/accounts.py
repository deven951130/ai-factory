"""AI 帳號：新增 / 登入 / 登出 / 移除。所有帳號都在軟體裡自己登入，不沿用電腦上 CLI 的登入。

- Claude、ChatGPT（Codex）：每個帳號一個獨立的 CLI 設定資料夾（CLAUDE_CONFIG_DIR / CODEX_HOME），
  用瀏覽器登入，登入資訊存在資料夾裡、互不影響。
- Gemini、OpenAI 相容 API：帳號就是一把 API 金鑰。Gemini 帳號另有自己的 GEMINI_CLI_HOME。
帳號清單存在 data/accounts.json（金鑰也在裡面，不會傳給瀏覽器）；設定資料夾在 data/accounts/<id>。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from .connectors import (CODEX_FILE_AUTH, NO_WINDOW, ConnectorError, _fail_detail, _kill_tree, _which, claude_env,
                         codex_env, run_subprocess)

# login：cli = 用 CLI 開瀏覽器登入；key = 輸入 API 金鑰。folder：帳號需要自己的設定資料夾。
PROVIDERS = {
    "claude": {"login": "cli", "folder": True},
    "codex": {"login": "cli", "folder": True},
    "gemini": {"login": "key", "folder": True},
    "openai": {"login": "key", "folder": False},
}
STATUS_TIMEOUT = 30.0
LOGIN_TIMEOUT = 600.0  # 等使用者在瀏覽器完成登入
NAME_MAX = 40
KEY_MAX = 500
URL_RE = re.compile(r"https://\S+")
OnUrl = Callable[[str], Awaitable[None]]


async def _nothing(_: str) -> None:
    pass


def _login_error(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip() and "https://" not in ln]
    bad = [ln for ln in lines if "fail" in ln.lower() or "error" in ln.lower()]
    return ((bad or lines or ["（沒有輸出）"])[-1])[-300:]


async def cli_login(cmd: list[str], env: dict, workdir: Path | None, on_url: OnUrl,
                    codes: asyncio.Queue) -> tuple[bool, str]:
    """CLI 會自己開瀏覽器（本機回呼即完成）並印出備用網址。
    Claude 用備用網址登入時頁面會給授權碼，要貼回 CLI 的 stdin。"""
    kwargs: dict = dict(NO_WINDOW) if sys.platform == "win32" else {"start_new_session": True}
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, cwd=str(workdir) if workdir else None, **kwargs,
    )
    output: list[str] = []
    url_sent = False

    async def read(stream: asyncio.StreamReader) -> None:
        nonlocal url_sent
        while line := await stream.readline():
            text = line.decode("utf-8", errors="replace")
            output.append(text)
            m = URL_RE.search(text)
            if m and not url_sent:
                url_sent = True
                await on_url(m.group(0))

    async def feed() -> None:
        assert proc.stdin
        try:
            while True:
                proc.stdin.write((await codes.get()).strip().encode("utf-8") + b"\n")
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # CLI 已結束

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(asyncio.gather(read(proc.stdout), read(proc.stderr)), LOGIN_TIMEOUT)
        code = await proc.wait()
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        return False, f"逾時（{LOGIN_TIMEOUT:.0f}s 內沒有完成登入）"
    except BaseException:
        await _kill_tree(proc)  # 取消登入 / 伺服器關閉：不留等待回呼的 CLI
        raise
    finally:
        feeder.cancel()
    return (True, "") if code == 0 else (False, _login_error("".join(output)))


class ClaudeAuth:
    """以 `claude auth ...` 子程序操作某個設定資料夾的登入狀態。"""

    exe_name = "claude"
    missing = "未安裝 claude CLI（npm install -g @anthropic-ai/claude-code）"

    def __init__(self, workdir: Path | None = None):
        self.workdir = workdir

    def _exe(self) -> str:
        exe = _which(self.exe_name)
        if not exe:
            raise ConnectorError(self.missing, retryable=False)
        return exe

    def env(self, config_dir: str) -> dict:
        return claude_env(config_dir)

    async def _run(self, *args: str, config_dir: str) -> tuple[int, str, str]:
        return await run_subprocess([self._exe(), *args], "", _nothing, STATUS_TIMEOUT,
                                    self.env(config_dir), self.workdir)

    async def status(self, config_dir: str) -> dict:
        code, out, err = await self._run("auth", "status", "--json", config_dir=config_dir)
        try:
            d = json.loads(out)
        except ValueError as e:
            raise ConnectorError(_fail_detail(code, out, err)) from e
        return {"logged_in": bool(d.get("loggedIn")), "email": d.get("email") or "",
                "plan": d.get("subscriptionType") or ""}

    async def login(self, config_dir: str, on_url: OnUrl, codes: asyncio.Queue) -> tuple[bool, str]:
        return await cli_login([self._exe(), "auth", "login"], self.env(config_dir), self.workdir, on_url, codes)

    async def logout(self, config_dir: str) -> None:
        code, out, err = await self._run("auth", "logout", config_dir=config_dir)
        if code != 0:
            raise ConnectorError(_fail_detail(code, out, err))


def codex_identity(codex_home: str) -> dict:
    """從 CODEX_HOME/auth.json 的 id_token 讀出 email 與方案（只顯示用，不驗簽）。"""
    try:
        auth = json.loads((Path(codex_home) / "auth.json").read_text(encoding="utf-8"))
        token = (auth.get("tokens") or {}).get("id_token") or ""
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        plan = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or ""
        return {"email": claims.get("email") or "", "plan": plan}
    except (OSError, ValueError, IndexError, AttributeError):
        return {"email": "", "plan": ""}


class CodexAuth(ClaudeAuth):
    """以 `codex login ...` 操作某個 CODEX_HOME 的 ChatGPT 登入。"""

    exe_name = "codex"
    missing = "未安裝 codex CLI（npm install -g @openai/codex）"

    def env(self, config_dir: str) -> dict:
        return codex_env(config_dir)

    async def status(self, config_dir: str) -> dict:
        code, out, err = await self._run("login", "status", *CODEX_FILE_AUTH, config_dir=config_dir)
        if code == 0:
            return {"logged_in": True, **codex_identity(config_dir)}
        if "not logged in" in (out + err).lower():
            return {"logged_in": False, "email": "", "plan": ""}
        raise ConnectorError(_fail_detail(code, out, err))

    async def login(self, config_dir: str, on_url: OnUrl, codes: asyncio.Queue) -> tuple[bool, str]:
        cmd = [self._exe(), "login", *CODEX_FILE_AUTH]
        return await cli_login(cmd, self.env(config_dir), self.workdir, on_url, codes)

    async def logout(self, config_dir: str) -> None:
        code, out, err = await self._run("logout", *CODEX_FILE_AUTH, config_dir=config_dir)
        if code != 0:
            raise ConnectorError(_fail_detail(code, out, err))


class FakeAuth:
    """模擬模式 / 測試用，不呼叫真正的 CLI。登入時貼上任何授權碼（"bad" 除外）就算成功。"""

    def __init__(self) -> None:
        self.users: dict[str, str] = {}

    async def status(self, config_dir: str) -> dict:
        email = self.users.get(config_dir, "")
        return {"logged_in": bool(email), "email": email, "plan": "pro" if email else ""}

    async def logout(self, config_dir: str) -> None:
        self.users.pop(config_dir, None)

    async def login(self, config_dir: str, on_url: OnUrl, codes: asyncio.Queue) -> tuple[bool, str]:
        await on_url("https://example.invalid/oauth/authorize")
        if (await codes.get()).strip() == "bad":
            return False, "Login failed: Request failed with status code 400"
        self.users[config_dir] = f"{Path(config_dir).name}@example.com"
        return True, ""


class Accounts:
    def __init__(self, file: Path, auths: dict, root: Path):
        self.file = file
        self.auths = auths  # provider -> ClaudeAuth / CodexAuth / FakeAuth（只有 cli 類需要）
        self.root = root  # 帳號的設定資料夾放這裡：<root>/<帳號 id>
        self.items: dict[str, dict] = {}
        self.legacy_nodes: dict[str, str] = {}  # 舊版 accounts.json 的節點分配，交給 Factory 搬進節點設定
        self.status: dict[str, dict] = {}
        self.logins: dict[str, dict] = {}  # 進行中的登入：{"task", "codes", "url"}
        self.errors: dict[str, str] = {}  # 最近一次登入失敗的原因
        self.on_change: Callable[[], Awaitable[None]] | None = None
        self._load()

    # ---------- 儲存 ----------
    def _load(self) -> None:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            print(f"[accounts] {self.file} 讀取失敗，當作沒有帳號：{e}", file=sys.stderr)
            return
        for a in raw.get("accounts", []):
            provider = a.get("provider") or "claude"  # 舊版只有 Claude 帳號
            aid = a.get("id")
            if not aid or provider not in PROVIDERS or (PROVIDERS[provider]["folder"] and not a.get("config_dir")):
                continue
            self.items[aid] = {"id": aid, "name": a.get("name") or aid, "provider": provider,
                               "config_dir": a.get("config_dir") or "", "key": a.get("key") or ""}
        # 舊版的「預設帳號」（沿用電腦上的 Claude 登入）已移除，分配到它的節點改成未指定。
        self.legacy_nodes = {n: a for n, a in raw.get("nodes", {}).items() if a in self.items}

    def _save(self) -> None:
        data = {"accounts": [{k: v for k, v in a.items() if v or k in ("id", "name", "provider")}
                             for a in self.items.values()]}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file)

    async def _changed(self) -> None:
        if self.on_change:
            await self.on_change()

    # ---------- 查詢 ----------
    def get(self, aid: str) -> dict:
        return self.items[aid]  # 不存在 → KeyError

    def _cli(self, aid: str) -> dict:
        a = self.get(aid)
        if PROVIDERS[a["provider"]]["login"] != "cli":
            raise ValueError("這個帳號用 API 金鑰登入，請直接輸入金鑰")
        return a

    def public(self) -> list[dict]:
        out = []
        for a in self.items.values():
            key = a["key"]
            out.append({"id": a["id"], "name": a["name"], "provider": a["provider"], "config_dir": a["config_dir"],
                        "key_hint": f"••••{key[-4:]}" if len(key) >= 8 else ("••••" if key else ""),
                        "status": self.status.get(a["id"]),
                        "login": {"url": self.logins[a["id"]]["url"]} if a["id"] in self.logins else None,
                        "error": self.errors.get(a["id"], "")})
        return out

    async def refresh(self, only: str | None = None) -> None:
        ids = [only] if only else list(self.items)

        async def one(aid: str) -> dict:
            a = self.items[aid]
            if PROVIDERS[a["provider"]]["login"] == "key":
                return {"logged_in": bool(a["key"]), "email": "", "plan": ""}
            try:
                return await self.auths[a["provider"]].status(a["config_dir"])
            except Exception as e:  # 查不到狀態不等於沒登入：節點不因此離線
                return {"logged_in": None, "email": "", "plan": "", "error": str(e)}

        results = await asyncio.gather(*(one(a) for a in ids))
        for aid, st in zip(ids, results):
            if aid in self.items:  # 查詢期間可能被移除
                self.status[aid] = st

    # ---------- 操作 ----------
    def add(self, name: str, provider: str) -> dict:
        name = name.strip()
        if provider not in PROVIDERS:
            raise ValueError("不支援的帳號類型")
        if not name:
            raise ValueError("帳號名稱是空的")
        if len(name) > NAME_MAX:
            raise ValueError(f"帳號名稱太長（最多 {NAME_MAX} 字）")
        if any(a["name"] == name for a in self.items.values()):
            raise ValueError("已經有同名的帳號")
        aid = uuid.uuid4().hex[:8]
        cfg = ""
        if PROVIDERS[provider]["folder"]:
            (self.root / aid).mkdir(parents=True, exist_ok=True)
            cfg = str(self.root / aid)
        self.items[aid] = {"id": aid, "name": name, "provider": provider, "config_dir": cfg, "key": ""}
        self.status[aid] = {"logged_in": False, "email": "", "plan": ""}  # 全新的帳號
        self._save()
        return next(p for p in self.public() if p["id"] == aid)

    def set_key(self, aid: str, key: str) -> None:
        a = self.get(aid)
        if PROVIDERS[a["provider"]]["login"] != "key":
            raise ValueError("這個帳號要用瀏覽器登入")
        key = key.strip()
        if not key:
            raise ValueError("金鑰是空的")
        if len(key) > KEY_MAX or any(c.isspace() for c in key):
            raise ValueError("金鑰格式不對（不能有空白或換行）")
        a["key"] = key
        self.errors.pop(aid, None)
        self._save()
        self.status[aid] = {"logged_in": True, "email": "", "plan": ""}

    def start_login(self, aid: str) -> None:
        a = self._cli(aid)
        if aid in self.logins:
            return
        self.errors.pop(aid, None)
        lg: dict = {"codes": asyncio.Queue(), "url": ""}
        self.logins[aid] = lg
        lg["task"] = asyncio.create_task(self._login(aid, a, lg))

    async def _login(self, aid: str, a: dict, lg: dict) -> None:
        async def on_url(url: str) -> None:
            lg["url"] = url
            await self._changed()

        try:
            ok, msg = await self.auths[a["provider"]].login(a["config_dir"], on_url, lg["codes"])
        except asyncio.CancelledError:
            raise  # cancel_login() 負責後續更新
        except Exception as e:
            ok, msg = False, str(e)
        finally:
            if self.logins.get(aid) is lg:
                del self.logins[aid]
        if aid not in self.items:
            return
        if not ok:
            self.errors[aid] = f"登入失敗：{msg}"
        await self.refresh(aid)
        await self._changed()

    def submit_code(self, aid: str, code: str) -> None:
        self.get(aid)
        lg = self.logins.get(aid)
        if not lg:
            raise ValueError("這個帳號目前沒有進行中的登入")
        if not code.strip():
            raise ValueError("授權碼是空的")
        lg["codes"].put_nowait(code)

    async def cancel_login(self, aid: str) -> None:
        self.get(aid)
        lg = self.logins.pop(aid, None)
        if lg:
            lg["task"].cancel()
            await asyncio.gather(lg["task"], return_exceptions=True)

    async def logout(self, aid: str) -> None:
        a = self.get(aid)
        if PROVIDERS[a["provider"]]["login"] == "key":
            a["key"] = ""
            self._save()
        else:
            await self.cancel_login(aid)
            await self.auths[a["provider"]].logout(a["config_dir"])
        await self.refresh(aid)

    async def remove(self, aid: str) -> None:
        a = self.get(aid)
        await self.cancel_login(aid)
        if PROVIDERS[a["provider"]]["login"] == "cli" and (self.status.get(aid) or {}).get("logged_in"):
            await self.auths[a["provider"]].logout(a["config_dir"])  # 清掉登入資訊；資料夾本身保留
        del self.items[aid]
        self.status.pop(aid, None)
        self.errors.pop(aid, None)
        self._save()

    async def shutdown(self) -> None:
        for aid in list(self.logins):
            await self.cancel_login(aid)
