#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
ok()   { printf "  ${G}✓${N} %s\n" "$1"; }
warn() { printf "  ${Y}!${N} %s\n" "$1"; }
step() { printf "\n%s\n" "$1"; }
die()  { printf "\n  ${R}✗ %s${N}\n\n" "$1"; exit 1; }

set -a; source .env; set +a

step "1. Проверяю настройки"
[ -n "${LIVEKIT_URL:-}" ]        || die "LIVEKIT_URL не задан"
[ -n "${LIVEKIT_API_KEY:-}" ]    || die "LIVEKIT_API_KEY не задан"
[ -n "${LIVEKIT_API_SECRET:-}" ] || die "LIVEKIT_API_SECRET не задан"
[ -n "${NOVOFON_PHONE:-}" ]      || die "NOVOFON_PHONE не задан"
ok "$LIVEKIT_URL"
ok "номер: $NOVOFON_PHONE"

step "2. Ставлю livekit-cli"
if ! command -v lk >/dev/null 2>&1; then
  curl -sSL https://get.livekit.io/cli | bash
fi
export PATH="$PATH:/usr/local/bin:$HOME/bin"
ok "$(lk --version 2>&1 | head -1)"

export LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET

step "3. Входящий транк"
cat > /tmp/trunk.json <<JSON
{
  "trunk": {
    "name": "novofon-inbound",
    "numbers": ["${NOVOFON_PHONE}"]
  }
}
JSON
lk sip inbound create /tmp/trunk.json || warn "возможно транк уже есть"

step "4. Правило маршрутизации"
cat > /tmp/dispatch.json <<'JSON'
{
  "name": "inbound-calls",
  "rule": { "dispatchRuleIndividual": { "roomPrefix": "call-" } }
}
JSON
lk sip dispatch create /tmp/dispatch.json || warn "возможно правило уже есть"

rm -f /tmp/trunk.json /tmp/dispatch.json

SIP_HOST="${LIVEKIT_URL#wss://}"; SIP_HOST="${SIP_HOST%%/*}"

cat <<MSG

──────────────────────────────────────────────────────────
  Готово на нашей стороне.

  В кабинете Новофона для номера ${NOVOFON_PHONE}:
    Переадресация -> внешний SIP-адрес:

        sip:${SIP_HOST}

    Если поля раздельные:
        хост: ${SIP_HOST}
        порт: 5060

  Точный адрес также виден в панели LiveKit Cloud:
  Telephony -> Configuration

  Проверка: позвонить на ${NOVOFON_PHONE}
  Логи:     docker compose logs -f agent
──────────────────────────────────────────────────────────

MSG
