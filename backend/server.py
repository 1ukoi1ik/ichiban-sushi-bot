import os
import sqlite3
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
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DB_PATH = "orders.db"

STEP_LABELS = {1: "Принят ✓", 2: "Готовим 👨‍🍳", 3: "В пути 🛵", 4: "Доставлен 🏠"}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_num TEXT PRIMARY KEY,
                step INTEGER DEFAULT 1,
                name TEXT,
                phone TEXT,
                address TEXT,
                total INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    allow_origins=["*"],
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
        conn.execute(
            "INSERT OR REPLACE INTO orders (order_num, step, name, phone, address, total) VALUES (?,?,?,?,?,?)",
            (num, 1, order.name, order.phone, order.address, order.total)
        )
        conn.commit()

    text = format_order(order)
    keyboard = make_status_keyboard(num, 1)
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard

    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)

    return {"ok": True}


@app.get("/order-status/{order_num}")
async def get_order_status(order_num: str):
    with get_db() as conn:
        row = conn.execute("SELECT step FROM orders WHERE order_num=?", (order_num,)).fetchone()
    if not row:
        return {"ok": False, "step": 1}
    return {"ok": True, "step": row["step"]}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    callback = data.get("callback_query")
    if not callback:
        return {"ok": True}

    cb_data = callback.get("data", "")
    if not cb_data.startswith("status:"):
        return {"ok": True}

    _, order_num, step_str = cb_data.split(":")
    step = int(step_str)

    with get_db() as conn:
        conn.execute("UPDATE orders SET step=? WHERE order_num=?", (step, order_num))
        conn.commit()

    label = STEP_LABELS.get(step, "")
    new_text = callback["message"]["text"]
    # обновить строку статуса
    lines = new_text.split("\n")
    lines = [l if not l.startswith("📋 Статус:") else f"📋 Статус: {label}" for l in lines]
    new_text = "\n".join(lines)

    keyboard = make_status_keyboard(order_num, step)
    async with httpx.AsyncClient() as client:
        payload = {
            "chat_id": callback["message"]["chat"]["id"],
            "message_id": callback["message"]["message_id"],
            "text": new_text,
            "parse_mode": "Markdown",
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        await client.post(f"{TG_API}/editMessageText", json=payload)
        await client.post(f"{TG_API}/answerCallbackQuery", json={
            "callback_query_id": callback["id"],
            "text": f"Статус обновлён: {label}"
        })

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
