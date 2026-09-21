from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("AZS_DB_PATH", str(DATA_DIR / "azs.sqlite3")))
POLL_SECONDS = max(60, int(os.getenv("AZS_POLL_SECONDS", "300")))
CACHE_TTL_SECONDS = max(15, int(os.getenv("AZS_CACHE_TTL_SECONDS", "60")))
REQUEST_TIMEOUT = max(5, int(os.getenv("AZS_REQUEST_TIMEOUT", "20")))

AUTH_PASSWORD_HASH = os.getenv("AZS_AUTH_PASSWORD_HASH", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("AZS_TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_USER_ID_RAW = os.getenv("AZS_TELEGRAM_ALLOWED_USER_ID", "").strip()
SESSION_SECRET = os.getenv("AZS_SESSION_SECRET", "").strip()
SESSION_HOURS = max(1, min(168, int(os.getenv("AZS_SESSION_HOURS", "12"))))
COOKIE_SECURE = os.getenv("AZS_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}
TRUST_PROXY_HEADERS = os.getenv("AZS_TRUST_PROXY_HEADERS", "0").strip().lower() in {"1", "true", "yes", "on"}
PUBLIC_BASE_URL = os.getenv("AZS_PUBLIC_BASE_URL", "").strip().rstrip("/")
TELEGRAM_WEBHOOK_SECRET = os.getenv("AZS_TELEGRAM_WEBHOOK_SECRET", "").strip()
AUTH_COOKIE_NAME = "azs_session"

try:
    TELEGRAM_ALLOWED_USER_ID = int(TELEGRAM_ALLOWED_USER_ID_RAW) if TELEGRAM_ALLOWED_USER_ID_RAW else 0
except ValueError:
    TELEGRAM_ALLOWED_USER_ID = 0

AUTH_ENABLED = bool(
    AUTH_PASSWORD_HASH
    and TELEGRAM_BOT_TOKEN
    and TELEGRAM_ALLOWED_USER_ID
    and SESSION_SECRET
)
TELEGRAM_APPROVAL_READY = bool(
    AUTH_ENABLED
    and TELEGRAM_WEBHOOK_SECRET
    and PUBLIC_BASE_URL.startswith("https://")
)

GEOPORTAL_BASE_URL = os.getenv("AZS_GEOPORTAL_BASE_URL", "https://azs.geoportal40.ru/").rstrip("/") + "/"
GEOPORTAL_API_URL = os.getenv(
    "AZS_GEOPORTAL_API_URL",
    GEOPORTAL_BASE_URL
    + "api/v1/tables/geoportal40/maps/azs/tables/1/geojson"
    + "?srid=4326"
    + "&fields=id"
    + "&fields=ai92"
    + "&fields=ai95"
    + "&fields=dt"
    + "&fields=ai98"
    + "&fields=ai100"
    + "&fields=ai95_1"
    + "&fields=dt_1"
    + "&fields=name2"
    + "&fields=name3"
    + "&fields=address",
)

MSK = timezone(timedelta(hours=3))
_REFRESH_LOCK = threading.Lock()

FUEL_KEYS = ("ai92", "ai95", "ai95_1", "dt", "dt_1", "ai98", "ai100")


def now_msk() -> datetime:
    return datetime.now(MSK)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fetch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at TEXT NOT NULL,
                ok INTEGER NOT NULL,
                station_count INTEGER,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS station (
                station_id INTEGER PRIMARY KEY,
                owner_raw TEXT NOT NULL,
                owner_group TEXT NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                longitude REAL NOT NULL,
                latitude REAL NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_fetch_id INTEGER,
                FOREIGN KEY(last_fetch_id) REFERENCES fetch_log(id)
            );

            CREATE TABLE IF NOT EXISTS snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                ai92 INTEGER NOT NULL,
                ai95 INTEGER NOT NULL,
                ai95_1 INTEGER NOT NULL,
                dt INTEGER NOT NULL,
                dt_1 INTEGER NOT NULL,
                ai98 INTEGER NOT NULL,
                ai100 INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                FOREIGN KEY(station_id) REFERENCES station(station_id)
            );

            CREATE INDEX IF NOT EXISTS idx_snapshot_station_date
                ON snapshot(station_id, snapshot_date, fetched_at);

            CREATE INDEX IF NOT EXISTS idx_snapshot_date
                ON snapshot(snapshot_date, fetched_at);

            CREATE INDEX IF NOT EXISTS idx_station_last_fetch
                ON station(last_fetch_id);

            -- v2 tables use a stable synthetic UID so Geoportal source IDs may repeat
            -- without collapsing different stations into one SQLite row.
            CREATE TABLE IF NOT EXISTS station_v2 (
                station_uid TEXT PRIMARY KEY,
                source_id TEXT,
                owner_raw TEXT NOT NULL,
                owner_group TEXT NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                longitude REAL NOT NULL,
                latitude REAL NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_fetch_id INTEGER,
                FOREIGN KEY(last_fetch_id) REFERENCES fetch_log(id)
            );

            CREATE TABLE IF NOT EXISTS snapshot_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_uid TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                ai92 INTEGER NOT NULL,
                ai95 INTEGER NOT NULL,
                ai95_1 INTEGER NOT NULL,
                dt INTEGER NOT NULL,
                dt_1 INTEGER NOT NULL,
                ai98 INTEGER NOT NULL,
                ai100 INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                FOREIGN KEY(station_uid) REFERENCES station_v2(station_uid)
            );

            CREATE INDEX IF NOT EXISTS idx_snapshot_v2_station_date
                ON snapshot_v2(station_uid, snapshot_date, fetched_at);

            CREATE INDEX IF NOT EXISTS idx_snapshot_v2_date
                ON snapshot_v2(snapshot_date, fetched_at);

            CREATE INDEX IF NOT EXISTS idx_station_v2_last_fetch
                ON station_v2(last_fetch_id);
            """
        )


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "да", "on"}


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def normalize_owner(owner: str) -> str:
    owner = clean_text(owner)
    if "калуганефтепродукт" in owner.lower():
        return "Калуганефтепродукт"
    return owner or "Не указан"


def make_station_uid(source_id: Any, longitude: float, latitude: float, address: str, name: str) -> str:
    # Geoportal's source ID is useful metadata but must not be assumed globally unique.
    # Coordinates + address/name keep the identifier stable while preventing collisions.
    raw = "|".join(
        [
            clean_text(source_id),
            f"{longitude:.6f}",
            f"{latitude:.6f}",
            clean_text(address).lower(),
            clean_text(name).lower(),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def fetch_geoportal() -> dict[str, Any]:
    request = urllib.request.Request(
        GEOPORTAL_API_URL,
        method="GET",
        headers={
            "Accept": "application/geo+json, application/json",
            "User-Agent": "AZS-Dashboard/1.0 (+Geoportal40 monitoring)",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read()
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Geoportal HTTP {response.status}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError("Geoportal returned invalid JSON") from exc
    return payload


def parse_geojson(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("type") != "FeatureCollection":
        raise RuntimeError("Unexpected Geoportal response: not a FeatureCollection")

    features = payload.get("features")
    if not isinstance(features, list):
        raise RuntimeError("Unexpected Geoportal response: features is missing")

    rows: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue

        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []

        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError, IndexError):
            continue

        source_id = clean_text(props.get("id", feature.get("id")))
        owner_raw = clean_text(props.get("name2"))
        name = clean_text(props.get("name3")) or (f"АЗС {source_id}" if source_id else "АЗС")
        address = clean_text(props.get("address"))
        station_uid = make_station_uid(source_id, lon, lat, address, name)

        fuels = {key: as_bool(props.get(key)) for key in FUEL_KEYS}

        rows.append(
            {
                "station_uid": station_uid,
                "source_id": source_id,
                "owner_raw": owner_raw,
                "owner_group": normalize_owner(owner_raw),
                "name": name,
                "address": address,
                "longitude": lon,
                "latitude": lat,
                **fuels,
            }
        )

    if not rows:
        raise RuntimeError("Geoportal returned no valid stations")

    rows.sort(key=lambda x: (x["owner_group"].lower(), x["name"].lower(), x["station_uid"]))
    return rows


def state_hash(row: dict[str, Any]) -> str:
    state = "|".join("1" if row[key] else "0" for key in FUEL_KEYS)
    return hashlib.sha1(state.encode("ascii")).hexdigest()


def last_success_fetch(conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    owns = conn is None
    conn = conn or db_connect()
    try:
        return conn.execute(
            """
            SELECT id, fetched_at, station_count
            FROM fetch_log
            WHERE ok = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        if owns:
            conn.close()


def last_failed_fetch() -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT fetched_at, error
            FROM fetch_log
            WHERE ok = 0
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


def seconds_since(value: str) -> float:
    try:
        dt = datetime.fromisoformat(value)
        return max(0.0, (now_msk() - dt).total_seconds())
    except Exception:
        return 10**9


def store_fetch(rows: list[dict[str, Any]], fetched_at: datetime) -> dict[str, Any]:
    fetched = iso(fetched_at)
    snap_date = fetched_at.date().isoformat()

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO fetch_log(fetched_at, ok, station_count, error) VALUES (?, 1, ?, NULL)",
            (fetched, len(rows)),
        )
        fetch_id = int(cur.lastrowid)
        inserted_snapshots = 0

        for row in rows:
            conn.execute(
                """
                INSERT INTO station_v2(
                    station_uid, source_id, owner_raw, owner_group, name, address,
                    longitude, latitude, first_seen_at, last_seen_at, last_fetch_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station_uid) DO UPDATE SET
                    source_id=excluded.source_id,
                    owner_raw=excluded.owner_raw,
                    owner_group=excluded.owner_group,
                    name=excluded.name,
                    address=excluded.address,
                    longitude=excluded.longitude,
                    latitude=excluded.latitude,
                    last_seen_at=excluded.last_seen_at,
                    last_fetch_id=excluded.last_fetch_id
                """,
                (
                    row["station_uid"],
                    row["source_id"],
                    row["owner_raw"],
                    row["owner_group"],
                    row["name"],
                    row["address"],
                    row["longitude"],
                    row["latitude"],
                    fetched,
                    fetched,
                    fetch_id,
                ),
            )

            h = state_hash(row)
            prev = conn.execute(
                """
                SELECT snapshot_date, state_hash
                FROM snapshot_v2
                WHERE station_uid = ?
                ORDER BY fetched_at DESC, id DESC
                LIMIT 1
                """,
                (row["station_uid"],),
            ).fetchone()

            should_insert = (
                prev is None
                or prev["state_hash"] != h
                or prev["snapshot_date"] != snap_date
            )

            if should_insert:
                conn.execute(
                    """
                    INSERT INTO snapshot_v2(
                        station_uid, fetched_at, snapshot_date,
                        ai92, ai95, ai95_1, dt, dt_1, ai98, ai100, state_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["station_uid"],
                        fetched,
                        snap_date,
                        int(row["ai92"]),
                        int(row["ai95"]),
                        int(row["ai95_1"]),
                        int(row["dt"]),
                        int(row["dt_1"]),
                        int(row["ai98"]),
                        int(row["ai100"]),
                        h,
                    ),
                )
                inserted_snapshots += 1

        conn.commit()

    return {
        "fetch_id": fetch_id,
        "fetched_at": fetched,
        "station_count": len(rows),
        "inserted_snapshots": inserted_snapshots,
    }

def record_failure(error: Exception) -> None:
    message = clean_text(str(error))[:1000] or error.__class__.__name__
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO fetch_log(fetched_at, ok, station_count, error) VALUES (?, 0, NULL, ?)",
            (iso(now_msk()), message),
        )


def refresh_geoportal(force: bool = False) -> dict[str, Any]:
    with _REFRESH_LOCK:
        with db_connect() as conn:
            previous = last_success_fetch(conn)
            v2_count = conn.execute("SELECT COUNT(*) AS n FROM station_v2").fetchone()["n"]
            if (
                not force
                and previous is not None
                and v2_count > 0
                and seconds_since(previous["fetched_at"]) < CACHE_TTL_SECONDS
            ):
                return {
                    "ok": True,
                    "cached": True,
                    "fetched_at": previous["fetched_at"],
                    "station_count": previous["station_count"],
                }

        try:
            rows = parse_geojson(fetch_geoportal())
            result = store_fetch(rows, now_msk())
            return {"ok": True, "cached": False, **result}
        except Exception as exc:
            record_failure(exc)
            previous = last_success_fetch()
            if previous is not None:
                return {
                    "ok": False,
                    "cached": True,
                    "fetched_at": previous["fetched_at"],
                    "station_count": previous["station_count"],
                    "error": clean_text(str(exc)),
                }
            raise


def serialize_station(row: sqlite3.Row) -> dict[str, Any]:
    fuels = {key: bool(row[key]) for key in FUEL_KEYS}
    return {
        "id": row["station_uid"],
        "source_id": row["source_id"],
        "owner_raw": row["owner_raw"],
        "owner": row["owner_group"],
        "name": row["name"],
        "address": row["address"],
        "longitude": row["longitude"],
        "latitude": row["latitude"],
        "fetched_at": row["fetched_at"],
        "snapshot_date": row["snapshot_date"],
        "fuels": fuels,
        "available_any": any(fuels.values()),
        "portal_url": f"{GEOPORTAL_BASE_URL}#{row['longitude']}_{row['latitude']}_17",
    }

def get_current_stations() -> tuple[list[dict[str, Any]], sqlite3.Row | None]:
    with db_connect() as conn:
        fetch = last_success_fetch(conn)
        if fetch is None:
            return [], None

        rows = conn.execute(
            """
            SELECT
                s.station_uid, s.source_id, s.owner_raw, s.owner_group, s.name, s.address,
                s.longitude, s.latitude,
                ? AS fetched_at, p.snapshot_date,
                p.ai92, p.ai95, p.ai95_1, p.dt, p.dt_1, p.ai98, p.ai100
            FROM station_v2 s
            JOIN snapshot_v2 p ON p.id = (
                SELECT p2.id
                FROM snapshot_v2 p2
                WHERE p2.station_uid = s.station_uid
                ORDER BY p2.fetched_at DESC, p2.id DESC
                LIMIT 1
            )
            WHERE s.last_fetch_id = ?
            ORDER BY s.owner_group COLLATE NOCASE, s.name COLLATE NOCASE
            """,
            (fetch["fetched_at"], fetch["id"]),
        ).fetchall()

        return [serialize_station(row) for row in rows], fetch


def get_historical_stations(snapshot_date: str) -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.station_uid, s.source_id, s.owner_raw, s.owner_group, s.name, s.address,
                s.longitude, s.latitude,
                p.fetched_at, p.snapshot_date,
                p.ai92, p.ai95, p.ai95_1, p.dt, p.dt_1, p.ai98, p.ai100
            FROM station_v2 s
            JOIN snapshot_v2 p ON p.id = (
                SELECT p2.id
                FROM snapshot_v2 p2
                WHERE p2.station_uid = s.station_uid
                  AND p2.snapshot_date = ?
                ORDER BY p2.fetched_at DESC, p2.id DESC
                LIMIT 1
            )
            ORDER BY s.owner_group COLLATE NOCASE, s.name COLLATE NOCASE
            """,
            (snapshot_date,),
        ).fetchall()
        return [serialize_station(row) for row in rows]


