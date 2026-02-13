import os
import json
from pathlib import Path
from datetime import datetime, date, timedelta
import gspread
import time
from http.client import RemoteDisconnected
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
from urllib3.exceptions import ProtocolError
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
import logging

# === Загрузка переменных окружения ===
load_dotenv("Project.env")
SHEET_ID = os.getenv("SHEET_ID")
SERVICE_ACCOUNT_FILE = "my-project-main-463510-1da399d0ee21.json"
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

TAG_CACHE_PATH = Path("tag_cache.json")
TAG_CACHE: dict[str, str] = {}
TAG_CACHE_LOADED = False

# Raised when Sheets are unavailable for order summary reads.
class SheetsUnavailableError(Exception):
    pass

# === Глобальный клиент для повторного использования ===
CLIENT: gspread.Client | None = None
SPREADSHEET: gspread.Spreadsheet | None = None
WORKSHEET_CACHE: dict[str, gspread.Worksheet] = {}
CACHE_TTL_SEC = 3600
LAST_CACHE_RESET = 0.0

def get_gspread_client() -> gspread.Client:
    """Возвращает авторизованный клиент Google Sheets (singleton)."""
    global CLIENT
    if CLIENT is None:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        CLIENT = gspread.authorize(creds)
        if hasattr(CLIENT, "session"):
            CLIENT.session.timeout = 60
        elif hasattr(CLIENT, "http_client") and hasattr(CLIENT.http_client, "session"):
            CLIENT.http_client.session.timeout = 60
    return CLIENT

def _get_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    global SPREADSHEET
    if SPREADSHEET is None:
        SPREADSHEET = client.open_by_key(SHEET_ID)
    return SPREADSHEET

def _clear_sheet_cache() -> None:
    global SPREADSHEET, WORKSHEET_CACHE, LAST_CACHE_RESET
    SPREADSHEET = None
    WORKSHEET_CACHE = {}
    LAST_CACHE_RESET = time.time()

def _cache_expired() -> bool:
    return (time.time() - LAST_CACHE_RESET) >= CACHE_TTL_SEC

def get_worksheet(sheet_name: str):
    """Возвращает worksheet по имени листа."""
    if _cache_expired():
        _clear_sheet_cache()
    if sheet_name in WORKSHEET_CACHE:
        return WORKSHEET_CACHE[sheet_name]
    client = get_gspread_client()
    sheet = _open_worksheet_with_retry(client, sheet_name)
    WORKSHEET_CACHE[sheet_name] = sheet
    return sheet

def _is_retryable_open_error(err: Exception) -> bool:
    if _should_retry_api_error(err):
        return True
    if isinstance(err, (RequestsConnectionError, RequestsTimeout, ProtocolError, RemoteDisconnected)):
        return True
    msg = str(err).lower()
    return "remote end closed connection" in msg or "connection aborted" in msg

def _open_worksheet_with_retry(client: gspread.Client, sheet_name: str, retries: int = 5, base_delay: float = 0.5):
    for attempt in range(retries):
        try:
            return _get_spreadsheet(client).worksheet(sheet_name)
        except Exception as e:
            _clear_sheet_cache()
            if attempt == retries - 1 or not _is_retryable_open_error(e):
                raise
            time.sleep(base_delay * (2 ** attempt))

def _should_retry_api_error(err: Exception) -> bool:
    status = getattr(getattr(err, "response", None), "status", None)
    if status in (500, 502, 503, 504):
        return True
    msg = str(err)
    return "500" in msg or "Internal error" in msg

def _append_row_with_retry(sheet, row, retries: int = 5, base_delay: float = 0.5):
    for attempt in range(retries):
        try:
            return sheet.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            _clear_sheet_cache()
            if attempt == retries - 1 or not _should_retry_api_error(e):
                raise
            time.sleep(base_delay * (2 ** attempt))

def _batch_get_with_retry(sheet, ranges, retries: int = 5, base_delay: float = 0.5):
    for attempt in range(retries):
        try:
            return sheet.batch_get(ranges)
        except Exception as e:
            _clear_sheet_cache()
            if attempt == retries - 1 or not _should_retry_api_error(e):
                raise
            time.sleep(base_delay * (2 ** attempt))

