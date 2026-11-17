# Voice Ordering Agent

AI phone agent that takes food delivery orders in Russian for a restaurant: it
answers the call, sells from the real menu, and puts a confirmed order on the
kitchen board. Python, LiveKit Agents, FastAPI, PostgreSQL.

Built for Tarelka, a delivery café in Makhachkala, Dagestan, and piloted on a
dedicated phone number in August 2026.

[![CI](https://github.com/nurzhan2/voice-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/nurzhan2/voice-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)

[Русская версия](README.ru.md)

## Problem

- A delivery café loses orders whenever the only operator is busy, off shift,
  or the line is engaged during the evening rush.
- A second operator costs more than the margin on the calls they would catch.
- IVR menus and keyword bots make callers press buttons or repeat themselves;
  callers hang up.
- The owner already had a sales script that works. The job was to follow it on
  every call without drifting, inventing dishes, or pushing too hard.

## Features

- **Natural phone conversation in Russian**: streaming speech recognition,
  semantic end-of-turn detection, and replies generated while the caller is
  still finishing a sentence.
- **No invented dishes**: the menu never enters the prompt; items are found by
  tool call and added only by database ID.
- **Server-side sales limits**: the upsell counter lives in Redis and the API
  stops returning suggestions after two offers, whatever the prompt says.
- **Prices re-read at confirmation**: the in-call draft is treated as untrusted;
  totals are recomputed from PostgreSQL, and sold-out dishes block the order.
- **One tool layer, two channels**: phone and WhatsApp share the same tools,
  guards and sales script.
- **Kitchen board**: a single-page panel with live orders, a stop list, and
  call transcripts for reviewing how the agent spoke.

## Architecture

```mermaid
flowchart LR
    P[Phone call] -->|SIP :5080| S[livekit-sip]
    S --> L[LiveKit server]
    L --> A[agent.py]
    W[WhatsApp] -->|webhook| B[wa_bot.py]
    A --> T[tools.dispatch + guards]
    B --> T
    T --> F[FastAPI backend]
    F --> R[(Redis: call draft)]
    F --> DB[(PostgreSQL: menu, orders, transcripts)]
    F --> K[Kitchen board]
    F -.-> I[iiko POS]
```

- `agent.py` runs the voice loop: Deepgram for speech-to-text, an OpenAI model
  with function calling, ElevenLabs for speech. It knows nothing about the menu.
- `tools.py` declares the tools the model may call and routes them to the
  backend; `guards.py` attaches constraints to the results. WhatsApp enters the
  same `dispatch`, so both channels get identical rules.
- The FastAPI backend owns the order: trigram search over dish names and
  pronunciation aliases, the per-call draft in Redis (4 h TTL), and the final
  order in PostgreSQL.
- The menu lives in `menu.yaml` and is synced into the database on start, so a
  menu change is a reviewed diff rather than an edit in an admin UI.
- iiko is the restaurant's POS. The client is written but not connected; see
  Limitations.

## Tech stack

| Layer | Technology | Why |
|---|---|---|
| Voice pipeline | LiveKit Agents 1.6 | turn detection, preemptive generation and a SIP bridge in one framework |
| Speech-to-text | Deepgram nova-3 | streaming Russian recognition; falls back to OpenAI without a key |
| LLM | OpenAI gpt-4.1-mini | function calling with low enough latency for a phone turn |
| Text-to-speech | ElevenLabs Flash v2.5 | fast first byte and a natural Russian voice; falls back to OpenAI |
| API | FastAPI, Pydantic | async I/O; request models mirror the tool schemas |
| Storage | PostgreSQL 16 + pg_trgm | orders, menu and transcripts; fuzzy search over names and aliases |
| Call state | Redis 7 | the draft order lives only for the call and expires on its own |
| Telephony | self-hosted livekit-sip | see the SIP decision below |
| Messaging | Wazzup API | WhatsApp Business gateway with webhooks |
| Edge | Caddy 2 | automatic TLS for the panel and the webhook |
| Deploy | Docker Compose, systemd, bash | one VPS, provisioned by one script |

## Key decisions & trade-offs

- **The menu never enters the prompt.** Pasting the menu into the system prompt
  is the obvious route; on real calls the model then offered dishes that don't
  exist. Here dish names and prices come only from tool calls, and `add_item`
  accepts only an ID returned by search, so an invented dish is rejected by the
  API. The cost is an extra round trip to the model on most turns.

- **Constraints ride on tool results, not only in the prompt.** In one call the
  model offered soy sauce, which the menu doesn't have, then had to refuse it
  when the guest said yes. The same rule in the system prompt was ignored; attached to the search result as a
  "this list is complete" note, it holds. The catch: the guard has to run in the
  shared tool layer. It first lived in the voice agent only, and WhatsApp
  searches reached the model without it.

- **Limits enforced by the server, not requested from the model.** The upsell
  cap is a counter in the Redis draft; prices are recomputed from PostgreSQL at
  confirmation. The prompt still describes both rules, but the API makes them
  hold when the model drifts. The cost is a few extra queries per order.

- **Self-hosted SIP instead of the managed one.** The host blocks UDP 5060, and
  calls through LiveKit Cloud connected with one-way audio: the cloud advertised
  one IP in SDP and sent RTP from another, and the carrier stopped sending the
  caller's voice. A bridge on port 5080, on the same public IP as the media,
  fixed both. One more service to run, and the trunk had to be created through
  the Twirp API because the CLI dropped the codec settings.

- **Mask the tool-call pause instead of only shortening it.** Latency work cut
  what could be cut: the VAD model is preloaded per worker, tool steps per turn
  are capped, and replies are capped at 120 tokens. What remains is a model
  round trip per tool call, so the agent plays keyboard typing during it. It
  changes no timing; a caller hears an operator entering the order rather than
  dead air.

## Working with the LLM

- **Quality checks.** `make smoke-llm` plays an eleven-turn order against the
  live model and asserts on behaviour: no phrases the client's script bans, one
  upsell attempt, the address validated, and no order created before the guest
  agreed. Call transcripts are stored and shown in the panel for manual review.
- **Guards.** Dish IDs only from search; a complete-list note on every search
  result; the upsell counter and price re-check in the backend; a hard
  `max_completion_tokens` cap so replies stay short enough for a phone line.
- **Prompting for speech.** Telling a model to "sound natural" changes nothing.
  The prompt instead carries six robotic-vs-human pairs, most of the robotic
  lines taken from the agent's own early calls,
  a fifteen-word ceiling per reply, and the operator's own banned phrases.
- **Cost and latency.** Speech synthesis bills by character, so the reply cap is
  also a cost control. Each tool call adds a model round trip; the agent may
  take at most three per turn.
- **Provider failures.** Speech-to-text and text-to-speech fall back to OpenAI
  when a key is missing. WhatsApp answers with an apology if a reply fails. See
  Limitations for what is not covered.

## Quick start

Tests need no services or API keys:

```bash
git clone https://github.com/nurzhan2/voice-agent.git
cd voice-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
make test
```

Backend and kitchen board (Docker):

```bash
cp .env.example .env     # local defaults work as is
make dev                 # postgres, redis, backend
```

The board opens at `http://localhost:8000/admin`; the token is `BACKEND_TOKEN`
from `.env`. API docs are at `/docs`. To fill the board with sample orders:
`make demo`.

Text conversation with the agent, same script and tools, no telephony
(needs `OPENAI_API_KEY`):

```bash
BACKEND_URL=http://localhost:8000 make chat
```

Phone calls need the LiveKit, SIP and speech keys and a server with a public IP;
`scripts/deploy.sh` provisions one, `scripts/setup_livekit.sh` registers the
trunk.

| Variable | Purpose | Example |
|---|---|---|
| `OPENAI_API_KEY` | the model that runs the conversation | `sk-...` |
| `DATABASE_URL` | PostgreSQL connection | `postgresql://tarelka:pass@postgres:5432/tarelka` |
| `REDIS_URL` | call drafts | `redis://redis:6379/0` |
| `BACKEND_TOKEN` | protects the API and the board | any long random string |
| `DEEPGRAM_API_KEY` | streaming STT; OpenAI used without it | |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | voice; OpenAI used without them | |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | voice sessions | `ws://127.0.0.1:7880` |
| `NOVOFON_SIP_*` | SIP trunk credentials | |
| `WAZZUP_API_KEY`, `WAZZUP_CHANNEL_ID` | WhatsApp | |
| `IIKO_API_LOGIN`, `IIKO_ORGANIZATION_ID` | POS export; orders stay on the board without it | |

## Testing

```bash
make test     # pytest
make lint     # ruff check + format check
```

The pytest suite runs the FastAPI app against in-memory fakes of PostgreSQL and
Redis that fail on any query the code isn't expected to send. It covers the
order guards (unknown IDs rejected, sizes required, prices re-read at confirm,
sold-out dishes, minimum order, missing address), the upsell cap, opening hours
including overnight shifts, the delivery-zone check, and routing for every tool
the model can call. CI runs lint and tests on every push.

`make smoke` and `make smoke-llm` are end-to-end scripts against a running stack
and the live model. They need API keys, so they stay out of CI.

## Project structure

```
agent/        voice agent, WhatsApp bot, tools, guards, sales prompt
backend/app/  FastAPI app, menu sync, Redis drafts, iiko client, kitchen board
tests/        pytest suite with PostgreSQL and Redis fakes
scripts/      server provisioning, SIP trunk setup, voice picker
caddy/        TLS reverse proxy config
livekit/      self-hosted LiveKit server config
menu.yaml     menu, pronunciation aliases and restaurant settings
```

## Results

- **Pilot.** In August 2026 the agent ran on a dedicated phone number and took
  orders end to end: a caller ordered by voice and the order appeared on the
  kitchen board with items, address, cutlery and payment method.
- **One-way audio, found by packet capture.** On the failing calls the carrier
  sent 25 RTP packets of 12 bytes each, headers with no audio, then nothing,
  while 1,096 packets with audio went the other way. Identical on G.722 and
  PCMU, which ruled out codec negotiation. Moving SIP onto the media's public IP
  fixed it.
- **Menu.** 170 dishes in 16 categories with about 470 pronunciation aliases.
  Callers say "пеперони", "пиперони" or just "острая пицца" for the same pizza;
  recognition depended on aliases more than on any model setting.
- **Tests.** 35 tests, about 1 second on a laptop.

## Limitations & roadmap

- **Pilot only.** The restaurant's main line was never switched over, and the
  pilot server is not running now.
- **Consent to confirm is enforced by the prompt alone.** The backend checks the
  address, hours, minimum order and prices, but nothing at the API level proves
  the guest said yes. Next: a consent flag with the guest's words on
  `confirm_order`, rejected server-side when missing.
- **Fallbacks are chosen at startup.** If ElevenLabs runs out of quota
  mid-month, calls go silent instead of switching voices; this happened during
  testing. Next: a runtime fallback adapter. There is no fallback LLM.
- **Delivery zone is a string match** on town names plus a street-word
  heuristic, with no geocoding.
- **iiko is untested against a live account.** The client uses the v1 token
  endpoint; the restaurant's key requires v2 credentials that never arrived.
- **WhatsApp delivery is blocked.** The bot replied and the gateway accepted the
  messages, but Meta blocked the restaurant's WhatsApp Business number.
- **Single restaurant.** Menu, settings and orders have no tenant scoping.
- **Deprecated LiveKit APIs.** Endpointing options and `metrics_collected` are
  deprecated in 1.6 and will go in 2.0; the version is pinned until they are
  migrated.

## My role

Designed and built end to end as the only developer on a client project:
architecture, backend, agent and prompt, telephony, deployment, tests. The
restaurant provided the sales script and the menu.

## Terms

Client work, published with the client's permission. No open-source license is
granted: the code is shown for review, not for reuse.

Nurzhan Kalmurat · [LinkedIn](https://linkedin.com/in/kalmuratnurzhan) · [GitHub](https://github.com/nurzhan2)
