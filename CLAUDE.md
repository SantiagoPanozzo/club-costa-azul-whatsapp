# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp bot for Club Costa Azul (Uruguay). Receives forwarded Meta webhook payloads from an upstream service, processes them through a conversation state machine, and replies via the Graph API. It also calls a Club Costa Azul services API for member (socio) and activity data.

## Commands

```bash
# Install dependencies
uv sync

# Run locally (hot-reload)
uv run uvicorn app.main:app --reload --port 8000

# Docker
docker build -t costa-azul-bot .
docker run --env-file .env -p 8000:8000 costa-azul-bot
```

No test suite or linter is configured yet.

## Architecture

```
Meta Cloud API → upstream webhook (forwards raw payload) → POST /webhook (this bot)
this bot → Graph API (send messages to users)
this bot → Club Costa Azul services API (socios / actividades / inscripciones)
```

This service does **not** handle Meta webhook verification or receive Meta's webhook directly. An upstream service forwards the raw payload unmodified.

### Key modules

- **`app/main.py`** — FastAPI app with `/webhook` (POST) and `/health` (GET). Always returns 200 to prevent upstream retries.
- **`app/conversation.py`** — Thin router: sign-in → global keyword middleware → flow dispatch → main menu. Entry point: `handle_message()`. Does not contain flow logic.
- **`app/state.py`** — In-memory `SessionStore` keyed by phone number. `Session` holds `socio`, `active_flow` (registry key), and `flow_state` (typed per-flow dataclass). Thread-safe via `threading.Lock`. Volatile (lost on restart). Replace with Redis/DB for production, keeping `get`/`reset` interface.
- **`app/flows/base.py`** — `BaseFlow[T]` ABC (generic over the flow's state dataclass) and `FlowResult` StrEnum. All flows subclass this.
- **`app/flows/activities.py`** — Activity sign-up flow. `ActivitiesStep` StrEnum, `ActivitiesState` dataclass, `ActivitiesFlow` implementation.
- **`app/flows/__init__.py`** — `FLOW_REGISTRY` mapping menu item IDs to flow instances. Adding a flow = one entry here + one `MENU_OPTIONS` row in `conversation.py`.
- **`app/services_client.py`** — Async `httpx` client wrapping the Club Costa Azul API. Endpoints: `/socios/by-whatsapp/{number}`, `/actividades`, `/socio/{id}/inscripciones`, `/inscripciones`. All errors raise `ServicesAPIError`.
- **`app/whatsapp_client.py`** — Async `httpx` client for the Meta Graph API. Supports `send_text`, `send_buttons` (max 3), and `send_list` (max 10 rows). Sending failures are logged, not raised.
- **`app/webhook_parser.py`** — Parses raw Meta payload into `IncomingMessage` dataclasses. Handles `text` and `interactive` (list_reply, button_reply) message types.
- **`app/config.py`** — `pydantic-settings` config loaded from `.env`.

### Adding new flows

See **[docs/flows.md](docs/flows.md)** for the full guide. In short: create a flow module in `app/flows/` with a `StrEnum` for steps, a state dataclass, and a `BaseFlow` subclass; register it in `FLOW_REGISTRY`; add a menu row.

### Conversation flow (Spanish)

All user-facing messages are in Spanish. The bot auto-identifies the user by WhatsApp number (no login prompt), shows their current activity enrollments, offers available activities, and confirms sign-up via interactive buttons.

### WhatsApp API constraints

- Button titles: max 20 chars
- List section titles: max 24 chars
- List row descriptions: max 72 chars
- Max 3 buttons per message, max 10 rows per list
- These limits are enforced via `_truncate()` in `whatsapp_client.py`.

## Environment Variables

Copy `.env.example` to `.env`. Required vars: `WHATSAPP_API_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `SERVICES_API_BASE_URL`. Optional: `WHATSAPP_API_VERSION` (default `v20.0`), `LOG_LEVEL` (default `INFO`).

## Deployment

Deployed to Railway via Dockerfile. Railway injects `$PORT` at runtime; the CMD respects it.
