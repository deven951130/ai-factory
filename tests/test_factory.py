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

from backend.connectors import (
    ClaudeConnector,
    ConnectorError,
    GeminiConnector,
    OllamaConnector,
    ollama_base,
    run_subprocess,
)
from backend.factory import ASK_HINT, MAX_ASKS, Factory, find_question
from backend.main import create_app

CFG = Path(__file__).resolve().parent.parent / "nodes.json"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="假 CLI 用 shebang 腳本")


def make(tmp_path):
    return Factory(CFG, tmp_path, force_fake=True)


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
    cfg.write_bytes(b"\xef\xbb\xbf" + CFG.read_bytes())
    assert len(Factory(cfg, tmp_path / "d", force_fake=True).nodes) == 4


# ---------------- API / WebSocket ----------------

def test_api_chat_and_state(tmp_path):
    app = create_app(force_fake=True, data_dir=tmp_path)
    with TestClient(app) as c:
        assert len(c.get("/api/state").json()["nodes"]) == 4
        with c.websocket_connect("/ws") as ws:
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


def test_lone_surrogate_title_does_not_break_dashboard(tmp_path):
    app = create_app(force_fake=True, data_dir=tmp_path)
    body = '{"title": "abc\\ud83d", "prompt": "\\ud83c x"}'  # 前端把 emoji 切一半的樣子
    with TestClient(app) as c:
        r = c.post("/api/tasks", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_offline_node_rechecked_on_chat(tmp_path):
    app = create_app(force_fake=True, data_dir=tmp_path)
    with TestClient(app) as c:
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
def test_claude_is_error_result(tmp_path, monkeypatch):
    res = json.dumps({"type": "result", "is_error": True, "result": "Prompt is too long"})
    d = fake_cli(tmp_path, "claude", f"import sys\nsys.stdin.read()\nprint({res!r})\nsys.exit(1)\n")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    c = ClaudeConnector(id="c", label="C", kind="claude", workdir=tmp_path)
    with pytest.raises(ConnectorError) as e:
        asyncio.run(c.run("hi", nothing))
    assert "Prompt is too long" in str(e.value) and not e.value.retryable


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
