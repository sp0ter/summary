import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv, dotenv_values
import datetime
import pytz
import re
import logging
from logging.handlers import RotatingFileHandler
import asyncio
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Настройка логирования в файл
logger = logging.getLogger('discord_summary_bot')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler('summary.log', maxBytes=5*1024*1024, backupCount=2)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

scheduler = AsyncIOScheduler(timezone='Europe/Kyiv')

# Загрузка переменных окружения
load_dotenv(dotenv_path='.env')

# Проверка обязательных переменных окружения
TOKEN = os.getenv('DISCORD_SUMMARYBOT_TOKEN')
GUILD_ID = os.getenv('GUILD_ID')
SUMMARY_ID = os.getenv('SUMMARY_ID')
SUMMARY_ROLE_ID = os.getenv('SUMMARY_ROLE_ID')

if not all([TOKEN, GUILD_ID, SUMMARY_ID, SUMMARY_ROLE_ID]):
    logger.error("Отсутствуют обязательные переменные окружения: DISCORD_BOT_TOKEN, GUILD_ID, SUMMARY_ID, SUMMARY_ROLE_ID")
    exit(1)

# Преобразование в нужные типы
try:
    GUILD_ID = int(GUILD_ID)
    SUMMARY_ID = int(SUMMARY_ID)
    SUMMARY_ROLE_ID = int(SUMMARY_ROLE_ID)
except ValueError as e:
    logger.error(f"Ошибка преобразования переменных окружения в числа: {e}")
    exit(1)

# Глобальные параметры
SUMMARY_CHANNEL_IDS = []
INCLUDE_ROLE_MENTIONS = os.getenv('INCLUDE_ROLE_MENTIONS', 'True').lower() == 'true'
MAX_MESSAGES = int(os.getenv('MAX_MESSAGES', '500'))
COLLECTION_TIMEOUT = int(os.getenv('COLLECTION_TIMEOUT', '300'))

# Парсинг списка ID каналов
def parse_channel_ids(raw_ids):
    if not raw_ids:
        return []
    cleaned_ids = raw_ids.strip().split(',')
    ids = []
    for part in cleaned_ids:
        part = part.strip()
        if not part:
            continue
        match = re.match(r'^(\d+)', part)
        if match:
            ids.append(int(match.group(1)))
    return ids

def load_channel_ids_from_env():
    env_vars = dotenv_values(dotenv_path='.env')
    raw_ids = env_vars.get("DISCORD_CHANNELS", "")
    logger.info(f"Raw DISCORD_CHANNELS: {raw_ids}")
    return parse_channel_ids(raw_ids)

# Загружаем каналы
SUMMARY_CHANNEL_IDS = load_channel_ids_from_env()

# Настройка интентов
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def update_progress(message, current, total, extra_info=""):
    progress = int((current / total) * 100) if total > 0 else 0
    progress_text = f"🔄 Собираю дайджест... {progress}% ({current}/{total} каналов)"
    if extra_info:
        progress_text += f"\n{extra_info}"
    try:
        await message.edit(content=progress_text)
    except Exception as e:
        logger.error(f"Ошибка обновления прогресса: {e}")

def get_yesterday_kyiv():
    kyiv_tz = pytz.timezone('Europe/Kyiv')
    now = datetime.datetime.now(kyiv_tz)
    yesterday = now - datetime.timedelta(days=1)
    start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end

