import os
import asyncio
import sys
import time
import re
from pathlib import Path
from datetime import datetime, date, timedelta
from telegram import (
    ReplyKeyboardRemove,
    Update,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault
)
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters
)
from dotenv import load_dotenv
import logging
from logging.handlers import RotatingFileHandler

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

def safe_print(text: str):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"))

from google_sheets import (
    SheetsUnavailableError,
    SHEET_ID,
    append_weight_row,
    find_nomenclature,
    get_order_balance,
    get_order_net_by_date,
    get_spool_counts,
    clear_last_user_spool_number,
    mark_last_user_row_error,
    summarize_order,
    summarize_orders_by_date,
    refresh_tag_cache
)
from delete_last import delete_last_entry
from keyboards import order_keyboard, spool_keyboard, lrp_keyboard
from devices import DEVICES
from server_scales import get_scale_median
from print_google_sheet_pdf import print_google_sheet_pdf
from test import toggle_test_mode, is_test_mode

# === ENV ===
PROJECT_ENV_PATH = "Project.env"
load_dotenv(PROJECT_ENV_PATH)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FORWARD_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
EDITOR_USER_IDS_FILE = "EDITOR_USER_IDS.txt"

def parse_user_ids(value: str) -> set[int]:
    ids: set[int] = set()
    for part in (value or "").replace(";", ",").replace("\n", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids

def load_editor_user_ids() -> set[int]:
    try:
        raw = Path(EDITOR_USER_IDS_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return set()
    return parse_user_ids(raw)

EDITOR_USER_IDS = load_editor_user_ids()

def is_editor(user_id: int) -> bool:
    return user_id in EDITOR_USER_IDS

def is_effective_test_mode(user_id: int) -> bool:
    return (not is_editor(user_id)) or is_test_mode(user_id)

async def notify_test_mode_if_needed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_editor(user_id):
        return
    if user_id in test_notice_sent:
        return
    await send_message_with_retry(context, update.effective_chat.id, "Вы работаете в режиме тест 🔒")
    test_notice_sent.add(user_id)

async def send_message_with_retry(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    retries: int = 3,
    delay_sec: float = 1.0,
    **kwargs
):
    for attempt in range(retries):
        try:
            await context.bot.send_message(chat_id, text, **kwargs)
            return True
        except RetryAfter as e:
            if attempt == retries - 1:
                return False
            await asyncio.sleep(max(delay_sec, float(e.retry_after)))
        except (TimedOut, NetworkError):
            if attempt == retries - 1:
                return False
            await asyncio.sleep(delay_sec * (attempt + 1))

async def send_order_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, order_code: str):
    reply_to = update.message.message_id if update.message else None
    try:
        gross, net, count, nomenclature = await _run_sheet_op(summarize_order, order_code, LOG_SHEETS)
    except SheetsUnavailableError:
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            MSG_SHEETS_UNAVAILABLE,
            reply_to_message_id=reply_to
        )
        return
    if count == 0:
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            MSG_ORDER_NOT_FOUND,
            reply_to_message_id=reply_to
        )
        return
    if not nomenclature:
        nomenclature = find_nomenclature(order_code) or "Б/Н"
    msg = (
        f"{order_code}\n"
        f"{nomenclature}\n"
        f"Всего:\n"
        f"Брутто: {format_kg(gross)} кг\n"
        f"Нетто: {format_kg(net)} кг\n"
        f"Бобин: {count} шт."
    )
    reply_markup = ReplyKeyboardRemove() if update.effective_chat.type != "private" else None
    await send_message_with_retry(
        context,
        update.effective_chat.id,
        msg,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to
    )

async def send_date_report(update: Update, context: ContextTypes.DEFAULT_TYPE, report_date: date):
    reply_to = update.message.message_id if update.message else None
    try:
        summaries = await _run_sheet_op(
            summarize_orders_by_date,
            report_date,
            LOG_SHEETS,
            BUSINESS_DAY_CUTOFF_HOUR
        )
    except SheetsUnavailableError:
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            MSG_SHEETS_UNAVAILABLE,
            reply_to_message_id=reply_to
        )
        return
    if not summaries:
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            MSG_ORDERS_NOT_FOUND,
            reply_to_message_id=reply_to
        )
        return

    date_text = report_date.strftime("%d.%m.%Y")
    reply_markup = ReplyKeyboardRemove() if update.effective_chat.type != "private" else None
    await send_message_with_retry(
        context,
        update.effective_chat.id,
        f"{MSG_DATE_PREFIX}{date_text}",
        reply_markup=reply_markup,
        reply_to_message_id=reply_to
    )

    for order_code, data in summaries.items():
        nomenclature = data.get("nomenclature") or find_nomenclature(order_code) or "Б/Н"
        msg = (
            f"{order_code}\n"
            f"{nomenclature}\n"
            f"Всего:\n"
            f"Брутто: {format_kg(data['gross'])} кг\n"
            f"Нетто: {format_kg(data['net'])} кг\n"
            f"Бобин: {data['count']} шт."
        )
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            msg,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to
        )

