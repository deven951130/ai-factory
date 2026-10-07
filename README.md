# AI Factory（視覺化 AI 工廠）

串接商業 AI（Claude / Gemini 訂閱 CLI）與本地 AI（Ollama），以即時 Dashboard 呈現各節點狀態，
並可隨時對任一 AI 發問；AI 缺資訊時會暫停任務並在 Dashboard 向你提問。

> 從 [`ai-game-studio`](https://github.com/deven951130/ai-game-studio) 的 `apps/factory/` 拆出。不操作網頁 DOM、不需 API 金鑰：Claude 走 `claude -p`、Gemini 走 `gemini -p`、Ollama 走 HTTP。

## 啟動

Windows（PowerShell）一鍵啟動：建立 `.venv`、安裝套件、開瀏覽器。

```powershell
.\start.ps1            # 真實模式
.\start.ps1 -Fake      # 模擬節點示範
```

手動：

```bash
pip install -r requirements.txt
python -m uvicorn backend.main:app --port 8000     # 開 http://localhost:8000
FACTORY_FAKE=1 python -m uvicorn backend.main:app   # 全部節點改為模擬，免登入示範
python -m pytest -q                                  # 測試
```

節點與生產線在 `nodes.json` 設定（改 model、增減節點、調整階段與 prompt 模板）。

## 功能

- 節點卡片：閒置 / 執行中 / 等你回答 / 失敗 / 離線（啟動時自動偵測 CLI 與 Ollama）
- 生產線：預設 分析(Gemini) → 實作(Claude Sonnet) → 審查(Claude Opus)，逐階段傳遞輸出
- 隨時發問：對話面板選任一節點，回覆即時串流
- AI 提問：階段回覆含 `[[ASK_USER: 問題]]` → 任務暫停、節點轉橘色、瀏覽器通知，你回答後續跑
- 失敗處理：每次呼叫最多重試 2 次，仍失敗任務進 `blocked`；同一節點同時只跑一件
- 事件紀錄：`data/events.jsonl`

## 已知限制 / 下一步

- 目前只有 Web 介面；桌面殼（Electron）與安裝檔尚未做。
- 任務只做文字生成（Claude 工具已停用）；尚無寫檔 / 跑測試 / git worktree 沙箱。
- 任務與節點狀態僅存記憶體，重啟後不還原（僅事件 log 持久化）。
- Gemini CLI、Ollama 在 CI/雲端環境未實測，僅在 Claude CLI 做過真實呼叫。
