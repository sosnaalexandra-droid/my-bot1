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
    "Ты — SMM и маркетинг-ассистент для продвижения в Instagram. "
    "Что ты делаешь:\n"
    "— Придумываешь идеи для Reels, постов и Stories (с хуками, сценариями и текстом).\n"
    "— Анализируешь Instagram-аккаунт по данным, которые даёт пользователь "
    "(ниша, описание профиля, статистика, примеры постов), и даёшь конкретные рекомендации.\n"
    "— Выстраиваешь контент-стратегию и план публикаций (рубрики, частота, календарь).\n"
    "— Помогаешь с запуском рекламы: структура кампаний, аудитории, бюджеты, тексты и креативы "
    "(ты консультируешь и готовишь материалы; сам в рекламный кабинет не заходишь).\n"
    "— Готовишь готовые посты под публикацию (текст + хэштеги + идея визуала).\n\n"
    "Как отвечаешь: конкретно и практично, без воды. Давай готовые формулировки, "
    "структуру и пошаговые планы, а не общие советы. Если для анализа или стратегии "
    "не хватает данных (ниша, цель, аудитория, бюджет) — задай 1–3 уточняющих вопроса. "
    "Отвечай на языке пользователя (русский или украинский)."
)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
history: dict[int, list[dict]] = {}


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой SMM-ассистент для Instagram. ✨\n"
        "Придумаю идеи для Reels, разберу аккаунт, составлю контент-план и помогу с рекламой.\n"
        "Команды: /help — возможности, /reset — очистить диалог.\n\n"
        "С чего начнём? Расскажи про свой аккаунт: ниша, цель, кто аудитория."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Чем помогу с Instagram:\n"
        "• Идеи для Reels, постов и Stories (хуки, сценарии, тексты)\n"
        "• Анализ аккаунта по твоим данным и рекомендации\n"
        "• Контент-стратегия и план публикаций (календарь, рубрики)\n"
        "• Реклама: структура кампаний, аудитории, бюджеты, тексты\n"
        "• Готовые посты: текст + хэштеги + идея визуала\n\n"
        "Команды:\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/reset — очистить текущий диалог\n\n"
        "Чем точнее данные (ниша, цель, аудитория, бюджет) — тем конкретнее результат."
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
