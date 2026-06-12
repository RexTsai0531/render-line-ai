# Render LINE AI Bot

這是一個可直接部署到 Render 的 LINE AI 機器人。

## 功能

- 驗證 LINE Webhook 簽章
- 收到文字訊息後呼叫 OpenAI 產生回覆
- 先即時回覆「正在思考」，再在背景執行 LLM 並用 push message 傳正式答案
- 提供 `/` health check
- 提供 `/webhook` 給 LINE Messaging API 使用

## 環境變數

請在 Render 的 Environment 區設定：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_API_BASE`，如果你用 NVIDIA 相容 OpenAI API，就填你的 NVIDIA endpoint
- `OPENAI_MODEL`，預設 `llama-3.1-8b-instruct`
- `OPENAI_TIMEOUT_SECONDS`，選填，預設 `60`
- `OPENAI_MAX_TOKENS`，選填，預設 `256`
- `SYSTEM_PROMPT`，選填

如果你的 LLM 是 NVIDIA 提供的 OpenAI 相容模型，通常可以這樣填：

- `OPENAI_API_KEY` = 你的 NVIDIA API Key
- `OPENAI_API_BASE` = NVIDIA 提供的 OpenAI 相容 base URL
- `OPENAI_MODEL` = 你的模型名稱，例如 `llama-3.1-8b-instruct`

如果你的 NVIDIA 服務文件有指定不同的 base URL 或模型名稱，請以那份文件為準。

## Render 部署

1. 將這個專案推到 GitHub
2. 在 Render 建立 Web Service
3. Build Command 設成 `pip install -r requirements.txt`
4. Start Command 設成 `gunicorn wsgi:app --bind 0.0.0.0:$PORT --timeout 120 --graceful-timeout 120`
5. 把 LINE Webhook URL 設成 `https://你的服務網域/webhook`

## LINE 設定

- 在 LINE Developers 建立 Messaging API channel
- 取得 Channel Secret 與 Channel Access Token
- 開啟 Webhook
- 關閉 Auto-reply，避免跟 bot 回覆互相干擾

## 本機測試

```bash
python app.py
```

開啟 `http://127.0.0.1:10000/` 應該會看到 `{"status":"ok"}`
