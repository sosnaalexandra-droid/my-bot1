"""Telegram-агент на Claude з інструментами (Tool Use).

Інструменти: calculate, get_datetime, save_note / list_notes / delete_note, read_url.
Також: обробка фото через Claude Vision.
Нотатки зберігаються у папці notes/, окремий файл на кожного user_id.
"""
import os
import re
import ast
import json
import base64
import asyncio
import logging
import operator
from datetime import datetime
from zoneinfo import ZoneInfo

# Прибираємо проксі-змінні, щоб не заважали запитам
for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(v, None)

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

# --- Налаштування ---
MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 1500
HISTORY_LIMIT = 20          # скільки повідомлень діалогу пам'ятати
MAX_TOOL_STEPS = 6          # запобіжник від зациклення інструментів
TZ = ZoneInfo("Europe/Kyiv")

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(HERE, "notes")
os.makedirs(NOTES_DIR, exist_ok=True)

SYSTEM_PROMPT = (
    "Ти — бізнес-асистент у Telegram. Допомагаєш підприємцю зі стратегією, "
    "задачами, фінансами, текстами та щоденною рутиною. Відповідай українською, "
    "конкретно й по суті, без зайвої води.\n\n"
    "У тебе є інструменти — користуйся ними, коли це доречно:\n"
    "— calculate: точні обчислення;\n"
    "— get_datetime: поточна дата і час (Київ);\n"
    "— save_note / list_notes / delete_note: нотатки користувача;\n"
    "— read_url: прочитати вміст веб-сторінки за посиланням.\n"
    "Коли користувач просить щось запам'ятати — збережи нотатку. "
    "Коли потрібні актуальні дані зі сторінки — прочитай її через read_url."
)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
history: dict[int, list[dict]] = {}


# ===================== Інструменти =====================

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Недопустимий вираз")


def tool_calculate(expression: str) -> str:
    try:
        result = _eval_node(ast.parse(expression, mode="eval").body)
        return f"{expression} = {result}"
    except Exception as e:
        return f"Не вдалося обчислити: {e}"


def tool_get_datetime() -> str:
    now = datetime.now(TZ)
    days = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
    return now.strftime("%Y-%m-%d %H:%M") + f" ({days[now.weekday()]}, Київ)"


def _notes_path(uid: int) -> str:
    return os.path.join(NOTES_DIR, f"{uid}.json")


def _load_notes(uid: int) -> list:
    path = _notes_path(uid)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_notes(uid: int, notes: list):
    with open(_notes_path(uid), "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)


def tool_save_note(uid: int, text: str) -> str:
    notes = _load_notes(uid)
    notes.append({"ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "text": text})
    _save_notes(uid, notes)
    return f"Нотатку збережено (всього: {len(notes)})."


def tool_list_notes(uid: int) -> str:
    notes = _load_notes(uid)
    if not notes:
        return "Нотаток поки немає."
    return "\n".join(f"{i}. [{n['ts']}] {n['text']}" for i, n in enumerate(notes, 1))


def tool_delete_note(uid: int, number: int) -> str:
    notes = _load_notes(uid)
    if not notes:
        return "Нотаток немає."
    if not isinstance(number, int) or number < 1 or number > len(notes):
        return f"Невірний номер. Доступно 1..{len(notes)}."
    removed = notes.pop(number - 1)
    _save_notes(uid, notes)
    return f"Видалено нотатку {number}: {removed['text']}"


def tool_read_url(url: str) -> str:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        r = httpx.get(
            url, timeout=20, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClaudeAgentBot/1.0)"},
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        return text[:3000] if text else "Сторінка порожня або без тексту."
    except Exception as e:
        return f"Не вдалося прочитати сторінку: {e}"


TOOLS = [
    {
        "name": "calculate",
        "description": "Точно обчислює арифметичний вираз (+,-,*,/,**,%,//).",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "Напр. '1250*0.2+300'"}},
            "required": ["expression"],
        },
    },
    {
        "name": "get_datetime",
        "description": "Повертає поточну дату, час і день тижня (часовий пояс Київ).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_note",
        "description": "Зберігає нотатку користувача.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Текст нотатки"}},
            "required": ["text"],
        },
    },
    {
        "name": "list_notes",
        "description": "Повертає всі нотатки користувача з номерами.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_note",
        "description": "Видаляє нотатку за її номером зі списку.",
        "input_schema": {
            "type": "object",
            "properties": {"number": {"type": "integer", "description": "Номер нотатки"}},
            "required": ["number"],
        },
    },
    {
        "name": "read_url",
        "description": "Завантажує веб-сторінку і повертає її текст (до 3000 символів).",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Посилання на сторінку"}},
            "required": ["url"],
        },
    },
]


