from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
import logging
import random
import re
import shlex
from zoneinfo import ZoneInfo

from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .ais import AISProvider, AISProviderError, VesselPosition, build_provider
from .config import Settings, load_settings
from .geo import haversine_km
from .storage import Watch, WatchStore


HELP_TEXT = """בוט מעקב AIS

/start

הבוט יבקש מספר MMSI או IMO, יציג מידע עדכני על כלי השיט, יבקש רדיוס במיילים ימיים ויתחיל מעקב.

פקודות נוספות:
/list
/remove
/reset
/empty_list
"""


logger = logging.getLogger(__name__)

ASK_VESSEL, ASK_RADIUS, ASK_LOCATION, ASK_REMOVE_VESSEL, ASK_RESET_CODE, ASK_EMPTY_LIST_CODE = range(6)
KM_PER_NAUTICAL_MILE = 1.852
JERUSALEM_TZ = ZoneInfo("Asia/Jerusalem")

START_KEYBOARD = ReplyKeyboardMarkup(
    [["/start"], ["/list", "/remove"], ["/reset", "/empty_list"]],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="לחצו /start כדי להתחיל",
)

VESSEL_PROMPT = "הזינו מספר כלי שיט: MMSI בן 9 ספרות או IMO בן 7 ספרות."
RADIUS_PROMPT = "באיזה מרחק להתריע? הזינו מיילים ימיים, למשל 3 או 3.5."
LOCATION_PROMPT = "שלחו מיקום Telegram כדי שאדע מאיזו נקודה למדוד."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_message.reply_text(VESSEL_PROMPT, reply_markup=ReplyKeyboardRemove())
    return ASK_VESSEL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("בוטל.", reply_markup=START_KEYBOARD)
    return ConversationHandler.END


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if context.args:
        return await remove_by_text(update, context, " ".join(context.args))
    await update.effective_message.reply_text("הזינו את מספר ה-MMSI או ה-IMO של כלי השיט שברצונכם להסיר מהמעקב.")
    return ASK_REMOVE_VESSEL


async def receive_remove_vessel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await remove_by_text(update, context, update.effective_message.text or "")


async def remove_by_text(update: Update, context: ContextTypes.DEFAULT_TYPE, value: str) -> int:
    store: WatchStore = context.application.bot_data["store"]
    provider: AISProvider = context.application.bot_data["provider"]
    try:
        query_type, query_value = parse_vessel_identifier(value)
    except ValueError:
        await update.effective_message.reply_text("לא זיהיתי MMSI או IMO תקין. נסו שוב.")
        return ASK_REMOVE_VESSEL

    removed_count = store.remove_by_vessel(update.effective_chat.id, query_value)
    if removed_count == 0 and query_type == "imo":
        try:
            position = await fetch_position_by_identifier(provider, query_type, query_value)
        except Exception as exc:
            logger.warning("IMO lookup failed during remove for %s: %s", query_value, exc)
        else:
            if position.mmsi:
                removed_count = store.remove_by_vessel(update.effective_chat.id, position.mmsi)

    context.user_data.clear()
    if removed_count:
        await update.effective_message.reply_text(
            f"המעקב עבור {query_value} הוסר מהרשימה.",
            reply_markup=START_KEYBOARD,
        )
    else:
        await update.effective_message.reply_text(
            f"לא נמצא מעקב פעיל עבור {query_value}.",
            reply_markup=START_KEYBOARD,
        )
    return ConversationHandler.END


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = f"{random.randint(100, 999)}"
    context.user_data.clear()
    context.user_data["reset_code"] = code
    await update.effective_message.reply_text(f"הזינו את הקוד הבא לאישור: {code}")
    return ASK_RESET_CODE


async def receive_reset_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    expected_code = context.user_data.get("reset_code")
    provided_code = (update.effective_message.text or "").strip()
    if provided_code != expected_code:
        await update.effective_message.reply_text("הקוד שגוי. נסו שוב או שלחו /cancel לביטול.")
        return ASK_RESET_CODE

    store: WatchStore = context.application.bot_data["store"]
    store.stop_all_active(update.effective_chat.id)
    context.user_data.clear()
    await update.effective_message.reply_text("כל המעקבים בוטלו.", reply_markup=START_KEYBOARD)
    return ConversationHandler.END


async def empty_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = f"{random.randint(10000, 99999)}"
    context.user_data.clear()
    context.user_data["empty_list_code"] = code
    await update.effective_message.reply_text(
        f"אם אתם באמת רוצים לנקות את הרשימה, הקלידו את הקוד הבא: {code}"
    )
    return ASK_EMPTY_LIST_CODE


