# Render LINE AI Bot

這是一個可部署到 Render 的 LINE AI 機器人。

## 功能

- 驗證 LINE Webhook 簽章
- 文字訊息自動呼叫 NVIDIA LLM
- 圖片訊息可送進 vision model 分析
- 支援 `help`、`reset`、`remember`、`memories`、`forget`
- 會根據個人記憶庫，在回覆前先把相關資訊放進提示詞
- 會先檢索私有磁碟上的 RAG 規則，再把命中的規則塞進回答上下文

## 環境變數

請在 Render 的 Environment 區設定：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_API_BASE`，NVIDIA OpenAI 相容 base URL
- `OPENAI_MODEL`，預設 `meta/llama-3.1-8b-instruct`
- `OPENAI_VISION_MODEL`，選填，圖片分析用模型，預設同 `OPENAI_MODEL`
- `OPENAI_TIMEOUT_SECONDS`，預設 `60`
- `OPENAI_MAX_TOKENS`，預設 `1024`
- `OPENAI_TEMPERATURE`，預設 `0.2`
- `OPENAI_TOP_P`，預設 `0.7`
- `SYSTEM_PROMPT`，選填
- `BOT_DATA_DIR`，選填，預設 `data`

範例：

```text
OPENAI_API_KEY=你的NVIDIA_API_KEY
OPENAI_API_BASE=https://integrate.api.nvidia.com/v1
OPENAI_MODEL=meta/llama-3.1-8b-instruct
OPENAI_VISION_MODEL=meta/llama-3.1-8b-instruct
```

## 指令

- `help`：顯示指令說明
- `reset`：清除你的個人記憶庫
- `remember <內容>`：新增一條記憶
- `memories`：查看目前記憶
- `forget <id 或 關鍵字>`：刪除某條記憶

## RAG 知識庫

- 預設路徑：`/var/data/knowledge_base.json`
- 這裡放客服規則、話術、分類規則、門禁密碼等私有內容
- bot 回覆前會先檢索相關條目，再把命中的規則交給 LLM
- 這份檔案請放在 Render 私有磁碟，不要提交到 GitHub

## 圖片

直接傳圖片給 bot，若模型支援 vision，就會嘗試分析圖片內容。

## Render 部署

1. 將專案推到 GitHub
2. 在 Render 建立 Web Service
3. Build Command 設成 `pip install -r requirements.txt`
4. Start Command 設成 `gunicorn wsgi:app --bind 0.0.0.0:$PORT --timeout 120 --graceful-timeout 120`
5. 把 LINE Webhook URL 設成 `https://你的服務網址/webhook`
6. 在 Render 加一個 Private Disk，掛載到 `/var/data`
7. 在那個磁碟裡建立 `knowledge_base.json`

## LINE 設定

- 在 LINE Developers 建立 Messaging API channel
- 取得 Channel Secret 與 Channel Access Token
- 開啟 Webhook
- 關掉 Auto-reply

## 本機測試

```bash
python app.py
```

打開 `http://127.0.0.1:10000/` 應該會看到 `{"status":"ok"}`

## 注意

記憶庫目前儲存在 `BOT_DATA_DIR` 指定的本機檔案中。若 Render 執行環境沒有持久化磁碟，重新部署或重建後記憶可能會消失。
