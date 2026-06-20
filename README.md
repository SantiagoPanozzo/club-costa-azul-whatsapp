# Club Costa Azul – WhatsApp Bot (bootstrap)

Stateful WhatsApp bot for Club Costa Azul (Uruguay). This bootstrap implements:

- Automatic sign-in by WhatsApp number against the services API.
- A main menu (single option for now).
- Activity sign-up: list current inscriptions, list available activities, confirm, sign up.

## Architecture

This service does **not** talk to Meta for verification or receive Meta's
webhook directly. An existing upstream webhook receives Meta's calls and
forwards the raw payload, unmodified, via POST to `/webhook` on this service.
This service replies to users by calling the Graph API directly (it needs its
own WhatsApp token + phone number id).

```
Meta Cloud API → existing webhook (forwards raw payload) → POST /webhook (this service)
this service → Graph API (send message) → user
this service → Club Costa Azul services API (socios / actividades / inscripciones)
```

## Project layout

```
app/
  main.py            FastAPI app, /webhook endpoint
  config.py           Env-based settings
  webhook_parser.py    Parses raw Meta payload into IncomingMessage objects
  conversation.py      Conversation/state-machine logic (sign-in, menu, signup)
  state.py             In-memory per-phone session store
  services_client.py   Club Costa Azul services API client
  whatsapp_client.py    Graph API client (send text/list/buttons)
```

## Setup

```bash
uv sync
cp .env.example .env  # fill in the values
```

Required env vars:

| Var | Description |
|---|---|
| `WHATSAPP_API_TOKEN` | Graph API access token for this bot's WhatsApp number |
| `WHATSAPP_PHONE_NUMBER_ID` | Phone number id used to send messages |
| `WHATSAPP_API_VERSION` | Graph API version, default `v20.0` |
| `SERVICES_API_BASE_URL` | Base URL of the Club Costa Azul services API |

## Run locally

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Health check: `GET /health`
Webhook endpoint (point your local webhook forwarder here): `POST /webhook`

## Run with Docker

```bash
docker build -t costa-azul-bot .
docker run --env-file .env -p 8000:8000 costa-azul-bot
```

## Deploy to Railway

1. Push this repo, create a new Railway service from it (Dockerfile detected automatically).
2. Set the env vars listed above in the Railway service settings.
3. Railway injects `$PORT` at runtime; the Dockerfile's `CMD` already respects it.
4. Point the upstream webhook's forwarding target at this service's `/webhook` URL.

## Known limitations / next steps

- **State storage is in-memory** (`app/state.py`): fine for a single instance/bootstrap,
  but lost on restart and incorrect with >1 replica. Swap for Redis (or similar) before
  scaling, keeping the same `get`/`reset` interface.
- **No request signature verification** at `/webhook` — relies on the upstream webhook
  service and network boundary for trust. Add a shared secret if this endpoint is
  ever exposed beyond that.
- **No idempotency/dedup** on Meta message IDs — a retried webhook delivery could be
  processed twice. Low risk in this bootstrap, worth adding `wamid` dedup later.
- **Activity lists are capped at 10 rows** (WhatsApp's interactive-list limit); if a
  club ever has more than 10 simultaneously open activities, this will need pagination.
- **No automated tests** included in this bootstrap.
