import asyncio
import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.accounts import ClaudeAuth, CodexAuth
from backend.connectors import (
    ClaudeConnector,
    CodexConnector,
    ConnectorError,
    GeminiConnector,
    OllamaConnector,
    OpenAIConnector,
    ollama_base,
    run_subprocess,
)
from backend.factory import ASK_HINT, MAX_ASKS, Factory, find_question
from backend.main import ROOT, create_app

# repo 的 nodes.json 是全新安裝的起點（空的）；測試用自己的範例設定。
SAMPLE = {
    "nodes": [
        {"id": "gemini", "label": "Gemini", "kind": "gemini", "role": "分析", "timeout": 600},
        {"id": "claude-opus", "label": "Claude Opus", "kind": "claude", "model": "opus", "timeout": 900},
        {"id": "claude-opus-2", "label": "Claude Opus 2", "kind": "claude", "model": "opus", "timeout": 900},
        {"id": "ollama", "label": "本地 Qwen", "kind": "ollama", "model": "qwen2.5-coder:7b", "timeout": 300},
    ],
    "pipeline": [
        {"name": "分析", "node": "gemini", "template": "分析需求：\n\n{input}"},
        {"name": "實作", "node": "claude-opus", "template": "實作：\n\n{input}"},
        {"name": "審查", "node": "claude-opus-2", "template": "審查：\n\n{input}"},
    ],
}
SAMPLE_NODES = len(SAMPLE["nodes"])
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="假 CLI 用 shebang 腳本")


