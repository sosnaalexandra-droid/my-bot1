"""Telegram-агент на Claude з інструментами (Tool Use).

Інструменти: calculate, get_datetime, save_note / list_notes / delete_note,
read_url, set_reminder / list_reminders / delete_reminder.
Також: обробка фото через Claude Vision.
Нотатки й нагадування зберігаються у папках notes/ та reminders/ (окремий файл на user_id).
Нагадування надсилаються автоматично через JobQueue (перевірка щохвилини).
"""
import os
import re
import ast
import json
import time
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
MAX_TOKENS = 4000
TG_LIMIT = 3900  # запас до ліміту Telegram у 4096 символів
HISTORY_LIMIT = 20
MAX_TOOL_STEPS = 6
TZ = ZoneInfo("Europe/Kyiv")
WHEN_FMT = "%Y-%m-%d %H:%M"

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(HERE, "notes")
REM_DIR = os.path.join(HERE, "reminders")
os.makedirs(NOTES_DIR, exist_ok=True)
os.makedirs(REM_DIR, exist_ok=True)

# --- Instagram Graph API (Meta). Значення беруться з .env на сервері ---
IG_VERSION = os.environ.get("IG_API_VERSION", "v21.0")
IG_TOKEN = os.environ.get("INSTAGRAM_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_BASE = f"https://graph.facebook.com/{IG_VERSION}"
IG_NOT_CONFIGURED = (
    "Instagram ще не підключений. У файлі .env на сервері потрібні INSTAGRAM_TOKEN "
    "та IG_USER_ID (їх видає Meta for Developers). Попроси користувача налаштувати підключення."
)

SYSTEM_PROMPT = (
    "Ти — бізнес-асистент у Telegram. Допомагаєш підприємцю зі стратегією, "
    "задачами, фінансами, текстами та щоденною рутиною. Відповідай українською, "
    "конкретно й по суті, без зайвої води.\n\n"
    "У тебе є інструменти — користуйся ними, коли це доречно:\n"
    "— calculate: точні обчислення;\n"
    "— get_datetime: поточна дата і час (Київ);\n"
    "— save_note / list_notes / delete_note: нотатки користувача;\n"
    "— set_reminder / list_reminders / delete_reminder: нагадування;\n"
    "— read_url: прочитати вміст веб-сторінки за посиланням.\n\n"
    "Важливо про нагадування: час завжди передавай в абсолютному форматі "
    "'РРРР-ММ-ДД ГГ:ХХ' за київським часом. Якщо користувач каже відносний час "
    "('завтра о 9', 'через годину') — спершу виклич get_datetime, порахуй точний "
    "момент і лише тоді виклич set_reminder.\n\n"
    "Коли просять проаналізувати Instagram — працюй як топовий SMM-стратег. "
    "Акаунтів може бути декілька: спершу виклич instagram_accounts. Якщо користувач "
    "не вказав конкретний — покажи список і спитай, який аналізувати (або, якщо він "
    "просить усі, пройдись по кожному). Далі збери дані через instagram_profile, "
    "instagram_recent_media та instagram_insights, передаючи потрібний account "
    "(для чужих акаунтів — instagram_competitor). Порахуй ER "
    "(середні (лайки+коментарі)/підписники × 100%), оціни контент-мікс і типи постів, "
    "частоту й регулярність, частку Reels. Дай конкретний аудит: 3 сильні сторони, "
    "3 слабкі, і покроковий план зростання на 2 тижні з ідеями Reels та рубриками. "
    "Спирайся лише на реальні цифри з інструментів; якщо даних бракує — скажи, яких саме."
)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
history: dict[int, list[dict]] = {}


# ===================== Допоміжне: сховище JSON =====================

def _load_json(path: str) -> list:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_json(path: str, data: list):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
    return now.strftime(WHEN_FMT) + f" ({days[now.weekday()]}, Київ)"


# --- Нотатки ---
def _notes_path(uid: int) -> str:
    return os.path.join(NOTES_DIR, f"{uid}.json")


def tool_save_note(uid: int, text: str) -> str:
    notes = _load_json(_notes_path(uid))
    notes.append({"ts": datetime.now(TZ).strftime(WHEN_FMT), "text": text})
    _save_json(_notes_path(uid), notes)
    return f"Нотатку збережено (всього: {len(notes)})."


def tool_list_notes(uid: int) -> str:
    notes = _load_json(_notes_path(uid))
    if not notes:
        return "Нотаток поки немає."
    return "\n".join(f"{i}. [{n['ts']}] {n['text']}" for i, n in enumerate(notes, 1))


def tool_delete_note(uid: int, number) -> str:
    notes = _load_json(_notes_path(uid))
    if not notes:
        return "Нотаток немає."
    if not isinstance(number, int) or number < 1 or number > len(notes):
        return f"Невірний номер. Доступно 1..{len(notes)}."
    removed = notes.pop(number - 1)
    _save_json(_notes_path(uid), notes)
    return f"Видалено нотатку {number}: {removed['text']}"


# --- Нагадування ---
def _rem_path(uid: int) -> str:
    return os.path.join(REM_DIR, f"{uid}.json")


def _parse_when(s: str):
    return datetime.strptime(s, WHEN_FMT).replace(tzinfo=TZ)


def tool_set_reminder(uid: int, chat_id: int, when: str, text: str) -> str:
    try:
        dt = _parse_when(when)
    except Exception:
        return "Невірний формат часу. Потрібен 'РРРР-ММ-ДД ГГ:ХХ'."
    if dt <= datetime.now(TZ):
        return "Цей час уже минув. Вкажи майбутній момент."
    rems = _load_json(_rem_path(uid))
    rems.append({
        "id": datetime.now(TZ).strftime("%Y%m%d%H%M%S%f"),
        "when": when, "text": text, "chat_id": chat_id,
    })
    _save_json(_rem_path(uid), rems)
    return f"Нагадування поставлено на {when}: {text}"


def tool_list_reminders(uid: int) -> str:
    rems = sorted(_load_json(_rem_path(uid)), key=lambda r: r["when"])
    if not rems:
        return "Активних нагадувань немає."
    return "\n".join(f"{i}. [{r['when']}] {r['text']}" for i, r in enumerate(rems, 1))


def tool_delete_reminder(uid: int, number) -> str:
    rems = sorted(_load_json(_rem_path(uid)), key=lambda r: r["when"])
    if not rems:
        return "Нагадувань немає."
    if not isinstance(number, int) or number < 1 or number > len(rems):
        return f"Невірний номер. Доступно 1..{len(rems)}."
    removed = rems.pop(number - 1)
    _save_json(_rem_path(uid), rems)
    return f"Видалено нагадування: {removed['text']}"


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


# --- Instagram Graph API (підтримка кількох акаунтів) ---
def _ig_ready() -> bool:
    return bool(IG_TOKEN)


def _ig_get(path: str, params: dict) -> dict:
    q = dict(params or {})
    q["access_token"] = IG_TOKEN
    r = httpx.get(f"{IG_BASE}/{path}", params=q, timeout=25)
    data = r.json()
    if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
        msg = data.get("error", {}).get("message", r.text) if isinstance(data, dict) else r.text
        raise RuntimeError(msg)
    return data


def _trim(s: str, limit: int = 3500) -> str:
    return s if len(s) <= limit else s[:limit] + " …(обрізано)"


_ig_accounts_cache = {"ts": 0.0, "data": []}


def _ig_accounts() -> list:
    """Усі Instagram-бізнес-акаунти, доступні за токеном (через сторінки FB). Кеш 5 хв."""
    if _ig_accounts_cache["data"] and time.time() - _ig_accounts_cache["ts"] < 300:
        return _ig_accounts_cache["data"]
    d = _ig_get("me/accounts", {"fields": "name,instagram_business_account{id,username}", "limit": 50})
    accts = []
    for p in d.get("data", []):
        iba = p.get("instagram_business_account")
        if iba and iba.get("id"):
            accts.append({"page": p.get("name"), "ig_id": iba["id"], "username": iba.get("username")})
    if not accts and IG_USER_ID:  # запасний варіант — один явно заданий акаунт
        accts.append({"page": None, "ig_id": IG_USER_ID, "username": None})
    _ig_accounts_cache.update(ts=time.time(), data=accts)
    return accts


def _resolve_ig_id(selector) -> str:
    accts = _ig_accounts()
    if not accts:
        raise RuntimeError("За цим токеном не знайдено жодного Instagram-бізнес-акаунта.")
    if not selector:
        if len(accts) > 1:
            names = ", ".join(a.get("username") or a.get("page") or a["ig_id"] for a in accts)
            raise RuntimeError(f"Акаунтів декілька: {names}. Уточни, який саме.")
        return accts[0]["ig_id"]
    s = str(selector).strip().lstrip("@").lower()
    for a in accts:
        if (a.get("username") or "").lower() == s or (a.get("page") or "").lower() == s:
            return a["ig_id"]
    if s.isdigit() and 1 <= int(s) <= len(accts):
        return accts[int(s) - 1]["ig_id"]
    names = ", ".join(a.get("username") or a.get("page") or a["ig_id"] for a in accts)
    raise RuntimeError(f"Акаунт '{selector}' не знайдено. Доступні: {names}")


def tool_ig_accounts() -> str:
    if not _ig_ready():
        return IG_NOT_CONFIGURED
    try:
        accts = _ig_accounts()
        if not accts:
            return "За цим токеном не знайдено Instagram-бізнес-акаунтів."
        return _trim(json.dumps(accts, ensure_ascii=False))
    except Exception as e:
        return f"Помилка Instagram API (accounts): {e}"


def tool_ig_profile(account=None) -> str:
    if not _ig_ready():
        return IG_NOT_CONFIGURED
    try:
        ig_id = _resolve_ig_id(account)
        d = _ig_get(ig_id, {"fields": "username,name,biography,followers_count,follows_count,media_count"})
        return _trim(json.dumps(d, ensure_ascii=False))
    except Exception as e:
        return f"Помилка Instagram API (profile): {e}"


def tool_ig_recent_media(account=None, limit=24) -> str:
    if not _ig_ready():
        return IG_NOT_CONFIGURED
    try:
        ig_id = _resolve_ig_id(account)
        n = max(1, min(int(limit or 24), 50))
        d = _ig_get(f"{ig_id}/media", {
            "fields": "caption,media_type,media_product_type,timestamp,like_count,comments_count",
            "limit": n,
        })
        items = []
        for it in d.get("data", []):
            items.append({
                "date": (it.get("timestamp") or "")[:10],
                "type": it.get("media_product_type") or it.get("media_type"),
                "likes": it.get("like_count"),
                "comments": it.get("comments_count"),
                "caption": (it.get("caption") or "")[:80],
            })
        return _trim(json.dumps(items, ensure_ascii=False), 6000)
    except Exception as e:
        return f"Помилка Instagram API (media): {e}"


def tool_ig_insights(account=None) -> str:
    if not _ig_ready():
        return IG_NOT_CONFIGURED
    try:
        ig_id = _resolve_ig_id(account)
        d = _ig_get(f"{ig_id}/insights", {"metric": "reach", "period": "days_28", "metric_type": "total_value"})
        return _trim(json.dumps(d.get("data", d), ensure_ascii=False))
    except Exception as e:
        return f"Помилка Instagram API (insights): {e}"


def tool_ig_competitor(username: str) -> str:
    if not _ig_ready():
        return IG_NOT_CONFIGURED
    uname = re.sub(r"[^A-Za-z0-9._]", "", str(username or ""))
    if not uname:
        return "Вкажи коректний username (латиниця, крапки, підкреслення)."
    try:
        accts = _ig_accounts()
        if not accts:
            raise RuntimeError("Немає доступного Instagram-акаунта для запиту.")
        node_id = accts[0]["ig_id"]
        fields = (f"business_discovery.username({uname})"
                  "{username,followers_count,media_count,"
                  "media.limit(10){like_count,comments_count,caption,media_type,timestamp}}")
        d = _ig_get(node_id, {"fields": fields})
        return _trim(json.dumps(d.get("business_discovery", d), ensure_ascii=False))
    except Exception as e:
        return f"Помилка Instagram API (competitor): {e}"


TOOLS = [
    {"name": "calculate",
     "description": "Точно обчислює арифметичний вираз (+,-,*,/,**,%,//).",
     "input_schema": {"type": "object",
                      "properties": {"expression": {"type": "string", "description": "Напр. '1250*0.2+300'"}},
                      "required": ["expression"]}},
    {"name": "get_datetime",
     "description": "Повертає поточну дату, час і день тижня (часовий пояс Київ).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "save_note",
     "description": "Зберігає нотатку користувача.",
     "input_schema": {"type": "object",
                      "properties": {"text": {"type": "string"}},
                      "required": ["text"]}},
    {"name": "list_notes",
     "description": "Повертає всі нотатки користувача з номерами.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "delete_note",
     "description": "Видаляє нотатку за її номером.",
     "input_schema": {"type": "object",
                      "properties": {"number": {"type": "integer"}},
                      "required": ["number"]}},
    {"name": "set_reminder",
     "description": "Ставить нагадування: бот сам надішле повідомлення у вказаний час.",
     "input_schema": {"type": "object",
                      "properties": {"when": {"type": "string", "description": "Час 'РРРР-ММ-ДД ГГ:ХХ' (Київ)"},
                                     "text": {"type": "string", "description": "Про що нагадати"}},
                      "required": ["when", "text"]}},
    {"name": "list_reminders",
     "description": "Показує активні нагадування користувача з номерами.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "delete_reminder",
     "description": "Видаляє нагадування за його номером.",
     "input_schema": {"type": "object",
                      "properties": {"number": {"type": "integer"}},
                      "required": ["number"]}},
    {"name": "read_url",
     "description": "Завантажує веб-сторінку і повертає її текст (до 3000 символів).",
     "input_schema": {"type": "object",
                      "properties": {"url": {"type": "string"}},
                      "required": ["url"]}},
    {"name": "instagram_accounts",
     "description": "Список усіх підключених Instagram-акаунтів (username та сторінка). Виклич першим, якщо акаунтів декілька.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "instagram_profile",
     "description": "Профіль акаунта: username, біо, підписники, кількість постів. Якщо акаунтів кілька — вкажи account (username).",
     "input_schema": {"type": "object",
                      "properties": {"account": {"type": "string", "description": "username або назва акаунта"}}}},
    {"name": "instagram_recent_media",
     "description": "Останні пости акаунта з лайками, коментарями, типом і датою (за замовчуванням 24).",
     "input_schema": {"type": "object",
                      "properties": {"account": {"type": "string", "description": "username акаунта (якщо їх кілька)"},
                                     "limit": {"type": "integer", "description": "Скільки постів (1..50), типово 24"}}}},
    {"name": "instagram_insights",
     "description": "Інсайти акаунта за 28 днів (охоплення/reach). Вкажи account, якщо акаунтів кілька.",
     "input_schema": {"type": "object",
                      "properties": {"account": {"type": "string", "description": "username акаунта"}}}},
    {"name": "instagram_competitor",
     "description": "Публічні метрики ІНШОГО бізнес-акаунта за username (підписники, пости, останні публікації).",
     "input_schema": {"type": "object",
                      "properties": {"username": {"type": "string"}},
                      "required": ["username"]}},
]


def execute_tool(name: str, tool_input: dict, uid: int, chat_id: int) -> str:
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
        if name == "set_reminder":
            return tool_set_reminder(uid, chat_id, str(tool_input.get("when", "")),
                                     str(tool_input.get("text", "")).strip())
        if name == "list_reminders":
            return tool_list_reminders(uid)
        if name == "delete_reminder":
            return tool_delete_reminder(uid, tool_input.get("number"))
        if name == "read_url":
            return tool_read_url(str(tool_input.get("url", "")))
        if name == "instagram_accounts":
            return tool_ig_accounts()
        if name == "instagram_profile":
            return tool_ig_profile(tool_input.get("account"))
        if name == "instagram_recent_media":
            return tool_ig_recent_media(tool_input.get("account"), tool_input.get("limit", 10))
        if name == "instagram_insights":
            return tool_ig_insights(tool_input.get("account"))
        if name == "instagram_competitor":
            return tool_ig_competitor(tool_input.get("username", ""))
        return f"Невідомий інструмент: {name}"
    except Exception as e:
        log.exception("Tool %s failed", name)
        return f"Помилка інструменту {name}: {e}"


# ===================== Агентний цикл =====================

def _run_agent_sync(messages: list, uid: int, chat_id: int) -> str:
    for _ in range(MAX_TOOL_STEPS):
        resp = claude.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
            tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip() or "Готово."
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = execute_tool(block.name, block.input or {}, uid, chat_id)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})
    return "Забагато кроків із інструментами, спробуй переформулювати запит."