def _start_pending_input(
    chat_id: int,
    user_id: int,
    kind: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    handler
):
    key = (chat_id, user_id)
    pending_inputs[key] = {
        "texts": [text],
        "kind": kind,
        "update": update,
        "context": context,
        "handler": handler
    }
    asyncio.create_task(_finalize_pending_input(key))

async def _finalize_pending_input(key: tuple[int, int]):
    await asyncio.sleep(PENDING_INPUT_WINDOW_SEC)
    pending = pending_inputs.pop(key, None)
    if not pending:
        return
    texts = pending["texts"]
    kind = pending["kind"]
    update = pending["update"]
    context = pending["context"]
    handler = pending["handler"]

    if len(texts) != 1:
        if kind == "spool_scan":
            msg = MSG_SPOOL_RETRY
        else:
            msg = MSG_RETRY
        await context.bot.send_message(update.effective_chat.id, msg)
        return

    try:
        await handler(texts[0])
    except Exception as e:
        logging.exception("Pending input handler failed: %s", e)

# === LOGGING ===
log_handler = RotatingFileHandler(
    "Bug_log.txt",
    maxBytes=100 * 1024 * 1024,
    backupCount=1,
    encoding="utf-8",
)
log_handler.setLevel(logging.ERROR)
log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
root_logger = logging.getLogger()
root_logger.setLevel(logging.ERROR)
root_logger.addHandler(log_handler)

# Suppress successful HTTP request logs and avoid leaking bot token in URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

NETWORK_LOG_THROTTLE_SEC = 3600.0
_network_log_last: dict[str, float] = {}

def _is_network_error(exc: Exception) -> bool:
    if isinstance(exc, (TimedOut, NetworkError)):
        return True
    text = str(exc).lower()
    return (
        "getaddrinfo failed" in text
        or "connecterror" in text
        or "network error" in text
        or "remote disconnected" in text
    )

def _log_throttled_network(level: int, key: str, message: str, *args):
    now = time.time()
    last = _network_log_last.get(key, 0.0)
    if now - last < NETWORK_LOG_THROTTLE_SEC:
        return
    _network_log_last[key] = now
    logging.log(level, message, *args)

# === Состояние пользователей ===
# chat_id → { lrp: int, code: str | None }
user_states: dict[int, dict] = {}
order_query_states: set[tuple[int, int]] = set()
date_query_states: set[tuple[int, int]] = set()
in_flight_requests: set[tuple[int, int]] = set()
LOG_SHEETS = sorted({device["log_sheet_name"] for device in DEVICES.values()})
test_notice_sent: set[int] = set()
pending_inputs: dict[tuple[int, int], dict] = {}
pending_state_tasks: dict[tuple[int, int], asyncio.Task] = {}
PENDING_INPUT_WINDOW_SEC = 2.0
IN_FLIGHT_TIMEOUT_SEC = 300.0
PENDING_STATE_TIMEOUT_SEC = 300.0
DONE_DEDUP_WINDOW_SEC = 3.0
last_done_sent: dict[tuple[int, int], float] = {}
MSG_PROCESSING_COMMAND = "\u041e\u0431\u0440\u0430\u0431\u0430\u0442\u044b\u0432\u0430\u0435\u0442\u0441\u044f \u0437\u0430\u043f\u0440\u043e\u0441 \u23f3"
MSG_PROCESSING_BALANCE = "\u0420\u0430\u0441\u0447\u0435\u0442 \u043e\u0441\u0442\u0430\u0442\u043a\u0430 \u043f\u043e \u0437\u0430\u043a\u0430\u0437\u0443 \u23f3"
MSG_RETRY = "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0434\u0430\u043d\u043d\u044b\u0435 \u0437\u0430\u043d\u043e\u0432\u043e \U0001f501"
MSG_BUSY = "\u0417\u0430\u043f\u0440\u043e\u0441 \u0443\u0436\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f. \u0414\u043e\u0436\u0434\u0438\u0442\u0435\u0441\u044c \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f.\U0001f552"
MSG_TIMEOUT = "\u0417\u0430\u043f\u0440\u043e\u0441 \u0441\u0431\u0440\u043e\u0448\u0435\u043d \u043f\u043e \u0442\u0430\u0439\u043c\u0430\u0443\u0442\u0443. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0441\u043d\u043e\u0432\u0430.\u23f0"
MSG_DATE_PROMPT = "\u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u0434\u0430\u0442\u0443 \U0001f4c5"
MSG_SPOOL_RETRY = "\u041f\u043e\u0432\u0442\u043e\u0440\u043d\u0430\u044f \u043f\u043e\u043f\u044b\u0442\u043a\u0430. \u041e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 \u0448\u043f\u0443\u043b\u044e \u0435\u0449\u0435 \u0440\u0430\u0437.\u26a0\ufe0f"
MSG_ORDER_NOT_FOUND = "\u0417\u0430\u043a\u0430\u0437 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d \u274c"
MSG_ORDERS_NOT_FOUND = "\u0417\u0430\u043a\u0430\u0437\u044b \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b \u274c"
MSG_DONE = "\u0417\u0430\u043f\u0440\u043e\u0441 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d \u2705"
MSG_SHEETS_UNAVAILABLE = "\u041d\u0435\u0442 \u0441\u0432\u044f\u0437\u0438 \u0441 Google Sheets, \u043f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u043f\u043e\u0437\u0436\u0435.\u26a0\ufe0f"
MSG_DATE_PREFIX = "\u0414\u0430\u0442\u0430: "
SHEETS_OP_TIMEOUT_SEC = 300.0
BUSINESS_DAY_CUTOFF_HOUR = 9