def execute_tool(name: str, tool_input: dict, uid: int) -> str:
    try:
        if name == "calculate":
            return tool_calculate(str(tool_input.get("expression", "")))
        if name == "get_datetime":
            return tool_get_datetime()
        if name == "save_note":
            return tool_save_note(uid, str(tool_input.get("text", "")).strip())
        if name == "list_notes":
            return tool_list_notes(uid)
        if name == "delete_note":
            return tool_delete_note(uid, tool_input.get("number"))
        if name == "read_url":
            return tool_read_url(str(tool_input.get("url", "")))
        return f"Невідомий інструмент: {name}"
    except Exception as e:
        log.exception("Tool %s failed", name)
        return f"Помилка інструменту {name}: {e}"


# ===================== Агентний цикл =====================

def _run_agent_sync(messages: list, uid: int) -> str:
    """Виконує цикл Tool Use і повертає фінальний текст відповіді."""
    for _ in range(MAX_TOOL_STEPS):
        resp = claude.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
            tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip() \
                or "Готово."
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = execute_tool(block.name, block.input or {}, uid)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })
        messages.append({"role": "user", "content": tool_results})
    return "Забагато кроків із інструментами, спробуй переформулювати запит."


async def run_agent(messages: list, uid: int) -> str:
    return await asyncio.to_thread(_run_agent_sync, messages, uid)


# ===================== Хендлери =====================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Вітаю! Я ваш бізнес-асистент з інструментами. 🤖\n"
        "Вмію: рахувати, зберігати нотатки, підказувати дату й час, читати веб-сторінки "
        "за посиланням і розбирати надіслані фото.\n\n"
        "Команди: /help — можливості, /notes — мої нотатки, /reset — очистити діалог."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Що я вмію:\n"
        "• Відповідати на бізнес-питання, стратегія й тексти\n"
        "• 🧮 Рахувати (просто попроси порахувати)\n"
        "• 📝 Зберігати нотатки: «запам'ятай ...», перегляд — /notes\n"
        "• 🕒 Казати дату й час\n"
        "• 🔗 Читати сторінку за посиланням\n"
        "• 🖼 Розбирати надіслані фото\n\n"
        "Команди:\n"
        "/start — привітання\n"
        "/help — ця довідка\n"
        "/notes — показати нотатки\n"
        "/reset — очистити поточний діалог"
    )


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    history.pop(update.effective_user.id, None)
    await update.message.reply_text("Діалог очищено. Починаємо заново 🙂")


async def notes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Твої нотатки:\n" + tool_list_notes(update.effective_user.id))


async def chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_text = update.message.text
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    working = history.get(uid, []) + [{"role": "user", "content": user_text}]
    try:
        answer = await run_agent(working, uid)
        hist = history.setdefault(uid, [])
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": answer})
        history[uid] = hist[-HISTORY_LIMIT:]
        await update.message.reply_text(answer)
    except Exception:
        log.exception("chat error")
        await update.message.reply_text("Упс, щось пішло не так. Спробуй ще раз.")


async def photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    caption = update.message.caption or "Що на цьому фото? Коротко опиши й підкажи, чим це корисно для бізнесу."
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        tg_file = await update.message.photo[-1].get_file()
        buf = await tg_file.download_as_bytearray()
        b64 = base64.standard_b64encode(bytes(buf)).decode()
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": caption},
        ]
        working = history.get(uid, []) + [{"role": "user", "content": content}]
        answer = await run_agent(working, uid)
        hist = history.setdefault(uid, [])
        hist.append({"role": "user", "content": f"[фото] {caption}"})
        hist.append({"role": "assistant", "content": answer})
        history[uid] = hist[-HISTORY_LIMIT:]
        await update.message.reply_text(answer)
    except Exception:
        log.exception("photo error")
        await update.message.reply_text("Не вдалося опрацювати фото. Спробуй ще раз.")


def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    log.info("Agent bot started with tools")
    app.run_polling()


if __name__ == "__main__":
    main()