def available_dates(limit: int = 120) -> list[str]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT snapshot_date
            FROM snapshot_v2
            ORDER BY snapshot_date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [row["snapshot_date"] for row in rows]


def station_history(station_uid: str, days: int) -> dict[str, Any]:
    end = now_msk().date()
    start = end - timedelta(days=days - 1)

    with db_connect() as conn:
        station = conn.execute(
            """
            SELECT station_uid, source_id, owner_group, name, address, longitude, latitude
            FROM station_v2
            WHERE station_uid = ?
            """,
            (station_uid,),
        ).fetchone()

        if station is None:
            raise HTTPException(status_code=404, detail="Station not found")

        rows = conn.execute(
            """
            SELECT *
            FROM snapshot_v2
            WHERE station_uid = ?
              AND snapshot_date BETWEEN ? AND ?
            ORDER BY snapshot_date ASC, fetched_at ASC, id ASC
            """,
            (station_uid, start.isoformat(), end.isoformat()),
        ).fetchall()

    latest_by_day: dict[str, sqlite3.Row] = {}
    changes_by_day: dict[str, int] = {}
    previous_hash_by_day: dict[str, str] = {}

    for row in rows:
        day = row["snapshot_date"]
        latest_by_day[day] = row
        if day not in previous_hash_by_day:
            previous_hash_by_day[day] = row["state_hash"]
            changes_by_day[day] = 0
        elif previous_hash_by_day[day] != row["state_hash"]:
            changes_by_day[day] += 1
            previous_hash_by_day[day] = row["state_hash"]

    daily = []
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        row = latest_by_day.get(key)
        if row is None:
            daily.append({"date": key, "known": False, "fetched_at": None, "changes": 0, "fuels": None})
        else:
            daily.append(
                {
                    "date": key,
                    "known": True,
                    "fetched_at": row["fetched_at"],
                    "changes": changes_by_day.get(key, 0),
                    "fuels": {fuel: bool(row[fuel]) for fuel in FUEL_KEYS},
                }
            )
        cursor += timedelta(days=1)

    return {
        "station": {
            "id": station["station_uid"],
            "source_id": station["source_id"],
            "owner": station["owner_group"],
            "name": station["name"],
            "address": station["address"],
            "longitude": station["longitude"],
            "latitude": station["latitude"],
        },
        "days": days,
        "history": daily,
    }


