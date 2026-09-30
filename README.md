---
title: Bba Chat
emoji: 👀
colorFrom: gray
colorTo: indigo
sdk: docker
pinned: false
---

# bba-chat

Ask AI backend for the Big Brain Ape dashboard. The live Space is [simzy-bba-chat.hf.space](https://simzy-bba-chat.hf.space) (`Simzy/bba-chat`).

The dashboard posts a question and polls this inbox for a `role=agent` reply. This server stores those messages and, after each `POST /send`, asks a Virtuals GAME `ChatAgent` that speaks as Big Brain Ape / BigBrain2Bot (Virtuals agent #96114, DegenClaw #1729).

`@BigBrain2Bot` on Telegram stays on Virtuals. This Space does not register a Telegram webhook.

## How Ask AI uses it

The dashboard (`app35.html`) does this:

1. `POST /send` with `{"text": "...", "user": "analysis-app"}`.
2. Poll `GET /messages?since=<user message ts>` about every 4 seconds, for up to 120 seconds.
3. Render the first `role=agent` message whose `ts` is greater than that user timestamp.

The tasks page uses the same inbox with `user: "Simzy"`.

Agent replies are produced by `game-sdk` 0.1.5:

```python
from game_sdk.game.chat_agent import Chat, ChatAgent

agent = ChatAgent(api_key=os.environ["GAME_API_KEY"], prompt=AGENT_PROMPT)
chat = agent.create_chat(partner_id=user, partner_name=user)
response = chat.next(user_text)  # response.message is stored as role=agent
```

`ChatAgent` lives in `game_sdk.game.chat_agent` (not `game_sdk.game.agent`). The published 0.1.5 wheel includes that module. The key must start with `apt-` or the SDK raises before any call.

Sessions are per `user`. The SDK has no save/resume helper, so this app stores `conversation_id` in `$BBA_CHAT_DATA/sessions.json` and rebuilds `Chat(conversation_id, client)` for the next turn. If a conversation is finished or the next call fails, the id is dropped and `create_chat` runs again.

The Ask AI page hides agent text that contains `telegram`, `on it`, `get back to you`, or `I'll review`. GAME replies are rewritten so those phrases do not swallow the answer. `POST /respond` text is stored as sent.

## HTTP contract

| Method | Path | Body / query | Notes |
| --- | --- | --- | --- |
| GET | `/` | | Health plus message count and whether `GAME_API_KEY` is set |
| GET | `/health` | | Same booleans as `/` |
| GET | `/messages` | `since` (unix seconds, default 0) | `{"messages": [...], "total": N}` |
| POST | `/send` | `{"text", "user"}` | Stores `role=user`, then replies in the background |
| POST | `/respond` | `{"text", "secret"}` | Manual `role=agent` write. Does not call GAME |
| GET | `/pending` | `secret` | User messages after the latest agent message |

User message shape: `id`, `ts`, `role` (`user`), `text`, `user`.

Agent message shape: `id`, `ts`, `role` (`agent`), `text`.

`/` and `/health` include `game_api_key_set` and `game_api_key_v2` (true only when the key starts with `apt-`). They never return the key.

Without `GAME_API_KEY`, `/send` still returns immediately and stores an agent message that says the key is not configured. A key that does not start with `apt-` stores that error instead of raising.

## Secrets Casey must set on the Space

In the Space: **Settings → Variables and secrets → New secret**.

| Secret | Required | Purpose |
| --- | --- | --- |
| `GAME_API_KEY` | Yes, for live replies | Virtuals GAME v2 key. Must start with `apt-`. From the GAME console. |
| `BBA_CHAT_SECRET` | Recommended | Protects `POST /respond` and `GET /pending`. If unset, the process falls back to `bba-chat-2026`. |

Do not put either value in the Dockerfile or the repo. The image only sets `BBA_CHAT_DATA=/data`.

Optional runtime setting (not a secret): `BBA_CHAT_GAME_TIMEOUT` seconds for the GAME HTTP calls. Default 75, clamped to 10–110 so a failure still lands inside the dashboard's 120 second poll.

After saving secrets, restart the Space so the container sees them.

## Health check

```bash
curl -s https://simzy-bba-chat.hf.space/health
```

Expect `"status": "ok"` and `"game_api_key_set": true` once the secret is present. Then:

```bash
curl -s -X POST https://simzy-bba-chat.hf.space/send \
  -H 'Content-Type: application/json' \
  -d '{"text":"What are the three lenses?","user":"casey"}'
curl -s 'https://simzy-bba-chat.hf.space/messages?since=0'
```

A GAME reply usually shows up within 30–90 seconds. If the key is missing, the agent error row is written as soon as the background task runs.

## Run locally

```bash
pip install -r requirements.txt
export BBA_CHAT_DATA=/tmp/bba-chat
export GAME_API_KEY=apt-your-key
export BBA_CHAT_SECRET=choose-a-secret
python app.py
```

The process listens on `0.0.0.0:7860`. Messages go to `$BBA_CHAT_DATA/messages.json`. `chat.json` in this repo is the old stub and is not the live store.

## Deploy

This repo is the Space source: `Dockerfile`, `app.py`, `requirements.txt`. Point the Hugging Face Space at this repo, or copy these files into `Simzy/bba-chat`, then set the secrets above and rebuild. Rebuilding without `GAME_API_KEY` keeps the inbox up and stores the not-configured agent message.
