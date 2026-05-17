import os
import json
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
DADATA_KEY = os.getenv("DADATA_KEY", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

STEP_LABELS = {0: "Отправлен 📨", 1: "Принят ✓", 2: "Готовим 👨‍🍳", 3: "В пути 🛵", 4: "Доставлен 🏠"}

CLIENT_MESSAGES = {
    1: "✅ Ваш заказ принят в работу! Уже начинаем готовить.",
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
                    step INTEGER DEFAULT 0,
                    name TEXT,
                    phone TEXT,
                    address TEXT,
                    total INTEGER,
                    user_id BIGINT,
                    items JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS items JSONB")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT,
                    phone TEXT,
                    addresses TEXT[] DEFAULT '{}',
                    avatar TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS order_counters (
                    day DATE PRIMARY KEY,
                    counter INTEGER DEFAULT 0
                )
            """)
            cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS avatar TEXT")
            # нормализация телефонов: убрать форматирование, оставить +7XXXXXXXXXX
            cur.execute("""
                UPDATE clients
                SET phone = '+7' || right(regexp_replace(phone, '[^0-9]', '', 'g'), 10)
                WHERE phone IS NOT NULL AND phone != ''
                  AND phone NOT SIMILAR TO '\+7[0-9]{10}'
            """)
            cur.execute("""
                UPDATE orders
                SET phone = '+7' || right(regexp_replace(phone, '[^0-9]', '', 'g'), 10)
                WHERE phone IS NOT NULL AND phone != ''
                  AND phone NOT SIMILAR TO '\+7[0-9]{10}'
            """)
        conn.commit()


def next_order_num_atomic() -> str:
    import datetime
    today = datetime.date.today()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_counters (day, counter) VALUES (%s, 1)
                ON CONFLICT (day) DO UPDATE SET counter = order_counters.counter + 1
                RETURNING counter
            """, (today,))
            row = cur.fetchone()
        conn.commit()
    return f"#{row['counter']:03d}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "DELETE"],
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
    discount: int = 0
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
    if order.discount:
        lines.append(f"\n💰 *Итого: {order.total:,} ₽* (скидка {order.discount}%)")
    else:
        lines.append(f"\n💰 *Итого: {order.total:,} ₽*")
    lines.append(f"💳 Оплата: {order.payment}")
    lines.append(f"\n👤 {order.name}")
    if order.phone:
        lines.append(f"📱 {order.phone}")
    lines.append(f"📍 {order.address}")
    if order.comment:
        lines.append(f"💬 {order.comment}")
    lines.append(f"\n📋 Статус: {STEP_LABELS[0]}")
    return "\n".join(lines)


@app.post("/order")
async def receive_order(order: Order):
    num = next_order_num_atomic()
    with get_db() as conn:
        with conn.cursor() as cur:
            items_json = json.dumps([{"name": i.name, "qty": i.qty, "price": i.price} for i in order.items])
            cur.execute(
                "INSERT INTO orders (order_num, step, name, phone, address, total, user_id, items) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (num, 0, order.name, order.phone, order.address, order.total, order.user_id, items_json)
            )
        conn.commit()

    text = format_order(order)
    keyboard = make_status_keyboard(num, 0)
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

    return {"ok": True, "order_num": num}


@app.get("/orders/history/{user_id}")
async def get_order_history(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_num, step, total, items, created_at FROM orders WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",
                (user_id,)
            )
            rows = cur.fetchall()
    return {"ok": True, "orders": [
        {
            "num": r["order_num"],
            "step": r["step"],
            "total": r["total"],
            "items": r["items"] or [],
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


MINI_APP_URL = "https://1ukoi1ik.github.io/ichiban-react/"
WELCOME_VIDEO_URL = "https://github.com/1ukoi1ik/ichiban-sushi-bot/raw/main/welcome.mp4"
_welcome_file_id = None


async def send_welcome(chat_id: int, client: httpx.AsyncClient):
    global _welcome_file_id
    keyboard = {"inline_keyboard": [
        [{"text": "🍣 Сделать заказ", "web_app": {"url": MINI_APP_URL}}],
        [{"text": "🎟️ Карта гостя", "web_app": {"url": MINI_APP_URL + "?card=1"}}],
    ]}
    resp = await client.post(f"{TG_API}/sendAnimation", json={
        "chat_id": chat_id,
        "animation": _welcome_file_id or WELCOME_VIDEO_URL,
        "reply_markup": keyboard
    })
    result = resp.json()
    if result.get("ok") and not _welcome_file_id:
        _welcome_file_id = result["result"]["animation"]["file_id"]


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    message = data.get("message")
    if message and message.get("text") == "/start":
        async with httpx.AsyncClient() as client:
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


def norm_phone(p: str) -> str:
    if not p:
        return p
    digits = ''.join(filter(str.isdigit, p))
    if not digits:
        return p
    # убираем leading 7 или 8 если номер начинается с них
    if len(digits) == 11 and digits[0] in ('7', '8'):
        digits = digits[1:]
    if len(digits) == 10:
        return f'+7{digits}'
    return f'+{digits}'


class ClientPhone(BaseModel):
    user_id: int
    phone: str
    name: str = ""


class ClientAddress(BaseModel):
    user_id: int
    address: str


class ClientUpdate(BaseModel):
    user_id: int
    name: str = ""
    phone: str = ""


@app.post("/profile/update")
async def update_profile(data: ClientUpdate):
    phone = norm_phone(data.phone)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clients (user_id, name, phone, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET name = CASE WHEN %s != '' THEN %s ELSE clients.name END,
                    phone = CASE WHEN %s != '' THEN %s ELSE clients.phone END,
                    updated_at = NOW()
            """, (data.user_id, data.name, phone,
                  data.name, data.name,
                  phone, phone))
        conn.commit()
    return {"ok": True}


@app.post("/profile/update-name")
async def update_profile_name(data: ClientUpdate):
    """Обновляет только имя, телефон не трогает если уже есть."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clients (user_id, name, phone, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET name = CASE WHEN %s != '' THEN %s ELSE clients.name END,
                    updated_at = NOW()
            """, (data.user_id, data.name, data.phone,
                  data.name, data.name))
        conn.commit()
    return {"ok": True}


@app.post("/profile/phone")
async def save_profile_phone(data: ClientPhone):
    phone = norm_phone(data.phone)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clients (user_id, name, phone, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET phone = EXCLUDED.phone,
                    name = CASE WHEN EXCLUDED.name != '' THEN EXCLUDED.name ELSE clients.name END,
                    updated_at = NOW()
            """, (data.user_id, data.name, phone))
        conn.commit()
    return {"ok": True}


@app.post("/profile/address/delete")
async def delete_profile_address(data: ClientAddress):
    with get_db() as conn:
        with conn.cursor() as cur:
            # убрать из clients.addresses
            cur.execute("""
                UPDATE clients SET addresses = array_remove(addresses, %s), updated_at = NOW()
                WHERE user_id = %s
            """, (data.address, data.user_id))
            # если адрес из orders — занулить его там тоже (trim для надёжности)
            cur.execute("""
                UPDATE orders SET address = NULL
                WHERE user_id = %s AND trim(address) = trim(%s)
            """, (data.user_id, data.address))
            # убедиться что запись в clients есть (для будущих операций)
            cur.execute("""
                INSERT INTO clients (user_id, updated_at) VALUES (%s, NOW())
                ON CONFLICT (user_id) DO NOTHING
            """, (data.user_id,))
        conn.commit()
    return {"ok": True}


@app.post("/profile/address")
async def save_profile_address(data: ClientAddress):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clients (user_id, addresses, updated_at)
                VALUES (%s, ARRAY[%s], NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET addresses = CASE
                    WHEN %s = ANY(clients.addresses) THEN clients.addresses
                    ELSE array_prepend(%s, clients.addresses[0:9])
                END,
                updated_at = NOW()
            """, (data.user_id, data.address, data.address, data.address))
        conn.commit()
    return {"ok": True}


class AvatarData(BaseModel):
    user_id: int
    avatar: str

@app.post("/profile/avatar")
async def set_avatar(data: AvatarData):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clients (user_id, avatar, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET avatar = EXCLUDED.avatar, updated_at = NOW()
            """, (data.user_id, data.avatar))
        conn.commit()
    return {"ok": True}


@app.get("/profile/{user_id}")
async def get_profile(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as total_orders,
                       COALESCE(SUM(total), 0) as total_sum,
                       SUM(CASE WHEN created_at >= date_trunc('month', NOW()) THEN total ELSE 0 END) as month_sum
                FROM orders WHERE user_id=%s
            """, (user_id,))
            agg_row = cur.fetchone()
            cur.execute("""
                SELECT name, phone, address FROM orders WHERE user_id=%s
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,))
            order_row = cur.fetchone()
            cur.execute("""
                SELECT DISTINCT address FROM orders
                WHERE user_id=%s AND address IS NOT NULL AND address != ''
            """, (user_id,))
            order_addresses = [r["address"] for r in cur.fetchall()]
            cur.execute("SELECT name, phone, addresses, avatar FROM clients WHERE user_id=%s", (user_id,))
            client_row = cur.fetchone()

    if not agg_row and not client_row:
        return {"ok": True, "new_client": True}

    total = int(agg_row["total_orders"]) if agg_row else 0
    total_sum = int(agg_row["total_sum"] or 0) if agg_row else 0
    discount = 15 if total_sum >= 15000 else 10 if total_sum >= 7000 else 5 if total_sum >= 3000 else 0

    name = (client_row["name"] if client_row and client_row["name"] else None) or (order_row["name"] if order_row else "") or ""
    phone = norm_phone((client_row["phone"] if client_row and client_row["phone"] else None) or (order_row["phone"] if order_row else "") or "")
    addresses = list(client_row["addresses"]) if client_row and client_row["addresses"] else []
    for oa in order_addresses:
        if oa not in addresses:
            addresses.append(oa)

    return {
        "ok": True,
        "new_client": False,
        "name": name,
        "phone": phone,
        "addresses": addresses,
        "total_orders": total,
        "total_sum": total_sum,
        "month_sum": int(agg_row["month_sum"] or 0) if agg_row else 0,
        "discount": discount,
        "avatar": client_row["avatar"] if client_row and client_row["avatar"] else None,
    }


@app.get("/profile/by-phone/{phone}")
async def get_profile_by_phone(phone: str):
    phone = norm_phone(phone)
    phone_bare = phone.lstrip('+')
    digits = ''.join(filter(str.isdigit, phone))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM clients WHERE regexp_replace(phone, '[^0-9]', '', 'g') = %s LIMIT 1", (digits,))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT user_id, phone, name FROM orders WHERE regexp_replace(phone, '[^0-9]', '', 'g') = %s ORDER BY created_at DESC LIMIT 1", (digits,))
                row = cur.fetchone()
    if not row:
        return {"ok": False}
    return {"ok": True, "user_id": row["user_id"], "phone": row.get("phone"), "name": row.get("name")}


@app.get("/suggest/address")
async def suggest_address(q: str):
    if not q or len(q) < 2:
        return {"suggestions": []}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address",
                headers={"Authorization": f"Token {DADATA_KEY}", "Content-Type": "application/json"},
                json={"query": q, "count": 5, "restrict_value": True, "locations": [{"city": "Луганск"}]},
                timeout=3.0,
            )
            data = r.json()
            def fmt(s: dict) -> str:
                d = s.get("data", {})
                parts = []
                street = d.get("street_with_type") or d.get("street")
                house = d.get("house")
                if street:
                    parts.append(street)
                if house:
                    parts.append(f"д {house}")
                return ", ".join(parts) if parts else s.get("value", "")
            suggestions = [fmt(s) for s in data.get("suggestions", []) if fmt(s)]
            return {"suggestions": suggestions}
        except Exception:
            return {"suggestions": []}


@app.get("/next-order-num")
def next_order_num_endpoint():
    return {"num": next_order_num_atomic()}


@app.get("/health")
def health():
    return {"ok": True}
