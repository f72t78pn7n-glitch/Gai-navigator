"""
GAI Navigator — Telegram Mini App.

Бот читает геолокации из группы и сохраняет их как точки.
Mini App показывает карту и предупреждает при приближении.

Запуск:  python main.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    ReactionTypeEmoji,
    WebAppInfo,
)
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")  # https://xxxx.trycloudflare.com
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "points.db")
POINT_TTL_MIN = int(os.getenv("POINT_TTL_MINUTES", "120"))  # сколько живёт точка
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"  # 1 = не проверять подпись Telegram (только для тестов)
# Если задано — бот принимает локации только из этих чатов (id через запятую)
ALLOWED_CHATS = {int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip()}

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN в файле .env")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gai")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# ───────────────────────── База данных ─────────────────────────


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'gai',
                user_id INTEGER,
                user_name TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_points_msg ON points(chat_id, message_id)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_points_geo ON points(lat, lon)")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS votes (
                point_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (point_id, user_id)
            )
            """
        )


def upsert_point(chat_id, message_id, lat, lon, user_id, user_name):
    now = int(time.time())
    with db() as c:
        row = c.execute(
            "SELECT id FROM points WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        if row:  # live-локация обновилась
            c.execute(
                "UPDATE points SET lat=?, lon=?, updated_at=? WHERE id=?",
                (lat, lon, now, row["id"]),
            )
        else:
            c.execute(
                """INSERT INTO points
                   (chat_id, message_id, lat, lon, user_id, user_name, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (chat_id, message_id, lat, lon, user_id, user_name, now, now),
            )


def cleanup():
    border = int(time.time()) - POINT_TTL_MIN * 60
    with db() as c:
        c.execute("DELETE FROM points WHERE updated_at < ?", (border,))
        c.execute("DELETE FROM votes WHERE point_id NOT IN (SELECT id FROM points)")


# ───────────────────────── Геометрия ─────────────────────────


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ───────────────────────── Авторизация Telegram ─────────────────────────


def verify_init_data(init_data: str) -> dict:
    """Проверяет подпись initData от Telegram Mini App. Возвращает данные пользователя."""
    if DEV_MODE and not init_data:
        return {"id": 0, "first_name": "dev"}
    if not init_data:
        raise HTTPException(401, "Откройте приложение из Telegram")
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = parsed.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            raise ValueError("bad hash")
        if time.time() - int(parsed.get("auth_date", 0)) > 3 * 86400:
            raise ValueError("expired")
        return json.loads(parsed["user"])
    except Exception:
        raise HTTPException(401, "Неверная подпись Telegram")


def current_user(x_init_data: str = Header(default="")) -> dict:
    return verify_init_data(x_init_data)


# ───────────────────────── Telegram-бот ─────────────────────────

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
BOT_USERNAME = ""


def app_url() -> str:
    return BASE_URL or "https://example.com"


def open_app_button_group() -> InlineKeyboardMarkup:
    # В группах web_app-кнопки запрещены, поэтому ссылка на Main Mini App бота
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚓 Открыть навигатор", url=f"https://t.me/{BOT_USERNAME}?startapp")]
        ]
    )


@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚓 Открыть навигатор", web_app=WebAppInfo(url=app_url()))]
        ]
    )
    await m.answer(
        "Привет! Добавь меня в группу, где скидывают геолокации постов ГАИ.\n"
        "Каждая отправленная локация станет точкой на карте, "
        "а приложение предупредит, когда ты к ней приближаешься.",
        reply_markup=kb,
    )


@dp.message(Command("app"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_app(m: Message):
    await m.answer("Навигатор с постами:", reply_markup=open_app_button_group())


@dp.message(F.location)
@dp.edited_message(F.location)
async def on_location(m: Message):
    if ALLOWED_CHATS and m.chat.id not in ALLOWED_CHATS:
        return
    loc = m.location
    name = m.from_user.full_name if m.from_user else "—"
    uid = m.from_user.id if m.from_user else None
    upsert_point(m.chat.id, m.message_id, loc.latitude, loc.longitude, uid, name)
    log.info("Точка из чата %s: %.5f, %.5f", m.chat.id, loc.latitude, loc.longitude)
    try:  # реакция вместо ответа, чтобы не спамить в группе
        await m.react([ReactionTypeEmoji(emoji="👍")])
    except Exception:
        pass


async def cleanup_loop():
    while True:
        try:
            cleanup()
        except Exception:
            log.exception("cleanup")
        await asyncio.sleep(60)


# ───────────────────────── API ─────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    global BOT_USERNAME
    init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info("Бот: @%s", BOT_USERNAME)
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Навигатор", web_app=WebAppInfo(url=BASE_URL))
            )
        except Exception:
            log.exception("menu button")
    tasks = [
        asyncio.create_task(dp.start_polling(bot, handle_signals=False)),
        asyncio.create_task(cleanup_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


class NewPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Vote(BaseModel):
    value: int = Field(ge=-1, le=1)


@app.get("/api/points")
def get_points(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius: int = Query(5000, ge=100, le=50000),
    user: dict = Depends(current_user),
):
    now = int(time.time())
    dlat = radius / 111_320
    dlon = radius / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    with db() as c:
        rows = c.execute(
            """SELECT * FROM points
               WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
                 AND updated_at > ? AND score > -2""",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon, now - POINT_TTL_MIN * 60),
        ).fetchall()
    out = []
    for r in rows:
        d = haversine(lat, lon, r["lat"], r["lon"])
        if d <= radius:
            out.append(
                {
                    "id": r["id"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "kind": r["kind"],
                    "user_name": r["user_name"],
                    "age_min": (now - r["updated_at"]) // 60,
                    "score": r["score"],
                    "distance": round(d),
                }
            )
    out.sort(key=lambda p: p["distance"])
    return {"points": out}


@app.post("/api/points")
def add_point(p: NewPoint, user: dict = Depends(current_user)):
    name = user.get("first_name", "—")
    now = int(time.time())
    with db() as c:
        c.execute(
            """INSERT INTO points (chat_id, message_id, lat, lon, user_id, user_name, created_at, updated_at)
               VALUES (0, NULL, ?, ?, ?, ?, ?, ?)""",
            (p.lat, p.lon, user.get("id"), name, now, now),
        )
    return {"ok": True}


@app.post("/api/points/{point_id}/vote")
def vote_point(point_id: int, v: Vote, user: dict = Depends(current_user)):
    uid = user.get("id", 0)
    with db() as c:
        if not c.execute("SELECT 1 FROM points WHERE id=?", (point_id,)).fetchone():
            raise HTTPException(404, "Точка не найдена")
        c.execute(
            "INSERT OR REPLACE INTO votes (point_id, user_id, value) VALUES (?,?,?)",
            (point_id, uid, v.value),
        )
        score = c.execute(
            "SELECT COALESCE(SUM(value),0) s FROM votes WHERE point_id=?", (point_id,)
        ).fetchone()["s"]
        if v.value > 0:  # «стоит» продлевает жизнь точке
            c.execute(
                "UPDATE points SET score=?, updated_at=? WHERE id=?",
                (score, int(time.time()), point_id),
            )
        else:
            c.execute("UPDATE points SET score=? WHERE id=?", (score, point_id))
    return {"ok": True, "score": score}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
