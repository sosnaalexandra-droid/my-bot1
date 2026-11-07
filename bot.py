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
    "Ти — AI бізнес-асистент для підприємців. "
    "Твої сильні сторони: стратегічне мислення, управління пріоритетами, "
    "комунікація, фінанси і ROI. Будь конкретним, давай цифри та плани. "
    "Відповідай тією мовою, якою пише користувач (українською або російською)."
)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
history: dict[int, list[dict]] = {}


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Вітаю! Я ваш AI бізнес-асистент. 📈\n"
        "Допоможу зі стратегією, пріоритетами, фінансами та ROI — конкретно, з цифрами й планами.\n"
        "Команди: /help — можливості, /reset — очистити діалог."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Чим допоможу:\n"
        "• Стратегія і планування бізнесу\n"
        "• Пріоритети та управління задачами\n"
        "• Фінанси, розрахунки ROI, цифри\n"
        "• Комунікація: листи, перемовини, тексти\n\n"
        "Команди:\n"
        "/start — привітання\n"
        "/help — ця довідка\n"
        "/reset — очистити поточний діалог"
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