async def _run_sheet_op(func, *args, **kwargs):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=SHEETS_OP_TIMEOUT_SEC
        )
    except asyncio.TimeoutError as e:
        raise SheetsUnavailableError("Sheets operation timed out") from e

def _business_date(dt: datetime | None = None) -> date:
    base = dt or datetime.now()
    return (base - timedelta(hours=BUSINESS_DAY_CUTOFF_HOUR)).date()



def format_kg(value: float) -> str:
    s = f"{value:.2f}".replace(".", ",")
    if s.endswith(",00"):
        return s[:-3]
    if s.endswith("0"):
        return s[:-1]
    return s

def _format_balance_message(balance: float | None, net_today: float | None) -> str:
    if balance is None or net_today is None:
        return "Остаток заказа: нет данных⚠️"
    remaining = balance - net_today
    return f"Остаток заказа: {format_kg(remaining)} кг ➡️"

async def _send_balance_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    balance: float | None,
    net_today: float | None
):
    reply_to = update.message.message_id if update.message else None
    await send_message_with_retry(
        context,
        update.effective_chat.id,
        _format_balance_message(balance, net_today),
        reply_to_message_id=reply_to
    )
    await send_message_with_retry(context, update.effective_chat.id, "📷 Отсканируйте шпулю.")

async def _ensure_net_today(state: dict, order_code: str) -> float | None:
    if not order_code:
        return None
    today = _business_date()
    net_today = state.get("net_today")
    net_today_date = state.get("net_today_date")
    if net_today is not None and net_today_date == today:
        return net_today
    net_today = await _run_sheet_op(get_order_net_by_date, order_code, today, LOG_SHEETS, BUSINESS_DAY_CUTOFF_HOUR)
    state["net_today"] = net_today
    state["net_today_date"] = today
    return net_today

def parse_tare_weight_kg(text: str) -> float | None:
    value = text.strip()
    if value.isdigit() and len(value) in (12, 13):
        if len(value) == 12:
            value = "0" + value
        weight_str = value[7:12]
        try:
            grams = int(weight_str)
            return grams / 1000.0
        except ValueError:
            return None

    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None

def parse_report_date(text: str) -> date | None:
    value = text.strip()
    if not value:
        return None
    parts = re.findall(r"\d+", value)
    today = datetime.now().date()
    try:
        if len(parts) == 3:
            day, month, year = (int(p) for p in parts)
            if year < 100:
                year += 2000
        elif len(parts) == 2:
            day, month = (int(p) for p in parts)
            year = today.year
        elif len(parts) == 1:
            day = int(parts[0])
            month = today.month
            year = today.year
        else:
            return None
        return date(year, month, day)
    except ValueError:
        return None

def _should_send_done(chat_id: int, user_id: int) -> bool:
    now = time.monotonic()
    key = (chat_id, user_id)
    last_time = last_done_sent.get(key, 0.0)
    if (now - last_time) < DONE_DEDUP_WINDOW_SEC:
        return False
    last_done_sent[key] = now
    return True

async def send_done_if_needed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if _should_send_done(chat_id, user_id):
        await send_message_with_retry(context, chat_id, MSG_DONE)

