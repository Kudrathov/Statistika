import os
import json
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import httpx

# === КОНФИГУРАЦИЯ ===
# Переменные берутся из настроек (Variables) на Railway для безопасности
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")
SOURCE_CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID", -1001471933679))  # Канал-источник
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", -1003469691743))  # Канал-зеркало

# Файл карты сообщений хранится в памяти во время работы
MESSAGE_MAP_FILE = 'message_map.json'

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# === ЗАГРУЗКА/СОХРАНЕНИЕ МАППИНГА ===
def load_message_map():
    """Загрузка маппинга сообщений для редактирования из файла"""
    if os.path.exists(MESSAGE_MAP_FILE):
        try:
            with open(MESSAGE_MAP_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {MESSAGE_MAP_FILE}: {e}")
    return {}


def save_message_map(data):
    """Сохранение маппинга сообщений во временный файл"""
    try:
        with open(MESSAGE_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {MESSAGE_MAP_FILE}: {e}")


# === ЛОГИКА ОПРЕДЕЛЕНИЯ ИСХОДА 3/2 ===
def is_32_outcome(text: str) -> bool:
    """Проверяет, является ли сообщение исходом 3/2 (игрок=3 карты, банкир=2)."""
    if '👈' in text or '👉' in text:
        return False

    parts = []
    start = 0
    while True:
        open_idx = text.find('(', start)
        if open_idx == -1:
            break
        close_idx = text.find(')', open_idx)
        if close_idx == -1:
            break
        parts.append(text[open_idx + 1:close_idx])
        start = close_idx + 1

    if len(parts) != 2:
        return False

    player_str = parts[0]
    banker_str = parts[1]

    player_count = sum(1 for ch in player_str if ch in '♣♦♥♠')
    banker_count = sum(1 for ch in banker_str if ch in '♣♦♥♠')

    return player_count == 3 and banker_count == 2


def add_32_indicator(text: str) -> str:
    """Добавляет 🟩 только если это финальное сообщение (без 👉/👈)"""
    if '👈' in text or '👉' in text:
        return text
    if is_32_outcome(text):
        return text + " 🟩"
    return text


# === БЕЗОПАСНАЯ ОТПРАВКА С АВТОПОВТОРАМИ ===
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=0.5, max=3),
    retry=retry_if_exception_type((httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TimeoutException))
)
async def safe_send_message(bot, chat_id, text):
    """Безопасная отправка сообщения с защитой от сетевых сбоев"""
    return await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=0.5, max=3),
    retry=retry_if_exception_type((httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TimeoutException))
)
async def safe_edit_message(bot, chat_id, message_id, text):
    """Безопасное редактирование сообщения"""
    try:
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception as e:
        if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
            # Если оригинал на редактирование не найден — шлем как новое сообщение
            return await safe_send_message(bot, chat_id, text)
        raise


# === ОБРАБОТЧИК МЕССЕДЖЕЙ ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем, откуда прилетел пост (обычный или отредактированный)
    if update.channel_post:
        message = update.channel_post
        is_edit = False
    elif update.edited_channel_post:
        message = update.edited_channel_post
        is_edit = True
    else:
        return

    # Фильтруем по ID канала
    if message.chat.id != SOURCE_CHAT_ID:
        return

    original_text = message.text or ""
    source_message_id = message.message_id

    # Добавляем индикатор 🟩 при необходимости
    enhanced_text = add_32_indicator(original_text)
    message_map = load_message_map()

    try:
        if is_edit:
            key = str(source_message_id)
            if key in message_map:
                target_msg_id = message_map[key]
                try:
                    await safe_edit_message(context.bot, TARGET_CHAT_ID, target_msg_id, enhanced_text)
                    logger.info(f"✏️ Отредактировано в зеркале: {target_msg_id}")
                except Exception as e:
                    logger.error(f"❌ Ошибка изменения, отправляем новое: {e}")
                    sent = await safe_send_message(context.bot, TARGET_CHAT_ID, enhanced_text)
                    message_map[key] = sent.message_id
                    save_message_map(message_map)
            else:
                # Если в маппинге не нашлось записи, шлем заново
                sent = await safe_send_message(context.bot, TARGET_CHAT_ID, enhanced_text)
                message_map[key] = sent.message_id
                save_message_map(message_map)
                logger.info(f"📤 Создано новое (было утеряно): {sent.message_id}")
        else:
            # Обычный новый пост
            sent = await safe_send_message(context.bot, TARGET_CHAT_ID, enhanced_text)
            message_map[str(source_message_id)] = sent.message_id
            save_message_map(message_map)
            logger.info(f"📥 Скопировано в зеркало: {sent.message_id} | {'🟩' if '🟩' in enhanced_text else '—'}")

    except Exception as e:
        logger.exception(f"❌ Критическая ошибка при работе зеркала: {e}")


# === ТОЧКА ВХОДА ===
def main():
    logger.info("🚀 Запуск легковесного зеркала...")
    logger.info(f"📡 Источник: {SOURCE_CHAT_ID} | 🎯 Назначение: {TARGET_CHAT_ID}")

    app = Application.builder().token(BOT_TOKEN).build()

    # Слушаем новые и измененные посты
    app.add_handler(MessageHandler(
        filters.Chat(SOURCE_CHAT_ID) & filters.TEXT,
        handle_message
    ))
    app.add_handler(MessageHandler(
        filters.Chat(SOURCE_CHAT_ID) & filters.UpdateType.EDITED_CHANNEL_POST & filters.TEXT,
        handle_message
    ))

    logger.info("⚡ Бот в режиме зеркалирования запущен и готов!")
    app.run_polling()


if __name__ == '__main__':
    main()