async def run_agent(messages: list, uid: int, chat_id: int) -> str:
    return await asyncio.to_thread(_run_agent_sync, messages, uid, chat_id)


# ===================== Перевірка нагадувань =====================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    for fname in os.listdir(REM_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(REM_DIR, fname)
        rems = _load_json(path)
        if not rems:
            continue
        due, keep = [], []
        for r in rems:
            try:
                is_due = _parse_when(r["when"]) <= now
            except Exception:
                is_due = False  # битий запис — лишаємо, не надсилаємо
            (due if is_due else keep).append(r)
        if not due:
            continue
        for r in due:
            try:
                await context.bot.send_message(chat_id=r["chat_id"], text=f"⏰ Нагадування: {r['text']}")
            except Exception:
                log.exception("Не вдалося надіслати нагадування")
        _save_json(path, keep)


# ===================== Хендлери =====================

async def reply_long(update: Update, text: str):
    """Надсилає довгу відповідь кількома повідомленнями (ліміт Telegram ~4096)."""
    text = text or "Готово."
    if len(text) <= TG_LIMIT:
        await update.message.reply_text(text)
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > TG_LIMIT:  # дуже довгий рядок — ріжемо жорстко
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:TG_LIMIT])
            line = line[TG_LIMIT:]
        if len(cur) + len(line) + 1 > TG_LIMIT:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    for c in chunks:
        await update.message.reply_text(c)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Вітаю! Я ваш бізнес-асистент з інструментами. 🤖\n"
        "Вмію: рахувати, зберігати нотатки, ставити нагадування, підказувати дату й час, "
        "читати веб-сторінки за посиланням і розбирати надіслані фото.\n\n"
        "Команди: /help — можливості, /notes — нотатки, /reminders — нагадування, /reset — очистити діалог."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Що я вмію:\n"
        "• Відповідати на бізнес-питання, стратегія й тексти\n"
        "• 🧮 Рахувати\n"
        "• 📝 Зберігати нотатки: «запам'ятай ...», перегляд — /notes\n"
        "• ⏰ Нагадування: «нагадай завтра о 9 про ...», перегляд — /reminders\n"
        "• 🕒 Казати дату й час\n"
        "• 🔗 Читати сторінку за посиланням\n"
        "• 🖼 Розбирати надіслані фото\n"
        "• 📊 Аналізувати Instagram як топ-SMM (кілька акаунтів) — /ig або /ig username\n"
        "• 🎬 Монтажний сценарій Reels — /reels тема\n"
        "• 🖼 Карусель слайд-за-слайдом — /carousel тема\n"
        "• 🗓 Контент-план на тиждень — /plan акаунт/ніша\n\n"
        "Команди:\n"
        "/start — привітання\n"
        "/help — ця довідка\n"
        "/notes — показати нотатки\n"
        "/reminders — показати нагадування\n"
        "/ig — SMM-аудит Instagram-акаунта\n"
        "/reels тема — сценарій Reels із субтитрами\n"
        "/carousel тема — карусель по слайдах\n"
        "/plan акаунт/ніша — контент-план на тиждень\n"
        "/reset — очистити поточний діалог"
    )


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    history.pop(update.effective_user.id, None)
    await update.message.reply_text("Діалог очищено. Починаємо заново 🙂")