async def receive_empty_list_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    expected_code = context.user_data.get("empty_list_code")
    provided_code = (update.effective_message.text or "").strip()
    if provided_code != expected_code:
        await update.effective_message.reply_text("הקוד שגוי. נסו שוב או בחרו פקודה אחרת.")
        return ASK_EMPTY_LIST_CODE

    store: WatchStore = context.application.bot_data["store"]
    deleted_count = store.delete_inactive(update.effective_chat.id)
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"הרשימה נוקתה. נמחקו {deleted_count} מעקבים לא פעילים.",
        reply_markup=START_KEYBOARD,
    )
    return ConversationHandler.END


async def receive_vessel_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    provider: AISProvider = context.application.bot_data["provider"]
    try:
        query_type, query_value = parse_vessel_identifier(update.effective_message.text or "")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ASK_VESSEL

    lookup_message = await update.effective_message.reply_text("בודק נתוני AIS עדכניים. זה עשוי לקחת עד דקה.")
    try:
        position = await fetch_position_by_identifier(provider, query_type, query_value)
    except Exception as exc:
        logger.warning("Initial AIS lookup failed for %s %s: %s", query_type, query_value, exc)
        await lookup_message.edit_text(
            "לא הצלחתי לקבל מידע עדכני לכלי השיט הזה. בדקו את המספר ונסו שוב."
        )
        return ASK_VESSEL

    stored_query_type = "mmsi" if position.mmsi else query_type
    stored_query_value = position.mmsi or query_value
    store: WatchStore = context.application.bot_data["store"]
    if store.has_active_vessel(update.effective_chat.id, stored_query_value):
        context.user_data.clear()
        await lookup_message.edit_text("כלי השיט הזה כבר נמצא במעקב.")
        return ConversationHandler.END

    context.user_data["query_type"] = stored_query_type
    context.user_data["query_value"] = stored_query_value
    await lookup_message.edit_text(vessel_info_text(position), parse_mode=ParseMode.HTML)
    await update.effective_message.reply_text(RADIUS_PROMPT)
    return ASK_RADIUS


