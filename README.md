# Боевой дашборд АЗС на данных Geoportal40

Это самостоятельное веб-приложение, не зависящее от Alpha BI.

## Что уже работает

- Актуальные станции берутся напрямую из Geoportal40 GeoJSON.
- 7 видов топлива: АИ-92, АИ-95, АИ-95+, ДТ, ДТ+, АИ-98, АИ-100.
- Фильтры по топливу, владельцу, поиску и архивной дате.
- KPI: с топливом, всего АЗС, обеспеченность, время получения данных.
- Круговая диаграмма владельцев.
- Проваливание владелец → список АЗС.
- Режимы «Все АЗС» / «Только с топливом».
- Официальная карта Geoportal40 во встроенном iframe.
- Кнопка «На карте» открывает конкретную АЗС через hash `#longitude_latitude_17`.
- История за 30 дней для каждой АЗС.
- SQLite-архив заполняется автоматически.
- Если Geoportal временно недоступен, используется последний успешный срез с явной пометкой.
- Автоопрос Geoportal по умолчанию каждые 5 минут.

## Важно про историю

Публичный GeoJSON endpoint Geoportal40 отдаёт текущий срез, а не прошлые 30 дней.

Поэтому приложение начинает честно накапливать историю с первого запуска:
- минимум один снимок каждой АЗС за день;
- дополнительный снимок при каждом изменении набора топлива внутри дня.

Дни до первого запуска отображаются серыми как «архив ещё не накоплен». Их можно позже импортировать из уже существующей истории Alpha BI/БД отдельным мигратором.

## Endpoint Geoportal40

По умолчанию backend использует:

`https://azs.geoportal40.ru/api/v1/tables/geoportal40/maps/azs/tables/1/geojson?srid=4326&fields=id&fields=ai92&fields=ai95&fields=dt&fields=ai98&fields=ai100&fields=ai95_1&fields=dt_1&fields=name2&fields=name3&fields=address`

Клиентский браузер к API напрямую не обращается. Запрос выполняет backend.

## Быстрый запуск Windows

1. Установить Python 3.11+.
2. Распаковать проект.
3. Запустить `start_windows.bat`.
4. Открыть `http://127.0.0.1:8080`.

Файл сам создаст `.venv`, установит зависимости и запустит сервер.

## Linux / Astra Linux

```bash
chmod +x start_linux.sh
./start_linux.sh
```

Открыть:

`http://IP_СЕРВЕРА:8080`

## Docker

```bash
docker compose up -d --build
```

Открыть:

`http://IP_СЕРВЕРА:8080`

База истории сохраняется в `./data/azs.sqlite3`.

## Настройки

Через переменные окружения:

- `AZS_POLL_SECONDS=300` — частота автоматического опроса.
- `AZS_CACHE_TTL_SECONDS=60` — минимальный интервал между обычными запросами.
- `AZS_REQUEST_TIMEOUT=20` — таймаут Geoportal.
- `AZS_DB_PATH=data/azs.sqlite3` — файл SQLite.
- `AZS_GEOPORTAL_BASE_URL` — базовый адрес портала.
- `AZS_GEOPORTAL_API_URL` — полный URL GeoJSON, если endpoint изменится.

## API нашего приложения

- `GET /api/health`
- `GET /api/meta`
- `GET /api/stations`
- `GET /api/stations?date=YYYY-MM-DD`
- `GET /api/history/{station_id}?days=30`
- `POST /api/refresh`

## Что сделать для промышленного размещения

Для внутреннего сервера можно оставить Uvicorn за Nginx/Caddy.

Рекомендуемо:
- HTTPS;
- reverse proxy;
- systemd или Docker restart policy;
- ежедневный backup файла `data/azs.sqlite3`;
- ограничить доступ по внутренней сети, если дашборд не должен быть публичным.

## Структура

```text
azs_geoportal_dashboard/
├── app.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── start_windows.bat
├── start_linux.sh
├── static/
│   ├── index.html
│   ├── styles.css
│   └── app.js
└── data/
    └── .gitkeep
```


## Установка одной командой из GitHub

После публикации репозитория:

```bash
git clone <REPO_URL> /opt/azs-geoportal-dashboard \
  && cd /opt/azs-geoportal-dashboard \
  && sudo bash install.sh
```

Или через `install.sh` с URL репозитория:

```bash
curl -fsSL <RAW_INSTALL_SH_URL> | sudo REPO_URL=<REPO_URL> bash
```

Для приватного репозитория проще сначала выполнить `git clone` через авторизацию GitHub, затем `sudo bash install.sh`.