def write_sample(path: Path, data: dict = SAMPLE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def make(tmp_path, fake=True):
    return Factory(write_sample(tmp_path / "nodes.json"), tmp_path, force_fake=fake)


def sample_app(tmp_path):
    write_sample(tmp_path / "nodes.json")  # create_app 預設讀 data_dir/nodes.json
    return create_app(force_fake=True, data_dir=tmp_path)


async def nothing(_):
    pass


async def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# ---------------- 管線與狀態 ----------------

def test_pipeline_runs_to_done(tmp_path):
    async def go():
        f = make(tmp_path)
        t = f.create_task("t", "做一個貪食蛇")
        await f.run_task(t["id"])
        return t
    t = asyncio.run(go())
    assert t["status"] == "done" and len(t["outputs"]) == 3


def test_ask_user_pauses_and_resumes(tmp_path):
    async def go():
        f = make(tmp_path)
        prompts = []

        async def run(prompt, on_chunk):
            prompts.append(prompt)
            return "[[ASK_USER: 要幾個玩家？]]" if len(prompts) == 1 else "ok"
        f.connectors["gemini"].run = run
        t = f.create_task("t", "x")
        job = asyncio.create_task(f.run_task(t["id"]))
        await wait_for(lambda: t["status"] == "waiting_user")
        assert t["question"]["text"] == "要幾個玩家？"
        assert f.nodes["gemini"]["status"] == "waiting"
        assert f.answer(t["id"], "兩個")
        await job
        return t, prompts
    t, prompts = asyncio.run(go())
    assert t["status"] == "done"
    # 續跑的 prompt 帶著問答，而且仍保留提問規則
    assert "要幾個玩家？" in prompts[1] and "兩個" in prompts[1] and prompts[1].endswith(ASK_HINT)


def test_echoed_hint_is_not_a_question():
    prompt = "分析這個需求" + ASK_HINT
    assert find_question("資訊足夠，無需輸出 [[ASK_USER: 你的問題]]。結果如下…", prompt) is None
    # 使用者輸入本身含標記，被照抄回來也不算提問
    p2 = "請解釋 [[ASK_USER: 範例]] 是什麼" + ASK_HINT
    assert find_question("[[ASK_USER: 範例]] 是一種標記", p2) is None
    assert find_question("[[ASK_USER: 要用什麼引擎？]]", prompt) == "要用什麼引擎？"


def test_ask_rounds_are_capped(tmp_path):
    async def go():
        f = make(tmp_path)
        n = {"c": 0}

        async def run(prompt, on_chunk):
            n["c"] += 1
            return f"[[ASK_USER: 問題{n['c']}]]"
        f.connectors["gemini"].run = run
        t = f.create_task("t", "x")
        job = asyncio.create_task(f.run_task(t["id"]))
        for i in range(MAX_ASKS):
            await wait_for(lambda: (t["question"] or {}).get("text") == f"問題{i + 1}")
            assert f.answer(t["id"], "a")
        await asyncio.wait_for(job, 5)
        return t, n["c"]
    t, calls = asyncio.run(go())
    assert t["status"] == "done" and calls == MAX_ASKS + 1


def test_failure_retries_then_blocks(tmp_path):
    async def go():
        f = make(tmp_path)
        n = {"c": 0}

        async def boom(prompt, on_chunk):
            n["c"] += 1
            raise ConnectorError("額度已滿")
        f.connectors["gemini"].run = boom
        t = f.create_task("t", "x")
        await f.run_task(t["id"])
        return t, n["c"], f
    t, calls, f = asyncio.run(go())
    assert t["status"] == "blocked" and calls == 3
    assert f.nodes["gemini"]["status"] == "failed"


def test_non_retryable_error_is_not_retried(tmp_path):
    async def go():
        f = make(tmp_path)
        n = {"c": 0}

        async def slow(prompt, on_chunk):
            n["c"] += 1
            raise ConnectorError("逾時（300s）", retryable=False)
        f.connectors["gemini"].run = slow
        t = f.create_task("t", "x")
        await f.run_task(t["id"])
        return t, n["c"]
    t, calls = asyncio.run(go())
    assert t["status"] == "blocked" and calls == 1


def test_unexpected_exception_blocks_task(tmp_path):
    async def go():
        f = make(tmp_path)

        async def weird(prompt, on_chunk):
            raise ValueError("bad json")
        f.connectors["gemini"].run = weird
        t = f.create_task("t", "x")
        await f.run_task(t["id"])
        return t
    t = asyncio.run(go())
    assert t["status"] == "blocked" and "ValueError" in t["error"]


def test_template_with_json_braces(tmp_path):
    async def go():
        f = make(tmp_path)
        f.pipeline = [{"name": "s", "node": "gemini", "template": '請以 JSON 輸出 {"steps": []}：\n\n{input}'}]
        seen = []

        async def run(prompt, on_chunk):
            seen.append(prompt)
            return "ok"
        f.connectors["gemini"].run = run
        t = f.create_task("t", "做遊戲")
        await f.run_task(t["id"])
        return t, seen
    t, seen = asyncio.run(go())
    assert t["status"] == "done" and '{"steps": []}' in seen[0] and "做遊戲" in seen[0]


def test_event_log_failure_does_not_stick_task(tmp_path):
    async def go():
        f = make(tmp_path)
        f._log_file = tmp_path / "no-such-dir" / "events.jsonl"  # 寫入一定失敗
        t = f.create_task("t", "x")
        await f.run_task(t["id"])
        return t
    assert asyncio.run(go())["status"] == "done"


def test_waiting_status_survives_other_calls(tmp_path):
    async def go():
        f = make(tmp_path)
        n = {"c": 0}

        async def run(prompt, on_chunk):
            n["c"] += 1
            return "[[ASK_USER: Q?]]" if n["c"] == 1 else "ok"
        f.connectors["gemini"].run = run
        t = f.create_task("t", "x")
        job = asyncio.create_task(f.run_task(t["id"]))
        await wait_for(lambda: t["status"] == "waiting_user")
        await f.chat("gemini", "順便問一下")  # 同一節點的其他呼叫
        status = f.nodes["gemini"]["status"]
        f.answer(t["id"], "A")
        await job
        return status, f.nodes["gemini"]["status"]
    during, after = asyncio.run(go())
    assert during == "waiting" and after == "idle"


def test_cancel_running_task(tmp_path):
    async def go():
        f = make(tmp_path)

        async def hang(prompt, on_chunk):
            await asyncio.sleep(60)
        f.connectors["gemini"].run = hang
        t = f.start_task("t", "x")
        await wait_for(lambda: f.nodes["gemini"]["status"] == "running")
        assert f.cancel(t["id"])
        await wait_for(lambda: t["status"] == "cancelled")
        return f
    f = asyncio.run(go())
    assert f.nodes["gemini"]["status"] == "idle"


def test_cancel_during_retry_backoff_settles_node(tmp_path):
    async def go():
        f = make(tmp_path)

        async def flaky(prompt, on_chunk):
            raise ConnectorError("exit 1")
        f.connectors["gemini"].run = flaky
        t = f.start_task("t", "x")
        await wait_for(lambda: f.nodes["gemini"]["detail"].endswith("嘗試 1"))
        await asyncio.sleep(0.05)  # 落在第一次失敗後的重試等待裡
        f.cancel(t["id"])
        await wait_for(lambda: t["status"] == "cancelled")
        return f
    f = asyncio.run(go())
    assert f.nodes["gemini"]["status"] == "idle" and f.nodes["gemini"]["task_id"] is None


def test_cancel_waiting_task(tmp_path):
    async def go():
        f = make(tmp_path)

        async def ask(prompt, on_chunk):
            return "[[ASK_USER: Q?]]"
        f.connectors["gemini"].run = ask
        t = f.start_task("t", "x")
        await wait_for(lambda: t["status"] == "waiting_user")
        f.cancel(t["id"])
        await wait_for(lambda: t["status"] == "cancelled")
        return t, f
    t, f = asyncio.run(go())
    assert t["question"] is None and f.nodes["gemini"]["status"] == "idle" and not f.questions


def test_nodes_json_with_bom(tmp_path):
    cfg = tmp_path / "nodes.json"
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps(SAMPLE).encode())
    assert len(Factory(cfg, tmp_path / "d", force_fake=True).nodes) == SAMPLE_NODES


def test_task_runs_the_pipeline_it_started_with(tmp_path):
    async def go():
        f = make(tmp_path)
        t = f.create_task("t", "x")
        await f.set_pipeline([{"name": "只有一步", "node": "ollama", "template": "{input}"}])
        await f.run_task(t["id"])
        return t, f
    t, f = asyncio.run(go())
    assert t["status"] == "done" and [o["stage"] for o in t["outputs"]] == ["分析", "實作", "審查"]
    assert [s["name"] for s in f.pipeline] == ["只有一步"]


