# ТГБОТ — Контекст проекта

## Токены и конфиги

```
BOT_TOKEN=8744671658:AAH7qnr_9ivwvZVGgOPd-yTN9VGh23dsrrM
CHAT_ID=2042550439
```

## Деплой

- **Фронтенд:** GitHub Pages — https://1ukoi1ik.github.io/ichiban-sushi-bot/
- **Бэкенд:** Railway — https://ichiban-sushi-bot-production.up.railway.app
- **Репо:** https://github.com/1ukoi1ik/ichiban-sushi-bot

## Файлы

- `index.html` — весь фронтенд (~2000 строк)
- `backend/server.py` — FastAPI бэкенд
- `backend/requirements.txt` — зависимости Python

## БД (Railway PostgreSQL)

Таблица `orders`: `order_num, step, name, phone, address, total, user_id, created_at`