async def reset_in_flight_after_timeout(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(IN_FLIGHT_TIMEOUT_SEC)
    key = (chat_id, user_id)
    if key in in_flight_requests:
        in_flight_requests.discard(key)
        await context.bot.send_message(chat_id, MSG_TIMEOUT)

def start_pending_state_timeout(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    key = (chat_id, user_id)
    existing = pending_state_tasks.pop(key, None)
    if existing:
        existing.cancel()

    async def _task():
        await asyncio.sleep(PENDING_STATE_TIMEOUT_SEC)
        if key in order_query_states or key in date_query_states:
            order_query_states.discard(key)
            date_query_states.discard(key)
            await context.bot.send_message(chat_id, MSG_TIMEOUT)
        pending_state_tasks.pop(key, None)

    pending_state_tasks[key] = asyncio.create_task(_task())

def cancel_pending_states(chat_id: int, user_id: int) -> None:
    key = (chat_id, user_id)
    order_query_states.discard(key)
    date_query_states.discard(key)
    pending_inputs.pop(key, None)
    task = pending_state_tasks.pop(key, None)
    if task:
        task.cancel()


# === /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states.pop(chat_id, None)

    await send_message_with_retry(context, update.effective_chat.id, 
        "Выберите ЛРП ⚠️",
        reply_markup=lrp_keyboard()
    )
    await notify_test_mode_if_needed(update, context)



# === /order ===
async def order_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private" and chat_id != FORWARD_CHAT_ID:
        return
    user_id = update.effective_user.id
    key = (chat_id, user_id)
    cancel_pending_states(chat_id, user_id)
    if key in in_flight_requests or key in date_query_states:
        await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
        return
    if key in order_query_states:
        reply_markup = ReplyKeyboardRemove() if update.effective_chat.type != "private" else None
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            "Введите номер заказа 📥",
            reply_markup=reply_markup
        )
        start_pending_state_timeout(chat_id, user_id, context)
        return
    order_query_states.add(key)
    start_pending_state_timeout(chat_id, user_id, context)
    reply_markup = ReplyKeyboardRemove() if update.effective_chat.type != "private" else None
    await send_message_with_retry(context, update.effective_chat.id, "Введите номер заказа 📥", reply_markup=reply_markup)

# === /date ===
async def date_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private" and chat_id != FORWARD_CHAT_ID:
        return
    user_id = update.effective_user.id
    key = (chat_id, user_id)
    cancel_pending_states(chat_id, user_id)
    if key in in_flight_requests or key in order_query_states:
        await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
        return
    if key in date_query_states:
        reply_markup = ReplyKeyboardRemove() if update.effective_chat.type != "private" else None
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            MSG_DATE_PROMPT,
            reply_markup=reply_markup
        )
        start_pending_state_timeout(chat_id, user_id, context)
        return
    date_query_states.add(key)
    start_pending_state_timeout(chat_id, user_id, context)
    reply_markup = ReplyKeyboardRemove() if update.effective_chat.type != "private" else None
    await send_message_with_retry(context, update.effective_chat.id, MSG_DATE_PROMPT, reply_markup=reply_markup)

# === /test ===
async def test_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_editor(user_id):
        await send_message_with_retry(context, update.effective_chat.id, "Режим тест 🔒 (для вас всегда включен)")
        return
    enabled = toggle_test_mode(user_id)
    await send_message_with_retry(context, update.effective_chat.id, "Режим тест 🔒" if enabled else "Рабочий режим 🔓")

# === Error handler ===
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if err and _is_network_error(err):
        _log_throttled_network(logging.ERROR, "unhandled_network", "Unhandled network error: %s", err)
        return
    logging.exception("Unhandled error: %s", err)

# === Выбор ЛРП ===
async def select_lrp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if "ЛРП" not in text:
        return

    try:
        lrp_id = None
        normalized = text.strip()
        for device_id, device in DEVICES.items():
            if device.get("name") == normalized:
                lrp_id = device_id
                break
        if lrp_id is None:
            raise ValueError
    except ValueError:
        user_states.pop(chat_id, None)
        await send_message_with_retry(context, update.effective_chat.id, "Выберите ЛРП! ⚠️", reply_markup=lrp_keyboard())
        return

    user_states[chat_id] = {
        "lrp": lrp_id,
        "order": None,
        "nomenclature": None,
        "tare_weight": None,
        "net_weight": None,
        "trash_comment": False,
        "balance": None,
        "net_today": None,
        "net_today_date": None,
        "spool_seq_ok": None,
        "spool_seq_trash": None,
        "spool_seq_date": None
    }

    await send_message_with_retry(context, update.effective_chat.id, 
        f"✅ {DEVICES[lrp_id]['name']} выбран.\nВведите номер заказа 📥",
        reply_markup=order_keyboard()
    )
    await notify_test_mode_if_needed(update, context)