# ---------------- 節點 / 生產線設定 ----------------

def test_fresh_install_starts_empty(tmp_path):
    assert json.loads((ROOT / "nodes.json").read_text(encoding="utf-8")) == {"nodes": [], "pipeline": []}
    app = create_app(force_fake=True, data_dir=tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        st = c.get("/api/state").json()
        assert st["nodes"] == [] and st["pipeline"] == [] and st["accounts"] == []
        r = c.post("/api/tasks", json={"title": "", "prompt": "x"})
        assert r.status_code == 409 and "生產線" in r.json()["detail"]
    assert (tmp_path / "nodes.json").exists()  # 設定檔放在資料夾，不改 repo 的預設


def test_node_without_account_never_uses_computer_login(tmp_path):
    async def go():
        f = make(tmp_path, fake=False)  # 真實模式：不呼叫 CLI 就能判斷
        problem = f._account_problem("claude-opus")
        with pytest.raises(ConnectorError) as e:
            await f.call("claude-opus", "hi")
        return f, problem, e.value
    f, problem, err = asyncio.run(go())
    assert "尚未指定帳號" in problem and not err.retryable
    assert f.nodes["claude-opus"]["status"] == "offline"
    assert f._account_problem("ollama") == ""  # 本地模型不需要帳號


def test_node_and_pipeline_editing(tmp_path):
    app = sample_app(tmp_path)
    cfg = tmp_path / "nodes.json"
    with TestClient(app, base_url=LOCAL) as c:
        f = app.state.factory
        n = c.post("/api/nodes", json={"kind": "ollama", "label": "本地 Llama", "model": "llama3:8b",
                                       "host": "127.0.0.1:9"}).json()
        nid = n["id"]
        assert nid.startswith("ollama-") and f.connectors[nid].extra["host"] == "127.0.0.1:9"
        saved = {x["id"]: x for x in json.loads(cfg.read_text(encoding="utf-8"))["nodes"]}
        assert saved[nid]["model"] == "llama3:8b"
        assert c.post("/api/nodes", json={"kind": "ollama", "label": "本地 Llama"}).status_code == 409  # 同名
        assert c.post("/api/nodes", json={"kind": "nope", "label": "x"}).status_code == 409
        assert c.post("/api/nodes", json={"kind": "openai", "label": "API", "model": "m",
                                          "base_url": "ftp://x"}).status_code == 409
        # 只改模型：其他欄位保留
        r = c.patch(f"/api/nodes/{nid}", json={"model": "qwen3:8b"})
        assert r.status_code == 200 and r.json()["label"] == "本地 Llama" and f.connectors[nid].model == "qwen3:8b"
        assert c.patch("/api/nodes/nope", json={"model": "x"}).status_code == 404

        bad = c.put("/api/pipeline", json={"steps": [{"name": "a", "node": nid, "template": "沒有輸入"}]})
        assert bad.status_code == 409 and "{input}" in bad.json()["detail"]
        assert c.put("/api/pipeline", json={"steps": [{"name": "a", "node": "nope", "template": "{input}"}]}).status_code == 409
        ok = c.put("/api/pipeline", json={"steps": [{"name": "草稿", "node": nid, "template": "寫：{input}"},
                                                    {"name": "審查", "node": "gemini", "template": "{input}"}]})
        assert ok.status_code == 200 and [s["name"] for s in f.pipeline] == ["草稿", "審查"]
        assert json.loads(cfg.read_text(encoding="utf-8"))["pipeline"][0]["node"] == nid

        r = c.delete(f"/api/nodes/{nid}")  # 生產線還在用
        assert r.status_code == 409 and "草稿" in r.json()["detail"]
        assert c.put("/api/pipeline", json={"steps": []}).status_code == 200
        assert c.delete(f"/api/nodes/{nid}").status_code == 200
        assert nid not in f.nodes and nid not in json.loads(cfg.read_text(encoding="utf-8"))["nodes"]

    # 重啟後設定還在
    f2 = create_app(force_fake=True, data_dir=tmp_path).state.factory
    assert f2.pipeline == [] and len(f2.nodes) == SAMPLE_NODES


def test_legacy_account_assignments_are_migrated(tmp_path):
    acct_dir = tmp_path / "old-acct"
    acct_dir.mkdir()
    (tmp_path / "accounts.json").write_text(json.dumps({
        "accounts": [{"id": "abc", "name": "第二帳號", "config_dir": str(acct_dir)}],
        "nodes": {"claude-opus-2": "abc", "claude-opus": "default"},
    }), encoding="utf-8")
    f = make(tmp_path, fake=False)
    assert f.accounts.items["abc"]["provider"] == "claude"
    assert f.specs["claude-opus-2"]["account"] == "abc" and f.connectors["claude-opus-2"].extra["config_dir"] == str(acct_dir)
    assert f.specs["claude-opus"].get("account") is None  # 舊的「預設帳號」已移除
    saved = {x["id"]: x for x in json.loads((tmp_path / "nodes.json").read_text(encoding="utf-8"))["nodes"]}
    assert saved["claude-opus-2"]["account"] == "abc"


# ---------------- API / WebSocket ----------------

LOCAL = "http://127.0.0.1:8000"
WS_HOST = {"Host": "127.0.0.1:8000"}  # TestClient 的 WebSocket 不用 base_url，Host 固定是 testserver


def test_config_and_data_dir_from_env(tmp_path, monkeypatch):
    # 桌面版用環境變數把設定與資料放到 %LOCALAPPDATA%\AIFactory
    cfg = write_sample(tmp_path / "my-nodes.json")
    monkeypatch.setenv("FACTORY_CONFIG", str(cfg))
    monkeypatch.setenv("FACTORY_DATA_DIR", str(tmp_path / "appdata"))
    app = create_app(force_fake=True)
    f = app.state.factory
    assert f.data_dir == tmp_path / "appdata" and (tmp_path / "appdata" / "workspace").is_dir()
    assert f.config_path == cfg and len(f.nodes) == SAMPLE_NODES


def test_rejects_foreign_origin_and_host(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    app = sample_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get("/api/state").status_code == 200
        assert c.post("/api/tasks", json={"title": "", "prompt": "x"},
                      headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200
        # 其他網站的頁面發出的 POST
        assert c.post("/api/tasks/x/cancel", headers={"Origin": "https://evil.example"}).status_code == 403
        # DNS rebinding：Host 是外部網域
        assert c.get("/api/state", headers={"Host": "evil.example:8000"}).status_code == 403
        # 其他網站的頁面開 WebSocket
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={**WS_HOST, "Origin": "https://evil.example"}) as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={**WS_HOST, "Origin": "null"}) as ws:
                ws.receive_json()
        with c.websocket_connect("/ws", headers={**WS_HOST, "Origin": "http://localhost:8000"}) as ws:
            assert ws.receive_json()["type"] == "snapshot"

def test_api_chat_and_state(tmp_path):
    app = sample_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        assert len(c.get("/api/state").json()["nodes"]) == SAMPLE_NODES
        with c.websocket_connect("/ws", headers=WS_HOST) as ws:
            assert ws.receive_json()["type"] == "snapshot"
            assert c.post("/api/chat", json={"node": "ollama", "message": "hi"}).status_code == 200
            seen = set()
            while "ai" not in seen:
                ev = ws.receive_json()
                if ev["type"] == "chat":
                    seen.add(ev["role"])
        assert c.post("/api/chat", json={"node": "nope", "message": "x"}).status_code == 404
        assert c.post("/api/tasks/zzz/answer", json={"text": "x"}).status_code == 409
        assert c.post("/api/tasks/zzz/cancel").status_code == 409


def test_allowed_hosts_env_is_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALLOWED_HOSTS", "DESKTOP-7H2K9QX, 192.168.1.10:8000, fe80::1, [fe80::2]")
    app = create_app(force_fake=True, data_dir=tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        for host in ("desktop-7h2k9qx:8000", "DESKTOP-7H2K9QX:8000", "192.168.1.10:8000",
                     "[fe80::1]:8000", "[fe80::2]:8000", "localhost:8000"):
            assert c.get("/api/state", headers={"Host": host}).status_code == 200, host
        assert c.get("/api/state", headers={"Host": "192.168.1.11:8000"}).status_code == 403


def test_lone_surrogate_title_does_not_break_dashboard(tmp_path):
    app = sample_app(tmp_path)
    body = '{"title": "abc\\ud83d", "prompt": "\\ud83c x"}'  # 前端把 emoji 切一半的樣子
    with TestClient(app, base_url=LOCAL) as c:
        r = c.post("/api/tasks", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        with c.websocket_connect("/ws", headers=WS_HOST) as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_offline_node_rechecked_on_chat(tmp_path):
    app = sample_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        f = app.state.factory
        f.nodes["ollama"]["status"] = "offline"  # 假裝啟動時 Ollama 還沒起來
        assert c.post("/api/chat", json={"node": "ollama", "message": "hi"}).status_code == 200


# ---------------- 子程序 / CLI 連接器 ----------------

def test_subprocess_stream_keeps_multibyte_chars(tmp_path):
    text = "中文串流測試" * 2000  # 遠超過讀取塊大小，必定跨塊
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.stderr.write('x' * 200000)\n",
        encoding="utf-8",
    )
    chunks = []

    async def on_chunk(t):
        chunks.append(t)
    code, out, err = asyncio.run(run_subprocess([sys.executable, str(script)], text, on_chunk, 30))
    assert code == 0 and out == text and "".join(chunks) == text and "�" not in out
    assert len(err) == 200000


def fake_cli(tmp_path, name, body):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


@posix_only
def test_claude_stream_json_parsing(tmp_path, monkeypatch):
    lines = [
        {"type": "system", "subtype": "init"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "藍"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "色"}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "藍色"},
    ]
    payload = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)
    # 每次只寫 7 bytes，確保 NDJSON 行與中文字都被切開
    d = fake_cli(tmp_path, "claude", (
        "import sys, os, time\n"
        "sys.stdin.read()\n"
        f"data = {payload!r}.encode()\n"
        "assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        "for i in range(0, len(data), 7):\n"
        "    sys.stdout.buffer.write(data[i:i+7]); sys.stdout.flush()\n"
    ))
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    chunks = []

    async def on_chunk(t):
        chunks.append(t)
    c = ClaudeConnector(id="c", label="C", kind="claude", model="sonnet", workdir=tmp_path)
    assert asyncio.run(c.run("hi", on_chunk)) == "藍色"
    assert chunks == ["藍", "色"]


