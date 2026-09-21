#!/usr/bin/env bash
set -Eeuo pipefail

APP="/opt/azs-geoportal-dashboard"
cd "$APP"

echo
echo "=== Настройка защищённого входа AZS ==="
echo "Секреты вводятся только здесь и не отправляются в GitHub/Google Drive."
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

read -rsp "НОВЫЙ токен Telegram-бота: " BOT_TOKEN
echo
read -rp "Telegram user ID [691131427]: " TG_USER
TG_USER="${TG_USER:-691131427}"

if ! [[ "$TG_USER" =~ ^[0-9]+$ ]]; then
  echo "Telegram user ID должен состоять из цифр."
  exit 1
fi

echo
echo "Проверяю Telegram-бота..."

GETME="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" || true)"
if ! printf '%s' "$GETME" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo "Не удалось авторизоваться в Telegram Bot API. Проверь новый токен."
  exit 1
fi

TEST="$(curl -fsS --max-time 15 \
  --data-urlencode "chat_id=${TG_USER}" \
  --data-urlencode "text=✅ AZS: Telegram-подтверждение входа настроено. Этот чат будет получать одноразовые коды." \
  "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" || true)"

if ! printf '%s' "$TEST" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo
  echo "Бот работает, но не смог отправить сообщение пользователю ${TG_USER}."
  echo "Открой @my_fed_helper_robot в Telegram, нажми Start (/start) и запусти этот скрипт ещё раз."
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

umask 077
cat > .env.tmp <<EOF
AZS_AUTH_PASSWORD_HASH=${PASSWORD_HASH}
AZS_TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
AZS_TELEGRAM_ALLOWED_USER_ID=${TG_USER}
AZS_SESSION_SECRET=${SESSION_SECRET}
AZS_SESSION_HOURS=12
AZS_COOKIE_SECURE=0
EOF

mv .env.tmp .env
chmod 600 .env

unset PASSWORD PASSWORD2 BOT_TOKEN PASSWORD_HASH SESSION_SECRET

echo
echo "Перезапускаю дашборд..."
docker compose up -d --build

echo "Жду backend..."
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/api/health >/tmp/azs-auth-health.json 2>/dev/null; then
    break
  fi
  sleep 1
done

echo
echo "=== HEALTH ==="
cat /tmp/azs-auth-health.json
echo
echo
echo "Готово. Открой http://31.77.13.113:8080"
echo "Пароль в .env НЕ хранится: там только scrypt-хеш."
echo
echo "ВАЖНО: сайт пока работает по HTTP. После настройки домена/HTTPS выставим AZS_COOKIE_SECURE=1."
