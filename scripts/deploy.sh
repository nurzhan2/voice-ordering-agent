#!/usr/bin/env bash
#
# Установка на чистый сервер Ubuntu 22/24.
#
#   scp -r restaurant-ai root@СЕРВЕР:/opt/
#   ssh root@СЕРВЕР
#   cd /opt/restaurant-ai && bash scripts/deploy.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
ok()   { printf "  ${G}✓${N} %s\n" "$1"; }
warn() { printf "  ${Y}!${N} %s\n" "$1"; }
step() { printf "\n%s\n" "$1"; }
die()  { printf "\n  ${R}✗ %s${N}\n\n" "$1"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запускать от root: sudo bash scripts/deploy.sh"

# ---------------------------------------------------------------------------
step "1. Docker"

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "docker compose не установился"
ok "$(docker --version)"

# ---------------------------------------------------------------------------
step "2. Настройки"

[ -f .env ] || die ".env не найден. Скопируй .env.example в .env и заполни."

missing=()
for var in OPENAI_API_KEY DOMAIN; do
  value=$(grep -E "^${var}=" .env | cut -d= -f2- || true)
  [ -n "$value" ] || missing+=("$var")
done
[ ${#missing[@]} -eq 0 ] || die "В .env не заполнено: ${missing[*]}"

# Пароли из примера в проде оставлять нельзя: панель заказов смотрит наружу.
for weak in "change_me_please" "internal_service_token_change_me" \
            "please_change_this_secret_min_32_chars_long"; do
  if grep -q "$weak" .env; then
    die "В .env остались значения из примера ($weak).
Замени POSTGRES_PASSWORD, BACKEND_TOKEN и LIVEKIT_API_SECRET на свои.
Сгенерировать: openssl rand -hex 24"
  fi
done
ok "Настройки заполнены"

DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2-)

# ---------------------------------------------------------------------------
step "3. Порты"

for p in 80 443 5060; do
  if ss -lntu 2>/dev/null | grep -q ":$p "; then
    warn "порт $p занят — проверь, что там не чужой сервис"
  fi
done

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp   >/dev/null
  ufw allow 443/tcp  >/dev/null
  ufw allow 5060/udp >/dev/null                 # SIP
  ufw allow 10000:20000/udp >/dev/null          # голосовой поток
  ufw allow 50000:50200/udp >/dev/null          # WebRTC
  ok "Файрвол настроен"
else
  warn "ufw не активен — порты открывать не нужно"
fi

# ---------------------------------------------------------------------------
step "4. Сборка и запуск"

docker compose pull  --quiet 2>/dev/null || true
docker compose up -d --build

printf "  ${D}жду бэкенд${N}"
for _ in $(seq 1 45); do
  sleep 2; printf "."
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then READY=1; break; fi
done
printf "\n"
[ -n "${READY:-}" ] || die "Бэкенд не поднялся. Смотри: docker compose logs backend"
ok "Бэкенд отвечает, меню загружено"

# ---------------------------------------------------------------------------
step "5. Автозапуск"

cat > /etc/systemd/system/restaurant-ai.service <<UNIT
[Unit]
Description=Restaurant AI
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$(pwd)
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable restaurant-ai >/dev/null 2>&1
ok "Поднимется само после перезагрузки сервера"

# ---------------------------------------------------------------------------
step "6. Ежедневная резервная копия базы"

mkdir -p /opt/backups
cat > /etc/cron.daily/restaurant-ai-backup <<CRON
#!/bin/bash
cd $(pwd)
docker compose exec -T postgres pg_dump -U \$(grep POSTGRES_USER .env | cut -d= -f2) \
  \$(grep POSTGRES_DB .env | cut -d= -f2) | gzip > /opt/backups/db-\$(date +%F).sql.gz
find /opt/backups -name 'db-*.sql.gz' -mtime +14 -delete
CRON
chmod +x /etc/cron.daily/restaurant-ai-backup
ok "Копия базы раз в сутки, хранится две недели"

# ---------------------------------------------------------------------------
cat <<FINAL

──────────────────────────────────────────────────────────
  Установлено.

  Панель заказов:   https://${DOMAIN}/admin
                    вход по BACKEND_TOKEN из .env

  Дальше:
    1. Телефония        bash scripts/setup_sip.sh
    2. WhatsApp         docker compose exec wa-bot python wa_bot.py setup
                        (сначала убедись, что https://${DOMAIN} открывается)

  Полезное:
    docker compose logs -f agent      что происходит на звонках
    docker compose logs -f wa-bot     переписка
    docker compose restart backend    применить правки menu.yaml
──────────────────────────────────────────────────────────

FINAL