# === Обработка текста (код / вес / удаление) ===
async def process_code(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, lrp_id: int):
    chat_id = update.effective_chat.id
    device = DEVICES[lrp_id]
    state = user_states.get(chat_id, {})
    if state.get("order") is None:
        await context.bot.send_message(
            chat_id,
            "Введите номер заказа 📥",
            reply_markup=order_keyboard()
        )
        return
    tare_weight = parse_tare_weight_kg(code)
    if tare_weight is None:
        logging.warning("Invalid tare format: %s", code)
        await context.bot.send_message(chat_id, "Неверный формат шпули, повторите сканирование ⚠️")
        return

    if is_effective_test_mode(update.effective_user.id):
        median_weight = 10.0
    else:
        scale = device.get("scale", {})
        median_weight = None
        for _attempt in range(3):
            try:
                median_weight, _last = await asyncio.wait_for(
                    asyncio.to_thread(
                        get_scale_median,
                        host=scale.get("host", "192.168.88.11"),
                        port=scale.get("port", 8235),
                        duration=scale.get("duration", 1.0)
                    ),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                median_weight = None
            if median_weight is not None:
                break
        if median_weight is None:
            logging.error("Scale read failed for chat_id=%s lrp_id=%s", chat_id, lrp_id)
            await send_message_with_retry(context, update.effective_chat.id, "Весы не отвечают, повторите операцию ⚠️")
            return

    user_id = update.effective_user.id
    weight = round(median_weight, 2)
    tare_weight = round(tare_weight, 2)
    net_weight = round(weight - tare_weight, 2)
    state["tare_weight"] = tare_weight
    state["net_weight"] = net_weight

    order = state.get("order")
    order_nomenclature = state.get("nomenclature")
    is_trash = state.get("trash_comment")
    spool_number = None
    if is_trash:
        seq = state.get("spool_seq_trash")
        today = _business_date()
        if state.get("spool_seq_date") != today:
            state["spool_seq_ok"] = None
            state["spool_seq_trash"] = None
            state["spool_seq_date"] = today
        if seq is None:
            try:
                ok_cnt, trash_cnt = await _run_sheet_op(
                    get_spool_counts,
                    order,
                    DEVICES[lrp_id]["log_sheet_name"],
                    today,
                    BUSINESS_DAY_CUTOFF_HOUR
                )
                state["spool_seq_ok"] = ok_cnt
                state["spool_seq_trash"] = trash_cnt
                state["spool_seq_date"] = today
                seq = trash_cnt
            except Exception as e:
                logging.exception("Spool count calc failed: %s", e)
                seq = None
        if seq is not None:
            seq += 1
            state["spool_seq_trash"] = seq
            spool_number = seq
    else:
        seq = state.get("spool_seq_ok")
        today = _business_date()
        if state.get("spool_seq_date") != today:
            state["spool_seq_ok"] = None
            state["spool_seq_trash"] = None
            state["spool_seq_date"] = today
        if seq is None:
            try:
                ok_cnt, trash_cnt = await _run_sheet_op(
                    get_spool_counts,
                    order,
                    DEVICES[lrp_id]["log_sheet_name"],
                    today,
                    BUSINESS_DAY_CUTOFF_HOUR
                )
                state["spool_seq_ok"] = ok_cnt
                state["spool_seq_trash"] = trash_cnt
                state["spool_seq_date"] = today
                seq = ok_cnt
            except Exception as e:
                logging.exception("Spool count calc failed: %s", e)
                seq = None
        if seq is not None:
            seq += 1
            state["spool_seq_ok"] = seq
            spool_number = seq
    write_ok = append_weight_row(
        order,
        order_nomenclature or "",
        weight,
        tare_weight,
        net_weight,
        user_id,
        DEVICES[lrp_id]["log_sheet_name"],
        comment="Trash" if state.get("trash_comment") else None,
        spool_number=spool_number
    )
    if not write_ok:
        await context.bot.send_message(chat_id, "Запись данных не удалась, повторите взвешивание⚠️")
        return
    if is_effective_test_mode(update.effective_user.id):
        mark_last_user_row_error(user_id, DEVICES[lrp_id]["log_sheet_name"])
        clear_last_user_spool_number(user_id, DEVICES[lrp_id]["log_sheet_name"])
        if spool_number is not None:
            seq_key = "spool_seq_trash" if is_trash else "spool_seq_ok"
            seq = state.get(seq_key)
            if isinstance(seq, int):
                state[seq_key] = max(0, seq - 1)
    order_line = f"Заказ: {order}\n" if order else ""
    nom_line = f"{order_nomenclature}\n" if order_nomenclature else ""

    prefix = "⛔ БРАК\n" if is_trash else ""
    msg = (
        f"{prefix}"
        f"✅ Данные записаны\n"
        f"{device['name']}\n"
        f"{order_line}"
        f"{nom_line}"
        f"Брутто: {weight:.2f} кг\n"
        f"Тара: {tare_weight:.2f} кг\n"
        f"Нетто: {net_weight:.2f} кг"
    )
    state["trash_comment"] = False

    await context.bot.send_message(chat_id, msg, reply_markup=spool_keyboard())
    balance = state.get("balance")
    if balance is not None:
        today = _business_date()
        net_today = state.get("net_today")
        if net_today is None or state.get("net_today_date") != today:
            try:
                net_today = await _run_sheet_op(get_order_net_by_date, order, today, LOG_SHEETS, BUSINESS_DAY_CUTOFF_HOUR)
                state["net_today"] = net_today
                state["net_today_date"] = today
            except SheetsUnavailableError:
                net_today = None
            except Exception as e:
                logging.exception("Net today calc failed: %s", e)
                net_today = None
        if net_today is not None and not is_trash:
            net_today += net_weight
            state["net_today"] = net_today
            state["net_today_date"] = today
        await _send_balance_message(update, context, balance, net_today)
    if not is_effective_test_mode(update.effective_user.id):
        await context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=msg)

    if device.get("printer"):
        try:
            printed = False
            for _attempt in range(3):
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            print_google_sheet_pdf,
                            spreadsheet_id=device["printer"]["spreadsheet_id"],
                            sheet_gid=device["printer"]["sheet_gid"],
                            printer_name=device["printer"]["printer_name"],
                            foxit_path=device["printer"]["foxit_path"],
                            file_suffix=device.get("name") or lrp_id,
                            paper_width_mm=device["printer"].get("paper_width_mm", 100),
                            paper_height_mm=device["printer"].get("paper_height_mm", 100),
                            skip_print=is_effective_test_mode(update.effective_user.id)
                        ),
                        timeout=70.0
                    )
                    printed = True
                    break
                except asyncio.TimeoutError:
                    printed = False
            if not printed:
                await send_message_with_retry(context, update.effective_chat.id, "Принтер не отвечает, повторите операцию ⚠️")
        except Exception as e:
            logging.exception("Print error for chat_id=%s: %s", chat_id, e)
