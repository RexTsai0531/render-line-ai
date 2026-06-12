# Render LINE AI Bot

這是一個可部署到 Render 的 LINE AI 機器人。

## 功能

- 驗證 LINE Webhook 簽章
- 收到文字訊息後在背景執行 LLM，並用 push message 傳正式答案
- 提供 `/` health check
- 提供 `/webhook` 給 LINE Messaging API 使用

## 環境變數

請在 Render 的 Environment 區設定：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_API_BASE`，NVIDIA OpenAI 相容 base URL
- `OPENAI_MODEL`，預設 `meta/llama-3.1-8b-instruct`
- `OPENAI_TIMEOUT_SECONDS`，預設 `60`
- `OPENAI_MAX_TOKENS`，預設 `1024`
- `OPENAI_TEMPERATURE`，預設 `0.2`
- `OPENAI_TOP_P`，預設 `0.7`
- `SYSTEM_PROMPT`，選填

範例：

```text
OPENAI_API_KEY=你的NVIDIA_API_KEY
OPENAI_API_BASE=https://integrate.api.nvidia.com/v1
OPENAI_MODEL=meta/llama-3.1-8b-instruct
```

## Render 部署

1. 將專案推到 GitHub
2. 在 Render 建立 Web Service
3. Build Command 設成 `pip install -r requirements.txt`
4. Start Command 設成 `gunicorn wsgi:app --bind 0.0.0.0:$PORT --timeout 120 --graceful-timeout 120`
5. 把 LINE Webhook URL 設成 `https://你的服務網址/webhook`

## LINE 設定

- 在 LINE Developers 建立 Messaging API channel
- 取得 Channel Secret 與 Channel Access Token
- 開啟 Webhook
- 關掉 Auto-reply

## 指令

- `help`: 顯示可用指令
- `reset`: 目前沒有對話記憶，所以會告訴你不用清除

## 本機測試

```bash
python app.py
```

打開 `http://127.0.0.1:10000/` 應該會看到 `{"status":"ok"}`