# === Поиск номенклатуры по коду ===
def find_nomenclature(code: str) -> str | None:
    if not code:
        return None
    _load_tag_cache()
    if code in TAG_CACHE:
        return TAG_CACHE.get(code) or None
    if not TAG_CACHE:
        try:
            refresh_tag_cache()
        except Exception as e:
            print(f"Ошибка при обновлении кэша номенклатуры: {e}")
            return None
        return TAG_CACHE.get(code) or None
    return None


def _load_tag_cache() -> None:
    global TAG_CACHE, TAG_CACHE_LOADED
    if TAG_CACHE_LOADED:
        return
    try:
        raw = TAG_CACHE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            TAG_CACHE = {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        TAG_CACHE = {}
    except Exception as e:
        print(f"Ошибка чтения кэша номенклатуры: {e}")
        TAG_CACHE = {}
    TAG_CACHE_LOADED = True


def refresh_tag_cache() -> int:
    global TAG_CACHE, TAG_CACHE_LOADED
    tag_sheet = get_worksheet("Tag")
    values = tag_sheet.get_all_values()
    new_cache: dict[str, str] = {}
    for row in values[1:]:
        if len(row) < 2:
            continue
        code = row[0].strip()
        nomenclature = row[1].strip()
        if code:
            new_cache[code] = nomenclature
    TAG_CACHE = new_cache
    TAG_CACHE_LOADED = True
    TAG_CACHE_PATH.write_text(
        json.dumps(TAG_CACHE, ensure_ascii=False),
        encoding="utf-8"
    )
    return len(TAG_CACHE)

def append_weight_row(
    order_code: str,
    nomenclature: str,
    weight_kg: float,
    tare_weight_kg: float,
    net_weight_kg: float,
    user_id: int,
    sheet_name: str,
    comment: str | None = None,
    spool_number: int | None = None
):
    """
    Добавляет строку взвешивания:
    A=номер заказа, B=брутто, C=время, D=user_id, F=тара, G=нетто, H=номенклатура,
    I=комментарий, J=порядковый номер бобины.
    """
    try:
        sheet = get_worksheet(sheet_name)
        timestamp = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        row = [
            order_code,
            weight_kg,
            timestamp,
            user_id,
            "",
            tare_weight_kg,
            net_weight_kg,
            nomenclature,
            comment or "",
            spool_number if spool_number is not None else ""
        ]
        _append_row_with_retry(sheet, row)
        return True
    except Exception as e:
        print(f"❌ Ошибка записи взвешивания в {sheet_name}: {e}")
        return False

def mark_last_user_row_error(user_id: int, sheet_name: str) -> bool:
    """
    Помечает последнюю строку пользователя значением 'Eror' в колонке E.
    """
    try:
        sheet = get_worksheet(sheet_name)
        values = sheet.get_all_values()
        user_rows = [
            (idx + 1, row) for idx, row in enumerate(values)
            if len(row) >= 4 and str(row[3]).strip() == str(user_id)
        ]
        if not user_rows:
            return False
        last_index, _last_row = user_rows[-1]
        sheet.update_cell(last_index, 5, "Eror")
        return True
    except Exception as e:
        print(f"Ошибка пометки Eror для пользователя {user_id}: {e}")
        return False

def clear_last_user_spool_number(user_id: int, sheet_name: str) -> bool:
    """
    Очищает колонку J у последней строки пользователя.
    """
    try:
        sheet = get_worksheet(sheet_name)
        values = sheet.get_all_values()
        user_rows = [
            (idx + 1, row) for idx, row in enumerate(values)
            if len(row) >= 4 and str(row[3]).strip() == str(user_id)
        ]
        if not user_rows:
            return False
        last_index, _last_row = user_rows[-1]
        sheet.update_cell(last_index, 10, "")
        return True
    except Exception as e:
        print(f"Ошибка очистки номера бобины для пользователя {user_id}: {e}")
        return False
def _parse_float(value: str) -> float:
    value = (value or "").strip().replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return 0.0

def get_spool_counts(
    order_code: str,
    sheet_name: str,
    target_date: date,
    cutoff_hour: int | None = None
) -> tuple[int, int]:
    """
    Возвращает (ok_count, trash_count) для заказа по листу за бизнес-день.
    Учитываются только строки без Eror в колонке E.
    """
    if not order_code:
        return (0, 0)
    sheet = get_worksheet(sheet_name)
    cols = _batch_get_with_retry(sheet, ["A:A", "C:C", "E:E", "I:I"])
    col_a = cols[0] if len(cols) > 0 else []
    col_c = cols[1] if len(cols) > 1 else []
    col_e = cols[2] if len(cols) > 2 else []
    col_i = cols[3] if len(cols) > 3 else []
    max_len = max(len(col_a), len(col_c), len(col_e), len(col_i))
    ok_count = 0
    trash_count = 0
    for idx in range(1, max_len):
        a = col_a[idx][0].strip() if idx < len(col_a) and col_a[idx] else ""
        if a != order_code:
            continue
        c = col_c[idx][0].strip() if idx < len(col_c) and col_c[idx] else ""
        if cutoff_hour is None:
            row_date = _parse_date_cell(c)
        else:
            row_dt = _parse_datetime_cell(c)
            if row_dt is None:
                continue
            row_date = (row_dt - timedelta(hours=cutoff_hour)).date()
        if row_date != target_date:
            continue
        e = col_e[idx][0].strip() if idx < len(col_e) and col_e[idx] else ""
        if e:
            continue
        i = col_i[idx][0].strip() if idx < len(col_i) and col_i[idx] else ""
        if i == "Trash":
            trash_count += 1
        else:
            ok_count += 1
    return (ok_count, trash_count)

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

def summarize_order(order_code: str, sheet_names: list[str]) -> tuple[float, float, int, str | None]:
    """
    Sum weights for the given order across multiple Log sheets.
    Only rows with empty E and I columns are counted.
    Returns (gross_sum, net_sum, count, nomenclature).
    """
    gross_sum = 0.0
    net_sum = 0.0
    count = 0
    nomenclature: str | None = None

    read_ok = 0
    for sheet_name in sheet_names:
        try:
            sheet = get_worksheet(sheet_name)
            cols = _batch_get_with_retry(sheet, ["A:A", "B:B", "E:E", "G:G", "H:H", "I:I"])
        except Exception as e:
            print(f"Error reading sheet {sheet_name}: {e}")
            continue
        read_ok += 1

        col_a = cols[0] if len(cols) > 0 else []
        col_b = cols[1] if len(cols) > 1 else []
        col_e = cols[2] if len(cols) > 2 else []
        col_g = cols[3] if len(cols) > 3 else []
        col_h = cols[4] if len(cols) > 4 else []
        col_i = cols[5] if len(cols) > 5 else []

        max_len = max(len(col_a), len(col_b), len(col_e), len(col_g), len(col_h), len(col_i))
        for idx in range(1, max_len):
            a = col_a[idx][0].strip() if idx < len(col_a) and col_a[idx] else ""
            if a != order_code:
                continue

            e = col_e[idx][0].strip() if idx < len(col_e) and col_e[idx] else ""
            i = col_i[idx][0].strip() if idx < len(col_i) and col_i[idx] else ""
            if e or i:
                continue

            b = col_b[idx][0] if idx < len(col_b) and col_b[idx] else ""
            g = col_g[idx][0] if idx < len(col_g) and col_g[idx] else ""
            h = col_h[idx][0].strip() if idx < len(col_h) and col_h[idx] else ""

            gross_sum += _parse_float(b)
            net_sum += _parse_float(g)
            count += 1

            if not nomenclature and h:
                nomenclature = h

    if sheet_names and read_ok == 0:
        raise SheetsUnavailableError("Failed to read any sheets.")
    return gross_sum, net_sum, count, nomenclature

def summarize_orders_by_date(
    target_date: date,
    sheet_names: list[str],
    cutoff_hour: int | None = None
) -> dict[str, dict]:
    summaries: dict[str, dict] = {}
    read_ok = 0
    for sheet_name in sheet_names:
        try:
            sheet = get_worksheet(sheet_name)
            cols = _batch_get_with_retry(sheet, ["A:A", "B:B", "C:C", "E:E", "G:G", "H:H", "I:I"])
        except Exception as e:
            print(f"Error reading sheet {sheet_name}: {e}")
            continue
        read_ok += 1

        col_a = cols[0] if len(cols) > 0 else []
        col_b = cols[1] if len(cols) > 1 else []
        col_c = cols[2] if len(cols) > 2 else []
        col_e = cols[3] if len(cols) > 3 else []
        col_g = cols[4] if len(cols) > 4 else []
        col_h = cols[5] if len(cols) > 5 else []
        col_i = cols[6] if len(cols) > 6 else []

        max_len = max(len(col_a), len(col_b), len(col_c), len(col_e), len(col_g), len(col_h), len(col_i))
        for idx in range(1, max_len):
            a = col_a[idx][0].strip() if idx < len(col_a) and col_a[idx] else ""
            if not a:
                continue

            c = col_c[idx][0].strip() if idx < len(col_c) and col_c[idx] else ""
            if cutoff_hour is None:
                row_date = _parse_date_cell(c)
            else:
                row_dt = _parse_datetime_cell(c)
                if row_dt is None:
                    continue
                row_date = (row_dt - timedelta(hours=cutoff_hour)).date()
            if row_date != target_date:
                continue

            e = col_e[idx][0].strip() if idx < len(col_e) and col_e[idx] else ""
            i = col_i[idx][0].strip() if idx < len(col_i) and col_i[idx] else ""
            if e or i:
                continue

            b = col_b[idx][0] if idx < len(col_b) and col_b[idx] else ""
            g = col_g[idx][0] if idx < len(col_g) and col_g[idx] else ""
            h = col_h[idx][0].strip() if idx < len(col_h) and col_h[idx] else ""

            entry = summaries.get(a)
            if entry is None:
                entry = {"gross": 0.0, "net": 0.0, "count": 0, "nomenclature": None}
                summaries[a] = entry

            entry["gross"] += _parse_float(b)
            entry["net"] += _parse_float(g)
            entry["count"] += 1
            if not entry["nomenclature"] and h:
                entry["nomenclature"] = h

    if sheet_names and read_ok == 0:
        raise SheetsUnavailableError("Failed to read any sheets.")
    return summaries

def get_order_balance(order_code: str) -> float | None:
    if not order_code:
        return None
    try:
        sheet = get_worksheet("Balance")
        cols = _batch_get_with_retry(sheet, ["A:A", "B:B"])
    except Exception as e:
        print(f"Error reading Balance sheet: {e}")
        raise

    col_a = cols[0] if len(cols) > 0 else []
    col_b = cols[1] if len(cols) > 1 else []
    max_len = max(len(col_a), len(col_b))
    for idx in range(1, max_len):
        a = col_a[idx][0].strip() if idx < len(col_a) and col_a[idx] else ""
        if a != order_code:
            continue
        b = col_b[idx][0] if idx < len(col_b) and col_b[idx] else ""
        return _parse_float(b)
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

def get_order_net_by_date(order_code: str, target_date: date, sheet_names: list[str], cutoff_hour: int | None = None) -> float:
    if not order_code:
        return 0.0
    net_sum = 0.0
    read_ok = 0
    for sheet_name in sheet_names:
        try:
            sheet = get_worksheet(sheet_name)
            cols = _batch_get_with_retry(sheet, ["A:A", "C:C", "E:E", "G:G", "I:I"])
        except Exception as e:
            print(f"Error reading sheet {sheet_name}: {e}")
            continue
        read_ok += 1

        col_a = cols[0] if len(cols) > 0 else []
        col_c = cols[1] if len(cols) > 1 else []
        col_e = cols[2] if len(cols) > 2 else []
        col_g = cols[3] if len(cols) > 3 else []
        col_i = cols[4] if len(cols) > 4 else []

        max_len = max(len(col_a), len(col_c), len(col_e), len(col_g), len(col_i))
        for idx in range(1, max_len):
            a = col_a[idx][0].strip() if idx < len(col_a) and col_a[idx] else ""
            if a != order_code:
                continue
            c = col_c[idx][0].strip() if idx < len(col_c) and col_c[idx] else ""
            if cutoff_hour is None:
                row_date = _parse_date_cell(c)
                if row_date != target_date:
                    continue
            else:
                row_dt = _parse_datetime_cell(c)
                if row_dt is None:
                    continue
                row_date = (row_dt - timedelta(hours=cutoff_hour)).date()
                if row_date != target_date:
                    continue
            e = col_e[idx][0].strip() if idx < len(col_e) and col_e[idx] else ""
            i = col_i[idx][0].strip() if idx < len(col_i) and col_i[idx] else ""
            if e or i:
                continue
            g = col_g[idx][0] if idx < len(col_g) and col_g[idx] else ""
            net_sum += _parse_float(g)

    if sheet_names and read_ok == 0:
        raise SheetsUnavailableError("Failed to read any sheets.")
    return net_sum
