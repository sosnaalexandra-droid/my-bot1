import os, logging

# На всякий случай убираем прокси-переменные, чтобы не мешали запросам
for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(v, None)

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

# --- Настройки ---
MODEL = "claude-sonnet-5-5"          # модель, которая отвечает в боте
MAX_TOKENS = 1024                    # максимальная длина одного ответа
HISTORY_LIMIT = 20                   # сколько последних сообщений помнить в диалоге

# Роль бота. Меняется словами через Claude — просто попроси изменить SYSTEM_PROMPT.
SYSTEM_PROMPT = (
    "Ты — доброжелательный личный помощник в Telegram. "
    "Помогаешь с любыми вопросами: идеи, тексты, планирование, объяснения простыми словами. "
    "Отвечай на том языке, на котором пишет пользователь (русский или украинский). "
    "Будь кратким и по существу, без лишней воды. "
    "Если вопрос непонятен — уточни одним коротким вопросом."
)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
history: dict[int, list[dict]] = {}


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой личный помощник на Claude. 🙂\n"
        "Спрашивай что угодно. Команды: /help — список возможностей, /reset — очистить диалог."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Вот что я умею:\n"
        "• Отвечать на любые вопросы и помогать с задачами\n"
        "• Писать и править тексты, объяснять простыми словами\n"
        "• Помнить наш диалог (последние сообщения)\n\n"
        "Команды:\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/reset — забыть текущий диалог и начать заново"
    )


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    history.pop(update.effective_user.id, None)
    await update.message.reply_text("Готово, диалог очищен. Начнём заново 🙂")


async def chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    history.setdefault(uid, []).append({"role": "user", "content": update.message.text})
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=history[uid],
        )
        text = resp.content[0].text
        history[uid].append({"role": "assistant", "content": text})
        history[uid] = history[uid][-HISTORY_LIMIT:]
        await update.message.reply_text(text)
    except Exception:
        log.exception("Ошибка при обращении к Claude")
        # Убираем неотвеченное сообщение из истории, чтобы диалог не "залипал"
        if history.get(uid):
            history[uid].pop()
        await update.message.reply_text("Упс, что-то пошло не так. Попробуй, пожалуйста, ещё раз.")


def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
