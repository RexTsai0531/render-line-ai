# Render LINE AI Bot

This is a LINE AI customer-service bot for Render.

## Features

- Verifies LINE webhook signatures
- Calls NVIDIA LLM for text replies
- Supports image analysis
- Supports `help`, `reset`, `remember`, `memories`, `forget`
- Uses a private knowledge base from Supabase for RAG
- Uses per-user memory to personalize answers
- Runs in a strict customer-service mode that prefers RAG rules over free-form generation

## Environment Variables

Set these in Render:

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_API_BASE` - NVIDIA OpenAI-compatible base URL
- `OPENAI_MODEL` - default `meta/llama-3.1-8b-instruct`
- `OPENAI_VISION_MODEL` - optional, defaults to `OPENAI_MODEL`
- `OPENAI_TIMEOUT_SECONDS` - default `60`
- `OPENAI_MAX_TOKENS` - default `1024`
- `OPENAI_TEMPERATURE` - default `0.2`
- `OPENAI_TOP_P` - default `0.7`
- `SYSTEM_PROMPT` - optional
- `BOT_DATA_DIR` - optional, default `data`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Example:

```text
OPENAI_API_KEY=your_nvidia_api_key
OPENAI_API_BASE=https://integrate.api.nvidia.com/v1
OPENAI_MODEL=meta/llama-3.1-8b-instruct
OPENAI_VISION_MODEL=meta/llama-3.1-8b-instruct
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
```

## Supabase RAG

Use `work/supabase_rag_schema.sql` to create:

- `knowledge_chunks` table
- `search_knowledge_chunks()` RPC
- RLS policy

The bot will call Supabase first. If Supabase is unavailable, it falls back to local knowledge storage.

Use `work/supabase_knowledge_seed.sql` to seed the private客服 knowledge base.

Use `work/supabase_store_passwords_schema.sql` to create the private `store_passwords` table.
Use `work/supabase_store_passwords_template.sql` as a template to insert store names and passwords.

## Commands

- `help` - show available commands
- `reset` - clear your personal memory
- `remember <text>` - add a memory
- `memories` - list memory items
- `forget <id or keyword>` - delete memory items

## Render Deploy

1. Push code to GitHub
2. Create a Web Service on Render
3. Set Build Command to `pip install -r requirements.txt`
4. Set Start Command to `gunicorn wsgi:app --bind 0.0.0.0:$PORT --timeout 120 --graceful-timeout 120`
5. Set LINE Webhook URL to `https://your-service-url/webhook`

## LINE Setup

- Create a Messaging API channel in LINE Developers
- Copy Channel Secret and Channel Access Token
- Enable Webhook
- Disable Auto-reply

## Local Test

```bash
python app.py
```

Open `http://127.0.0.1:10000/` and you should see `{"status":"ok"}`.
