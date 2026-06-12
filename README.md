# Render LINE AI Bot

這是一個可直接部署到 Render 的 LINE AI 機器人。

## 功能

- 驗證 LINE Webhook 簽章
- 收到文字訊息後呼叫 OpenAI 產生回覆
- 提供 `/` health check
- 提供 `/webhook` 給 LINE Messaging API 使用

## 環境變數

請在 Render 的 Environment 區設定：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`，預設 `gpt-4.1-mini`
- `SYSTEM_PROMPT`，選填

## Render 部署

1. 將這個專案推到 GitHub
2. 在 Render 建立 Web Service
3. Build Command 設成 `pip install -r requirements.txt`
4. Start Command 設成 `gunicorn wsgi:app --bind 0.0.0.0:$PORT`
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