@posix_only
def test_claude_multi_turn_keeps_every_turn(tmp_path, monkeypatch):
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ALPHA"}, {"type": "tool_use", "name": "TaskList"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "OMEGA"}]}},
        {"type": "result", "is_error": False, "num_turns": 2, "result": "OMEGA"},
    ]
    payload = "".join(json.dumps(x) + "\n" for x in lines)
    d = fake_cli(tmp_path, "claude", (
        "import sys\n"
        "assert sys.argv[sys.argv.index('--tools') + 1] == ''\n"
        f"sys.stdin.read()\nsys.stdout.write({payload!r})\n"
    ))
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    c = ClaudeConnector(id="c", label="C", kind="claude", workdir=tmp_path)
    assert asyncio.run(c.run("hi", nothing)) == "ALPHA\n\nOMEGA"


@posix_only
def test_claude_is_error_result(tmp_path, monkeypatch):
    res = json.dumps({"type": "result", "is_error": True, "result": "Prompt is too long"})
    d = fake_cli(tmp_path, "claude", f"import sys\nsys.stdin.read()\nprint({res!r})\nsys.exit(1)\n")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    c = ClaudeConnector(id="c", label="C", kind="claude", workdir=tmp_path)
    with pytest.raises(ConnectorError) as e:
        asyncio.run(c.run("hi", nothing))
    assert "Prompt is too long" in str(e.value) and not e.value.retryable