# === Обработка текста (код / вес / удаление) ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    user_id = update.effective_user.id

    key = (chat_id, user_id)
    if key in in_flight_requests:
        await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
        return
    if key in pending_inputs:
        pending_inputs[key]["texts"].append(text)
        return

    if (chat_id, user_id) in order_query_states:
        async def handler(order_code: str):
            key = (chat_id, user_id)
            task = pending_state_tasks.pop(key, None)
            if task:
                task.cancel()
            order_query_states.discard(key)
            if key in in_flight_requests:
                await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
                return
            in_flight_requests.add(key)
            asyncio.create_task(reset_in_flight_after_timeout(chat_id, user_id, context))
            await send_message_with_retry(context, update.effective_chat.id, MSG_PROCESSING_COMMAND)
            try:
                await send_order_summary(update, context, order_code)
                await send_done_if_needed(update, context)
            except Exception as e:
                logging.exception("Order summary failed: %s", e)
                await send_message_with_retry(context, update.effective_chat.id, MSG_RETRY)
            finally:
                in_flight_requests.discard(key)
        await handler(text)
        return

    if (chat_id, user_id) in date_query_states:
        async def handler(date_text: str):
            report_date = parse_report_date(date_text)
            if report_date is None:
                await send_message_with_retry(context, update.effective_chat.id, MSG_RETRY)
                return
            key = (chat_id, user_id)
            task = pending_state_tasks.pop(key, None)
            if task:
                task.cancel()
            date_query_states.discard(key)
            if key in in_flight_requests:
                await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
                return
            in_flight_requests.add(key)
            asyncio.create_task(reset_in_flight_after_timeout(chat_id, user_id, context))
            await send_message_with_retry(context, update.effective_chat.id, MSG_PROCESSING_COMMAND)
            try:
                await send_date_report(update, context, report_date)
                await send_done_if_needed(update, context)
            except Exception as e:
                logging.exception("Date report failed: %s", e)
                await send_message_with_retry(context, update.effective_chat.id, MSG_RETRY)
            finally:
                in_flight_requests.discard(key)
        await handler(text)
        return


    # ? ЛРП не выбран
    if chat_id not in user_states:
        await send_message_with_retry(
            context,
            update.effective_chat.id,
            "Выберите ЛРП! ⚠️",
            reply_markup=lrp_keyboard()
        )
        await notify_test_mode_if_needed(update, context)
        return

    state = user_states[chat_id]
    lrp_id = state["lrp"]

    # Сменить ЛРП
    if text == "🔄 Сменить ЛРП":
        user_states.pop(chat_id, None)
        await send_message_with_retry(context, update.effective_chat.id, 
            "Выберите ЛРП ⚠️",
            reply_markup=lrp_keyboard()
        )
        await notify_test_mode_if_needed(update, context)
        return
    # 🗑️ Удаление последней записи
    if "Удалить последнюю запись" in text:
        result = await delete_last_entry(update, context, DEVICES[lrp_id]["log_sheet_name"])
        if result:
            order = state.get("order")
            result_dt = result.get("datetime")
            today = _business_date()
            result_business_date = _business_date(result_dt) if result_dt else result.get("date")
            if (
                order
                and result.get("order") == order
                and result_business_date == today
                and not result.get("is_trash")
                and result.get("net") is not None
            ):
                net_today = state.get("net_today")
                if net_today is None or state.get("net_today_date") != today:
                    try:
                        net_today = await _run_sheet_op(get_order_net_by_date, order, today, LOG_SHEETS, BUSINESS_DAY_CUTOFF_HOUR)
                        state["net_today"] = net_today
                        state["net_today_date"] = today
                    except SheetsUnavailableError:
                        net_today = None
                    except Exception as e:
                        logging.exception("Net today calc failed: %s", e)
                        net_today = None
                if net_today is not None:
                    if result.get("action") == "delete":
                        net_today -= result["net"]
                    elif result.get("action") == "restore":
                        net_today += result["net"]
                    state["net_today"] = net_today
                    state["net_today_date"] = today
                    await _send_balance_message(update, context, state.get("balance"), net_today)
            if order and result.get("order") == order:
                if result.get("is_trash"):
                    seq_key = "spool_seq_trash"
                else:
                    seq_key = "spool_seq_ok"
                seq = state.get(seq_key)
                if isinstance(seq, int):
                    if result.get("action") == "delete":
                        seq = max(0, seq - 1)
                    elif result.get("action") == "restore":
                        seq += 1
                    state[seq_key] = seq
        return

    if text == "🔄 Сменить номер заказа":
        state["order"] = None
        state["nomenclature"] = None
        state["tare_weight"] = None
        state["net_weight"] = None
        state["trash_comment"] = False
        state["balance"] = None
        state["net_today"] = None
        state["net_today_date"] = None
        state["spool_seq_ok"] = None
        state["spool_seq_trash"] = None
        state["spool_seq_date"] = None
        await send_message_with_retry(context, update.effective_chat.id, 
            "Введите номер заказа 📥",
            reply_markup=order_keyboard()
        )
        return

    if text == "⛔ Брак":
        state["trash_comment"] = True
        await send_message_with_retry(context, update.effective_chat.id, 
            "📷 Сканировать шпулю (<b>БРАК</b> ⛔)",
            reply_markup=spool_keyboard(),
            parse_mode="HTML"
        )
        return

    if state.get("order") is None:
        async def handler(order_code: str):
            nomenclature = find_nomenclature(order_code) or "Б/Н"
            state["order"] = order_code
            state["nomenclature"] = nomenclature
            state["balance"] = None
            state["net_today"] = None
            state["net_today_date"] = None
            state["spool_seq_ok"] = None
            state["spool_seq_trash"] = None
            state["spool_seq_date"] = None

            await send_message_with_retry(context, update.effective_chat.id, 
                f"✅ Заказ: {order_code}\n{nomenclature}",
                reply_markup=spool_keyboard()
            )
            await send_message_with_retry(context, update.effective_chat.id, MSG_PROCESSING_BALANCE)
            try:
                state["balance"] = await _run_sheet_op(get_order_balance, order_code)
                state["net_today"] = await _ensure_net_today(state, order_code)
                ok_cnt, trash_cnt = await _run_sheet_op(
                    get_spool_counts,
                    order_code,
                    DEVICES[lrp_id]["log_sheet_name"],
                    _business_date(),
                    BUSINESS_DAY_CUTOFF_HOUR
                )
                state["spool_seq_ok"] = ok_cnt
                state["spool_seq_trash"] = trash_cnt
                state["spool_seq_date"] = _business_date()
            except SheetsUnavailableError:
                await send_message_with_retry(context, update.effective_chat.id, MSG_SHEETS_UNAVAILABLE)
            except Exception as e:
                logging.exception("Balance calc failed: %s", e)
                await send_message_with_retry(context, update.effective_chat.id, MSG_SHEETS_UNAVAILABLE)
            await _send_balance_message(update, context, state.get("balance"), state.get("net_today"))
        _start_pending_input(chat_id, user_id, "order_number", update, context, text, handler)
        return

    async def handler(code_text: str):
        await send_message_with_retry(context, update.effective_chat.id, MSG_PROCESSING_BALANCE)
        await process_code(update, context, code_text, lrp_id)
    _start_pending_input(chat_id, user_id, "spool_scan", update, context, text, handler)
    return