async def notes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Твої нотатки:\n" + tool_list_notes(update.effective_user.id))


async def reminders_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏰ Твої нагадування:\n" + tool_list_reminders(update.effective_user.id))


async def _agent_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prompt: str):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    working = history.get(uid, []) + [{"role": "user", "content": prompt}]
    try:
        answer = await run_agent(working, uid, chat_id)
        hist = history.setdefault(uid, [])
        hist.append({"role": "user", "content": prompt})
        hist.append({"role": "assistant", "content": answer})
        history[uid] = hist[-HISTORY_LIMIT:]
        await reply_long(update, answer)
    except Exception:
        log.exception("agent reply error")
        await update.message.reply_text("Упс, щось пішло не так. Спробуй ще раз.")


async def reels_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(ctx.args).strip() if getattr(ctx, "args", None) else ""
    if not topic:
        await update.message.reply_text(
            "Напиши тему після команди. Напр.:\n/reels ранкова рутина остеопата")
        return
    prompt = (
        f"Створи професійний монтажний сценарій Reels на тему: «{topic}». "
        "Пиши так, щоб можна було зібрати відео в CapCut. Структура:\n"
        "1) ХУК (перші 3 секунди) — 3 варіанти на вибір.\n"
        "2) РОЗКАДРОВКА по сценах із таймінгом (0–3с, 3–7с, ...): що в кадрі, дія, план.\n"
        "3) СУБТИТРИ построково — точний текст, який виводити на екран у кожній сцені.\n"
        "4) ЗВУК/МУЗИКА — настрій, темп, де акценти.\n"
        "5) ПІДПИС до посту + 5–10 релевантних хештегів.\n"
        "6) ЗАКЛИК до дії. Коротко й конкретно, без води."
    )
    await _agent_reply(update, ctx, prompt)


