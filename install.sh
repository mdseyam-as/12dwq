#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/azs-geoportal-dashboard}"
REPO_URL="${REPO_URL:-}"
BRANCH="${BRANCH:-main}"

log() { printf '\n\033[1;34m[AZS]\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31m[AZS ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || fail "Нужны root-права или sudo."
  SUDO="sudo"
fi

install_docker() {
  if command -v docker >/dev/null 2>&1; then
    return
  fi

  log "Docker не найден — устанавливаю."

  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y docker.io git curl ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y docker git curl ca-certificates
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y docker git curl ca-certificates
  else
    fail "Не удалось определить пакетный менеджер. Установи Docker вручную."
  fi

  $SUDO systemctl enable --now docker || true
}

install_compose() {
  if docker compose version >/dev/null 2>&1; then
    return
  fi

  log "Docker Compose plugin не найден — пробую установить."

  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get install -y docker-compose-plugin 2>/dev/null || \
    $SUDO apt-get install -y docker-compose
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y docker-compose-plugin 2>/dev/null || \
    $SUDO dnf install -y docker-compose
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y docker-compose-plugin 2>/dev/null || \
    $SUDO yum install -y docker-compose
  fi
}

compose_up() {
  if docker compose version >/dev/null 2>&1; then
    $SUDO docker compose up -d --build
  elif command -v docker-compose >/dev/null 2>&1; then
    $SUDO docker-compose up -d --build
  else
    fail "Docker Compose не найден."
  fi
}

install_docker
install_compose

if [ -n "$REPO_URL" ]; then
  log "Разворачиваю проект в $APP_DIR"
  if [ -d "$APP_DIR/.git" ]; then
    $SUDO git -C "$APP_DIR" fetch origin "$BRANCH"
    $SUDO git -C "$APP_DIR" reset --hard "origin/$BRANCH"
  else
    $SUDO mkdir -p "$(dirname "$APP_DIR")"
    $SUDO git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi
  cd "$APP_DIR"
else
  log "REPO_URL не указан — запускаю проект из текущей папки."
fi

$SUDO mkdir -p data
compose_up

log "Проверяю контейнер."
sleep 2
if $SUDO docker ps --format '{{.Names}}' | grep -qx 'azs-dashboard'; then
  echo
  echo "Готово."
  echo "Дашборд: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP'):8080"
  echo "Health:   http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP'):8080/api/health"
  echo
  echo "Логи:"
  echo "  sudo docker logs -f azs-dashboard"
else
  fail "Контейнер azs-dashboard не запустился. Выполни: sudo docker compose logs"
fi
