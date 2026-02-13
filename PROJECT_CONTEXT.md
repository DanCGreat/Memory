# PROJECT_CONTEXT.md

## Назначение
Единый источник контекста проекта `BasepackBot` для переноса между устройствами/сессиями Codex.

Использование в новом чате:
```text
Обнови память по проекту из файла PROJECT_CONTEXT.md и работай строго по нему.
```

---

## 1. Цель системы
Telegram-бот для ЛРП, который:
- ведет оператора по 3 уровням: ЛРП -> заказ -> шпуля;
- получает вес с сетевых весов (TCP);
- пишет запись в Google Sheets `Log*`;
- печатает этикетку через Google Sheet PDF + Foxit + локальный принтер;
- считает остаток заказа;
- поддерживает удаление/восстановление последней записи;
- ведет нумерацию бобин (отдельно основная/брак) по бизнес-дню.

## 2. Ключевые файлы
- `bot.py` — основная логика, состояния, роутинг сообщений, команды.
- `google_sheets.py` — чтение/запись Google Sheets, агрегации, кэши.
- `delete_last.py` — delete/restore последней записи пользователя.
- `devices.py` — конфиг ЛРП (весы, принтер, log-лист).
- `keyboards.py` — клавиатуры Telegram по уровням.
- `server_scales.py` — TCP чтение веса.
- `print_google_sheet_pdf.py` — экспорт/кроп/печать PDF.
- `test.py` — тест-режим по пользователю.

## 3. Внешние зависимости
- `python-telegram-bot`
- `gspread` + service account JSON
- Google Sheets
- TCP весы
- Foxit PDF Reader
- Локальный принтер Windows

## 4. Переменные и секреты
Хранить в `Project.env`:
- `TELEGRAM_TOKEN`
- `SHEET_ID`
- `TELEGRAM_CHAT_ID`
- `PRINTER_SPREADSHEET_ID`

Секретный файл:
- `my-project-main-463510-1da399d0ee21.json`

Никогда не коммитить:
- `Project.env`
- service account JSON
- логи

## 5. Бизнес-правила (обязательно)
- Бизнес-день: сдвиг суток на `09:00` (`BUSINESS_DAY_CUTOFF_HOUR = 9`).
- Остаток: `Balance(B) - net_today`.
- `net_today` считается по бизнес-дате.
- В отчеты/остатки не попадают строки с `E != ""` (`Eror`) и `I != ""` (`Trash`).
- Нумерация бобин:
  - отдельно для OK и Trash;
  - в пределах `order + LRP + business_day`;
  - после перезапуска/смены заказа и после delete/restore пересчитывается по листу.

## 6. Контракт Google Sheets
### 6.1 Log-листы (`Log1/8`, `Log4`, `Log5`, ...)
Колонки:
- `A` заказ
- `B` брутто
- `C` дата/время
- `D` user_id
- `E` метка удаления `Eror`
- `F` тара
- `G` нетто
- `H` номенклатура
- `I` комментарий (`Trash`)
- `J` номер бобины
- `K` сохраненный номер для restore

### 6.2 Balance
- `A` заказ
- `B` исходный остаток заказа

### 6.3 Tag
- `A` код заказа
- `B` номенклатура

## 7. Сценарии пользователя
### 7.1 Private chat
1. `/start` -> выбор ЛРП.
2. Уровень 2: ввод/скан заказа.
3. Уровень 3: ввод/скан шпули.
4. Опции уровня 3:
- `Брак`
- `Удалить последнюю запись`
- `Сменить номер заказа`

### 7.2 Group chat (только `FORWARD_CHAT_ID`)
Разрешены только командные сценарии:
- `/order` + номер заказа
- `/date` + дата

## 8. Сообщения прогресса
Разделены по типу операции:
- Команды (`/order`, `/date`): `Обрабатывается запрос ⏳`
- Расчет остатка: `Расчет остатка по заказу ⏳`
- Получение веса: `Получение веса ⚖️`

После сообщения остатка дополнительно отправляется:
- `📷 Отсканируйте шпулю.`

