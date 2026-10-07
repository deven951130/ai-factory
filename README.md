# AI Factory（視覺化 AI 工廠）

串接商業 AI（Claude、ChatGPT、Gemini、OpenAI 相容 API）與本地 AI（Ollama），以即時 Dashboard 呈現各節點狀態，
並可隨時對任一 AI 發問；AI 缺資訊時會暫停任務並在 Dashboard 向你提問。
AI 節點、帳號與生產線全部在 Dashboard 裡自己新增、登入與編輯。

> 從 [`ai-game-studio`](https://github.com/deven951130/ai-game-studio) 的 `apps/factory/` 拆出。不操作網頁 DOM：Claude 走 `claude -p`、ChatGPT 走 `codex exec`、Gemini 走 `gemini`（API 金鑰）、Ollama 與 OpenAI 相容 API 走 HTTP。

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
FACTORY_FAKE=1 python -m uvicorn backend.main:app   # 全部節點改為模擬；沒指定帳號的節點免登入示範
python -m pytest -q                                  # 測試
```

設定檔：`data/nodes.json`（不存在時從 repo 的 `nodes.json` 複製；repo 的版本是空的，代表全新安裝）。
Dashboard 上新增 / 修改的 AI 節點與生產線都會存回這個檔案。可用 `FACTORY_CONFIG`、`FACTORY_DATA_DIR` 指定位置。

### Windows 桌面版（安裝檔）

```powershell
.\build.ps1                  # 產出 dist\AIFactory\AIFactory.exe 與 dist\AIFactory-Setup-0.1.0.exe
.\build.ps1 -Version 0.2.0
```

- 需要 Python 3.11+；安裝檔需要 Inno Setup 6（`winget install JRSoftware.InnoSetup`），沒裝時只產出免安裝資料夾。
- 打包：PyInstaller（`--windowed`）+ pywebview（系統內建的 WebView2 視窗），入口是 `packaging/desktop.py`。關掉視窗就結束伺服器與所有 CLI 子程序；同時只能開一個。
- 安裝到 `%LOCALAPPDATA%\Programs\AI Factory`，不需要系統管理員權限。安裝精靈是英文（Inno Setup 沒有內建繁中），程式本身是中文。
- 設定與資料在 `%LOCALAPPDATA%\AIFactory`：`nodes.json`（AI 節點與生產線）、`data\`（帳號、帳號的登入資料夾、任務歷史、事件紀錄）、`logs\app.log`。更新版本不會刪除。
- 解除安裝時會詢問是否一併刪除帳號登入與所有設定（`%LOCALAPPDATA%\AIFactory` 與舊版的 `%USERPROFILE%\.claude-accounts`）；選「是」之後重新安裝就是全新狀態。安靜模式（`/SILENT`）解除安裝一律保留。
- Claude / Codex / Gemini CLI 與 Ollama 不包含在安裝檔內，每台電腦要各自安裝；帳號則在軟體裡登入。
- 沒有程式碼簽章：在別台電腦執行時 SmartScreen 會警告（「其他資訊 → 仍要執行」），部分防毒軟體可能誤判 PyInstaller 產生的 exe。
- 圖示：`packaging/app.ico`，由 `packaging/make_icon.py` 產生（需要 Pillow）。
- 開啟時播放 Oneiroverse Logo 動畫（約 4 秒，點一下或按任意鍵跳過；重新整理不會再播）：`frontend/oneiroverse/splash.js`，其他 Oneiroverse 網頁介面在 `<body>` 第一行引用即可。圖層由 `packaging/make_splash.py` 從 `packaging/brand/` 的 Logo 原圖產生（需要 Pillow）。

> 不要加 `--reload`：Windows 上 uvicorn 的 reload 模式改用 SelectorEventLoop，無法啟動 CLI 子程序。

## 第一次使用

全新安裝時沒有任何帳號、AI 與生產線，Dashboard 上方會顯示三個步驟：

1. 右邊「AI 帳號」：選類型、取名稱 →「新增帳號」，再登入（只用本地模型可以跳過）。
2. 「AI 節點」→「＋ 新增 AI」：選類型（Claude / ChatGPT（Codex）/ Gemini / 本地模型 / OpenAI 相容 API）、名稱、帳號、模型。
3. 「生產線」→「＋ 新增生產線」：取名稱、新增階段，每個階段選一個 AI 並寫指令。指令裡的 `{input}` 會換成上一階段的輸出（第一階段是任務內容），`{task}` 會換成原始任務內容（例如讓「審查」對照原始需求）。可調整順序、刪除階段。

之後隨時可以在節點卡片按「設定」修改或刪除 AI；生產線還在用的 AI 不能刪除，要先改生產線。

## 功能

- 節點卡片：閒置 / 執行中 / 等你回答 / 失敗 / 離線。每 15 秒重新偵測，Ollama 晚啟動也會自動變可用
- 本地模型：卡片上的下拉選單列出 Ollama 已安裝的模型，選了立刻切換並記住；可以新增多個本地節點（各自的模型與 Ollama 位址）
- 生產線：可以有多條（例如「遊戲企劃」、「寫程式」各一條），送任務時選要跑哪一條。逐階段傳遞輸出，任務卡片即時顯示 AI 正在寫的內容。任務照送出當下的生產線跑，之後修改或刪除生產線不影響進行中的任務
- 任務歷史：存在 `data/tasks.json`（模擬模式是 `fake-tasks.json`），重開軟體還在，最多保留 200 筆（超過時刪最舊的已結束任務）。關閉軟體時還沒完成的任務顯示「已中斷」
- 匯出結果：已結束的任務可「複製」或「下載」成 Markdown（任務內容 + 每個階段的輸出），也可以刪除
- 隨時發問：對話面板選任一節點，回覆逐字串流；可同時問多個 AI
- AI 提問：階段回覆含 `[[ASK_USER: 問題]]` → 任務暫停、節點轉橘色、瀏覽器通知，你回答後續跑（每階段最多問 3 次）
- 取消：進行中或等待回答的任務可隨時取消，CLI 子程序連同子孫程序一起結束
- 失敗處理：失敗最多重試 2 次；逾時、Claude 回報的錯誤不重試；仍失敗任務進「已阻擋」並顯示原因
- 事件紀錄：`data/events.jsonl`

## 各 AI 的前提

| 類型 | 電腦上要裝 | 帳號（在軟體裡登入） | 備註 |
|---|---|---|---|
| Claude | `claude` CLI（`npm install -g @anthropic-ai/claude-code`） | 瀏覽器登入（Claude 訂閱） | 子程序會移除 `ANTHROPIC_API_KEY`，避免改走按量計費；工具全部關閉，只做文字生成 |
| ChatGPT（Codex） | `codex` CLI（`npm install -g @openai/codex`） | 瀏覽器登入（ChatGPT 訂閱） | `codex exec --sandbox read-only`，並關閉執行指令、瀏覽器等工具，只做文字生成 |
| Gemini | `gemini` CLI（`npm install -g @google/gemini-cli`） | API 金鑰（Google AI Studio） | 每個帳號有自己的 `GEMINI_CLI_HOME`，不讀電腦上的 `~/.gemini` 與 Google 登入相關環境變數 |
| 本地模型 | Ollama，且至少 pull 一個對話模型 | 不需要 | 指定的模型沒裝時自動改用已安裝的對話模型（略過 embedding 模型）。位址可每個節點各自設定，留空用 `OLLAMA_HOST` 或 `127.0.0.1:11434`；本機連線不經系統 proxy |
| OpenAI 相容 API | 不需要 | API 金鑰（本機伺服器可不指定帳號） | 填 API 網址（例如 `https://api.openai.com/v1`、DeepSeek、xAI、OpenRouter，或 LM Studio 的 `http://127.0.0.1:1234/v1`）與模型名稱。走按量計費的服務會產生費用 |

### AI 帳號

所有帳號都在 Dashboard 的「AI 帳號」面板新增與登入，**不會沿用這台電腦上 CLI 自己的登入**（例如你平常用的 Claude Code）。沒有指定帳號、或帳號沒登入的節點顯示離線，不會執行。

- Claude / ChatGPT：「登入」→ 瀏覽器自動開登入頁，完成後面板自動更新。瀏覽器若已登入**另一個**帳號，把面板上的網址貼到無痕視窗登入（Claude 要再把頁面給的授權碼貼回面板；ChatGPT 的網址要在同一台電腦上開）。
- Gemini / OpenAI 相容 API：貼上 API 金鑰後按「儲存」。金鑰只存在本機，畫面上只顯示末四碼。
- 每個 Claude / ChatGPT / Gemini 帳號有自己的設定資料夾（`data/accounts/<id>`），登入資訊各自存放、互不影響；同一種 AI 可以有多個帳號，分給不同節點同時使用。
- 節點用哪個帳號在節點的「設定」選。「移除」帳號會先登出（或刪掉金鑰），用它的節點變成「未指定帳號」。
- 帳號清單（含 API 金鑰）存在 `data/accounts.json`；模擬模式用 `data/fake-accounts.json`，不呼叫真正的 CLI。
- 從 0.1.x 更新：Claude 帳號與節點分配會自動搬過來；原本的「預設帳號」（沿用電腦上的 Claude Code 登入）已移除，用它的節點要新增帳號並登入一次。

## 安全

- 只接受本機存取：`Host` 必須是 `localhost` / `127.0.0.1` / `::1`，瀏覽器帶的 `Origin` 也必須是本機。其他網站的頁面無法讀取 Dashboard 的 prompt、輸出，也無法取消任務。
- 要從區網其他電腦開啟：以 `--host 0.0.0.0` 啟動，並設定 `FACTORY_ALLOWED_HOSTS`（逗號分隔，例如 `192.168.1.10,DESKTOP-7H2K9QX`）。區網內任何人都能看到所有內容，也能操作 AI 帳號、節點與生產線，請只在信任的網路使用。
- API 金鑰存在 `data/accounts.json`，Windows 上以 DPAPI 加密（只有同一個 Windows 帳號解得開；複製到別台電腦會要求重新輸入），不會傳給瀏覽器。0.2.0 存的明文金鑰會在啟動時自動改成加密。其他系統（開發 / 測試）維持明文。

每個節點可在「設定」調整逾時（秒）。CLI 都在 `data/workspace/` 這個空資料夾執行，AI 讀不到本專案的檔案。

## 已知限制 / 下一步

- 桌面版沒有程式碼簽章，也沒有自動更新；新版本要重新執行安裝檔。
- 任務只做文字生成（Claude / Codex 的工具已停用）；尚無寫檔 / 跑測試 / git worktree 沙箱。
- 節點狀態只存在記憶體；重開時重新偵測。
- Gemini 只支援 API 金鑰：Gemini CLI 在非互動模式下不能用 Google 帳號手動登入。
- 實機驗證過：Claude 帳號登入狀態查詢、Ollama 模型清單與切換、全新安裝流程（模擬模式）。ChatGPT（Codex）以假 CLI、OpenAI 相容 API 以測試伺服器、Gemini 金鑰以環境變數檢查驗證，尚未用真實帳號跑過。
