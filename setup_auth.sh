#!/usr/bin/env bash
set -Eeuo pipefail

APP="/opt/azs-geoportal-dashboard"
DOMAIN="dashboard.opentaya.space"
EXPECTED_IP="31.77.13.113"
cd "$APP"

echo
echo "=== AZS: HTTPS + пароль + Telegram Approve/Deny ==="
echo

DNS_IPS="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
if ! printf '%s\n' "$DNS_IPS" | grep -qw "$EXPECTED_IP"; then
  echo "DNS ещё не указывает $DOMAIN на $EXPECTED_IP."
  echo "Сейчас вижу: ${DNS_IPS:-ничего}"
  echo "Подожди распространения DNS и запусти этот скрипт ещё раз."
  exit 2
fi

echo "DNS OK: $DOMAIN -> $EXPECTED_IP"
echo

read -rsp "Новый пароль сайта (минимум 12 символов): " PASSWORD
echo
read -rsp "Повтори пароль: " PASSWORD2
echo

if [ "$PASSWORD" != "$PASSWORD2" ]; then
  echo "Пароли не совпадают."
  exit 1
fi
if [ "${#PASSWORD}" -lt 12 ]; then
  echo "Пароль должен быть не короче 12 символов."
  exit 1
fi

echo
echo "ВАЖНО: используй НОВЫЙ токен @my_fed_helper_robot после перевыпуска в BotFather."
read -rsp "Новый токен Telegram-бота: " BOT_TOKEN
echo
read -rp "Telegram user ID [691131427]: " TG_USER
TG_USER="${TG_USER:-691131427}"

if ! [[ "$TG_USER" =~ ^[0-9]+$ ]]; then
  echo "Telegram user ID должен состоять из цифр."
  exit 1
fi

GETME="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" || true)"
if ! printf '%s' "$GETME" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo "Неверный токен или Telegram Bot API недоступен."
  exit 1
fi

BOT_USERNAME="$(printf '%s' "$GETME" | jq -r '.result.username // ""')"
echo "Бот: @${BOT_USERNAME}"

TEST="$(curl -fsS --max-time 15 \
  --data-urlencode "chat_id=${TG_USER}" \
  --data-urlencode "text=🔐 AZS: проверка связи. Следующие запросы на вход будут приходить с кнопками «Принять / Отклонить»." \
  "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" || true)"

if ! printf '%s' "$TEST" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo
  echo "Бот не смог написать пользователю ${TG_USER}."
  echo "Открой @my_fed_helper_robot, нажми Start (/start) и запусти скрипт ещё раз."
  exit 1
fi

PASSWORD_HASH="$(
  printf '%s' "$PASSWORD" | python3 -c '
import hashlib, os, sys
password=sys.stdin.read().encode("utf-8")
salt=os.urandom(16)
n,r,p=16384,8,1
digest=hashlib.scrypt(password,salt=salt,n=n,r=r,p=p,dklen=32)
print(f"scrypt:{n}:{r}:{p}:{salt.hex()}:{digest.hex()}")
'
)"
SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(48))')"
WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"

echo
echo "=== Устанавливаю Caddy ==="
if ! command -v caddy >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y caddy
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

cat >/etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    encode zstd gzip

    reverse_proxy 127.0.0.1:8080

    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "same-origin"
        -Server
    }
}
EOF

caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

umask 077
cat > .env.tmp <<EOF
AZS_AUTH_PASSWORD_HASH=${PASSWORD_HASH}
AZS_TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
AZS_TELEGRAM_ALLOWED_USER_ID=${TG_USER}
AZS_TELEGRAM_WEBHOOK_SECRET=${WEBHOOK_SECRET}
AZS_SESSION_SECRET=${SESSION_SECRET}
AZS_SESSION_HOURS=12
AZS_PUBLIC_BASE_URL=https://${DOMAIN}
AZS_COOKIE_SECURE=1
AZS_TRUST_PROXY_HEADERS=1
EOF
mv .env.tmp .env
chmod 600 .env

# Keep Docker port reachable only from the VPS itself.
cat > docker-compose.override.yml <<'EOF'
services:
  azs-dashboard:
    ports: !override
      - "127.0.0.1:8080:8080"
EOF
printf '\ndocker-compose.override.yml\n' >> .git/info/exclude 2>/dev/null || true

unset PASSWORD PASSWORD2 PASSWORD_HASH SESSION_SECRET

echo
echo "=== Перезапускаю приложение ==="
docker compose up -d --build

echo "Жду локальный backend..."
for i in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8080/api/health >/tmp/azs_https_health.json 2>/dev/null; then
    break
  fi
  sleep 1
done

if ! curl -fsS http://127.0.0.1:8080/api/health >/tmp/azs_https_health.json 2>/dev/null; then
  echo "Backend не поднялся."
  docker compose logs --tail=80
  exit 1
fi

echo
echo "Жду HTTPS-сертификат Caddy..."
HTTPS_OK=0
for i in $(seq 1 60); do
  if curl -fsS --max-time 10 "https://${DOMAIN}/api/health" >/tmp/azs_https_public.json 2>/dev/null; then
    HTTPS_OK=1
    break
  fi
  sleep 2
done

if [ "$HTTPS_OK" != "1" ]; then
  echo
  echo "HTTPS пока не поднялся. Последние логи Caddy:"
  journalctl -u caddy -n 60 --no-pager
  echo
  echo "Когда DNS/порты 80 и 443 станут доступны, повтори этот скрипт."
  exit 2
fi

echo
echo "=== Регистрирую Telegram webhook ==="
WEBHOOK_RESPONSE="$(curl -fsS --max-time 20 \
  --data-urlencode "url=https://${DOMAIN}/api/auth/telegram/webhook" \
  --data-urlencode "secret_token=${WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["callback_query"]' \
  --data-urlencode "drop_pending_updates=true" \
  "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook")"

if ! printf '%s' "$WEBHOOK_RESPONSE" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo "Telegram не принял webhook:"
  printf '%s\n' "$WEBHOOK_RESPONSE" | jq .
  exit 1
fi

WEBHOOK_INFO="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo")"

echo
echo "=== HEALTH ==="
cat /tmp/azs_https_public.json
echo
echo
echo "=== WEBHOOK ==="
printf '%s\n' "$WEBHOOK_INFO" | jq '{ok, url:.result.url, pending_update_count:.result.pending_update_count, last_error_message:.result.last_error_message}'
echo
echo "Готово: https://${DOMAIN}"
echo "Порт 8080 теперь доступен только локально на VPS."
echo "Вход: пароль -> кнопка Принять/Отклонить в Telegram."