async def collect_messages_from_yesterday(channel_ids=None, progress_message=None):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        logger.error(f"Не удалось найти сервер с ID {GUILD_ID}")
        return []

    start, end = get_yesterday_kyiv()
    summary_mention = f"<@&{SUMMARY_ROLE_ID}>"
    messages = []
    channels_to_scan = []

    logger.info(f"Сбор сообщений с {start.strftime('%d.%m.%Y %H:%M')} по {end.strftime('%d.%m.%Y %H:%M')} (Киев)")

    specific_channels = []
    if channel_ids:
        for channel_id in channel_ids:
            channel = guild.get_channel(channel_id)
            if channel:
                specific_channels.append(channel)
                channels_to_scan.append((channel, False))

    if INCLUDE_ROLE_MENTIONS:
        for channel in guild.text_channels:
            if channel not in specific_channels:
                channels_to_scan.append((channel, True))

    total_channels = len(channels_to_scan)
    processed_channels = 0
    messages_count = 0

    for channel, role_only in channels_to_scan:
        if progress_message:
            await update_progress(
                progress_message,
                processed_channels,
                total_channels,
                f"Собрано сообщений: {messages_count}"
                #f"Текущий канал: #{channel.name} | Собрано сообщений: {messages_count}"
            )

        try:
            channel_messages = []
            logger.info(f"Сканирование канала #{channel.name} ({'только теги роли' if role_only else 'все сообщения'})")

            async for msg in channel.history(after=start, before=end, oldest_first=True, limit=MAX_MESSAGES):
                if not msg.author.bot:
                    if not role_only or (role_only and summary_mention in msg.content):
                        channel_messages.append(msg)
                        messages_count += 1

            logger.info(f"Найдено {len(channel_messages)} сообщений в #{channel.name}")
            messages.extend(channel_messages)

        except Exception as e:
            logger.error(f"Ошибка при сканировании канала #{channel.name}: {e}")

        processed_channels += 1

    if progress_message:
        await update_progress(progress_message, total_channels, total_channels, f"✅ Сбор завершен! Всего: {len(messages)} сообщений")

    logger.info(f"Собрано {len(messages)} сообщений из {processed_channels} каналов")
    return messages

async def format_and_send_digest(channel, collected_messages):
    kyiv_tz = pytz.timezone('Europe/Kyiv')
    yesterday = datetime.datetime.now(kyiv_tz) - datetime.timedelta(days=1)
    summary_date = yesterday.strftime('%d.%m.%Y')

    messages_by_channel = {}
    channel_order_map = {channel_id: idx for idx, channel_id in enumerate(SUMMARY_CHANNEL_IDS)}

    for msg in collected_messages:
        channel_name = msg.channel.name
        channel_id = msg.channel.id
        if channel_name not in messages_by_channel:
            messages_by_channel[channel_name] = {"messages": [], "channel_id": channel_id}
        messages_by_channel[channel_name]["messages"].append(msg)

    await send_text_digest(channel, messages_by_channel, summary_date, channel_order_map)

async def send_text_digest(channel, messages_by_channel, summary_date, channel_order_map):
    text_summary = f"📚 **Выжимка 2TOP SQUAD за {summary_date}**\n"
    #роль squadmember
    role_mention = f"<@&829341057190723595>"

    channels_in_list = []
    channels_not_in_list = []

    for channel_name, data in messages_by_channel.items():
        channel_id = data["channel_id"]
        if channel_id in channel_order_map:
            channels_in_list.append((channel_name, data["messages"], channel_order_map[channel_id]))
        else:
            channels_not_in_list.append((channel_name, data["messages"]))

    channels_in_list.sort(key=lambda x: x[2])
    channels_not_in_list.sort(key=lambda x: len(x[1]), reverse=True)

    sorted_channels = [(name, msgs) for name, msgs, _ in channels_in_list] + channels_not_in_list

    for channel_name, msgs in sorted_channels:
        channel_name_cleaned = re.sub(r'[^a-zA-Z0-9а-яА-Я\s-]', '', channel_name)
        channel_text = f"\n**__{channel_name_cleaned.capitalize()}__**\n"

        for msg in msgs:
            content_preview = re.sub(r'^#{1,3}\s*', '', msg.content.split('\n')[0])
            content_preview = re.sub(r'\*\*(.*?)\*\*', r'\1', content_preview)
            content_preview = content_preview[:100] or "Пост без заголовка"
            if len(content_preview) == 100:
                content_preview += "..."
            channel_text += f"{content_preview}\n{msg.jump_url}\n"

        if len(text_summary) + len(channel_text) > 1900:
            await channel.send(text_summary)
            #text_summary = f"📚 **Выжимка 2TOP SQUAD за {summary_date} (продолжение)**\n" + channel_text
            text_summary = channel_text
        else:
            text_summary += channel_text

    if text_summary:
        #тег роли 
        text_summary += f"\n{role_mention}"
        await channel.send(text_summary)