# === Обработка текста в группах (только /order) ===
async def handle_group_order_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if chat_id != FORWARD_CHAT_ID:
        return

    key = (chat_id, user_id)
    if key in in_flight_requests:
        await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
        return
    if key in pending_inputs:
        pending_inputs[key]["texts"].append(text)
        return

    if (chat_id, user_id) in order_query_states:
        async def handler(order_code: str):
            key = (chat_id, user_id)
            task = pending_state_tasks.pop(key, None)
            if task:
                task.cancel()
            order_query_states.discard(key)
            if key in in_flight_requests:
                await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
                return
            in_flight_requests.add(key)
            asyncio.create_task(reset_in_flight_after_timeout(chat_id, user_id, context))
            await send_message_with_retry(context, update.effective_chat.id, MSG_PROCESSING_COMMAND)
            try:
                await send_order_summary(update, context, order_code)
                await send_done_if_needed(update, context)
            except Exception as e:
                logging.exception("Group order summary failed: %s", e)
                await send_message_with_retry(context, update.effective_chat.id, MSG_RETRY)
            finally:
                in_flight_requests.discard(key)
        await handler(text)
        return

    if (chat_id, user_id) in date_query_states:
        async def handler(date_text: str):
            report_date = parse_report_date(date_text)
            if report_date is None:
                await send_message_with_retry(context, update.effective_chat.id, MSG_RETRY)
                return
            key = (chat_id, user_id)
            task = pending_state_tasks.pop(key, None)
            if task:
                task.cancel()
            date_query_states.discard(key)
            if key in in_flight_requests:
                await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)
                return
            in_flight_requests.add(key)
            asyncio.create_task(reset_in_flight_after_timeout(chat_id, user_id, context))
            await send_message_with_retry(context, update.effective_chat.id, MSG_PROCESSING_COMMAND)
            try:
                await send_date_report(update, context, report_date)
                await send_done_if_needed(update, context)
            except Exception as e:
                logging.exception("Group date report failed: %s", e)
                await send_message_with_retry(context, update.effective_chat.id, MSG_RETRY)
            finally:
                in_flight_requests.discard(key)
        await handler(text)
        return

