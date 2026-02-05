import os
import time
from datetime import datetime, date
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ContextTypes
from google.oauth2.service_account import Credentials
import gspread

# Загрузка переменных окружения
load_dotenv("Project.env")
SHEET_ID = os.getenv("SHEET_ID")
SERVICE_ACCOUNT_FILE = "my-project-main-463510-1da399d0ee21.json"

# Авторизация в Google Sheets
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GC: gspread.Client | None = None

def get_gspread_client() -> gspread.Client:
    global GC
    if GC is None:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        GC = gspread.authorize(creds)
        if hasattr(GC, "session"):
            GC.session.timeout = 60
        elif hasattr(GC, "http_client") and hasattr(GC.http_client, "session"):
            GC.http_client.session.timeout = 60
    return GC

# Клавиатуры
# Хранение времени последнего действия
last_action_time = {}

def _parse_date_cell(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    date_part = value.split()[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(date_part, fmt).date()
        except ValueError:
            continue
    return None

def _parse_datetime_cell(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    date_only = _parse_date_cell(value)
    if date_only is None:
        return None
    return datetime.combine(date_only, datetime.min.time())

def _parse_float(value: str) -> float | None:
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None

# Удаление последней записи (универсальная функция)
async def delete_last_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, sheet_name: str):
    # Определяем источник: обычное сообщение или callback кнопка
    if update.callback_query:
        user_id = str(update.callback_query.from_user.id)
        reply = update.callback_query.edit_message_text
    else:
        user_id = str(update.effective_user.id)
        reply = update.message.reply_text

    now = time.time()
    if user_id in last_action_time and now - last_action_time[user_id] < 5:
        await reply(f"⏳ Подождите {int(5 - (now - last_action_time[user_id]))} сек.")
        return None
    last_action_time[user_id] = now

    try:
        sheet = get_gspread_client().open_by_key(SHEET_ID).worksheet(sheet_name)
        values = sheet.get_all_values()
    except Exception as e:
        await reply(f"❌ Ошибка доступа к таблице: {e}")
        return None

    if not values or len(values) < 2:
        await reply("⛔️ Нет записей для удаления.")
        return None

    user_rows = [
        (idx + 2, row) for idx, row in enumerate(values[1:])
        if len(row) >= 4 and row[3].strip() == user_id
    ]

    if not user_rows:
        await reply("⛔️ Нет ваших записей для удаления.")
        return None

    last_row_index, last_user_row = user_rows[-1]

    eror_value = ""
    if len(last_user_row) >= 5:
        eror_value = last_user_row[4].strip()

    code = last_user_row[0] if len(last_user_row) >= 1 else "?"
    weight = last_user_row[1] if len(last_user_row) >= 2 else "?"
    timestamp = last_user_row[2] if len(last_user_row) >= 3 else ""
    net_value = last_user_row[6] if len(last_user_row) >= 7 else ""
    comment = last_user_row[8] if len(last_user_row) >= 9 else ""
    row_date = _parse_date_cell(timestamp)
    row_dt = _parse_datetime_cell(timestamp)
    net_weight = _parse_float(net_value)
    is_trash = bool(comment.strip())

    if eror_value == "Eror":
        sheet.update_cell(last_row_index, 5, "")
        if len(last_user_row) >= 9 and last_user_row[8].strip() == "Trash":
            sheet.update_cell(last_row_index, 9, "")
        saved_seq = last_user_row[10] if len(last_user_row) >= 11 else ""
        if saved_seq:
            sheet.update_cell(last_row_index, 10, saved_seq)
            sheet.update_cell(last_row_index, 11, "")
        await reply(f"↩️ Отмена удаления:\nКод: {code}\nВес: {weight}")
        return {
            "action": "restore",
            "order": code,
            "date": row_date,
            "datetime": row_dt,
            "net": net_weight,
            "is_trash": is_trash
        }
    else:
        sheet.update_cell(last_row_index, 5, "Eror")
        current_seq = last_user_row[9] if len(last_user_row) >= 10 else ""
        if current_seq:
            sheet.update_cell(last_row_index, 11, current_seq)
        sheet.update_cell(last_row_index, 10, "")
        await reply(f"❌ Удалено:\nКод: {code}\nВес: {weight}")
        return {
            "action": "delete",
            "order": code,
            "date": row_date,
            "datetime": row_dt,
            "net": net_weight,
            "is_trash": is_trash
        }
