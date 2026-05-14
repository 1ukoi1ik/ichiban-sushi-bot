import os
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)


class OrderItem(BaseModel):
    name: str
    qty: int
    price: int


class Order(BaseModel):
    name: str
    phone: str
    address: str
    comment: str = ""
    payment: str
    items: List[OrderItem]
    total: int


def format_order(order: Order) -> str:
    lines = ["🍣 *Новый заказ — Ichiban Sushi*\n"]
    for item in order.items:
        lines.append(f"• {item.name} × {item.qty} — {item.price * item.qty:,} ₽")
    lines.append(f"\n💰 *Итого: {order.total:,} ₽*")
    lines.append(f"💳 Оплата: {order.payment}")
    lines.append(f"\n👤 {order.name}")
    lines.append(f"📱 {order.phone}")
    lines.append(f"📍 {order.address}")
    if order.comment:
        lines.append(f"💬 {order.comment}")
    return "\n".join(lines)


@app.post("/order")
async def receive_order(order: Order):
    text = format_order(order)
    async with httpx.AsyncClient() as client:
        await client.post(TG_API, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        })
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
