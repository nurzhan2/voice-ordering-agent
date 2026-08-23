# Voice Ordering Agent

**A production AI phone agent that takes food delivery orders in Russian.**
Built for a real restaurant in Makhachkala, Russia. It answers the phone,
holds a natural conversation, upsells, and pushes confirmed orders to the
kitchen board.

[Русская версия](README.ru.md)

---

## What it does

A guest calls the restaurant. The agent picks up, introduces itself, works
out what the guest wants, suggests dishes from the actual menu, upsells once,
takes the address and payment method, reads the order back, and only creates
it after explicit confirmation. The order appears on a kitchen display within
three seconds.

The same agent handles WhatsApp. Both channels share one tool layer and one
sales script — change the script once, both channels follow it.

```
 Phone ──SIP──▶ LiveKit SIP ──▶ agent.py ─┐
                                           ├─▶ FastAPI ─▶ PostgreSQL / Redis
 WhatsApp ──▶ Wazzup ──webhook──▶ wa_bot.py┘              │
                                                          └─▶ iiko (POS)
```

**Stack:** Python · LiveKit Agents · FastAPI · PostgreSQL · Redis · Docker ·
Caddy · Deepgram (STT) · OpenAI (LLM) · ElevenLabs (TTS)

---

## The interesting part: the model cannot invent a dish

The naive approach — put the menu in the system prompt — fails in production.
The model confidently offers dishes that don't exist. In one real call it
suggested soy sauce, the guest picked it, and the order couldn't be created.

Here the menu never enters the prompt at all. The agent is told, plainly, that
it does not know the menu. It can only learn dish names and prices by calling
tools, and it can only add items **by ID** returned from a database lookup.
An invented dish has no ID, so `add_item` rejects it.

Three more guards, each learned from a real failure:

**Prices are re-read from PostgreSQL at confirmation time.** The draft order
lives in Redis during the call, but Redis is treated as untrusted — a price
that drifted mid-call would otherwise reach the customer.

**`confirm_order` requires an explicit consent flag plus the guest's verbatim
words.** In an early call the agent confirmed an order right after the guest
answered a question about payment. The API now rejects confirmation unless the
model asserts, in a separate turn, that the guest agreed — and the backend
double-checks server-side.

**Tool results carry their own constraints.** Search responses include a field
telling the model the list is complete and nothing may be added to it.
Instructions attached to tool output are followed far more reliably than the
same rule in a system prompt.

---

## Engineering highlights

### Diagnosing one-way audio down to the packet

Calls connected, the guest heard the bot, but the bot heard nothing. The
provider insisted their recordings were fine.

Packet capture told a different story:

```
LiveKit → provider :  1096 RTP packets, full payload
provider → LiveKit :    25 RTP packets, 12 bytes each
```

Twelve bytes is exactly an RTP header with **zero audio payload**. The provider
sent half a second of empty packets and went silent. Testing across two codecs
(G.722 and PCMU) produced identical behaviour, which ruled out codec
negotiation and pinned the fault to the provider's media path.

### Working around a blocked SIP port

The hosting provider blocks UDP 5060 and only unblocks it for registered legal
entities with telecom regulator paperwork — weeks of process.

Rather than wait, the SIP bridge was self-hosted on port 5080, verified open by
sending a UDP probe from outside. This also solved a subtler problem: the cloud
SIP service advertised one IP in SDP while sending RTP from another (internal
NAT), which the provider treated as an asymmetric stream. Terminating SIP on
the same public IP as the media made the addresses match.

### Latency, measured rather than guessed

The first working version had a four-second gap before every reply. Per-stage
instrumentation showed where the time actually went, and four changes brought
it down:

| Change | Why it mattered |
|---|---|
| Prewarm the VAD model | Each call ran in a fresh process and reloaded Silero from disk |
| Cap tool steps per turn | Tools run sequentially; each adds a full round trip to the LLM |
| `max_completion_tokens` | The prompt asked for brevity; the API now enforces it |
| Keyboard typing during tool calls | A filled pause reads as far shorter than silence — and an order-taker typing is exactly what a caller expects to hear |

### Making the agent sound human

LLMs default to polished written prose. Told to "sound natural", they don't
change. What works is showing concrete contrasts:

> **Robotic:** "I recommend the lamb kebab — juicy, on the bone, very popular
> right now. Shall we add it?"
>
> **Human:** "We've got lamb kebab, eight ninety. Take it?"

The prompt carries six such pairs, a hard fifteen-word ceiling per turn, and a
list of banned phrases pulled from the client's own operator script.

---

## Testing

Voice agents are hard to test — the failure modes are conversational, not
exceptions.

```bash
make smoke        # 21 API checks: search, pricing, delivery zone, confirmation guards
make smoke-llm    # runs a scripted dialogue against the live model
make wa-test      # replays Wazzup webhooks without touching WhatsApp
```

`smoke-llm` is the useful one. It plays out an eleven-turn conversation and
asserts on behaviour: no banned phrases, upsell attempted, address validated,
and — critically — that the order was **not** created before the guest agreed.
That last check exists because a real call once failed it.

---

## Running it

```bash
cp .env.example .env      # fill in API keys
docker compose up -d
```

For telephony, add the SIP overlay and register the trunk:

```bash
docker compose -f docker-compose.yml -f livekit-sip.yml up -d
bash scripts/setup_livekit.sh
```

`scripts/deploy.sh` provisions a bare Ubuntu server end to end: Docker, TLS,
firewall, systemd autostart, and nightly database backups.

---

## Layout

```
agent/
  agent.py       voice agent — LiveKit session, tools, latency tuning
  wa_bot.py      WhatsApp — Wazzup webhooks, per-chat locking, dedup
  prompts.py     sales script, built from restaurant settings
  tools.py       tool definitions shared by both channels
  smoke.py       automated checks
backend/app/
  main.py        order API and admin panel
  db.py          menu sync from YAML, full-text search
  drafts.py      in-call order state in Redis
  iiko.py        POS integration
menu.yaml        170 dishes with pronunciation aliases and settings
```

**Aliases matter more than they look.** Each dish carries several spoken
variants — Russian callers say "пеперони", "пиперони", or just "острая пицца"
for the same pizza. Roughly 470 aliases across 170 dishes; recognition quality
depends on them more than on any model setting.

---

## Status

Running in production, taking live calls. Order intake, the sales script,
delivery-zone checks, the kitchen board and both channels all work. The POS
integration is written and waiting on credentials from the restaurant.

Built solo over three weeks, including all infrastructure and telephony.