async def send_summary(channel, channel_ids=None, progress_message=None):
    try:
        collected_messages = await asyncio.wait_for(
            collect_messages_from_yesterday(channel_ids, progress_message),
            timeout=COLLECTION_TIMEOUT
        )

        if collected_messages:
            await format_and_send_digest(channel, collected_messages)
            logger.info(f"Отправлен дайджест в канал #{channel.name}")
        else:
            await channel.send("ℹ️ Нет сообщений за вчерашний день.")
            logger.info("Нет сообщений для дайджеста")

        if progress_message:
            try:
                await progress_message.delete()
                logger.info("Сообщение с прогресс-баром удалено")
            except Exception as e:
                logger.error(f"Ошибка удаления прогресс-бара: {e}")

    except asyncio.TimeoutError:
        logger.error(f"Превышен таймаут ({COLLECTION_TIMEOUT}s) при сборе сообщений")
        error_msg = f"⚠️ Превышен таймаут ({COLLECTION_TIMEOUT}s). Попробуйте уменьшить количество каналов или отключить сбор тегов ролей."
        if progress_message:
            await progress_message.edit(content=error_msg)
        else:
            await channel.send(error_msg)
    except Exception as e:
        logger.error(f"Ошибка формирования дайджеста: {e}")
        await channel.send(f"❌ Ошибка формирования дайджеста: {str(e)[:1500]}")
        if progress_message:
            try:
                await progress_message.delete()
                logger.info("Сообщение с прогресс-баром удалено")
            except Exception as e:
                logger.error(f"Ошибка удаления прогресс-бара: {e}")

# Добавляем функцию для ежедневного дайджеста через планировщик
async def run_daily_summary():
    logger.info("Запуск daily_summary через apscheduler")
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            logger.error(f"Не удалось найти сервер с ID {GUILD_ID}")
            return
        channel = guild.get_channel(SUMMARY_ID)
        if not channel:
            logger.error(f"Не удалось найти канал с ID {SUMMARY_ID}")
            return
        progress_msg = await channel.send("🔄 Собираю дайджест...")
        await send_summary(channel, SUMMARY_CHANNEL_IDS or None, progress_msg)
        logger.info("Дайджест успешно сформирован")
    except Exception as e:
        logger.error(f"Ошибка в run_daily_summary: {e}")

@bot.command()
async def toggle_role_mentions(ctx):
    global INCLUDE_ROLE_MENTIONS
    INCLUDE_ROLE_MENTIONS = not INCLUDE_ROLE_MENTIONS
    status = "включено" if INCLUDE_ROLE_MENTIONS else "выключено"
    await ctx.send(f"🔄 Сбор сообщений с тегом роли {status}. Теперь бот будет собирать сообщения с тегом роли {'из всех каналов' if INCLUDE_ROLE_MENTIONS else 'только из указанных каналов'}.")
    logger.info(f"Сбор сообщений с тегом роли: {status}")

@bot.command()
async def set_max_messages(ctx, limit: int):
    global MAX_MESSAGES
    if limit < 10:
        await ctx.send("⚠️ Минимальное значение: 10 сообщений")
        return
    MAX_MESSAGES = limit
    await ctx.send(f"✅ Максимальное количество сообщений: {limit}")
    logger.info(f"Максимальное количество сообщений: {limit}")

@bot.command()
async def set_timeout(ctx, seconds: int):
    global COLLECTION_TIMEOUT
    if seconds < 30:
        await ctx.send("⚠️ Минимальное значение: 30 секунд")
        return
    COLLECTION_TIMEOUT = seconds
    await ctx.send(f"✅ Таймаут сбора: {seconds} секунд")
    logger.info(f"Таймаут сбора: {seconds} секунд")

@bot.command()
async def digest(ctx):
    progress_msg = await ctx.send("🔄 Подготовка к сбору сообщений...")
    await send_summary(ctx.channel, SUMMARY_CHANNEL_IDS or None, progress_msg)

@bot.command()
async def reload_channels(ctx):
    global SUMMARY_CHANNEL_IDS
    old_channels = SUMMARY_CHANNEL_IDS.copy()
    SUMMARY_CHANNEL_IDS = load_channel_ids_from_env()

    added = [ch for ch in SUMMARY_CHANNEL_IDS if ch not in old_channels]
    removed = [ch for ch in old_channels if ch not in SUMMARY_CHANNEL_IDS]

    response = f"♻️ Список каналов обновлен: {len(SUMMARY_CHANNEL_IDS)} каналов"
    if added:
        response += f"\n✅ Добавлено: {', '.join(map(str, added))}"
    if removed:
        response += f"\n❌ Удалено: {', '.join(map(str, removed))}"

    await ctx.send(response)
    logger.info(f"Обновлен список каналов: {SUMMARY_CHANNEL_IDS}")

