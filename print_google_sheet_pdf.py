import os
import time
import tempfile
import subprocess
import requests
import fitz  # PyMuPDF
import logging

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

def safe_print(text: str):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"))

def mm_to_pt(value_mm: float) -> float:
    return value_mm * 72.0 / 25.4

def _sanitize_suffix(value: str) -> str:
    if not value:
        return ""
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid else ch for ch in value.strip())
    cleaned = " ".join(cleaned.split())
    return cleaned.replace(" ", "_")
def _safe_remove(path: str, retries: int = 3, delay_sec: float = 0.5) -> bool:
    for attempt in range(retries):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt == retries - 1:
                return False
            time.sleep(delay_sec * (attempt + 1))
    return False

def _cleanup_temp_cropped(base_dir: str, suffix: str, max_age_sec: float = 1800.0) -> None:
    now = time.time()
    prefix = f"sheet_cropped{suffix}_"
    for name in os.listdir(base_dir):
        if not (name.startswith(prefix) and name.endswith(".pdf")):
            continue
        path = os.path.join(base_dir, name)
        try:
            if now - os.path.getmtime(path) < max_age_sec:
                continue
        except OSError:
            continue
        _safe_remove(path, retries=2, delay_sec=0.5)


def print_google_sheet_pdf(
    spreadsheet_id,
    sheet_gid,
    printer_name,
    foxit_path,
    paper_width_mm,
    paper_height_mm,
    file_suffix=None,
    skip_print=False
):
    # 1) Скачиваем PDF из Google Таблицы
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
        f"?format=pdf"
        f"&gid={sheet_gid}"
        f"&size=7"
        f"&portrait=true"
        f"&fitw=false"
        f"&scale=4"
        f"&source=labnol"
        f"&top_margin=0.10"
        f"&bottom_margin=0.10"
        f"&left_margin=0.10"
        f"&right_margin=0.10"
        f"&sheetnames=false"
        f"&printtitle=false"
        f"&pagenumbers=false"
        f"&gridlines=false"
        f"&fzr=false"
    )
    safe_print("📥 Загружаем PDF из Google Таблицы…")
    time.sleep(1.5)
    resp = requests.get(export_url, timeout=60)
    resp.raise_for_status()

    # 2) Сохраняем PDF с фиксированным именем
    base_dir = os.path.dirname(__file__)
    safe_suffix = _sanitize_suffix(str(file_suffix)) if file_suffix is not None else ""
    suffix = f"_{safe_suffix}" if safe_suffix else ""
    pdf_path = os.path.join(base_dir, f"sheet_original{suffix}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(resp.content)
    safe_print(f"💾 PDF сохранён: {pdf_path}")
    # 3) Масштабируем контент под 100x100 мм и обрезаем от верхнего левого угла
    cropped_pdf_path = os.path.join(base_dir, f"sheet_cropped{suffix}.pdf")
    target_w = mm_to_pt(paper_width_mm)
    target_h = mm_to_pt(paper_height_mm)
    offset_x = mm_to_pt(0.5)
    offset_y = mm_to_pt(0.3)
    temp_cropped_path = None
    with fitz.open(pdf_path) as src_doc:
        out_doc = fitz.open()
        try:
            for page in src_doc:
                blocks = page.get_text("blocks")
                if blocks:
                    content_rect = fitz.Rect(blocks[0][:4])
                    for b in blocks[1:]:
                        content_rect |= fitz.Rect(b[:4])
                else:
                    content_rect = page.rect

                if content_rect.width <= 0 or content_rect.height <= 0:
                    content_rect = page.rect

                avail_w = max(1.0, target_w - offset_x)
                avail_h = max(1.0, target_h - offset_y)
                scale = min(avail_w / content_rect.width, avail_h / content_rect.height)
                scaled_w = content_rect.width * scale
                scaled_h = content_rect.height * scale

                new_page = out_doc.new_page(width=target_w, height=target_h)
                target_rect = fitz.Rect(offset_x, offset_y, offset_x + scaled_w, offset_y + scaled_h)
                new_page.show_pdf_page(target_rect, src_doc, page.number, clip=content_rect)

            fd, temp_cropped_path = tempfile.mkstemp(
                prefix=f"sheet_cropped{suffix}_",
                suffix=".pdf",
                dir=base_dir
            )
            os.close(fd)
            out_doc.save(temp_cropped_path)
        finally:
            out_doc.close()

    final_cropped_path = cropped_pdf_path
    if temp_cropped_path:
        replaced = False
        for attempt in range(3):
            try:
                os.replace(temp_cropped_path, cropped_pdf_path)
                replaced = True
                break
            except PermissionError:
                time.sleep(0.5 * (attempt + 1))
        if not replaced:
            _safe_remove(temp_cropped_path, retries=2, delay_sec=0.5)
            raise PermissionError(f"cannot replace {cropped_pdf_path}")

    logging.info("Cropped PDF saved: %s", final_cropped_path)




    if skip_print:
        _cleanup_temp_cropped(base_dir, suffix, max_age_sec=1800.0)
        return

    # 4) Проверяем Foxit
    if not os.path.isfile(foxit_path):
        raise FileNotFoundError(f"Foxit Reader не найден по пути: {foxit_path}")

    # 5) Печать через Foxit CLI
    cmd = [
        foxit_path,
        "/t",
        final_cropped_path,
        printer_name
    ]
    safe_print(f"🖨️ Печатаем на {printer_name} через Foxit...")
    subprocess.run(cmd, check=True)
    safe_print("✅ Печать завершена.")

