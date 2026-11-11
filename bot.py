#!/usr/bin/env python3
"""
Телеграм-бот для учёта денег: расходы, доходы, долги, итоги.

Запуск:
    1. Получите токен у @BotFather (команда /newbot).
    2. Вставьте его в строку TOKEN ниже (или задайте переменную окружения BOT_TOKEN).
    3. python bot.py

Никаких библиотек ставить не нужно — только Python 3.8+.
Данные хранятся в файле money.db рядом со скриптом
(на хостинге Bothost — в /app/data/money.db).
Токен можно не вписывать в файл: бот берёт его из переменной BOT_TOKEN.
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ───────────────────────── Настройки ─────────────────────────

TOKEN = os.environ.get("BOT_TOKEN", "").strip() or "ВСТАВЬТЕ_ТОКЕН_СЮДА"
TZ = timezone(timedelta(hours=3))          # Москва, UTC+3
CURRENCY = "₽"

def _db_path():
    """Где лежит база. На хостинге (Bothost) — в /app/data, чтобы не стиралась при обновлениях."""
    explicit = os.environ.get("DATABASE_PATH") or os.environ.get("DB_PATH")
    if explicit:
        folder = os.path.dirname(explicit)
        if folder:
            os.makedirs(folder, exist_ok=True)
        return explicit
    if os.path.isdir("/app"):
        try:
            os.makedirs("/app/data", exist_ok=True)
            return "/app/data/money.db"
        except OSError:
            pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "money.db")


DB_PATH = _db_path()

# ───────────────────────── База данных ─────────────────────────

db = sqlite3.connect(DB_PATH)
db.execute(
    """CREATE TABLE IF NOT EXISTS entries (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ts      TEXT    NOT NULL,   -- ISO-время записи
        kind    TEXT    NOT NULL,   -- 'exp' расход, 'inc' доход, 'debt' долг, 'rb' шаблон «я рб»
        amount  REAL    NOT NULL,   -- для долга: + мне должны, − я должен
        person  TEXT,               -- только для долгов
        note    TEXT    NOT NULL DEFAULT ''
    )"""
)
db.execute("CREATE INDEX IF NOT EXISTS idx_user_ts ON entries (user_id, ts)")
db.commit()


def add_entry(user_id, kind, amount, note="", person=None):
    db.execute(
        "INSERT INTO entries (user_id, ts, kind, amount, person, note) VALUES (?,?,?,?,?,?)",
        (user_id, datetime.now(TZ).isoformat(timespec="seconds"), kind, amount, person, note),
    )
    db.commit()


# ───────────────────────── Разбор сумм ─────────────────────────

# 250 · 1 500 · 99,90 · 25к · 1.5k · 3 тыс · 2млн
AMOUNT_RE = (
    r"(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?"
    r"(?:\s?(?:к|k|тыс\.?|млн)(?![a-zа-яё]))?"
)
SIGN_RE = r"[+\-]?"

EXPENSE_RE = re.compile(rf"^({SIGN_RE})\s*({AMOUNT_RE})(?:\s+(.*))?$", re.I | re.S)
EXPENSE_REV_RE = re.compile(rf"^(.*\S)\s+({SIGN_RE})({AMOUNT_RE})$", re.I | re.S)
DEBT_RE = re.compile(
    rf"^(долг|мне\s+долж(?:ен|на|ны)|я\s+долж(?:ен|на))\s+"
    rf"(.+?)\s+({SIGN_RE})\s*({AMOUNT_RE})(?:\s+(.*))?$",
    re.I | re.S,
)


def parse_amount(raw):
    s = raw.lower().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    mult = 1
    for suffix, m in (("млн", 1_000_000), ("тыс.", 1000), ("тыс", 1000), ("к", 1000), ("k", 1000)):
        if s.endswith(suffix):
            s, mult = s[: -len(suffix)], m
            break
    return round(float(s) * mult, 2)


def money(x):
    x = round(x, 2)
    text = f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"
    return text.replace(",", " ").replace("-", "−") + " " + CURRENCY


# ───────────────────────── Отчёты ─────────────────────────

def period_start(period):
    now = datetime.now(TZ)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        day -= timedelta(days=day.weekday())
    elif period == "month":
        day = day.replace(day=1)
    return day.isoformat(timespec="seconds")


def spent_since(user_id, since):
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM entries WHERE user_id=? AND kind='exp' AND ts>=?",
        (user_id, since),
    ).fetchone()
    return row[0]


def report(user_id, period):
    titles = {"today": "📅 Сегодня", "week": "🗓 Эта неделя", "month": "📊 Этот месяц"}
    since = period_start(period)
    rows = db.execute(
        "SELECT kind, amount, note FROM entries "
        "WHERE user_id=? AND kind IN ('exp','inc') AND ts>=?",
        (user_id, since),
    ).fetchall()
    if not rows:
        return f"{titles[period]}\nЗаписей пока нет."

    spent = sum(a for k, a, _ in rows if k == "exp")
    earned = sum(a for k, a, _ in rows if k == "inc")
    lines = [titles[period], f"Расходы: {money(spent)}", f"Доходы: {money(earned)}"]
    if earned:
        lines.append(f"Баланс: {money(earned - spent)}")

    by_note = {}
    for kind, amount, note in rows:
        if kind == "exp":
            key = (note or "без описания").lower()
            by_note[key] = by_note.get(key, 0) + amount
    if by_note:
        lines.append("")
        lines.append("На что ушло:")
        top = sorted(by_note.items(), key=lambda kv: -kv[1])
        for note, amount in top[:15]:
            lines.append(f"• {note} — {money(amount)}")
        if len(top) > 15:
            rest = sum(a for _, a in top[15:])
            lines.append(f"• остальное — {money(rest)}")
    return "\n".join(lines)


def debt_balances(user_id):
    """Возвращает [(имя, баланс)], баланс > 0 — должны мне."""
    rows = db.execute(
        "SELECT person, amount FROM entries WHERE user_id=? AND kind='debt' ORDER BY id",
        (user_id,),
    ).fetchall()
    names, totals = {}, {}
    for person, amount in rows:
        key = person.lower()
        names[key] = person
        totals[key] = totals.get(key, 0) + amount
    return [(names[k], round(v, 2)) for k, v in totals.items() if abs(v) >= 0.005]


def debt_line(person, balance):
    if balance > 0:
        return f"{person} → вам: {money(balance)}"
    return f"Вы → {person}: {money(-balance)}"


def open_debts(user_id):
    """Незакрытые долги: [(имя, баланс, [(ts, сумма, за что), ...])].

    Когда баланс с человеком обнуляется, старые записи по нему считаются закрытыми.
    """
    rows = db.execute(
        "SELECT ts, person, amount, note FROM entries "
        "WHERE user_id=? AND kind='debt' ORDER BY id",
        (user_id,),
    ).fetchall()
    people = {}
    for ts, person, amount, note in rows:
        p = people.setdefault(person.lower(), {"name": person, "balance": 0.0, "items": []})
        p["name"] = person
        p["balance"] = round(p["balance"] + amount, 2)
        p["items"].append((ts, amount, note))
        if abs(p["balance"]) < 0.005:
            p["balance"], p["items"] = 0.0, []
    return [(p["name"], p["balance"], p["items"]) for p in people.values() if p["balance"]]


def debts_report(user_id):
    debts = open_debts(user_id)
    if not debts:
        return "🤝 Долгов нет."

    def section(title, people, sign):
        total = sum(abs(b) for _, b, _ in people)
        out = [f"{title}: {money(total)}"]
        for name, balance, items in sorted(people, key=lambda p: -abs(p[1])):
            out.append(f"• {name} — {money(abs(balance))}")
            for ts, amount, note in items:
                when = datetime.fromisoformat(ts).strftime("%d.%m")
                # в разделе «Я должен» знак переворачиваем: + значит «занял ещё»
                shown = amount * sign
                mark = "+" if shown > 0 else "−"
                what = note or ("без описания" if shown > 0 else "возврат")
                out.append(f"    {when}  {mark}{money(abs(shown))} — {what}")
        return out

    to_me = [d for d in debts if d[1] > 0]
    by_me = [d for d in debts if d[1] < 0]
    lines = []
    if to_me:
        lines += section("💰 Мне должны", to_me, 1)
    if by_me:
        if lines:
            lines.append("")
        lines += section("💸 Я должен", by_me, -1)
    return "\n".join(lines)


def describe(kind, amount, person, note):
    if kind == "exp":
        return f"➖ {money(amount)}" + (f" — {note}" if note else "")
    if kind == "inc":
        return f"➕ {money(amount)}" + (f" — {note}" if note else "")
    if kind == "rb":
        return f"📝 Я рб — итог {_num(amount)}" + (f" ({note})" if note else "")
    sign = "+" if amount > 0 else "−"
    return f"🤝 {person} {sign}{money(abs(amount))}" + (f" — {note}" if note else "")


def history(user_id, limit=10):
    rows = db.execute(
        "SELECT ts, kind, amount, person, note FROM entries "
        "WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    if not rows:
        return "Записей пока нет."
    lines = ["🧾 Последние записи"]
    for ts, kind, amount, person, note in rows:
        when = datetime.fromisoformat(ts).strftime("%d.%m %H:%M")
        lines.append(f"{when}  {describe(kind, amount, person, note)}")
    return "\n".join(lines)


def undo(user_id):
    row = db.execute(
        "SELECT id, kind, amount, person, note FROM entries "
        "WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not row:
        return "Отменять нечего."
    db.execute("DELETE FROM entries WHERE id=?", (row[0],))
    db.commit()
    return "Удалил: " + describe(*row[1:])


# ───────────────────────── Шаблон «я рб» ─────────────────────────

YA_RB_RE = re.compile(r"^я\s*рб(?![а-яё])\s*(.*)$", re.I | re.S)
YA_RB_TOOK_RE = re.compile(rf"взял\w*\s*-?\s*({AMOUNT_RE})", re.I)
YA_RB_CALC_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)")


def _num(x):
    x = round(x, 2)
    return str(int(x)) if x == int(x) else f"{x:.2f}".rstrip("0")


YA_RB_PERSON = "РБ"   # на кого записывается долг из шаблона


def ya_rb(rest, user_id=None):
    """«я рб» → готовый шаблон. Числа можно заменить: «я рб взял 30к, 50-10»."""
    took, a, b = "25к", 42.0, 25.0
    m = YA_RB_TOOK_RE.search(rest)
    if m:
        took = re.sub(r"\s+", "", m.group(1)).lower()
        rest = rest[: m.start()] + " " + rest[m.end():]
    m = YA_RB_CALC_RE.search(rest)
    if m:
        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
    total = a - b
    now = datetime.now(TZ)
    if user_id is not None:
        if total:
            add_entry(
                user_id, "debt", total,
                f"я рб: взял −{took}, {_num(a)} − {_num(b)}", YA_RB_PERSON,
            )
    return "\n".join(
        [
            f"Я рб — {now:%d.%m}",
            f"Дата: {now:%d.%m.%Y}",
            f"Взял −{took}",
            f"{_num(a)} − {_num(b)} = {_num(total)}",
            f"Итог: {_num(total)}",
        ]
    )


# ───────────────────────── Обработка сообщений ─────────────────────────

HELP = (
    "Я веду учёт денег. Просто пишите мне:\n\n"
    "💸 Расход: 250 кофе\n"
    "💰 Доход: +50к зарплата\n"
    "🤝 Мне должны: мне должен Никита 7000 за ремонт\n"
    "💸 Я должен: я должен Вася 1500 за обед\n"
    "↩️ Вернули часть: долг Никита -3000 вернул\n"
    "↩️ Я вернул: долг Вася 1500 вернул\n\n"
    "Суммы можно писать как 1500, 1 500, 1.5к или 2 тыс.\n\n"
    "📝 Шаблон: я рб (или со своими числами: я рб взял 30к, 50-10)\n\n"
    "Команды:\n"
    "/today — итоги за сегодня\n"
    "/week — за неделю\n"
    "/month — за месяц\n"
    "/debts — кто, сколько и за что должен\n"
    "/last — последние записи\n"
    "/undo — отменить последнюю запись"
)

KEYBOARD = {
    "keyboard": [
        ["📅 Сегодня", "🗓 Неделя", "📊 Месяц"],
        ["🤝 Долги", "🧾 История", "↩️ Отменить"],
    ],
    "resize_keyboard": True,
}

COMMANDS = {
    "/start": "help", "/help": "help", "помощь": "help",
    "/today": "today", "сегодня": "today",
    "/week": "week", "неделя": "week",
    "/month": "month", "месяц": "month",
    "/debts": "debts", "долги": "debts",
    "/last": "last", "история": "last",
    "/undo": "undo", "отменить": "undo", "отмена": "undo",
}


def handle(user_id, text):
    """Принимает текст сообщения, возвращает текст ответа."""
    text = text.strip().replace("−", "-").replace("–", "-").replace("—", "-")
    if not text:
        return HELP

    # Команды и кнопки (у кнопок в начале эмодзи — убираем его)
    word = re.sub(r"^[^\w/]+", "", text).lower().split("@")[0].strip()
    action = COMMANDS.get(word)
    if action == "help":
        return HELP
    if action in ("today", "week", "month"):
        return report(user_id, action)
    if action == "debts":
        return debts_report(user_id)
    if action == "last":
        return history(user_id)
    if action == "undo":
        return undo(user_id)

    # Шаблон «я рб»
    m = YA_RB_RE.match(text)
    if m:
        reply = ya_rb(m.group(1), user_id)
        balance = next(
            (b for p, b in debt_balances(user_id) if p.lower() == YA_RB_PERSON.lower()), 0
        )
        status = debt_line(YA_RB_PERSON, balance) if balance else f"{YA_RB_PERSON}: в расчёте ✅"
        return f"{reply}\n\n🤝 Записал итог в долги\n{status}"

    # Долг: «долг Никита 7000 [комментарий]»
    m = DEBT_RE.match(text)
    if m:
        prefix = m.group(1).lower()
        person, sign, raw, note = m.group(2).strip(), m.group(3), m.group(4), m.group(5) or ""
        amount = parse_amount(raw)
        if amount == 0:
            return "Сумма должна быть больше нуля."
        if sign == "-":
            amount = -amount
        if prefix.startswith("я"):   # «я должен …» — долг мой
            amount = -amount
        add_entry(user_id, "debt", amount, note.strip(), person)
        balance = next(
            (b for p, b in debt_balances(user_id) if p.lower() == person.lower()), 0
        )
        status = debt_line(person, balance) if balance else f"{person}: в расчёте ✅"
        return f"Записал: {describe('debt', amount, person, note.strip())}\n{status}"
    if text.lower().startswith(("долг", "я долж", "мне долж")):
        return (
            "Формат: кто, сколько и за что\n"
            "мне должен Никита 7000 за ремонт\n"
            "я должен Вася 1500 за обед"
        )

    # Расход или доход: «250 кофе», «+50к зарплата», «кофе 250»
    m = EXPENSE_RE.match(text)
    if m:
        sign, raw, note = m.group(1), m.group(2), (m.group(3) or "").strip()
    else:
        m = EXPENSE_REV_RE.match(text)
        if not m:
            return "Не понял 🤔 Напишите, например: 250 кофе\nВсе возможности — /help"
        note, sign, raw = m.group(1).strip(), m.group(2), m.group(3)

    amount = parse_amount(raw)
    if amount == 0:
        return "Сумма должна быть больше нуля."
    if sign == "+":
        add_entry(user_id, "inc", amount, note)
        return "Записал: " + describe("inc", amount, None, note)
    add_entry(user_id, "exp", amount, note)
    today = spent_since(user_id, period_start("today"))
    return f"Записал: {describe('exp', amount, None, note)}\nЗа сегодня потрачено: {money(today)}"


# ───────────────────────── Telegram API ─────────────────────────

API_URL = "https://api.telegram.org/bot{token}/{method}"


def api(method, http_timeout=20, **params):
    data = urllib.parse.urlencode(params).encode()
    url = API_URL.format(token=TOKEN, method=method)
    with urllib.request.urlopen(url, data, timeout=http_timeout) as resp:
        return json.loads(resp.read().decode())["result"]


def main():
    if not TOKEN or "ВСТАВЬТЕ" in TOKEN:
        sys.exit("Укажите токен бота: в строке TOKEN или в переменной окружения BOT_TOKEN.")

    try:
        me = api("getMe")
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            sys.exit("Telegram не принял токен. Проверьте, что скопировали его целиком.")
        raise
    api(
        "setMyCommands",
        commands=json.dumps(
            [
                {"command": "today", "description": "Итоги за сегодня"},
                {"command": "week", "description": "Итоги за неделю"},
                {"command": "month", "description": "Итоги за месяц"},
                {"command": "debts", "description": "Кто кому должен"},
                {"command": "last", "description": "Последние записи"},
                {"command": "undo", "description": "Отменить последнюю запись"},
                {"command": "help", "description": "Как пользоваться"},
            ]
        ),
    )
    print(f"Бот @{me['username']} запущен. База: {DB_PATH}. Остановить: Ctrl+C", flush=True)

    offset = 0
    while True:
        try:
            updates = api(
                "getUpdates",
                http_timeout=70,
                offset=offset,
                timeout=50,
                allowed_updates=json.dumps(["message"]),
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:  # сеть моргнула — подождём и продолжим
            print("Ошибка связи:", e)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = message.get("text")
            if not text or "from" not in message:
                continue
            try:
                reply = handle(message["from"]["id"], text)
            except Exception as e:
                print("Ошибка обработки:", repr(e))
                reply = "Что-то пошло не так, запись не сохранена. Попробуйте ещё раз."
            try:
                api(
                    "sendMessage",
                    chat_id=message["chat"]["id"],
                    text=reply,
                    reply_markup=json.dumps(KEYBOARD),
                )
            except Exception as e:
                print("Не удалось отправить ответ:", e)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nБот остановлен.")