@bot.command()
async def digest_from(ctx, *channel_mentions: discord.TextChannel):
    if not channel_mentions:
        await ctx.send("⚠️ Укажите хотя бы один канал. Например: `!digest_from #general #announcements`")
        return
    channel_ids = [ch.id for ch in channel_mentions]
    progress_msg = await ctx.send(f"🔄 Подготовка к сбору из {len(channel_ids)} каналов...")
    await send_summary(ctx.channel, channel_ids, progress_msg)

@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    logger.info(f"📌 Каналы для сбора: {SUMMARY_CHANNEL_IDS}")
    logger.info(f"🔔 Сбор сообщений с тегом роли: {'Включено' if INCLUDE_ROLE_MENTIONS else 'Выключено'}")
    logger.info(f"⏱️ Максимальный таймаут сбора: {COLLECTION_TIMEOUT} секунд")
    logger.info(f"📊 Максимальное количество сообщений: {MAX_MESSAGES}")
    logger.info(f"🕒 Системная временная зона: {time.tzname}")
    
    # Настраиваем планировщик для запуска дайджеста в 00:01 по киевскому времени
    scheduler.add_job(run_daily_summary, 'cron', hour=0, minute=1)
    scheduler.start()
    logger.info("Планировщик apscheduler запущен")

@bot.command()
async def check_schedule(ctx):
    try:
        if scheduler.running:
            next_run = scheduler.get_jobs()[0].next_run_time if scheduler.get_jobs() else None
            if next_run:
                kyiv_tz = pytz.timezone('Europe/Kyiv')
                next_run_kyiv = next_run.astimezone(kyiv_tz)
                now = datetime.datetime.now(kyiv_tz)
                time_until = next_run_kyiv - now
                hours, remainder = divmod(time_until.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                await ctx.send(
                    f"✅ Задача активна.\n"
                    f"⏰ Следующий запуск: {next_run_kyiv.strftime('%d.%m.%Y %H:%M:%S')} (Киев)\n"
                    f"⏱️ Осталось: {time_until.days} дней, {hours} часов, {minutes} минут, {seconds} секунд"
                )
                logger.info(f"Статус apscheduler: следующий запуск {next_run_kyiv}")
            else:
                await ctx.send("⚠️ Задача активна, но время следующего запуска неизвестно.")
                logger.warning("apscheduler активен, но нет запланированных задач")
        else:
            await ctx.send("❌ Планировщик не активен! Перезапустите бота.")
            logger.warning("apscheduler не активен")
    except Exception as e:
        await ctx.send(f"❌ Ошибка проверки: {str(e)}")
        logger.error(f"Ошибка проверки apscheduler: {e}")

@bot.command()
async def restart_scheduler(ctx):
    try:
        scheduler.shutdown()
        scheduler.start()
        scheduler.add_job(run_daily_summary, 'cron', hour=0, minute=1)  # Исправлено время на 00:01
        await ctx.send("🔄 Планировщик перезапущен.")
        next_run = scheduler.get_jobs()[0].next_run_time if scheduler.get_jobs() else None
        if next_run:
            kyiv_tz = pytz.timezone('Europe/Kyiv')
            next_run_kyiv = next_run.astimezone(kyiv_tz)
            now = datetime.datetime.now(kyiv_tz)
            time_until = next_run_kyiv - now
            hours, remainder = divmod(time_until.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            await ctx.send(
                f"⏰ Следующий запуск: {next_run_kyiv.strftime('%d.%m.%Y %H:%M:%S')} (Киев)\n"
                f"⏱️ Осталось: {time_until.days} дней, {hours} часов, {minutes} минут, {seconds} секунд"
            )
        logger.info("Планировщик apscheduler перезапущен")
    except Exception as e:
        await ctx.send(f"❌ Ошибка перезапуска: {str(e)}")
        logger.error(f"Ошибка перезапуска apscheduler: {e}")

if __name__ == "__main__":
    logger.info("Запуск бота...")
    bot.run(TOKEN)
