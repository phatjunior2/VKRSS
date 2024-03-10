import yt_dlp
import requests
import os
from telegram.ext import Updater, CommandHandler, CallbackContext, MessageHandler, Filters
import feedparser
import logging
from telegram import Update
from PIL import Image
from collections import defaultdict
import glob
import json
import subprocess

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = '7063898042:AAEAHf0vF47CvO0DMR3v4FgmEJ3jDtyygb8'
RSS_FEED_URLS = [
    'https://politepol.com/fd/w2s11hAZXmWn.xml',
    'https://www.youtube.com/feeds/videos.xml?channel_id=UC2ar6bIxQyaf9W8cYJ_I55w',
    # Добавьте другие URL-адреса RSS-лент при необходимости
]
CHECK_INTERVAL = 30  # Проверять каждые 30 сек. 

# Функции для сохранения и загрузки данных
def save_data(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f)

def load_data(filename, default):
    try:
        with open(filename) as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_bot_state(active_chats, sent_history):
    save_data('active_chats.json', list(active_chats))
    save_data('sent_history.json', {str(chat_id): list(history) for chat_id, history in sent_history.items()})

def load_bot_state():
    active_chats = set(load_data('active_chats.json', []))
    sent_history = defaultdict(set, {int(chat_id): set(history) for chat_id, history in load_data('sent_history.json', {}).items()})
    return active_chats, sent_history

# Основные функции бота
def convert_webp_to_jpg(thumbnail_filename):
    img = Image.open(thumbnail_filename).convert("RGB")
    jpg_filename = thumbnail_filename.rsplit('.', 1)[0] + ".jpg"
    img.save(jpg_filename, "jpeg")
    os.remove(thumbnail_filename)
    return jpg_filename

def download_audio(video_url):
    # Определение источника видео
    if "youtube.com" in video_url or "youtu.be" in video_url:
        format_preference = 'bestaudio/best'
    else:
        format_preference = 'best[height<=360][ext=mp4]'

    ydl_opts = {
        'format': format_preference,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
        }],
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'writethumbnail': True,
        'quiet': True,
        'ffmpeg_location': '/usr/bin/ffmpeg',
    }



    audio_file = None
    video_title = 'Unknown Title'
    video_uploader = 'Unknown Uploader'
    video_duration = 0
    video_link = video_url
    thumbnail_filename = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(video_url, download=True)
            video_duration = int(info_dict.get('duration', 0))
            if video_duration < 60:
                logger.info(f"Video {video_url} is shorter than 60 seconds, skipping.")
                return None, None, None, None, None, None

            audio_file = ydl.prepare_filename(info_dict).replace('.webm', '.m4a').replace('.mp4', '.m4a')
            video_title = info_dict.get('title', video_title)
            video_uploader = info_dict.get('uploader', video_uploader)
            base_filename = os.path.splitext(audio_file)[0]
            thumbnail_filename = f"{base_filename}.jpg"
            if not os.path.exists(thumbnail_filename):
                webp_filename = f"{base_filename}.webp"
                if os.path.exists(webp_filename):
                    thumbnail_filename = convert_webp_to_jpg(webp_filename)
            return audio_file, video_title, video_uploader, video_duration, video_link, thumbnail_filename
    except Exception as e:
        logger.error(f"Failed to download or process video {video_url}: {e}")
    return None, None, None, None, None, None

