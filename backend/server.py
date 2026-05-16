import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

STEP_LABELS = {1: "Принят ✓", 2: "Готовим 👨‍🍳", 3: "В пути 🛵", 4: "Доставлен 🏠"}

CLIENT_MESSAGES = {
    1: "✅ Ваш заказ принят! Мы уже начинаем его готовить.",
    2: "👨‍🍳 Ваш заказ готовится! Совсем скоро будет готов.",
    3: "🛵 Ваш заказ у курьера! Ожидайте доставку.",
    4: "🏠 Вы получили заказ. Приятного аппетита! 🍣",
}


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_num TEXT PRIMARY KEY,
                    step INTEGER DEFAULT 1,
                    name TEXT,
                    phone TEXT,
                    address TEXT,
                    total INTEGER,
                    user_id BIGINT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://1ukoi1ik.github.io"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class OrderItem(BaseModel):
    name: str
    qty: int
    price: int


class Order(BaseModel):
    name: str
    phone: str = ""
    address: str
    comment: str = ""
    payment: str = "Наличными курьеру"
    items: List[OrderItem]
    total: int
    order_num: Optional[str] = None
    user_id: Optional[int] = None


def make_status_keyboard(order_num: str, current_step: int):
    buttons = []
    for step, label in STEP_LABELS.items():
        if step > current_step:
            buttons.append({"text": f"→ {label}", "callback_data": f"status:{order_num}:{step}"})
    return {"inline_keyboard": [buttons]} if buttons else None


def format_order(order: Order) -> str:
    num = order.order_num or "—"
    lines = [f"🍣 *Новый заказ {num}*\n"]
    for item in order.items:
        lines.append(f"• {item.name} × {item.qty} — {item.price * item.qty:,} ₽")
    lines.append(f"\n💰 *Итого: {order.total:,} ₽*")
    lines.append(f"💳 Оплата: {order.payment}")
    lines.append(f"\n👤 {order.name}")
    if order.phone:
        lines.append(f"📱 {order.phone}")
    lines.append(f"📍 {order.address}")
    if order.comment:
        lines.append(f"💬 {order.comment}")
    lines.append(f"\n📋 Статус: {STEP_LABELS[1]}")
    return "\n".join(lines)


@app.post("/order")
async def receive_order(order: Order):
    num = order.order_num or "#0000"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (order_num, step, name, phone, address, total, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (order_num) DO UPDATE SET step=1, user_id=%s",
                (num, 1, order.name, order.phone, order.address, order.total, order.user_id, order.user_id)
            )
        conn.commit()

    text = format_order(order)
    keyboard = make_status_keyboard(num, 1)
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard

    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)
        # уведомить клиента о приёме заказа
        if order.user_id:
            await client.post(f"{TG_API}/sendMessage", json={
                "chat_id": order.user_id,
                "text": f"{CLIENT_MESSAGES[1]}\n\n🧾 Заказ {num} на сумму {order.total:,} ₽"
            })

    return {"ok": True}


@app.get("/orders/history/{user_id}")
async def get_order_history(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_num, step, total, created_at FROM orders WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",
                (user_id,)
            )
            rows = cur.fetchall()
    return {"ok": True, "orders": [
        {
            "num": r["order_num"],
            "step": r["step"],
            "total": r["total"],
            "date": r["created_at"].strftime("%d %b %H:%M") if r["created_at"] else ""
        }
        for r in rows
    ]}


@app.get("/order-status/{order_num}")
async def get_order_status(order_num: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT step FROM orders WHERE order_num=%s", (order_num,))
            row = cur.fetchone()
    if not row:
        return {"ok": False, "step": 1}
    return {"ok": True, "step": row["step"]}


MINI_APP_URL = "https://1ukoi1ik.github.io/ichiban-sushi-bot/"
import pathlib
WELCOME_VIDEO_PATH = str(pathlib.Path(__file__).parent.parent / "welcome.mp4")
_welcome_file_id = None


async def send_welcome(chat_id: int, client: httpx.AsyncClient):
    global _welcome_file_id
    keyboard = {"inline_keyboard": [[{
        "text": "🍣 Сделать заказ",
        "web_app": {"url": MINI_APP_URL}
    }]]}
    if _welcome_file_id:
        await client.post(f"{TG_API}/sendAnimation", json={
            "chat_id": chat_id,
            "animation": _welcome_file_id,
            "reply_markup": keyboard
        })
        return
    import os
    if os.path.exists(WELCOME_VIDEO_PATH):
        with open(WELCOME_VIDEO_PATH, "rb") as f:
            resp = await client.post(f"{TG_API}/sendAnimation", data={
                "chat_id": str(chat_id),
                "reply_markup": __import__("json").dumps(keyboard)
            }, files={"animation": ("welcome.mp4", f, "video/mp4")})
            result = resp.json()
            if result.get("ok"):
                _welcome_file_id = result["result"]["animation"]["file_id"]
    else:
        await client.post(f"{TG_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": "🍣 Добро пожаловать! Нажми кнопку чтобы сделать заказ.",
            "reply_markup": keyboard
        })


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    message = data.get("message")
    if message and message.get("text") == "/start":
        async with httpx.AsyncClient() as client:
            import os, glob
            search = glob.glob("/app/**/*.mp4", recursive=True) + glob.glob(str(pathlib.Path(__file__).parent.parent) + "/**/*.mp4", recursive=True)
            await client.post(f"{TG_API}/sendMessage", json={
                "chat_id": message["chat"]["id"],
                "text": f"path={WELCOME_VIDEO_PATH}\nexists={os.path.exists(WELCOME_VIDEO_PATH)}\nfound={search}"
            })
            await send_welcome(message["chat"]["id"], client)
        return {"ok": True}

    callback = data.get("callback_query")
    if not callback:
        return {"ok": True}

    cb_data = callback.get("data", "")
    if not cb_data.startswith("status:"):
        return {"ok": True}

    _, order_num, step_str = cb_data.split(":")
    step = int(step_str)

    user_id = None
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET step=%s WHERE order_num=%s RETURNING user_id", (step, order_num))
            row = cur.fetchone()
            if row:
                user_id = row["user_id"]
        conn.commit()

    label = STEP_LABELS.get(step, "")
    msg_text = callback["message"]["text"]
    lines = msg_text.split("\n")
    lines = [f"📋 Статус: {label}" if l.startswith("📋 Статус:") else l for l in lines]
    new_text = "\n".join(lines)

    keyboard = make_status_keyboard(order_num, step)
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/editMessageText", json={
            "chat_id": callback["message"]["chat"]["id"],
            "message_id": callback["message"]["message_id"],
            "text": new_text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard if keyboard else {"inline_keyboard": []},
        })
        await client.post(f"{TG_API}/answerCallbackQuery", json={
            "callback_query_id": callback["id"],
            "text": f"Статус: {label}"
        })
        # уведомить клиента
        if user_id and step in CLIENT_MESSAGES:
            await client.post(f"{TG_API}/sendMessage", json={
                "chat_id": user_id,
                "text": f"{CLIENT_MESSAGES[step]}\n\n🧾 Заказ {order_num}"
            })

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