# === WebApp-сканер ===
async def webapp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    code = update.message.web_app_data.data.strip()
    user_id = update.effective_user.id

    key = (chat_id, user_id)
    if key in pending_inputs:
        pending_inputs[key]["texts"].append(code)
        return

    if chat_id not in user_states:
        await context.bot.send_message(
            chat_id,
            "Выберите ЛРП! ⚠️",
            reply_markup=lrp_keyboard()
        )
        await notify_test_mode_if_needed(update, context)
        return

    state = user_states[chat_id]
    lrp_id = state["lrp"]

    if state.get("order") is None:
        async def handler(order_code: str):
            nomenclature = find_nomenclature(order_code) or "Б/Н"
            state["order"] = order_code
            state["nomenclature"] = nomenclature
            state["balance"] = None
            state["net_today"] = None
            state["net_today_date"] = None
            state["spool_seq_ok"] = None
            state["spool_seq_trash"] = None
            state["spool_seq_date"] = None
            await context.bot.send_message(
                chat_id,
                f"✅ Заказ: {order_code}\n{nomenclature}",
                reply_markup=spool_keyboard()
            )
            await send_message_with_retry(context, chat_id, MSG_PROCESSING_BALANCE)
            try:
                state["balance"] = await _run_sheet_op(get_order_balance, order_code)
                state["net_today"] = await _ensure_net_today(state, order_code)
                ok_cnt, trash_cnt = await _run_sheet_op(
                    get_spool_counts,
                    order_code,
                    DEVICES[lrp_id]["log_sheet_name"],
                    _business_date(),
                    BUSINESS_DAY_CUTOFF_HOUR
                )
                state["spool_seq_ok"] = ok_cnt
                state["spool_seq_trash"] = trash_cnt
                state["spool_seq_date"] = _business_date()
            except SheetsUnavailableError:
                await send_message_with_retry(context, chat_id, MSG_SHEETS_UNAVAILABLE)
            except Exception as e:
                logging.exception("Balance calc failed: %s", e)
                await send_message_with_retry(context, chat_id, MSG_SHEETS_UNAVAILABLE)
            await _send_balance_message(update, context, state.get("balance"), state.get("net_today"))
        _start_pending_input(chat_id, user_id, "order_number", update, context, code, handler)
        return

    async def handler(code_text: str):
        await process_code(update, context, code_text, lrp_id)
    _start_pending_input(chat_id, user_id, "spool_scan", update, context, code, handler)
    return

# === Busy non-text handler ===
async def handle_busy_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if (chat_id, user_id) in in_flight_requests:
        await send_message_with_retry(context, update.effective_chat.id, MSG_BUSY)

# === MAIN ===
def main():
    if not Path(PROJECT_ENV_PATH).is_file():
        safe_print(f"Missing {PROJECT_ENV_PATH} file")
        return
    if not TELEGRAM_TOKEN:
        safe_print("Missing TELEGRAM_TOKEN in Project.env")
        return
    if not SHEET_ID:
        safe_print("Missing SHEET_ID in Project.env")
        return
    if not Path("my-project-main-463510-1da399d0ee21.json").is_file():
        safe_print("Missing service account JSON file")
        return
    async def refresh_tag_cache_loop():
        base_delay = 1800
        delay = base_delay
        max_delay = 6 * 3600
        while True:
            await asyncio.sleep(delay)
            try:
                await asyncio.to_thread(refresh_tag_cache)
                delay = base_delay
            except Exception as e:
                if _is_network_error(e):
                    _log_throttled_network(logging.WARNING, "tag_cache_network", "Tag cache refresh failed: %s", e)
                else:
                    logging.warning("Tag cache refresh failed: %s", e)
                delay = min(delay * 2, max_delay)

    async def post_init(app):
        try:
            await asyncio.to_thread(refresh_tag_cache)
        except Exception as e:
            if _is_network_error(e):
                _log_throttled_network(logging.WARNING, "tag_cache_initial_network", "Initial tag cache refresh failed: %s", e)
            else:
                logging.warning("Initial tag cache refresh failed: %s", e)
        app.bot_data["tag_cache_task"] = asyncio.create_task(refresh_tag_cache_loop())
        try:
            await app.bot.set_my_commands(
                [
                    BotCommand("order", "Итог по заказу 📊"),
                    BotCommand("date", "Отчет по дате 📅"),
                ],
                scope=BotCommandScopeDefault()
            )
            await app.bot.set_my_commands(
                [
                    BotCommand("order", "Итог по заказу 📊"),
                    BotCommand("date", "Отчет по дате 📅"),
                    BotCommand("test", "Режим тест🔒"),
                ],
                scope=BotCommandScopeAllPrivateChats()
            )

        except Exception as e:
            logging.warning("Failed to set bot commands: %s", e)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).job_queue(None).build()

    app.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("order", order_summary_command))
    app.add_handler(CommandHandler("date", date_report_command))
    app.add_handler(CommandHandler("test", test_mode_command, filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("ЛРП") & filters.ChatType.PRIVATE, select_lrp))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA & filters.ChatType.PRIVATE, webapp_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_group_order_text))
    app.add_handler(MessageHandler(filters.ALL & ~filters.TEXT & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS), handle_busy_any))
    app.add_error_handler(error_handler)

    safe_print("🚀 Бот запущен")
    for attempt in range(5):
        try:
            app.run_polling(close_loop=False, drop_pending_updates=True)
            break
        except (TimedOut, NetworkError) as e:
            if attempt == 4:
                raise
            _log_throttled_network(
                logging.WARNING,
                "run_polling_network",
                "run_polling failed (attempt %s/5): %s",
                attempt + 1,
                e
            )
            time.sleep(2 * (attempt + 1))

if __name__ == "__main__":
    main()