async def receive_radius(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    app_data = context.application.bot_data
    settings: Settings = app_data["settings"]

    try:
        radius_nm = parse_radius_nm(update.effective_message.text or "")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ASK_RADIUS

    context.user_data["radius_nm"] = radius_nm
    if settings.observer_lat is not None and settings.observer_lon is not None:
        await create_watch_from_dialog(update, context, settings.observer_lat, settings.observer_lon)
        return ConversationHandler.END

    location_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("שליחת מיקום", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.effective_message.reply_text(LOCATION_PROMPT, reply_markup=location_keyboard)
    return ASK_LOCATION


async def receive_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    location = update.effective_message.location
    if location is None:
        await update.effective_message.reply_text(LOCATION_PROMPT)
        return ASK_LOCATION

    await create_watch_from_dialog(update, context, location.latitude, location.longitude)
    return ConversationHandler.END


async def create_watch_from_dialog(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    center_lat: float,
    center_lon: float,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: WatchStore = context.application.bot_data["store"]
    query_type = str(context.user_data["query_type"])
    query_value = str(context.user_data["query_value"])
    radius_nm = float(context.user_data["radius_nm"])
    radius_km = radius_nm * KM_PER_NAUTICAL_MILE

    watch_id = store.add_watch(
        chat_id=update.effective_chat.id,
        query_type=query_type,
        query_value=query_value,
        center_lat=center_lat,
        center_lon=center_lon,
        radius_km=radius_km,
        interval_minutes=settings.default_interval_minutes,
    )
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"המעקב התחיל.\n"
        f"כלי שיט: {query_type.upper()} {query_value}\n"
        f"רדיוס: {radius_nm:g} מייל ימי\n"
        f"מזהה מעקב: {watch_id}",
        reply_markup=START_KEYBOARD,
    )


def parse_vessel_identifier(value: str) -> tuple[str, str]:
    cleaned = value.strip().upper().replace("IMO", "").replace("MMSI", "")
    cleaned = re.sub(r"[^0-9]", "", cleaned)
    if len(cleaned) == 9:
        return "mmsi", cleaned
    if len(cleaned) == 7:
        return "imo", cleaned
    raise ValueError("הזינו MMSI תקין בן 9 ספרות או IMO תקין בן 7 ספרות.")


def parse_radius_nm(value: str) -> float:
    cleaned = value.strip().replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d)?", cleaned):
        raise ValueError("הזינו מרחק במיילים ימיים: מספר שלם או ספרה אחת אחרי הנקודה.")
    radius_nm = float(cleaned)
    if radius_nm <= 0:
        raise ValueError("הרדיוס חייב להיות גדול מאפס.")
    return radius_nm


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app_data = context.application.bot_data
    settings: Settings = app_data["settings"]
    store: WatchStore = app_data["store"]

    try:
        args = shlex.split(update.effective_message.text.partition(" ")[2])
        parsed = parse_watch_args(args, settings.default_interval_minutes, settings.min_interval_minutes)
    except ValueError as exc:
        await update.effective_message.reply_text(f"שגיאה: {exc}\n\n{HELP_TEXT}", reply_markup=START_KEYBOARD)
        return

    watch_id = store.add_watch(chat_id=update.effective_chat.id, **parsed)
    await update.effective_message.reply_text(
        "המעקב נוצר.\n"
        f"מזהה: {watch_id}\n"
        f"כלי שיט: {parsed['query_type'].upper()} {parsed['query_value']}\n"
        f"נקודת מדידה: {parsed['center_lat']:.5f}, {parsed['center_lon']:.5f}\n"
        f"רדיוס: {parsed['radius_km']:.2f} ק״מ\n"
        f"מרווח בדיקה: {parsed['interval_minutes']} דקות",
        reply_markup=START_KEYBOARD,
    )


def parse_watch_args(args: list[str], default_interval: int, min_interval: int) -> dict:
    if len(args) not in (5, 6):
        raise ValueError("ארגומנטים לא תקינים לפקודת /watch.")

    query_type = args[0].lower()
    if query_type not in {"mmsi", "imo", "name"}:
        raise ValueError("הארגומנט הראשון חייב להיות mmsi, imo או name.")

    query_value = args[1].strip()
    if query_type == "mmsi" and not query_value.isdigit():
        raise ValueError("MMSI חייב להכיל ספרות בלבד.")

    center_lat = float(args[2])
    center_lon = float(args[3])
    radius_km = float(args[4])
    interval_minutes = int(args[5]) if len(args) == 6 else default_interval

    if not -90 <= center_lat <= 90:
        raise ValueError("קו רוחב חייב להיות בין ‎-90 ל-90.")
    if not -180 <= center_lon <= 180:
        raise ValueError("קו אורך חייב להיות בין ‎-180 ל-180.")
    if radius_km <= 0:
        raise ValueError("הרדיוס חייב להיות גדול מאפס.")
    if interval_minutes < min_interval:
        raise ValueError(f"מרווח הבדיקה חייב להיות לפחות {min_interval} דקות.")

    return {
        "query_type": query_type,
        "query_value": query_value,
        "center_lat": center_lat,
        "center_lon": center_lon,
        "radius_km": radius_km,
        "interval_minutes": interval_minutes,
    }


async def list_watches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: WatchStore = context.application.bot_data["store"]
    watches = store.list_for_chat(update.effective_chat.id)
    if not watches:
        await update.effective_message.reply_text("אין מעקבים פעילים כרגע.", reply_markup=START_KEYBOARD)
        return

    lines = []
    for item in watches:
        state = "פעיל"
        if item.triggered:
            state = "התריע"
        elif not item.active:
            state = "נעצר"
        distance = "לא זמין" if item.last_distance_km is None else f"{item.last_distance_km / KM_PER_NAUTICAL_MILE:.2f} מייל ימי"
        lines.append(
            f"#{item.id} {state}: {item.query_type.upper()} {item.query_value}, "
            f"רדיוס {item.radius_km / KM_PER_NAUTICAL_MILE:g} מייל ימי, מרחק אחרון {distance}"
        )
    await update.effective_message.reply_text("\n".join(lines))


def _single_int_arg(args: list[str]) -> int | None:
    if len(args) != 1 or not args[0].isdigit():
        return None
    return int(args[0])


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("בודק עכשיו מעקבים שממתינים לבדיקה.")
    await run_due_checks(context)


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_due_checks(context)


async def run_due_checks(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: WatchStore = context.application.bot_data["store"]
    provider: AISProvider = context.application.bot_data["provider"]
    due = store.due_watches()
    if not due:
        return

    semaphore = asyncio.Semaphore(3)

    async def handle(item: Watch) -> None:
        async with semaphore:
            await check_watch(context, store, provider, item)

    await asyncio.gather(*(handle(item) for item in due))


async def check_watch(
    context: ContextTypes.DEFAULT_TYPE,
    store: WatchStore,
    provider: AISProvider,
    watch_item: Watch,
) -> None:
    try:
        position = await fetch_position(provider, watch_item)
    except Exception as exc:
        logger.warning("AIS check failed for watch %s: %s", watch_item.id, exc)
        store.mark_checked(watch_item.id, None, None)
        return

    distance = haversine_km(
        watch_item.center_lat,
        watch_item.center_lon,
        position.lat,
        position.lon,
    )

    if distance <= watch_item.radius_km:
        store.mark_triggered(watch_item.id, distance, position.timestamp)
        await context.bot.send_message(
            chat_id=watch_item.chat_id,
            text=alert_text(watch_item, position, distance),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        store.mark_checked(watch_item.id, distance, position.timestamp)


async def fetch_position_by_identifier(provider: AISProvider, query_type: str, query_value: str) -> VesselPosition:
    if query_type == "mmsi":
        return await provider.latest_position_by_mmsi(query_value)
    return await provider.latest_position_by_imo(query_value)


async def fetch_position(provider: AISProvider, watch_item: Watch) -> VesselPosition:
    if watch_item.query_type == "mmsi":
        return await provider.latest_position_by_mmsi(watch_item.query_value)
    if watch_item.query_type == "imo":
        return await provider.latest_position_by_imo(watch_item.query_value)
    return await provider.latest_position_by_name(watch_item.query_value)


def alert_text(watch_item: Watch, position: VesselPosition, distance_km: float) -> str:
    title = escape(position.name or watch_item.query_value)
    mmsi = f"\nMMSI: <code>{position.mmsi}</code>" if position.mmsi else ""
    seen_at = format_jerusalem_time(position.timestamp)
    seen = f"\nעדכון אחרון: <code>{seen_at}</code>" if seen_at else ""
    speed = f"\nמהירות: <code>{position.speed_knots:g} קשר</code>" if position.speed_knots is not None else ""
    return (
        f"כלי השיט נכנס לרדיוס שהוגדר.\n"
        f"המעקב נעצר אוטומטית.\n"
        f"מעקב #{watch_item.id}: <b>{title}</b>{mmsi}\n"
        f"מרחק: <b>{distance_km / KM_PER_NAUTICAL_MILE:.2f} מייל ימי</b> / "
        f"רדיוס {watch_item.radius_km / KM_PER_NAUTICAL_MILE:g} מייל ימי\n"
        f"מיקום: <code>{position.lat:.5f}, {position.lon:.5f}</code>"
        f"{seen}{speed}"
    )


def vessel_info_text(position: VesselPosition) -> str:
    name = escape(position.name or "לא זמין")
    course = f"{position.course:g}°" if position.course is not None else "לא זמין"
    speed = f"{position.speed_knots:g} קשר" if position.speed_knots is not None else "לא זמין"
    timestamp = format_jerusalem_time(position.timestamp) or "לא זמין"
    mmsi = position.mmsi or "לא זמין"
    return (
        f"נמצאו נתוני כלי שיט:\n"
        f"שם: <b>{name}</b>\n"
        f"MMSI: <code>{mmsi}</code>\n"
        f"כיוון: <code>{course}</code>\n"
        f"מהירות: <code>{speed}</code>\n"
        f"עדכון אחרון: <code>{timestamp}</code>"
    )


def format_jerusalem_time(value: str | None) -> str | None:
    parsed = parse_ais_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(JERUSALEM_TZ).strftime("%d/%m/%y %H:%M")


def parse_ais_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number = number // 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)

    candidates = [
        text,
        text.replace("Z", "+00:00"),
        text.replace(" UTC", "+00:00"),
    ]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    known_formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]
    for fmt in known_formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    logger.warning("Could not parse AIS timestamp: %s", value)
    return None


def build_app(settings: Settings) -> Application:
    if not settings.telegram_bot_token:
        raise AISProviderError("TELEGRAM_BOT_TOKEN is not configured")

    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    application.bot_data["settings"] = settings
    application.bot_data["store"] = WatchStore(settings.database_path)
    application.bot_data["provider"] = build_provider(settings)

    conversation = ConversationHandler(
        entry_points=[CommandHandler(["start", "new"], start)],
        states={
            ASK_VESSEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_vessel_number)],
            ASK_RADIUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_radius)],
            ASK_LOCATION: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_location)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    remove_conversation = ConversationHandler(
        entry_points=[CommandHandler("remove", remove_command)],
        states={
            ASK_REMOVE_VESSEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remove_vessel)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    reset_conversation = ConversationHandler(
        entry_points=[CommandHandler("reset", reset_command)],
        states={
            ASK_RESET_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reset_code)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    empty_list_conversation = ConversationHandler(
        entry_points=[CommandHandler("empty_list", empty_list_command)],
        states={
            ASK_EMPTY_LIST_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_empty_list_code)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conversation)
    application.add_handler(remove_conversation)
    application.add_handler(reset_conversation)
    application.add_handler(empty_list_conversation)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("watch", watch))
    application.add_handler(CommandHandler("list", list_watches))
    application.job_queue.run_repeating(
        scheduled_check,
        interval=60,
        first=10,
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    return application


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, reply_markup=START_KEYBOARD)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "התחלת מעקב חדש"),
            BotCommand("list", "רשימת מעקבים"),
            BotCommand("remove", "הסרת מעקב לפי MMSI/IMO"),
            BotCommand("reset", "ביטול כל המעקבים"),
            BotCommand("empty_list", "ניקוי מעקבים לא פעילים"),
            BotCommand("help", "עזרה"),
        ]
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    application = build_app(settings)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
