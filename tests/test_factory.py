import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.connectors import ConnectorError, FakeConnector
from backend.factory import Factory
from backend.main import create_app

CFG = Path(__file__).resolve().parent.parent / "nodes.json"


def make(tmp_path):
    return Factory(CFG, tmp_path, force_fake=True)


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
        calls = {"n": 0}

        async def run(prompt, on_chunk, timeout=300):
            calls["n"] += 1
            return "[[ASK_USER: 要幾個玩家？]]" if calls["n"] == 1 else f"ok:{prompt[-20:]}"
        f.connectors["gemini"].run = run
        t = f.create_task("t", "x")
        job = asyncio.create_task(f.run_task(t["id"]))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if t["status"] == "waiting_user":
                break
        assert t["status"] == "waiting_user" and t["question"]["text"] == "要幾個玩家？"
        assert f.nodes["gemini"]["status"] == "waiting"
        assert f.answer(t["id"], "兩個")
        await job
        return t
    t = asyncio.run(go())
    assert t["status"] == "done" and "兩個" in t["outputs"][0]["text"]


def test_failure_retries_then_blocks(tmp_path):
    async def go():
        f = make(tmp_path)
        n = {"c": 0}

        async def boom(prompt, on_chunk, timeout=300):
            n["c"] += 1
            raise ConnectorError("額度已滿")
        f.connectors["gemini"].run = boom
        t = f.create_task("t", "x")
        await f.run_task(t["id"])
        return t, n["c"], f
    t, calls, f = asyncio.run(go())
    assert t["status"] == "blocked" and calls == 3
    assert f.nodes["gemini"]["status"] == "failed"


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


def test_ollama_base_normalizes_host():
    from backend.connectors import ollama_base
    assert ollama_base("") == "http://127.0.0.1:11434"
    assert ollama_base("0.0.0.0") == "http://127.0.0.1:11434"
    assert ollama_base("127.0.0.1:9999") == "http://127.0.0.1:9999"
    assert ollama_base("http://box:11434/") == "http://box:11434"


def test_subprocess_stream_keeps_multibyte_chars(tmp_path):
    import sys
    from backend.connectors import _stream_subprocess
    text = "中文串流測試" * 200  # 遠超過 256 bytes，必定跨塊
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.stderr.write('x' * 200000)\n",
        encoding="utf-8",
    )
    chunks = []

    async def on_chunk(t):
        chunks.append(t)
    out = asyncio.run(_stream_subprocess([sys.executable, str(script)], text, on_chunk, 30))
    assert out == text and "".join(chunks) == text and "�" not in out


def test_unexpected_exception_blocks_task(tmp_path):
    async def go():
        f = make(tmp_path)

        async def weird(prompt, on_chunk, timeout=300):
            raise ValueError("bad json")
        f.connectors["gemini"].run = weird
        t = f.create_task("t", "x")
        await f.run_task(t["id"])
        return t
    t = asyncio.run(go())
    assert t["status"] == "blocked" and "ValueError" in t["error"]