def send_audio_file(chat_id, audio_file, title, uploader, duration, link, thumbnail_filename, context):
    # Проверка существования аудиофайла
    if not os.path.exists(audio_file):
        logger.error(f"Audio file does not exist: {audio_file}")
        return

    # Подготовка миниатюры
    thumb = None
    if thumbnail_filename and os.path.exists(thumbnail_filename):
        try:
            thumb = open(thumbnail_filename, 'rb')
        except Exception as e:
            logger.error(f"Failed to open thumbnail file {thumbnail_filename}: {e}")
            thumb = None  # В случае ошибки обнуляем миниатюру

    # Формирование подписи к сообщению
    caption = f'🎬 <a href="{link}">{title}</a>'

    # Отправка аудиофайла
    try:
        with open(audio_file, 'rb') as audio:
            context.bot.send_audio(chat_id=chat_id, audio=audio, title=title, performer=uploader, 
                                   duration=duration, thumb=thumb, caption=caption, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Failed to send audio to chat ID {chat_id}: {e}")
    finally:
        if thumb:
            thumb.close()  # Важно закрыть файл после использования

def embed_thumbnail(audio_file, thumbnail_file):
    output_file = audio_file.replace('.m4a', '_with_cover.m4a')
    cmd = [
        'ffmpeg',
        '-i', audio_file,  # Исходный аудиофайл
        '-i', thumbnail_file,  # Файл миниатюры
        '-map', '0:0',  # Выбрать аудиодорожку из первого файла
        '-map', '1:0',  # Выбрать видеодорожку (миниатюру) из второго файла
        '-c:v', 'png',  # Преобразовать изображение в PNG
        '-disposition:v', 'attached_pic',  # Установить изображение как обложку
        '-c:a', 'copy',  # Копировать аудиодорожку без изменений
        output_file  # Выходной файл
    ]
    try:
        subprocess.run(cmd, check=True)
        return output_file  # Вернуть путь к новому файлу с обложкой
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to embed thumbnail: {e}")
        return audio_file  # В случае ошибки вернуть оригинальный аудиофайл


def check_feed(context: CallbackContext):
    for rss_feed_url in RSS_FEED_URLS:
        feed = feedparser.parse(rss_feed_url)
        if feed.entries:
            for latest_entry in feed.entries[:2]:  # Проверяем две последние записи
                if any(latest_entry.id not in context.bot_data['sent_history'][chat_id] for chat_id in context.bot_data['active_chats']):
                    audio_file, video_title, video_uploader, video_duration, video_link, thumbnail_filename = download_audio(latest_entry.link)
                    if audio_file and thumbnail_filename:
                        audio_file_with_cover = embed_thumbnail(audio_file, thumbnail_filename)
                        if audio_file_with_cover:
                            for chat_id in context.bot_data['active_chats']:
                                if latest_entry.id not in context.bot_data['sent_history'][chat_id]:
                                    send_audio_file(chat_id, audio_file_with_cover, video_title, video_uploader, video_duration, video_link, thumbnail_filename, context)
                                    context.bot_data['sent_history'][chat_id].add(latest_entry.id)
                            save_bot_state(context.bot_data['active_chats'], context.bot_data['sent_history'])
                            
                            # Удаление скачанных и обработанных файлов
                            if os.path.exists(audio_file):
                                os.remove(audio_file)
                            if os.path.exists(audio_file_with_cover):
                                os.remove(audio_file_with_cover)
                            if thumbnail_filename and os.path.exists(thumbnail_filename):
                                os.remove(thumbnail_filename)
                    else:
                        logger.info("Video does not meet criteria, skipping.")

def start(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    # Явно добавляем пользователя в active_chats
    context.bot_data['active_chats'].add(chat_id)
    # Проверяем, существует ли запись для этого пользователя в sent_history, если нет - создаем пустой set
    context.bot_data['sent_history'].setdefault(chat_id, set())
    
    # Форматируем сообщение с использованием HTML
    message_text = (
        "<b>Ты подписан!</b>\n"
        "Каждые 30 минут я буду проверять каналы на наличие новых видео. Если на каком-то канале выйдет видео, я пришлю тебе аудиофайл.\n\n"
        "Сейчас ты подписан на каналы:\n"
        "• <a href='https://www.youtube.com/channel/UC-iU28QW_832Fx_3RJ_vYPQ'>Апвоут</a>\n"
        "• <a href='https://www.youtube.com/channel/UCMpoPt5DaBcscn1eLQxLCwA'>Тучный Жаб</a>\n"
        "• <a href='https://www.youtube.com/channel/UC2ar6bIxQyaf9W8cYJ_I55w'>Полосатый Мух</a>\n"
        "• <a href='https://www.youtube.com/channel/UCQz1XMX-QeSGUyZV7Dtgc5A'>Глеб Рандалайнен</a>\n\n"
        "Чтобы отписаться от всех, используй команду <b>/stop</b>."
    )
    
    update.message.reply_text(message_text, parse_mode='HTML', disable_web_page_preview=True)
    save_bot_state(context.bot_data['active_chats'], context.bot_data['sent_history'])
    
    if not context.job_queue.jobs():
        context.job_queue.run_repeating(check_feed, interval=CHECK_INTERVAL, first=1, context=context)


def stop(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    if chat_id in context.bot_data['active_chats']:
        context.bot_data['active_chats'].remove(chat_id)
        save_bot_state(context.bot_data['active_chats'], context.bot_data['sent_history'])
        update.message.reply_text(
            '<b>Ты отписался от всех аудио выпусков.</b> Чтобы подписаться снова, используй команду <b>/start</b>',
            parse_mode='HTML'
        )

def main():
    active_chats, sent_history = load_bot_state()  # Загрузка сохраненного состояния
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.bot_data['active_chats'] = active_chats
    dp.bot_data['sent_history'] = sent_history

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stop", stop))

    # Запуск задачи проверки RSS независимо от команды /start
    job_queue = updater.job_queue
    job_queue.run_repeating(check_feed, interval=CHECK_INTERVAL, first=1)

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
