from telegram import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

# URL для WebApp-сканера
SCANNER_URL = "https://dancgreat.github.io/telegram-scanner/"

# === Кнопки выбора ЛРП ===
def lrp_keyboard() -> ReplyKeyboardMarkup:
    """
    Клавиатура для выбора ЛРП (1-7).
    """
    buttons = [[KeyboardButton(f"ЛРП {i}")] for i in range(1, 8)]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# === Уровень 2: заказ ===
def order_keyboard() -> ReplyKeyboardMarkup:
    """
    Клавиатура после выбора ЛРП: ввод номера заказа.
    """
    scan_order_button = KeyboardButton(
        text="📷 Сканировать заказ",
        web_app=WebAppInfo(url=SCANNER_URL)
    )
    back_button = KeyboardButton(text="🔄 Сменить ЛРП")

    return ReplyKeyboardMarkup(
        [[scan_order_button], [back_button]],
        resize_keyboard=True
    )

# === Уровень 3: шпуля ===
def spool_keyboard() -> ReplyKeyboardMarkup:
    """
    Клавиатура после ввода номера заказа: работа со шпулей.
    """
    scan_spool_button = KeyboardButton(
        text="📷 Сканировать шпулю",
        web_app=WebAppInfo(url=SCANNER_URL)
    )
    trash_button = KeyboardButton(text="⛔ Брак")
    delete_button = KeyboardButton(text="Удалить последнюю запись🗑️")
    change_order_button = KeyboardButton(text="🔄 Сменить номер заказа")

    return ReplyKeyboardMarkup(
        [[scan_spool_button], [trash_button], [delete_button], [change_order_button]],
        resize_keyboard=True
    )
