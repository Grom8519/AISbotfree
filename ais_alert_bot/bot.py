from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from html import escape
import logging
from math import cos, radians
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
from .geo import haversine_km, initial_bearing_degrees
from .sheets import GoogleSheetsLogger
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
MONITOR_WINDOW_NM = 2.0
SERIOUS_SPEED_CHANGE_RATIO = 0.40
SERIOUS_COURSE_CHANGE_DEGREES = 30.0
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
    settings: Settings = context.application.bot_data["settings"]
    provider: AISProvider = context.application.bot_data["provider"]
    try:
        query_type, query_value = parse_vessel_identifier(update.effective_message.text or "")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ASK_VESSEL

    lookup_message = await update.effective_message.reply_text(
        f"\u05de\u05e1\u05e4\u05e8 {query_type.upper()} \u05e0\u05e7\u05dc\u05d8: <code>{escape(query_value)}</code>.\n"
        "\u05d1\u05d5\u05d3\u05e7 \u05d0\u05dd \u05d9\u05e9 \u05e0\u05ea\u05d5\u05e0\u05d9 AIS/GPS \u05d6\u05de\u05d9\u05e0\u05d9\u05dd \u05db\u05e8\u05d2\u05e2.",
        parse_mode=ParseMode.HTML,
    )
    try:
        position = await asyncio.wait_for(
            fetch_position_by_identifier(provider, query_type, query_value),
            timeout=settings.initial_lookup_seconds,
        )
    except Exception as exc:
        logger.warning(
            "Initial AIS lookup failed for %s %s: %r (%s)",
            query_type,
            query_value,
            exc,
            type(exc).__name__,
        )
        store: WatchStore = context.application.bot_data["store"]
        cached_name = store.latest_vessel_name(query_value)
        if store.has_active_vessel(update.effective_chat.id, query_value):
            context.user_data.clear()
            await lookup_message.edit_text("\u05db\u05dc\u05d9 \u05d4\u05e9\u05d9\u05d8 \u05d4\u05d6\u05d4 \u05db\u05d1\u05e8 \u05e0\u05de\u05e6\u05d0 \u05d1\u05de\u05e2\u05e7\u05d1.")
            return ConversationHandler.END
        context.user_data["query_type"] = query_type
        context.user_data["query_value"] = query_value
        context.user_data["initial_data_stale"] = True
        context.user_data["vessel_name"] = cached_name or ""
        name_line = f"\n\u05e9\u05dd \u05db\u05dc\u05d9 \u05e9\u05d9\u05d8: <b>{escape(cached_name)}</b>\n" if cached_name else ""
        await lookup_message.edit_text(
            f"\u05de\u05e1\u05e4\u05e8 {query_type.upper()} \u05e0\u05e7\u05dc\u05d8: <code>{escape(query_value)}</code>.\n"
            f"{name_line}"
            "\u05dc\u05d0 \u05d4\u05ea\u05e7\u05d1\u05dc\u05d5 \u05db\u05e8\u05d2\u05e2 \u05e0\u05ea\u05d5\u05e0\u05d9 AIS/GPS \u05d6\u05de\u05d9\u05e0\u05d9\u05dd \u05dc\u05db\u05dc\u05d9 \u05d4\u05e9\u05d9\u05d8 \u05d4\u05d6\u05d4. "
            "\u05d4\u05de\u05e2\u05e7\u05d1 \u05e2\u05d3\u05d9\u05d9\u05df \u05d9\u05ea\u05d7\u05d9\u05dc, \u05d5\u05d4\u05d1\u05d5\u05d8 \u05d9\u05de\u05e9\u05d9\u05da \u05dc\u05d7\u05e4\u05e9 \u05e2\u05d3\u05db\u05d5\u05e0\u05d9 GPS \u05d1\u05e8\u05e7\u05e2.",
            parse_mode=ParseMode.HTML,
        )
        await update.effective_message.reply_text(RADIUS_PROMPT)
        return ASK_RADIUS

    stored_query_type = "mmsi" if position.mmsi else query_type
    stored_query_value = position.mmsi or query_value
    store: WatchStore = context.application.bot_data["store"]
    if store.has_active_vessel(update.effective_chat.id, stored_query_value):
        context.user_data.clear()
        await lookup_message.edit_text("\u05db\u05dc\u05d9 \u05d4\u05e9\u05d9\u05d8 \u05d4\u05d6\u05d4 \u05db\u05d1\u05e8 \u05e0\u05de\u05e6\u05d0 \u05d1\u05de\u05e2\u05e7\u05d1.")
        return ConversationHandler.END

    context.user_data["query_type"] = stored_query_type
    context.user_data["query_value"] = stored_query_value
    context.user_data["initial_data_stale"] = is_stale_position(position.timestamp, settings.stale_position_minutes)
    context.user_data["vessel_name"] = position.name or ""
    await lookup_message.edit_text(
        vessel_info_text(position, settings.stale_position_minutes),
        parse_mode=ParseMode.HTML,
    )
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
    vessel_name = str(context.user_data.get("vessel_name") or "")
    radius_nm = float(context.user_data["radius_nm"])
    radius_km = radius_nm * KM_PER_NAUTICAL_MILE
    initial_data_stale = bool(context.user_data.get("initial_data_stale"))
    interval_minutes = (
        settings.stale_refresh_interval_minutes
        if initial_data_stale
        else settings.default_interval_minutes
    )

    watch_id = store.add_watch(
        chat_id=update.effective_chat.id,
        query_type=query_type,
        query_value=query_value,
        center_lat=center_lat,
        center_lon=center_lon,
        radius_km=radius_km,
        interval_minutes=interval_minutes,
        vessel_name=vessel_name or None,
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
    settings: Settings = context.application.bot_data["settings"]
    try:
        position = await fetch_position(provider, watch_item)
    except Exception as exc:
        logger.warning("AIS check failed for watch %s: %s", watch_item.id, exc)
        if is_due_utc(watch_item.predicted_alert_at):
            distance = watch_item.last_distance_km or watch_item.radius_km
            if not watch_item.calculation_alert_sent:
                sent = await safe_send_message(
                    context,
                    chat_id=watch_item.chat_id,
                    text=calculated_alert_without_position(watch_item, distance),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                if sent:
                    await log_sheet_alert(context, watch_item, None, distance, "calculation")
            else:
                sent = True
            predicted_alert_at = None if sent else watch_item.predicted_alert_at
            store.mark_checked(
                watch_item.id,
                watch_item.last_distance_km,
                watch_item.last_seen_at,
                predicted_alert_at,
                watch_item.last_speed_knots,
                watch_item.last_course,
                watch_item.eta_unavailable_notified,
                sent,
                watch_item.last_position_stale,
            )
            return
        store.mark_checked(
            watch_item.id,
            watch_item.last_distance_km,
            watch_item.last_seen_at,
            watch_item.predicted_alert_at,
            watch_item.last_speed_knots,
            watch_item.last_course,
            watch_item.eta_unavailable_notified,
            watch_item.calculation_alert_sent,
            watch_item.last_position_stale,
        )
        return

    distance = haversine_km(
        watch_item.center_lat,
        watch_item.center_lon,
        position.lat,
        position.lon,
    )

    if is_stale_position(position.timestamp, settings.stale_position_minutes):
        if watch_item.interval_minutes != settings.stale_refresh_interval_minutes:
            store.set_interval(watch_item.id, settings.stale_refresh_interval_minutes)
        store.mark_checked(
            watch_item.id,
            distance,
            position.timestamp,
            None,
            position.speed_knots,
            position.course,
            watch_item.eta_unavailable_notified,
            watch_item.calculation_alert_sent,
            True,
            position.name,
        )
        return

    if watch_item.interval_minutes != settings.default_interval_minutes:
        store.set_interval(watch_item.id, settings.default_interval_minutes)

    if distance <= watch_item.radius_km:
        store.mark_triggered(watch_item.id, distance, position.timestamp, position.name)
        sent = await safe_send_message(
            context,
            chat_id=watch_item.chat_id,
            text=alert_text(watch_item, position, distance, "gps"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if sent:
            await log_sheet_alert(context, watch_item, position, distance, "gps")
        return

    predicted_due = is_due_utc(watch_item.predicted_alert_at)
    calculation_alert_sent = watch_item.calculation_alert_sent
    if predicted_due and not calculation_alert_sent:
        sent = await safe_send_message(
            context,
            chat_id=watch_item.chat_id,
            text=alert_text(watch_item, position, distance, "calculation"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if sent:
            calculation_alert_sent = True
            await log_sheet_alert(context, watch_item, position, distance, "calculation")

    predicted_alert_at = calculate_predicted_alert_at_from_watch(
        watch_item,
        distance / KM_PER_NAUTICAL_MILE,
        watch_item.radius_km / KM_PER_NAUTICAL_MILE,
        position.lat,
        position.lon,
        position.timestamp,
        position.speed_knots,
        position.course,
    )
    eta_unavailable_notified = watch_item.eta_unavailable_notified
    if predicted_alert_at is None and not eta_unavailable_notified:
        eta_unavailable_notified = True
        await safe_send_message(
            context,
            chat_id=watch_item.chat_id,
            text="לא ניתן לחשב זמן הגעה לפי מהירות, כי המהירות היא אפס או לא זמינה. אמשיך לעקוב לפי נתוני GPS.",
        )

    await maybe_notify_serious_change(context, watch_item, position, distance)
    store.mark_checked(
        watch_item.id,
        distance,
        position.timestamp,
        to_sqlite_utc(predicted_alert_at),
        position.speed_knots,
        position.course,
        eta_unavailable_notified,
        calculation_alert_sent,
        False,
        position.name,
    )


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


def alert_text(watch_item: Watch, position: VesselPosition, distance_km: float, source: str) -> str:
    title = escape(position.name or watch_item.query_value)
    mmsi = f"\nMMSI: <code>{position.mmsi}</code>" if position.mmsi else ""
    seen_at = format_jerusalem_time(position.timestamp)
    seen = f"\nעדכון אחרון: <code>{seen_at}</code>" if seen_at else ""
    speed = f"\nמהירות: <code>{position.speed_knots:g} קשר</code>" if position.speed_knots is not None else ""
    source_text = "על בסיס נתוני GPS" if source == "gps" else "על בסיס חישוב זמן = מרחק / מהירות"
    return (
        f"כלי השיט נכנס לרדיוס שהוגדר.\n"
        f"שיטת זיהוי: {source_text}.\n"
        f"{'המעקב נעצר אוטומטית.' if source == 'gps' else 'המעקב ממשיך עד לקבלת התראת GPS.'}\n"
        f"מעקב #{watch_item.id}: <b>{title}</b>{mmsi}\n"
        f"מרחק: <b>{distance_km / KM_PER_NAUTICAL_MILE:.2f} מייל ימי</b> / "
        f"רדיוס {watch_item.radius_km / KM_PER_NAUTICAL_MILE:g} מייל ימי\n"
        f"מיקום: <code>{position.lat:.5f}, {position.lon:.5f}</code>"
        f"{seen}{speed}"
    )


def calculated_alert_without_position(watch_item: Watch, distance_km: float) -> str:
    return (
        "כלי השיט הגיע לרדיוס שהוגדר על בסיס חישוב זמן = מרחק / מהירות.\n"
        "לא התקבל עדכון GPS חדש ברגע החישוב, לכן ההתרעה מבוססת על הנתונים האחרונים שהיו זמינים.\n"
        "המעקב ממשיך עד לקבלת התראת GPS.\n"
        f"מעקב #{watch_item.id}: <b>{escape(watch_item.query_value)}</b>\n"
        f"מרחק אחרון: <b>{distance_km / KM_PER_NAUTICAL_MILE:.2f} מייל ימי</b> / "
        f"רדיוס {watch_item.radius_km / KM_PER_NAUTICAL_MILE:g} מייל ימי"
    )


async def log_sheet_alert(
    context: ContextTypes.DEFAULT_TYPE,
    watch_item: Watch,
    position: VesselPosition | None,
    distance_km: float,
    source: str,
) -> None:
    sheets_logger: GoogleSheetsLogger | None = context.application.bot_data.get("sheets_logger")
    if sheets_logger is None or not sheets_logger.enabled:
        return

    event_time = datetime.now(JERUSALEM_TZ)
    distance_nm = distance_km / KM_PER_NAUTICAL_MILE
    await asyncio.to_thread(
        sheets_logger.append_alert,
        event_time,
        position.name if position and position.name else watch_item.query_value,
        watch_item.radius_km / KM_PER_NAUTICAL_MILE,
        distance_nm if source == "gps" else None,
        distance_nm if source == "calculation" else None,
    )


async def maybe_notify_serious_change(
    context: ContextTypes.DEFAULT_TYPE,
    watch_item: Watch,
    position: VesselPosition,
    distance_km: float,
) -> None:
    distance_nm = distance_km / KM_PER_NAUTICAL_MILE
    radius_nm = watch_item.radius_km / KM_PER_NAUTICAL_MILE
    if not radius_nm < distance_nm <= radius_nm + MONITOR_WINDOW_NM:
        return

    messages = []
    if (
        watch_item.last_speed_knots is not None and
        position.speed_knots is not None and
        watch_item.last_speed_knots > 0
    ):
        speed_change = abs(position.speed_knots - watch_item.last_speed_knots) / watch_item.last_speed_knots
        if speed_change > SERIOUS_SPEED_CHANGE_RATIO:
            messages.append(
                f"המהירות השתנתה ביותר מ-40%: "
                f"{watch_item.last_speed_knots:g} קשר -> {position.speed_knots:g} קשר"
            )

    if watch_item.last_course is not None and position.course is not None:
        course_change = angular_difference_degrees(position.course, watch_item.last_course)
        if course_change > SERIOUS_COURSE_CHANGE_DEGREES:
            messages.append(
                f"הקורס השתנה ביותר מ-30°: "
                f"{watch_item.last_course:g}° -> {position.course:g}°"
            )

    if not messages:
        return

    await safe_send_message(
        context,
        chat_id=watch_item.chat_id,
        text=(
            "זוהה שינוי משמעותי בנתוני התנועה בשתי המיילים הימיים האחרונים לפני הרדיוס:\n"
            + "\n".join(messages)
        ),
    )


async def safe_send_message(context: ContextTypes.DEFAULT_TYPE, **kwargs: object) -> bool:
    try:
        await context.bot.send_message(**kwargs)
    except Exception as exc:
        logger.warning("Telegram send failed for chat %s: %s", kwargs.get("chat_id"), exc)
        return False
    return True


def calculate_predicted_alert_at(
    distance_nm: float,
    radius_nm: float,
    speed_knots: float | None,
    now: datetime | None = None,
) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    if speed_knots is None or speed_knots <= 0:
        return None
    remaining_nm = distance_nm - radius_nm
    if remaining_nm <= 0:
        return now
    return now + timedelta(hours=remaining_nm / speed_knots)


def calculate_predicted_alert_at_from_movement(
    previous_distance_nm: float | None,
    current_distance_nm: float,
    radius_nm: float,
    previous_seen_at: datetime | None,
    current_seen_at: datetime,
    fallback_speed_knots: float | None = None,
) -> datetime | None:
    remaining_nm = current_distance_nm - radius_nm
    if remaining_nm <= 0:
        return current_seen_at

    if previous_distance_nm is None or previous_seen_at is None:
        return calculate_predicted_alert_at(
            current_distance_nm,
            radius_nm,
            fallback_speed_knots,
            now=current_seen_at,
        )

    elapsed_hours = (current_seen_at - previous_seen_at).total_seconds() / 3600
    if elapsed_hours <= 0:
        return None

    closing_speed_knots = (previous_distance_nm - current_distance_nm) / elapsed_hours
    if closing_speed_knots <= 0:
        return None

    return current_seen_at + timedelta(hours=remaining_nm / closing_speed_knots)


def calculate_predicted_alert_at_from_watch(
    watch_item: Watch,
    current_distance_nm: float,
    radius_nm: float,
    vessel_lat: float,
    vessel_lon: float,
    current_seen_at_raw: str | None,
    fallback_speed_knots: float | None,
    course_degrees: float | None,
) -> datetime | None:
    current_seen_at = parse_ais_timestamp(current_seen_at_raw) or datetime.now(timezone.utc)
    projected_speed_knots = calculate_projected_speed_toward_center(
        vessel_lat,
        vessel_lon,
        watch_item.center_lat,
        watch_item.center_lon,
        fallback_speed_knots,
        course_degrees,
    )
    if projected_speed_knots is not None:
        return calculate_predicted_alert_at(
            current_distance_nm,
            radius_nm,
            projected_speed_knots,
            now=current_seen_at,
        )

    previous_seen_at = parse_ais_timestamp(watch_item.last_seen_at)
    previous_distance_nm = (
        watch_item.last_distance_km / KM_PER_NAUTICAL_MILE
        if watch_item.last_distance_km is not None
        else None
    )
    return calculate_predicted_alert_at_from_movement(
        previous_distance_nm,
        current_distance_nm,
        radius_nm,
        previous_seen_at,
        current_seen_at,
        fallback_speed_knots,
    )


def calculate_projected_speed_toward_center(
    vessel_lat: float,
    vessel_lon: float,
    center_lat: float,
    center_lon: float,
    speed_knots: float | None,
    course_degrees: float | None,
) -> float | None:
    if speed_knots is None or speed_knots <= 0 or course_degrees is None:
        return None

    bearing_to_center = initial_bearing_degrees(vessel_lat, vessel_lon, center_lat, center_lon)
    angle = angular_difference_degrees(course_degrees, bearing_to_center)
    projected_speed = speed_knots * cos(radians(angle))
    if projected_speed <= 0:
        return None
    return projected_speed


def angular_difference_degrees(current: float, previous: float) -> float:
    return abs((current - previous + 180) % 360 - 180)


def to_sqlite_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def is_due_utc(value: str | None) -> bool:
    parsed = parse_ais_timestamp(value)
    if parsed is None:
        return False
    return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)


async def list_watches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: WatchStore = context.application.bot_data["store"]
    watches = store.list_for_chat(update.effective_chat.id)
    if not watches:
        await update.effective_message.reply_text("\u05d0\u05d9\u05df \u05de\u05e2\u05e7\u05d1\u05d9\u05dd \u05e4\u05e2\u05d9\u05dc\u05d9\u05dd \u05db\u05e8\u05d2\u05e2.", reply_markup=START_KEYBOARD)
        return

    await update.effective_message.reply_text(
        "\n\n".join(format_watch_list_item(item) for item in watches),
        parse_mode=ParseMode.HTML,
    )


def format_watch_list_item(item: Watch) -> str:
    state = "\u05e4\u05e2\u05d9\u05dc"
    if item.triggered:
        state = "\u05d4\u05ea\u05e8\u05d9\u05e2"
    elif not item.active:
        state = "\u05e0\u05e2\u05e6\u05e8"

    name = escape(item.vessel_name or "\u05dc\u05d0 \u05d6\u05de\u05d9\u05df")
    distance = (
        "\u05dc\u05d0 \u05d6\u05de\u05d9\u05df"
        if item.last_distance_km is None
        else f"{item.last_distance_km / KM_PER_NAUTICAL_MILE:.2f} \u05de\u05d9\u05d9\u05dc \u05d9\u05de\u05d9"
    )
    speed = (
        "\u05dc\u05d0 \u05d6\u05de\u05d9\u05df"
        if item.last_speed_knots is None
        else f"{item.last_speed_knots:g} \u05e7\u05e9\u05e8"
    )
    course = "\u05dc\u05d0 \u05d6\u05de\u05d9\u05df" if item.last_course is None else f"{item.last_course:g}\u00b0"
    updated = format_jerusalem_time(item.last_seen_at) or "\u05dc\u05d0 \u05d6\u05de\u05d9\u05df"
    eta = format_jerusalem_time(item.predicted_alert_at) or "\u05dc\u05d0 \u05d6\u05de\u05d9\u05df"
    freshness = "\u05d9\u05e9\u05df" if item.last_position_stale else "\u05e2\u05d3\u05db\u05e0\u05d9"

    return (
        f"#{item.id} <b>{state}</b>\n"
        f"\u05e9\u05dd: <b>{name}</b>\n"
        f"{item.query_type.upper()}: <code>{escape(item.query_value)}</code>\n"
        f"\u05e8\u05d3\u05d9\u05d5\u05e1: <code>{item.radius_km / KM_PER_NAUTICAL_MILE:g} \u05de\u05d9\u05d9\u05dc \u05d9\u05de\u05d9</code>\n"
        f"\u05de\u05e8\u05d7\u05e7 \u05d0\u05d7\u05e8\u05d5\u05df: <code>{distance}</code>\n"
        f"\u05de\u05d4\u05d9\u05e8\u05d5\u05ea: <code>{speed}</code>\n"
        f"\u05db\u05d9\u05d5\u05d5\u05df: <code>{course}</code>\n"
        f"ETA: <code>{eta}</code>\n"
        f"\u05e2\u05d3\u05db\u05d5\u05df \u05d0\u05d7\u05e8\u05d5\u05df: <code>{updated}</code> ({freshness})"
    )


def vessel_info_text(position: VesselPosition, stale_after_minutes: int | None = None) -> str:
    name = escape(position.name or "לא זמין")
    course = f"{position.course:g}°" if position.course is not None else "לא זמין"
    speed = f"{position.speed_knots:g} קשר" if position.speed_knots is not None else "לא זמין"
    timestamp = format_jerusalem_time(position.timestamp) or "לא זמין"
    mmsi = position.mmsi or "לא זמין"
    stale_prefix = stale_position_prefix(position.timestamp, stale_after_minutes)
    return (
        f"{stale_prefix}"
        f"נמצאו נתוני כלי שיט:\n"
        f"שם: <b>{name}</b>\n"
        f"MMSI: <code>{mmsi}</code>\n"
        f"כיוון: <code>{course}</code>\n"
        f"מהירות: <code>{speed}</code>\n"
        f"עדכון אחרון: <code>{timestamp}</code>"
    )


def stale_position_prefix(timestamp: str | None, stale_after_minutes: int | None) -> str:
    if stale_after_minutes is None or stale_after_minutes <= 0:
        return ""

    parsed = parse_ais_timestamp(timestamp)
    if parsed is None:
        return ""

    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    if age <= timedelta(minutes=stale_after_minutes):
        return ""

    seen_at = format_jerusalem_time(timestamp) or "לא זמין"
    return (
        "שימו לב: זה אינו מידע חדש.\n"
        f"העדכון האחרון התקבל ב-<code>{seen_at}</code>.\n\n"
    )


def is_stale_position(timestamp: str | None, stale_after_minutes: int | None) -> bool:
    if stale_after_minutes is None or stale_after_minutes <= 0:
        return False

    parsed = parse_ais_timestamp(timestamp)
    if parsed is None:
        return True

    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return age > timedelta(minutes=stale_after_minutes)


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

    utc_suffix_match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d+))? ([+-]\d{4}) UTC",
        text,
    )
    if utc_suffix_match:
        base, fraction, offset = utc_suffix_match.groups()
        microseconds = (fraction or "0")[:6].ljust(6, "0")
        return datetime.strptime(
            f"{base}.{microseconds} {offset}",
            "%Y-%m-%d %H:%M:%S.%f %z",
        )

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
    application.bot_data["sheets_logger"] = GoogleSheetsLogger.from_settings(settings)

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