## 9. Delete/Restore логика
Файл: `delete_last.py`

- Delete:
  - ставит `E = Eror`
  - переносит `J -> K`
  - очищает `J`
- Restore:
  - снимает `E`
  - снимает `I=Trash` (если стояло)
  - возвращает `K -> J`
  - очищает `K`

В `bot.py` после delete/restore:
- пересчет `net_today` (если та же бизнес-дата, тот же заказ, не Trash);
- пересинхронизация счетчиков `spool_seq_ok/spool_seq_trash` по листу.

## 10. Нумерация бобин
- Перед присвоением номера вызывается `_ensure_spool_seq(...)`.
- Источник правды — `get_spool_counts(...)` из Google Sheets.
- Для OK и Trash счетчики отдельные.
- В test-режиме строка помечается `Eror`, номер в `J` очищается, локальный счетчик откатывается.

## 11. Надежность и таймауты
- Таймаут операций Sheets: `SHEETS_OP_TIMEOUT_SEC = 300`.
- Retry для Telegram и Sheets.
- Защита от дублей/гонок:
  - `pending_inputs`
  - `in_flight_requests`
  - таймауты pending/in-flight.
- Троттлинг сетевых ошибок в логах.

## 12. Логи
- `Bug_log.txt` (rotating file handler).
- При работе как NSSM могут использоваться:
  - `service_stdout.log`
  - `service_stderr.log`
- UTF-8 защита stdout/stderr включена в `bot.py`.

## 13. Конфиг ЛРП (`devices.py`)
Для каждого ЛРП:
- `name`
- `scale` (`host`, `port`, `duration`)
- `printer` (`printer_name`, `sheet_gid`, `foxit_path`, `paper_*`)
- `log_sheet_name`

`spreadsheet_id` для печати берется из env:
- `PRINTER_SPREADSHEET_ID`

## 14. Известные риски
- NSSM online-ротация stdout/stderr может зависать, если файл занят.
- Ошибки DNS/сети дают `getaddrinfo failed` и срывают polling.
- Некорректные форматы даты в `Log` ломают сводки по дате.
- Неправильные имена кнопок/ЛРП ломают переход 1 -> 2 уровень.

## 15. Операционный runbook (кратко)
### 15.1 Проверка сервиса
```powershell
sc query BasepackTelegramBot
```

### 15.2 Проверка stderr
```powershell
Get-Content -Path C:\BasepackBot\service_stderr.log -Tail 100
```

### 15.3 Убрать зависший STOP_PENDING
```powershell
sc queryex BasepackTelegramBot
taskkill /F /PID <PID>
```

### 15.4 Проверка связи с Telegram
```powershell
nslookup api.telegram.org
Test-NetConnection api.telegram.org -Port 443
```

## 16. Чеклист после изменения логики
1. Private flow: ЛРП -> заказ -> шпуля.
2. `Брак` не влияет на `net_today`.
3. Delete/restore корректно меняет `E/J/K`.
4. Нумерация после delete/restore и после restart корректна.
5. Остаток пересчитывается корректно.
6. `/order` и `/date` работают в группе и private как задумано.
7. Печать не блокирует поток и не падает по таймауту.

## 17. Что обновлять в этом файле при каждой существенной правке
- Измененные файлы и зачем.
- Измененные бизнес-правила.
- Новые env-переменные.
- Известные риски/ограничения.
- Короткий changelog.

## 18. Последние изменения
- 2026-02-13: отключено автоматическое форматирование Google Sheets при записи (удален вызов `_ensure_date_column_format`), формат таблиц теперь полностью управляется вручную в 1С/Google Sheets.
- 2026-02-13: вынесен `spreadsheet_id` принтера в env-переменную `PRINTER_SPREADSHEET_ID` (`devices.py`).
- 2026-02-13: обновлен `EDITOR_USER_IDS.txt`.
- 2026-02-13: создан и расширен `PROJECT_CONTEXT.md` для переноса полного контекста между ПК/сессиями.