async def carousel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(ctx.args).strip() if getattr(ctx, "args", None) else ""
    if not topic:
        await update.message.reply_text(
            "Напиши тему після команди. Напр.:\n/carousel 5 помилок постави")
        return
    prompt = (
        f"Створи карусель для Instagram на тему: «{topic}», слайд за слайдом. Структура:\n"
        "- СЛАЙД 1 — чіпляючий хук-заголовок.\n"
        "- СЛАЙДИ 2…N — по одній думці на слайд: текст НА слайді + що зобразити (візуал).\n"
        "- ОСТАННІЙ СЛАЙД — заклик до дії.\n"
        "Зроби 6–8 слайдів. Після слайдів дай ПІДПИС до посту + 5–10 хештегів. Коротко, по суті."
    )
    await _agent_reply(update, ctx, prompt)


async def plan_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(ctx.args).strip() if getattr(ctx, "args", None) else ""
    if not arg:
        await update.message.reply_text(
            "Напиши нішу або username підключеного акаунта. Напр.:\n"
            "/plan sosna_alexandra\n/plan остеопатія для мам")
        return
    prompt = (
        f"Склади контент-план на 7 днів для: «{arg}».\n"
        "Якщо це username підключеного Instagram-акаунта — спершу візьми його профіль і "
        "останні пости через інструменти та врахуй, який контент заходить (формати, теми, ER).\n"
        "Для КОЖНОГО дня (День 1…День 7) вкажи: формат (Reels / Карусель / Stories), тему, "
        "короткий хук і ціль (охоплення / залучення / продаж). Балансуй формати, роби акцент "
        "на те, що працює у цієї аудиторії.\n"
        "У кінці підкажи, для яких днів одразу згенерувати сценарій через /reels чи /carousel."
    )
    await _agent_reply(update, ctx, prompt)