# =========================
# Authentication / 2FA
# =========================

def init_auth_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_failure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                failed_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_auth_failure_ip_time
                ON auth_failure(ip, failed_at);

            CREATE TABLE IF NOT EXISTS auth_lockout (
                ip TEXT PRIMARY KEY,
                locked_until INTEGER NOT NULL,
                level INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_challenge (
                challenge_id TEXT PRIMARY KEY,
                ip TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                code_hash TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                consumed INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_auth_challenge_exp
                ON auth_challenge(expires_at);

            CREATE TABLE IF NOT EXISTS auth_session (
                token_hash TEXT PRIMARY KEY,
                ip TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_auth_session_exp
                ON auth_session(expires_at);
            """
        )

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(auth_challenge)").fetchall()
        }
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE auth_challenge ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "telegram_message_id" not in columns:
            conn.execute(
                "ALTER TABLE auth_challenge ADD COLUMN telegram_message_id INTEGER"
            )
        if "decision_at" not in columns:
            conn.execute(
                "ALTER TABLE auth_challenge ADD COLUMN decision_at INTEGER"
            )


def unix_now() -> int:
    return int(time.time())


def client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded = clean_text(request.headers.get("x-forwarded-for", ""))
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            if candidate:
                return candidate[:80]
    return (request.client.host if request.client else "unknown")[:80]


def short_ua(request: Request) -> str:
    return clean_text(request.headers.get("user-agent", ""))[:240]


def password_matches(password: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, expected_hex = AUTH_PASSWORD_HASH.split(":", 5)
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(expected_hex)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


def cleanup_auth(conn: sqlite3.Connection) -> None:
    now = unix_now()
    conn.execute("DELETE FROM auth_failure WHERE failed_at < ?", (now - 86400,))
    conn.execute("DELETE FROM auth_challenge WHERE expires_at < ?", (now - 3600,))
    conn.execute("DELETE FROM auth_session WHERE expires_at < ?", (now,))


def lockout_remaining(ip: str) -> int:
    with db_connect() as conn:
        cleanup_auth(conn)
        row = conn.execute(
            "SELECT locked_until FROM auth_lockout WHERE ip = ?",
            (ip,),
        ).fetchone()
        if row is None:
            return 0
        return max(0, int(row["locked_until"]) - unix_now())


def record_auth_failure(ip: str) -> int:
    now = unix_now()
    with db_connect() as conn:
        cleanup_auth(conn)
        conn.execute(
            "INSERT INTO auth_failure(ip, failed_at) VALUES (?, ?)",
            (ip, now),
        )
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM auth_failure WHERE ip = ? AND failed_at >= ?",
            (ip, now - 600),
        ).fetchone()["n"]

        if count < 5:
            return 0

        previous = conn.execute(
            "SELECT level FROM auth_lockout WHERE ip = ?",
            (ip,),
        ).fetchone()
        level = min(6, (int(previous["level"]) + 1) if previous else 0)

        # 15m, 30m, 1h, 2h, 4h, 8h, max 12h.
        seconds = min(43200, 900 * (2 ** level))
        locked_until = now + seconds

        conn.execute(
            """
            INSERT INTO auth_lockout(ip, locked_until, level, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                locked_until=excluded.locked_until,
                level=excluded.level,
                updated_at=excluded.updated_at
            """,
            (ip, locked_until, level, now),
        )
        conn.execute("DELETE FROM auth_failure WHERE ip = ?", (ip,))
        return seconds


def clear_auth_failures(ip: str) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM auth_failure WHERE ip = ?", (ip,))
        conn.execute("DELETE FROM auth_lockout WHERE ip = ?", (ip,))


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_session(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if not token:
        return None
    digest = session_token_hash(token)
    now = unix_now()
    with db_connect() as conn:
        cleanup_auth(conn)
        return conn.execute(
            """
            SELECT token_hash, ip, user_agent, created_at, expires_at
            FROM auth_session
            WHERE token_hash = ? AND expires_at > ?
            """,
            (digest, now),
        ).fetchone()


def is_authenticated(request: Request) -> bool:
    return (not AUTH_ENABLED) or get_session(request) is not None


def telegram_api(method: str, fields: dict[str, Any]) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Telegram bot token is not configured")

    encoded: dict[str, str] = {}
    for key, value in fields.items():
        if isinstance(value, (dict, list)):
            encoded[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(value, bool):
            encoded[key] = "true" if value else "false"
        else:
            encoded[key] = str(value)

    body = urllib.parse.urlencode(encoded).encode("utf-8")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    tg_request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(tg_request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed")
    return payload


def telegram_send_approval(challenge_id: str, ip: str, user_agent: str) -> int:
    message = (
        "<b>Вход в Мониторинг АЗС</b>\n\n"
        "Разрешить вход на <b>dashboard.opentaya.space</b>?\n\n"
        f"IP: <code>{html.escape(ip)}</code>\n"
        f"Устройство: {html.escape(user_agent[:140] or 'не определено')}\n"
        f"Время: <code>{html.escape(iso(now_msk()))}</code>\n\n"
        "Если это не вы — нажмите «Отклонить»."
    )
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Принять",
                    "callback_data": f"azs:approve:{challenge_id}",
                },
                {
                    "text": "❌ Отклонить",
                    "callback_data": f"azs:deny:{challenge_id}",
                },
            ]
        ]
    }
    payload = telegram_api(
        "sendMessage",
        {
            "chat_id": TELEGRAM_ALLOWED_USER_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        },
    )
    return int(payload["result"]["message_id"])


def telegram_finish_callback(
    callback_id: str,
    chat_id: int,
    message_id: int,
    approved: bool,
) -> None:
    text = "✅ Вход разрешён." if approved else "❌ Вход отклонён."
    try:
        telegram_api(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": False,
            },
        )
    except Exception:
        pass

    try:
        telegram_api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": (
                    "<b>Мониторинг АЗС</b>\n\n"
                    + text
                    + f"\n\nВремя: <code>{html.escape(iso(now_msk()))}</code>"
                ),
                "parse_mode": "HTML",
            },
        )
    except Exception:
        pass


def create_session(response: Response, request: Request) -> None:
    raw_token = secrets.token_urlsafe(48)
    digest = session_token_hash(raw_token)
    now = unix_now()
    expires = now + SESSION_HOURS * 3600

    with db_connect() as conn:
        cleanup_auth(conn)
        conn.execute(
            """
            INSERT INTO auth_session(token_hash, ip, user_agent, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (digest, client_ip(request), short_ua(request), now, expires),
        )

    response.set_cookie(
        AUTH_COOKIE_NAME,
        raw_token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


PUBLIC_PATHS = {
    "/login",
    "/api/health",
    "/api/auth/status",
    "/api/auth/password",
    "/api/auth/poll",
    "/api/auth/telegram/webhook",
    "/favicon.ico",
}


async def read_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def poller() -> None:
    while True:
        try:
            await asyncio.to_thread(refresh_geoportal, True)
        except Exception:
            pass
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_auth_db()
    try:
        await asyncio.to_thread(refresh_geoportal, False)
    except Exception:
        pass

    task = asyncio.create_task(poller())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Geoportal40 AZS Dashboard",
    version="1.2.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def auth_and_security_middleware(request: Request, call_next):
    path = request.url.path
    public = (
        path in PUBLIC_PATHS
        or path.startswith("/static/")
        or path.startswith("/api/auth/")
    )

    if AUTH_ENABLED and not public and not is_authenticated(request):
        if path.startswith("/api/"):
            response = JSONResponse({"detail": "Требуется авторизация"}, status_code=401)
        else:
            response = RedirectResponse("/login", status_code=303)
    else:
        response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src https://azs.geoportal40.ru; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    return response


@app.get("/")
def index(request: Request):
    if AUTH_ENABLED and not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page(request: Request):
    if AUTH_ENABLED and is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, Any]:
    return {
        "configured": AUTH_ENABLED,
        "telegram_approval_ready": TELEGRAM_APPROVAL_READY,
        "authenticated": bool(AUTH_ENABLED and is_authenticated(request)),
        "telegram_bot": "@my_fed_helper_robot",
        "session_hours": SESSION_HOURS,
        "public_url": PUBLIC_BASE_URL or None,
    }


@app.post("/api/auth/password")
async def auth_password(request: Request):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="Авторизация ещё не настроена на сервере")
    if not TELEGRAM_APPROVAL_READY:
        raise HTTPException(
            status_code=503,
            detail="Telegram-подтверждение ещё не настроено. Запусти setup_auth.sh на сервере.",
        )

    ip = client_ip(request)
    remaining = lockout_remaining(ip)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Повторите через {max(1, (remaining + 59) // 60)} мин.",
            headers={"Retry-After": str(remaining)},
        )

    payload = await read_json(request)
    password = str(payload.get("password", ""))

    if not password or len(password) > 256 or not password_matches(password):
        locked_for = record_auth_failure(ip)
        await asyncio.sleep(0.55)
        if locked_for:
            raise HTTPException(
                status_code=429,
                detail=f"Слишком много попыток. Вход заблокирован на {max(1, locked_for // 60)} мин.",
                headers={"Retry-After": str(locked_for)},
            )
        raise HTTPException(status_code=401, detail="Неверный пароль")

    clear_auth_failures(ip)

    now = unix_now()
    ua = short_ua(request)

    # Do not spam Telegram if the browser repeats the request.
    with db_connect() as conn:
        cleanup_auth(conn)
        existing = conn.execute(
            """
            SELECT challenge_id, expires_at
            FROM auth_challenge
            WHERE ip = ?
              AND user_agent = ?
              AND consumed = 0
              AND status = 'pending'
              AND expires_at > ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ip, ua, now),
        ).fetchone()

    if existing is not None:
        return {
            "ok": True,
            "challenge_id": existing["challenge_id"],
            "expires_in": max(1, int(existing["expires_at"]) - now),
            "telegram_bot": "@my_fed_helper_robot",
            "reused": True,
        }

    challenge_id = secrets.token_urlsafe(24)
    expires = now + 180

    with db_connect() as conn:
        cleanup_auth(conn)
        conn.execute(
            """
            INSERT INTO auth_challenge(
                challenge_id, ip, user_agent, code_hash,
                created_at, expires_at, attempts, consumed, status
            )
            VALUES (?, ?, ?, '', ?, ?, 0, 0, 'pending')
            """,
            (challenge_id, ip, ua, now, expires),
        )

    try:
        message_id = await asyncio.to_thread(
            telegram_send_approval,
            challenge_id,
            ip,
            ua,
        )
    except Exception:
        with db_connect() as conn:
            conn.execute(
                """
                UPDATE auth_challenge
                SET consumed = 1, status = 'failed'
                WHERE challenge_id = ?
                """,
                (challenge_id,),
            )
        raise HTTPException(
            status_code=502,
            detail="Не удалось отправить запрос подтверждения в Telegram.",
        )

    with db_connect() as conn:
        conn.execute(
            """
            UPDATE auth_challenge
            SET telegram_message_id = ?
            WHERE challenge_id = ?
            """,
            (message_id, challenge_id),
        )

    return {
        "ok": True,
        "challenge_id": challenge_id,
        "expires_in": 180,
        "telegram_bot": "@my_fed_helper_robot",
        "reused": False,
    }


@app.post("/api/auth/poll")
async def auth_poll(request: Request):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="Авторизация ещё не настроена")

    payload = await read_json(request)
    challenge_id = str(payload.get("challenge_id", ""))[:160]
    if not challenge_id:
        raise HTTPException(status_code=400, detail="Некорректный запрос на вход")

    now = unix_now()
    with db_connect() as conn:
        cleanup_auth(conn)
        row = conn.execute(
            """
            SELECT challenge_id, status, expires_at, consumed
            FROM auth_challenge
            WHERE challenge_id = ?
            """,
            (challenge_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Запрос на вход не найден")

        if int(row["expires_at"]) <= now:
            conn.execute(
                """
                UPDATE auth_challenge
                SET consumed = 1, status = 'expired'
                WHERE challenge_id = ?
                """,
                (challenge_id,),
            )
            return {"status": "expired"}

        status = str(row["status"] or "pending")

        if status == "denied":
            if not row["consumed"]:
                conn.execute(
                    "UPDATE auth_challenge SET consumed = 1 WHERE challenge_id = ?",
                    (challenge_id,),
                )
            return {"status": "denied"}

        if status != "approved":
            return {
                "status": "pending",
                "expires_in": max(1, int(row["expires_at"]) - now),
            }

        if row["consumed"]:
            return {"status": "consumed"}

        updated = conn.execute(
            """
            UPDATE auth_challenge
            SET consumed = 1
            WHERE challenge_id = ? AND consumed = 0 AND status = 'approved'
            """,
            (challenge_id,),
        ).rowcount

    if updated != 1:
        return {"status": "consumed"}

    response = JSONResponse({"status": "approved"})
    create_session(response, request)
    return response


@app.post("/api/auth/telegram/webhook")
async def telegram_webhook(request: Request):
    if not TELEGRAM_APPROVAL_READY:
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured")

    supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not supplied or not hmac.compare_digest(supplied, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await read_json(request)
    callback = payload.get("callback_query")
    if not isinstance(callback, dict):
        return {"ok": True}

    from_user = callback.get("from") or {}
    try:
        from_id = int(from_user.get("id", 0))
    except (TypeError, ValueError):
        from_id = 0

    callback_id = str(callback.get("id", ""))
    data = str(callback.get("data", ""))
    message = callback.get("message") or {}
    chat = message.get("chat") or {}

    try:
        chat_id = int(chat.get("id", 0))
        message_id = int(message.get("message_id", 0))
    except (TypeError, ValueError):
        chat_id = 0
        message_id = 0

    if from_id != TELEGRAM_ALLOWED_USER_ID or chat_id != TELEGRAM_ALLOWED_USER_ID:
        if callback_id:
            try:
                await asyncio.to_thread(
                    telegram_api,
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_id,
                        "text": "Нет доступа",
                        "show_alert": True,
                    },
                )
            except Exception:
                pass
        return {"ok": True}

    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "azs" or parts[1] not in {"approve", "deny"}:
        return {"ok": True}

    decision = parts[1]
    challenge_id = parts[2]
    now = unix_now()
    approved = decision == "approve"

    with db_connect() as conn:
        cleanup_auth(conn)
        row = conn.execute(
            """
            SELECT challenge_id, status, expires_at, consumed
            FROM auth_challenge
            WHERE challenge_id = ?
            """,
            (challenge_id,),
        ).fetchone()

        if row is None or row["consumed"]:
            valid = False
            expired = False
        elif int(row["expires_at"]) <= now:
            conn.execute(
                """
                UPDATE auth_challenge
                SET consumed = 1, status = 'expired', decision_at = ?
                WHERE challenge_id = ?
                """,
                (now, challenge_id),
            )
            valid = False
            expired = True
        elif str(row["status"] or "pending") != "pending":
            valid = False
            expired = False
        else:
            conn.execute(
                """
                UPDATE auth_challenge
                SET status = ?, decision_at = ?
                WHERE challenge_id = ? AND status = 'pending' AND consumed = 0
                """,
                ("approved" if approved else "denied", now, challenge_id),
            )
            valid = True
            expired = False

    if not valid:
        if callback_id:
            try:
                await asyncio.to_thread(
                    telegram_api,
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_id,
                        "text": "Запрос уже недействителен" if expired else "Запрос уже обработан",
                        "show_alert": False,
                    },
                )
            except Exception:
                pass
        return {"ok": True}

    if callback_id and chat_id and message_id:
        await asyncio.to_thread(
            telegram_finish_callback,
            callback_id,
            chat_id,
            message_id,
            approved,
        )

    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if token:
        with db_connect() as conn:
            conn.execute(
                "DELETE FROM auth_session WHERE token_hash = ?",
                (session_token_hash(token),),
            )
    response = JSONResponse({"ok": True})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/api/health")
def health() -> dict[str, Any]:
    last_ok = last_success_fetch()
    last_fail = last_failed_fetch()
    with db_connect() as conn:
        current_unique = conn.execute("SELECT COUNT(*) AS n FROM station_v2").fetchone()["n"]
        current_fetch_rows = 0
        if last_ok is not None:
            current_fetch_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM station_v2 WHERE last_fetch_id = ?",
                (last_ok["id"],),
            ).fetchone()["n"]
    return {
        "ok": last_ok is not None,
        "geoportal_base_url": GEOPORTAL_BASE_URL,
        "last_success": dict(last_ok) if last_ok else None,
        "last_failure": dict(last_fail) if last_fail else None,
        "stored_stations": current_unique,
        "current_fetch_stations": current_fetch_rows,
        "poll_seconds": POLL_SECONDS,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "auth_configured": AUTH_ENABLED,
        "telegram_approval_ready": TELEGRAM_APPROVAL_READY,
    }


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    try:
        refresh = refresh_geoportal(False)
    except Exception as exc:
        refresh = {"ok": False, "cached": False, "error": clean_text(str(exc))}
    last_ok = last_success_fetch()
    last_fail = last_failed_fetch()
    return {
        "refresh": refresh,
        "last_success": dict(last_ok) if last_ok else None,
        "last_failure": dict(last_fail) if last_fail else None,
        "dates": available_dates(),
        "fuels": {
            "ai92": "АИ-92",
            "ai95": "АИ-95",
            "ai95_1": "АИ-95+",
            "dt": "ДТ",
            "dt_1": "ДТ+",
            "ai98": "АИ-98",
            "ai100": "АИ-100",
        },
    }


@app.get("/api/stations")
def stations(
    date: str | None = Query(default=None, description="YYYY-MM-DD; omit for current Geoportal state"),
    force: bool = Query(default=False),
) -> dict[str, Any]:
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        rows = get_historical_stations(date)
        return {
            "mode": "history",
            "date": date,
            "stale": False,
            "count": len(rows),
            "stations": rows,
        }

    try:
        refresh = refresh_geoportal(force)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Geoportal unavailable and cache is empty: {exc}")

    rows, fetch = get_current_stations()
    return {
        "mode": "live",
        "date": None,
        "stale": not bool(refresh.get("ok")),
        "refresh_error": refresh.get("error"),
        "fetched_at": fetch["fetched_at"] if fetch else None,
        "count": len(rows),
        "stations": rows,
    }


@app.get("/api/history/{station_uid}")
def history(
    station_uid: str,
    days: int = Query(default=30, ge=1, le=180),
) -> dict[str, Any]:
    return station_history(station_uid, days)


@app.post("/api/refresh")
def force_refresh() -> dict[str, Any]:
    try:
        result = refresh_geoportal(True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Geoportal refresh failed: {exc}")
    rows, fetch = get_current_stations()
    return {
        **result,
        "current_count": len(rows),
        "current_fetched_at": fetch["fetched_at"] if fetch else None,
    }