def test_claude_config_dir_per_node(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.connectors._which", lambda name: "claude")
    monkeypatch.setenv("ACCT_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/global")
    acct = tmp_path / "acct2"
    c = ClaudeConnector(id="c", label="C", kind="claude", extra={"config_dir": "$ACCT_HOME/acct2"})
    ok, detail = c.available()
    assert not ok and "acct2" in detail  # 還沒登入（資料夾不存在）→ 離線
    acct.mkdir()
    assert c.available()[0]
    assert Path(c._env()["CLAUDE_CONFIG_DIR"]) == acct
    # 沒設 config_dir 的節點沿用外部環境
    assert ClaudeConnector(id="d", label="D", kind="claude")._env()["CLAUDE_CONFIG_DIR"] == "/global"


def poll(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def test_accounts_add_login_assign_remove(tmp_path):
    app = sample_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        f = app.state.factory
        assert c.get("/api/accounts").json() == []  # 全新安裝沒有任何帳號，不沿用電腦上的登入
        a = c.post("/api/accounts", json={"name": "工作帳號", "provider": "claude"}).json()
        aid = a["id"]
        assert a["provider"] == "claude" and not a["status"]["logged_in"]
        assert Path(a["config_dir"]).is_dir() and Path(a["config_dir"]).is_relative_to(tmp_path)
        assert not (tmp_path / "accounts.json").exists()  # 模擬模式不寫真正的帳號清單
        assert c.post("/api/accounts", json={"name": "工作帳號"}).status_code == 409
        assert c.post("/api/accounts", json={"name": "x", "provider": "nope"}).status_code == 409

        assert c.patch("/api/nodes/claude-opus-2", json={"account": aid}).status_code == 200
        assert f.connectors["claude-opus-2"].extra["config_dir"] == a["config_dir"]
        n = f.nodes["claude-opus-2"]
        assert n["account"] == aid and n["status"] == "offline" and "未登入" in n["detail"]
        assert f.nodes["claude-opus"]["account"] is None and f.nodes["claude-opus"]["status"] == "idle"  # 模擬模式免登入
        r = c.patch("/api/nodes/gemini", json={"account": aid})
        assert r.status_code == 409 and "類型" in r.json()["detail"]

        # 授權碼錯誤 → 登入失敗
        assert c.post(f"/api/accounts/{aid}/login").status_code == 200
        poll(lambda: f.accounts.logins.get(aid, {}).get("url"))
        assert c.post(f"/api/accounts/{aid}/code", json={"code": "bad"}).status_code == 200
        poll(lambda: aid in f.accounts.errors)
        assert "登入失敗" in f.accounts.errors[aid] and not f.accounts.status[aid]["logged_in"]
        # 取消登入
        assert c.post(f"/api/accounts/{aid}/login").status_code == 200
        assert c.post(f"/api/accounts/{aid}/cancel").status_code == 200
        assert aid not in f.accounts.logins
        assert c.post(f"/api/accounts/{aid}/code", json={"code": "x"}).status_code == 409
        # 成功登入 → 節點上線
        assert c.post(f"/api/accounts/{aid}/login").status_code == 200
        poll(lambda: f.accounts.logins.get(aid, {}).get("url"))
        assert c.post(f"/api/accounts/{aid}/code", json={"code": "ok"}).status_code == 200
        poll(lambda: f.nodes["claude-opus-2"]["status"] == "idle")
        assert f.accounts.status[aid]["email"] == f"{aid}@example.com" and not f.accounts.errors.get(aid)

        assert c.post("/api/accounts/nope/login").status_code == 404
        assert c.post(f"/api/accounts/{aid}/key", json={"key": "abc"}).status_code == 409  # Claude 不用金鑰
        assert c.post(f"/api/accounts/{aid}/logout").status_code == 200
        assert f.nodes["claude-opus-2"]["status"] == "offline"

    # 重啟後帳號與分配還在；移除帳號 → 節點變成未指定帳號
    app2 = create_app(force_fake=True, data_dir=tmp_path)
    with TestClient(app2, base_url=LOCAL) as c:
        f = app2.state.factory
        assert f.nodes["claude-opus-2"]["account"] == aid
        assert c.delete(f"/api/accounts/{aid}").status_code == 200
        assert f.nodes["claude-opus-2"]["account"] is None
        assert "config_dir" not in f.connectors["claude-opus-2"].extra
        assert c.get("/api/accounts").json() == []
        saved = {x["id"]: x for x in json.loads((tmp_path / "nodes.json").read_text(encoding="utf-8"))["nodes"]}
        assert saved["claude-opus-2"]["account"] is None


def test_key_accounts_never_reach_the_browser(tmp_path, monkeypatch):
    secret = "AIzaSy-test-secret-1234"
    app = sample_app(tmp_path)
    with TestClient(app, base_url=LOCAL) as c:
        f = app.state.factory
        a = c.post("/api/accounts", json={"name": "Gemini 金鑰", "provider": "gemini"}).json()
        aid = a["id"]
        assert c.post(f"/api/accounts/{aid}/login").status_code == 409  # 金鑰帳號不走瀏覽器登入
        assert c.post(f"/api/accounts/{aid}/key", json={"key": "has space"}).status_code == 409
        assert c.patch("/api/nodes/gemini", json={"account": aid}).status_code == 200
        assert "還沒有輸入 API 金鑰" in f.nodes["gemini"]["detail"]
        assert c.post(f"/api/accounts/{aid}/key", json={"key": secret}).status_code == 200
        assert f.nodes["gemini"]["status"] == "idle"
        extra = f.connectors["gemini"].extra
        assert extra["api_key"] == secret and extra["home"] == a["config_dir"]
        pub = c.get("/api/accounts").json()[0]
        assert pub["key_hint"] == "••••1234" and secret not in json.dumps(c.get("/api/state").json())
        with c.websocket_connect("/ws", headers=WS_HOST) as ws:
            assert secret not in json.dumps(ws.receive_json())
        # 金鑰交給 Gemini CLI 時，不沿用電腦上的 Google 登入設定
        monkeypatch.setenv("GOOGLE_API_KEY", "machine-key")
        monkeypatch.setenv("GOOGLE_GENAI_USE_GCA", "true")
        env = GeminiConnector(id="g", label="G", kind="gemini", extra=extra)._env()
        assert env["GEMINI_API_KEY"] == secret and env["GEMINI_CLI_HOME"] == a["config_dir"]
        assert "GOOGLE_API_KEY" not in env and "GOOGLE_GENAI_USE_GCA" not in env
        assert c.post(f"/api/accounts/{aid}/logout").status_code == 200  # 清除金鑰
        assert f.nodes["gemini"]["status"] == "offline"


FAKE_CLAUDE_AUTH = r'''
import json, os, sys
assert "ANTHROPIC_API_KEY" not in os.environ
marker = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "logged-in")
cmd = sys.argv[1:3]
if cmd == ["auth", "status"]:
    email = open(marker).read() if os.path.exists(marker) else ""
    print(json.dumps({"loggedIn": bool(email), "email": email or None, "subscriptionType": "max" if email else None}))
    sys.exit(0 if email else 1)
if cmd == ["auth", "login"]:
    print("Opening browser to sign in...", flush=True)
    print("If the browser didn't open, visit: https://example.invalid/authorize?code=true", flush=True)
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    if sys.stdin.readline().strip() != "good":
        print("Login failed: Request failed with status code 400", file=sys.stderr); sys.exit(1)
    open(marker, "w").write("second@example.com"); print("Login successful."); sys.exit(0)
if cmd == ["auth", "logout"]:
    os.remove(marker); sys.exit(0)
sys.exit(2)
'''


def fake_cli_any_os(tmp_path, body, name="claude"):
    """Windows 也能跑的假 CLI：.cmd（Windows）或 sh（POSIX）包一層 python。"""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    script = d / f"{name}_fake.py"
    script.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        (d / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        p = d / name
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def fake_claude_any_os(tmp_path, body):
    return fake_cli_any_os(tmp_path, body, "claude")


FAKE_CODEX = r'''
import base64, json, os, sys
sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
home = os.environ["CODEX_HOME"]
assert "OPENAI_API_KEY" not in os.environ
args = sys.argv[1:]
assert "secret_auth_storage" in args  # 憑證存在帳號資料夾
if args[:2] == ["login", "status"]:
    if os.path.exists(os.path.join(home, "auth.json")):
        print("Logged in using ChatGPT"); sys.exit(0)
    print("Not logged in", file=sys.stderr); sys.exit(1)
if args[:1] == ["login"]:
    print("If your browser did not open, navigate to this URL to authenticate:\n\nhttps://auth.example.invalid/oauth/authorize?x=1", flush=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": "me@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}).encode()).decode().rstrip("=")
    json.dump({"tokens": {"id_token": "h." + claims + ".s"}}, open(os.path.join(home, "auth.json"), "w"))
    print("Successfully logged in"); sys.exit(0)
if args[:1] == ["logout"]:
    os.remove(os.path.join(home, "auth.json")); sys.exit(0)
if args[:1] == ["exec"]:
    assert args[-1] == "-" and "--json" in args
    for f in ("shell_tool", "unified_exec", "browser_use", "computer_use"):
        assert args[args.index(f) - 1] == "--disable", f
    prompt = sys.stdin.read()
    events = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
              {"type": "error", "message": "Reconnecting... 1/5"},
              {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "想一下"}},
              {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "回覆：" + prompt}}]
    if prompt == "fail":
        events = events[:3] + [{"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}]
    else:
        events.append({"type": "turn.completed", "usage": {}})
    for e in events:
        print(json.dumps(e, ensure_ascii=False), flush=True)
    sys.exit(1 if prompt == "fail" else 0)
sys.exit(2)
'''


def test_codex_login_status_and_exec(tmp_path, monkeypatch):
    d = fake_cli_any_os(tmp_path, FAKE_CODEX, "codex")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-machine-key")
    home = tmp_path / "codex-home"
    home.mkdir()
    auth, urls = CodexAuth(tmp_path), []

    async def on_url(u):
        urls.append(u)

    async def go():
        assert (await auth.status(str(home)))["logged_in"] is False
        assert await auth.login(str(home), on_url, asyncio.Queue()) == (True, "")
        st = await auth.status(str(home))
        c = CodexConnector(id="x", label="X", kind="codex", workdir=tmp_path, extra={"codex_home": str(home)})
        chunks = []

        async def on_chunk(t):
            chunks.append(t)
        out = await c.run("中文", on_chunk)
        with pytest.raises(ConnectorError) as e:
            await c.run("fail", nothing)
        await auth.logout(str(home))
        return st, out, chunks, e.value, (await auth.status(str(home)))["logged_in"]

    st, out, chunks, err, after = asyncio.run(go())
    assert urls == ["https://auth.example.invalid/oauth/authorize?x=1"]
    assert st == {"logged_in": True, "email": "me@example.com", "plan": "plus"}
    assert out == "回覆：中文" and chunks == ["回覆：中文"]  # 不含推理過程
    assert "usage limit" in str(err) and not err.retryable
    assert after is False


class _OpenAI(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        ok = self.headers.get("Authorization") == "Bearer good"
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]} if ok else {"error": "bad key"}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in ("你", "好"):
            self.wfile.write(f'data: {json.dumps({"choices": [{"delta": {"content": piece}}]})}\n\n'.encode())
        self.wfile.write(b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')


def test_openai_compatible_api(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # 本機伺服器不能被送去 proxy
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    try:
        bad = OpenAIConnector(id="o", label="O", kind="openai", model="m1", extra={"base_url": base, "api_key": "nope"})
        assert bad.available() == (False, "API 金鑰無效或沒有權限")
        good = OpenAIConnector(id="o", label="O", kind="openai", model="m1", extra={"base_url": base, "api_key": "good"})
        assert good.available()[0] and good.models == ("m1", "m2")
        assert asyncio.run(good.run("hi", nothing)) == "你好"
        assert _OpenAI.seen[-1]["model"] == "m1" and _OpenAI.seen[-1]["stream"] is True
    finally:
        srv.shutdown()


def test_ollama_host_per_node(ollama_server, monkeypatch):
    ollama_server.tags = ["llama3:8b", "nomic-embed-text:latest"]
    host = os.environ["OLLAMA_HOST"]
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")  # 全域設定指向別處，節點自己的位址優先
    o = OllamaConnector(id="o", label="O", kind="ollama", extra={"host": host})
    assert o.available()[0] and o.models == ("llama3:8b",)


def test_claude_auth_cli_status_login_logout(tmp_path, monkeypatch):
    d = fake_claude_any_os(tmp_path, FAKE_CLAUDE_AUTH)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    cfg = tmp_path / "acct"
    cfg.mkdir()
    auth = ClaudeAuth(tmp_path)
    urls = []

    async def on_url(u):
        urls.append(u)

    async def go():
        assert (await auth.status(str(cfg)))["logged_in"] is False
        q = asyncio.Queue()
        q.put_nowait("wrong")
        ok, msg = await auth.login(str(cfg), on_url, q)
        assert not ok and "status code 400" in msg
        assert urls == ["https://example.invalid/authorize?code=true"]
        q = asyncio.Queue()
        q.put_nowait("good")
        assert await auth.login(str(cfg), on_url, q) == (True, "")
        assert await auth.status(str(cfg)) == {"logged_in": True, "email": "second@example.com", "plan": "max"}
        await auth.logout(str(cfg))
        assert (await auth.status(str(cfg)))["logged_in"] is False
        # 取消等待中的登入：CLI 被砍掉，不會卡住
        urls.clear()
        job = asyncio.create_task(auth.login(str(cfg), on_url, asyncio.Queue()))
        await wait_for(lambda: urls, timeout=15)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 15)

    asyncio.run(go())


@posix_only
def test_failure_detail_falls_back_to_stdout(tmp_path, monkeypatch):
    d = fake_cli(tmp_path, "gemini", "import sys\nsys.stdin.read()\nprint('Please login first')\nsys.exit(41)\n")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    g = GeminiConnector(id="g", label="G", kind="gemini", workdir=tmp_path)
    with pytest.raises(ConnectorError) as e:
        asyncio.run(g.run("hi", nothing))
    assert "Please login first" in str(e.value)


@posix_only
def test_gemini_env_and_stdin(tmp_path, monkeypatch):
    d = fake_cli(tmp_path, "gemini", (
        "import sys, os\n"
        "p = sys.stdin.read()\n"
        "assert os.environ['GEMINI_CLI_TRUST_WORKSPACE'] == 'true'\n"
        "assert os.environ['GEMINI_CLI_NO_RELAUNCH'] == 'true'\n"
        "print('echo:' + p + '|' + os.getcwd())\n"
    ))
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    ws = tmp_path / "ws"
    ws.mkdir()
    g = GeminiConnector(id="g", label="G", kind="gemini", workdir=ws)
    out = asyncio.run(g.run("多行\n提示", nothing))
    assert out == f"echo:多行\n提示|{ws}"


@posix_only
def test_timeout_kills_whole_process_tree(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    d = fake_cli(tmp_path, "gemini", (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    ))
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    g = GeminiConnector(id="g", label="G", kind="gemini", timeout=1.5, workdir=tmp_path)
    with pytest.raises(ConnectorError) as e:
        asyncio.run(g.run("hi", nothing))
    assert not e.value.retryable
    child = int(pidfile.read_text())
    time.sleep(0.3)
    try:
        os.kill(child, 0)
        alive = Path(f"/proc/{child}/stat").read_text().split()[2] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        alive = False
    assert not alive, "孫程序沒有被砍掉"


# ---------------- Ollama ----------------

class _Ollama(BaseHTTPRequestHandler):
    tags: list = []
    chat_lines: list = []
    chat_status = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": n} for n in self.tags]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(self.chat_status)
        self.end_headers()
        for line in self.chat_lines:
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()


@pytest.fixture
def ollama_server(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{srv.server_address[1]}")
    # 模擬 Windows 上設定了系統 proxy：本機請求不能被送去 proxy
    for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    yield _Ollama
    srv.shutdown()


def test_ollama_model_resolution(ollama_server):
    ollama_server.tags = ["nomic-embed-text:latest", "qwen2.5-coder:latest", "llama3:8b"]
    o = OllamaConnector(id="o", label="O", kind="ollama", model="qwen2.5-coder")
    ok, detail = o.available()
    assert ok and o.effective_model == "qwen2.5-coder:latest" and o.model == "qwen2.5-coder"
    o2 = OllamaConnector(id="o", label="O", kind="ollama", model="qwen2.5-coder:7b")
    ok, detail = o2.available()
    assert ok and o2.effective_model == "qwen2.5-coder:latest" and "改用" in detail  # 跳過 embedding 模型
    ollama_server.tags = ["nomic-embed-text:latest"]
    assert not OllamaConnector(id="o", label="O", kind="ollama", model="x").available()[0]


def test_ollama_stream_error_line(ollama_server):
    ollama_server.chat_status = 200
    ollama_server.chat_lines = [{"message": {"content": "def snake("}}, {"error": "llama runner process has terminated"}]
    o = OllamaConnector(id="o", label="O", kind="ollama", model="m")
    with pytest.raises(ConnectorError) as e:
        asyncio.run(o.run("hi", nothing))
    assert "terminated" in str(e.value)


def test_ollama_stream_without_done(ollama_server):
    ollama_server.chat_status = 200
    ollama_server.chat_lines = [{"message": {"content": "half"}}]
    o = OllamaConnector(id="o", label="O", kind="ollama", model="m")
    with pytest.raises(ConnectorError):
        asyncio.run(o.run("hi", nothing))


def test_ollama_http_error_body(ollama_server):
    ollama_server.chat_status = 404
    ollama_server.chat_lines = [{"error": "model 'm' not found"}]
    o = OllamaConnector(id="o", label="O", kind="ollama", model="m")
    with pytest.raises(ConnectorError) as e:
        asyncio.run(o.run("hi", nothing))
    assert "not found" in str(e.value)


def test_ollama_ok(ollama_server):
    ollama_server.chat_status = 200
    ollama_server.chat_lines = [{"message": {"content": "你"}}, {"message": {"content": "好"}, "done": True}]
    o = OllamaConnector(id="o", label="O", kind="ollama", model="m")
    assert asyncio.run(o.run("hi", nothing)) == "你好"


def test_ollama_base_normalizes_host():
    assert ollama_base("") == "http://127.0.0.1:11434"
    assert ollama_base("0.0.0.0") == "http://127.0.0.1:11434"
    assert ollama_base("127.0.0.1:9999") == "http://127.0.0.1:9999"
    assert ollama_base("http://box:11434/") == "http://box:11434"