async def ig_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    arg = " ".join(ctx.args).strip() if getattr(ctx, "args", None) else ""
    target = f"акаунта {arg}" if arg else ("акаунта (якщо їх кілька — спершу покажи список "
                                           "через instagram_accounts і спитай, який саме)")
    prompt = (
        f"Проведи повний SMM-аудит Instagram-{target}: візьми профіль, останні пости "
        "та інсайти через інструменти, порахуй ER, і дай конкретні рекомендації та план "
        "зростання на 2 тижні."
    )
    working = history.get(uid, []) + [{"role": "user", "content": prompt}]
    try:
        answer = await run_agent(working, uid, chat_id)
        hist = history.setdefault(uid, [])
        hist.append({"role": "user", "content": prompt})
        hist.append({"role": "assistant", "content": answer})
        history[uid] = hist[-HISTORY_LIMIT:]
        await reply_long(update, answer)
    except Exception:
        log.exception("ig_cmd error")
        await update.message.reply_text("Не вдалося зробити аудит. Перевір, чи підключений Instagram.")


async def chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    user_text = update.message.text
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    working = history.get(uid, []) + [{"role": "user", "content": user_text}]
    try:
        answer = await run_agent(working, uid, chat_id)
        hist = history.setdefault(uid, [])
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": answer})
        history[uid] = hist[-HISTORY_LIMIT:]
        await reply_long(update, answer)
    except Exception:
        log.exception("chat error")
        await update.message.reply_text("Упс, щось пішло не так. Спробуй ще раз.")


async def photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    caption = update.message.caption or "Що на цьому фото? Коротко опиши й підкажи, чим це корисно для бізнесу."
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        tg_file = await update.message.photo[-1].get_file()
        buf = await tg_file.download_as_bytearray()
        b64 = base64.standard_b64encode(bytes(buf)).decode()
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": caption},
        ]
        working = history.get(uid, []) + [{"role": "user", "content": content}]
        answer = await run_agent(working, uid, chat_id)
        hist = history.setdefault(uid, [])
        hist.append({"role": "user", "content": f"[фото] {caption}"})
        hist.append({"role": "assistant", "content": answer})
        history[uid] = hist[-HISTORY_LIMIT:]
        await reply_long(update, answer)
    except Exception:
        log.exception("photo error")
        await update.message.reply_text("Не вдалося опрацювати фото. Спробуй ще раз.")


def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CommandHandler("ig", ig_cmd))
    app.add_handler(CommandHandler("reels", reels_cmd))
    app.add_handler(CommandHandler("carousel", carousel_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    # Перевіряємо нагадування щохвилини
    app.job_queue.run_repeating(check_reminders, interval=60, first=10)
    log.info("Agent bot started with tools")
    app.run_polling()


if __name__ == "__main__":
    main()
