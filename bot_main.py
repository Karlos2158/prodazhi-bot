# -*- coding: utf-8 -*-
"""Telegram-бот учёта продаж.

Клавиатура постоянная: отчёт, категории, оплата, перемещение.
Запуск: python bot_main.py
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import ssl
from difflib import get_close_matches

import bot_sales as bs
import bot_vision as bv
import sales_core as sc

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "bot_config.json")
STATE_PATH = os.path.join(ROOT, "bot_state.json")
TEMP_DIR = os.path.join(ROOT, "bot_temp")

BTN_REPORT = "📊 Отчёт за день"
BTN_PERIOD = "📅 Отчёт за период"
BTN_CANCEL = "🗑 Удалить отчёт"
BTN_FIX = "✏️ Правка позиций"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "📖 Инструкции"
BTN_ENGINE_PREFIX = "🔄 Модель: "


def engine_label():
    return BTN_ENGINE_PREFIX + ("Gemini" if ENGINE == "gemini" else "локальная")
BTN_CATS = ["Вывод", "Матрица", "Новинка"]
BTN_CASH = ["Нал", "Карта"]
BTN_TRANSFER = ["Перемещение", "Не перемещение"]


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


CFG = load_json(CFG_PATH, {})
STATE = load_json(STATE_PATH, {"pending": [], "next": 1})
TOKEN = (CFG.get("bot_token") or "").strip()
MODEL = CFG.get("vision_model") or "qwen2.5vl:7b"
ENGINE = (CFG.get("vision_engine") or "local").lower()
GEMINI_KEY = (CFG.get("gemini_api_key") or "").strip()
GEMINI_MODEL = CFG.get("gemini_model") or "gemini-2.5-flash"
PATHS = CFG.get("paths", {})
ROOT_DIR = CFG.get("root") or r"G:\Sanator"
CONFIG_PATH = PATHS.get("config") or os.path.join(ROOT, "Конфиг.json")
HOME = CFG.get("home_point") or "Санаторий Лесная Поляна"


def base_for(date):
    try:
        year = str(date).split(".")[-1]
        if len(year) == 4 and year.isdigit():
            return os.path.join(ROOT_DIR, year, "foto")
    except (AttributeError, IndexError):
        pass
    return PATHS.get("base") or os.path.join(ROOT_DIR, time.strftime("%Y"), "foto")


_CATALOG_CACHE = {}


def _natural_key(name):
    # «(2)» и «10» сравниваем как числа, а не посимвольно
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def nom_for(date):
    # Берем САМЫЙ СВЕЖИЙ файл номенклатуры месяца: по дате изменения,
    # а при равных датах — по «естественному» имени (чтобы «(2)» было новее, чем без него)
    try:
        parts = str(date).split(".")
        year, month = parts[2], parts[1]
        d = os.path.join(ROOT_DIR, year, "nom", month)
        if os.path.isdir(d):
            files = [os.path.join(d, f) for f in os.listdir(d)
                     if f.lower().endswith((".xlsx", ".xls")) and not f.startswith("~$")]
            if files:
                files.sort(key=lambda p: (os.path.getmtime(p), _natural_key(os.path.basename(p))),
                           reverse=True)
                return files[0]
    except (AttributeError, IndexError, OSError):
        pass
    return PATHS.get("nom")


def catalog_for(date):
    p = nom_for(date)
    try:
        stamp = os.path.getmtime(p) if p and os.path.exists(p) else 0
    except OSError:
        stamp = 0
    key = (p or "", stamp)
    if key not in _CATALOG_CACHE:
        _CATALOG_CACHE[key] = sc.load_catalog(p)
    return _CATALOG_CACHE[key]


def app_config():
    return bs.load_app_config(CONFIG_PATH)


def current_rates():
    r = bs.rates_from_config(app_config())
    return r or bs.load_rates(PATHS["rates"])


def plan_value(year, month):
    v = bs.load_plan(ROOT_DIR, year, month)
    if v is None:
        v = bs.plan_for(app_config(), year, month)
    return v or 0.0


def send_help(chat_id):
    path = os.path.join(ROOT, "Инструкция.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        send(chat_id, "Файл инструкции не найден.")
        return
    chunk = ""
    for part in text.split("\n\n"):
        if len(chunk) + len(part) > 3500:
            send(chat_id, chunk.strip())
            chunk = ""
        chunk += part + "\n\n"
    if chunk.strip():
        send(chat_id, chunk.strip())


def show_plan(chat_id):
    import datetime
    d = datetime.datetime.now()
    plan = plan_value(d.year, d.month)
    if not plan:
        send(chat_id, "План на {:02d}.{} не задан. Задайте командой «план 450000».".format(d.month, d.year))
        return
    sold = month_sold(d.year, d.month)
    cfg = app_config()
    met = sold >= plan
    pct = bs.plan_percent(cfg, met)
    diff = plan - sold
    lines = [
        "План на {:02d}.{}: {} ₽".format(d.month, d.year, money(plan)),
        "Продано: {} ₽ ({:.1f}% плана)".format(money(sold), sold / plan * 100),
        ("До плана: {} ₽".format(money(diff)) if diff > 0 else "Перевыполнение: {} ₽".format(money(-diff))),
        "Сейчас: {:.0f}% ({}) → {} ₽".format(pct * 100, "план выполнен" if met else "план не выполнен", money(sold * pct)),
    ]
    if not met:
        lines.append("Если план выполнить: {:.0f}% → {} ₽".format(bs.plan_percent(cfg, True) * 100, money(sold * bs.plan_percent(cfg, True))))
    send(chat_id, "\n".join(lines))


def show_settings(chat_id):
    import datetime
    cfg = app_config()
    rates = bs.rates_from_config(cfg)
    now = datetime.datetime.now()
    plan = plan_value(now.year, now.month)
    p = cfg.get("зарплата_от_плана") or {}
    
    msg_text = "⚙️ Настройки\n\nПроценты категорий: Вывод {:.0f}%, Матрица {:.0f}%, Новинка {:.0f}%\nПлан на {:02d}.{}: {} ₽\nПри выполнении плана: {}%, при невыполнении: {}%\n\nЧтобы изменить, отправьте одну строку:\n«план 450000» — план на текущий месяц\n«вывод 5», «матрица 4», «новинка 6»\n«план выполнен 5», «план не выполнен 4»"
    send(chat_id, msg_text.format(
        rates.get("вывод", 0) * 100, rates.get("матриц", 0) * 100,
        rates.get("новинк", 0) * 100, now.month, now.year, money(plan),
        p.get("план_выполнен", 0), p.get("план_не_выполнен", 0)))


def month_sold(year, month):
    base = os.path.join(ROOT_DIR, str(year), "foto")
    total = 0.0
    try:
        for day in os.listdir(base):
            data = bs.load_day(base, day)
            for rec in data.get("receipts", []):
                total += float(rec.get("total") or 0)
    except OSError:
        pass
    return total


def keyboard():
    return {"keyboard": [
        [{"text": BTN_REPORT}, {"text": BTN_PERIOD}, {"text": BTN_CANCEL}],
        [{"text": BTN_SETTINGS}, {"text": BTN_FIX}, {"text": engine_label()}],
        [{"text": c} for c in BTN_CATS],
        [{"text": BTN_CASH[0]}, {"text": BTN_TRANSFER[0]}, {"text": BTN_TRANSFER[1]}, {"text": BTN_CASH[1]}],
    ], "resize_keyboard": True, "is_persistent": True}


def state_save():
    save_json(STATE_PATH, STATE)


def api(method, params=None, timeout=30):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        "https://api.telegram.org/bot{}/{}".format(TOKEN, method),
        data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("Telegram {}: {}".format(e.code, e.read().decode("utf-8", "replace")[:300]))


def send(chat_id, text, reply_markup=None):
    return api("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": reply_markup if reply_markup is not None else keyboard()})


def send_document(chat_id, path, caption=""):
    boundary = "----dsh" + uuid.uuid4().hex
    parts = []
    for k, v in [("chat_id", str(chat_id)), ("caption", caption)]:
        parts.append("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, k, v).encode("utf-8"))
    parts.append(("--{}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{}\"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".format(boundary, os.path.basename(path))).encode("utf-8"))
    with open(path, "rb") as f:
        parts.append(f.read())
    parts.append("\r\n--{}--\r\n".format(boundary).encode("utf-8"))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        "https://api.telegram.org/bot{}/sendDocument".format(TOKEN), data=b"".join(parts),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
    with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def download_photo(file_id):
    info = api("getFile", {"file_id": file_id})["result"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen("https://api.telegram.org/file/bot{}/{}".format(TOKEN, info["file_path"]), timeout=120, context=ctx) as r:
        return r.read()


def allowed(chat_id):
    return chat_id in CFG.get("allowed_chats", [])


def grant(chat_id):
    CFG.setdefault("allowed_chats", []).append(chat_id)
    save_json(CFG_PATH, CFG)


def money(v):
    return "{:,.2f}".format(float(v or 0)).replace(",", " ")


def receipt_label(time_str, date):
    return "{} от {}".format(time_str or "—", date)


def set_active_day(chat_id, date):
    STATE.setdefault("active_day", {})[str(chat_id)] = date
    state_save()


def store_temp(image_bytes):
    os.makedirs(TEMP_DIR, exist_ok=True)
    p = os.path.join(TEMP_DIR, uuid.uuid4().hex + ".jpg")
    with open(p, "wb") as f:
        f.write(image_bytes)
    return p


def new_pending(chat_id, **kw):
    item = {"id": STATE["next"], "chat": chat_id, "answered": False, "source": None,
            "idx": -1, "date": "", "rec_key": "", "name": "", "total": 0}
    item.update(kw)
    STATE["next"] += 1
    STATE["pending"].append(item)
    state_save()
    return item


def current_question(chat_id):
    for p in STATE["pending"]:
        if p["chat"] == chat_id and not p["answered"]:
            return p
    return None


def find_pending(chat_id, p_id):
    for item in STATE["pending"]:
        if item["id"] == p_id and item["chat"] == chat_id:
            return item
    return None


def item_state(date, rec_key, idx):
    """Текущее состояние позиции: чек, строка, совпадение, категория."""
    data = bs.load_day(base_for(date), date)
    for rec in data["receipts"]:
        if bs.receipt_key(rec) != rec_key:
            continue
        items = rec.get("items", [])
        if not (0 <= idx < len(items)):
            return None
        it = items[idx]
        src = it.get("name_override") or it.get("name", "")
        match, how = sc.match_item(catalog_for(date), src, it.get("price"))
        cat = it.get("cat_override") or (match["cat"] if match else "—")
        return rec, it, match, how, cat
    return None


def rate_for(cat):
    if not cat or cat == "—":
        return None
    return current_rates().get(bs.norm_cat(cat))


def similar_names(date, name, limit=6, cutoff=0.35):
    seen, names = set(), []
    for it in catalog_for(date):
        n = it["name"]
        if n not in seen:
            seen.add(n)
            names.append(n)
    scored = sorted(((sc.similarity(name, n), n) for n in names), reverse=True)
    return [n for s, n in scored[:limit] if s >= cutoff]


def make_reviews(chat_id, date, rec_key):
    """После оплаты спрашивает только про нечёткие совпадения и ненайденные позиции.

    Точное совпадение с номенклатурой подтверждения не требует — его можно
    поправить кнопкой «✏️ Правка позиций».
    """
    data = bs.load_day(base_for(date), date)
    catalog = catalog_for(date)
    for rec in data["receipts"]:
        if bs.receipt_key(rec) != rec_key:
            continue
        for idx, it in enumerate(rec.get("items", [])):
            if it.get("cat_override"):
                continue
            src = it.get("name_override") or it.get("name", "")
            match, how = sc.match_item(catalog, src, it.get("price"))
            if match and how == "точное совпадение названия":
                continue
            new_pending(chat_id, step="review", date=date, rec_key=rec_key, idx=idx,
                        name=it.get("name", ""), total=float(it.get("total") or 0))


def review_markup(p, has_match):
    pid = p["id"]
    if has_match:
        return {"inline_keyboard": [[
            {"text": "✅ Верно", "callback_data": "ok|{}".format(pid)},
            {"text": "✏️ Исправить", "callback_data": "fix|{}".format(pid)}]]}
    return {"inline_keyboard": [
        [{"text": "🔎 Похожие из номенклатуры", "callback_data": "fix|{}".format(pid)}],
        [{"text": "📦 Перемещение", "callback_data": "mv|{}".format(pid)},
         {"text": "✏️ Вписать вручную", "callback_data": "man|{}".format(pid)}]]}


def show_step(chat_id, p):
    step = p.get("step")
    if step == "date":
        send(chat_id, "Не смог прочитать дату на чеке.\nНапишите её в формате ДД.ММ.ГГГГ, например 05.10.2026.")
    elif step == "payment":
        send(chat_id, "Как оплачено по чеку от {}?".format(p["date"]),
             reply_markup={"inline_keyboard": [[
                 {"text": BTN_CASH[0], "callback_data": "pay|{}|0".format(p["id"])},
                 {"text": BTN_CASH[1], "callback_data": "pay|{}|1".format(p["id"])}]]})
    elif step == "review":
        st = item_state(p["date"], p["rec_key"], p["idx"])
        if not st:
            p["answered"] = True
            state_save()
            ask_next(chat_id)
            return
        rec, it, match, how, cat = st
        lines = ["«{}» — {} ₽".format(it.get("name", ""), money(it.get("total")))]
        if match:
            rate = rate_for(cat)
            lines.append("В номенклатуре: «{}»".format(match["name"]))
            line = "Категория: {}".format(cat)
            if rate:
                line += " ({}%)".format(int(round(rate * 100)))
            lines.append(line)
            if not it.get("cat_override") and how != "точное совпадение названия":
                lines.append("Подобрано автоматически — проверьте.")
        else:
            lines.append("Точного совпадения в номенклатуре нет.")
        send(chat_id, "\n".join(lines), reply_markup=review_markup(p, bool(match)))
    elif step == "select_nom":
        names = similar_names(p["date"], p["name"])
        kb = [[{"text": n, "callback_data": "sel|{}|{}".format(p["id"], i)}]
              for i, n in enumerate(names)]
        kb.append([{"text": "✏️ Вписать вручную", "callback_data": "man|{}".format(p["id"])},
                   {"text": "⬅️ Назад", "callback_data": "back|{}".format(p["id"])}])
        send(chat_id, "«{}» — {} ₽\nВыберите название из номенклатуры или впишите своё.".format(
            p["name"], money(p["total"])), reply_markup={"inline_keyboard": kb})
    elif step == "transfer":
        send(chat_id, "«{}» — {} ₽\nЭто перемещение с другой точки?".format(p["name"], money(p["total"])),
             reply_markup={"inline_keyboard": [[
                 {"text": "📦 Перемещение", "callback_data": "mv|{}".format(p["id"])},
                 {"text": "Не перемещение", "callback_data": "notmv|{}".format(p["id"])}]]})
    elif step == "manual_name":
        send(chat_id, "«{}» — {} ₽\nНапишите название товара текстом — как в номенклатуре или своё.".format(
            p["name"], money(p["total"])))
    elif step == "source":
        send(chat_id, "Напишите, откуда перемещён товар. Можно просто название точки.")
    else:
        kb = [[{"text": c, "callback_data": "cat|{}|{}".format(p["id"], i)}]
              for i, c in enumerate(BTN_CATS)]
        send(chat_id, "«{}» — {} ₽\nК какой категории отнести?".format(p["name"], money(p["total"])),
             reply_markup={"inline_keyboard": kb})


def ask_next(chat_id):
    p = current_question(chat_id)
    if not p:
        send(chat_id, "Все позиции распределены. Когда закончите — нажмите «{}».".format(BTN_REPORT))
        return
    show_step(chat_id, p)


def latest_day():
    best, best_t = None, 0
    try:
        for year in os.listdir(ROOT_DIR):
            ydir = os.path.join(ROOT_DIR, year, "foto")
            if not os.path.isdir(ydir):
                continue
            for m in os.listdir(ydir):
                md = os.path.join(ydir, m)
                if not os.path.isdir(md) or not m.isdigit():
                    continue
                for d in os.listdir(md):
                    p = os.path.join(md, d, "data.json")
                    if os.path.exists(p):
                        t = os.path.getmtime(p)
                        if t > best_t:
                            best, best_t = d, t
    except OSError:
        pass
    return best


DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?")


def parse_date_raw(g1, g2, g3=None):
    y = int(g3) if g3 else time.localtime().tm_year
    if y < 100:
        y += 2000
    try:
        from datetime import datetime
        return datetime(y, int(g2), int(g1)).strftime("%d.%m.%Y")
    except ValueError:
        return None


def all_dates(text):
    out = []
    for m in DATE_RE.finditer(text):
        d = parse_date_raw(*m.groups())
        if d and d not in out:
            out.append(d)
    return out


MONTH_STEMS = [("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("май", 5),
               ("мая", 5), ("июн", 6), ("июл", 7), ("август", 8), ("сентябр", 9),
               ("октябр", 10), ("ноябр", 11), ("декабр", 12)]


def month_days(month, year):
    import calendar
    last = calendar.monthrange(year, month)[1]
    return ("{:02d}.{:02d}.{:04d}".format(1, month, year),
            "{:02d}.{:02d}.{:04d}".format(last, month, year))


def find_month(text):
    low = text.lower()
    for stem, num in MONTH_STEMS:
        if stem in low:
            return num
    return None


def show_period_menu(chat_id):
    kb = {"inline_keyboard": [
        [{"text": "Этот месяц", "callback_data": "p|cur"},
         {"text": "Прошлый месяц", "callback_data": "p|prev"}],
        [{"text": "Отмена", "callback_data": "p|cancel"}],
    ]}
    send(chat_id, "За какой период?\nНажмите кнопку или напишите: «октябрь», «за октябрь 2026», «с 01.10 по 15.10».", reply_markup=kb)


def dates_between(a, b):
    from datetime import datetime, timedelta
    d1 = datetime.strptime(a, "%d.%m.%Y")
    d2 = datetime.strptime(b, "%d.%m.%Y")
    if d1 > d2:
        d1, d2 = d2, d1
    out = []
    while d1 <= d2 and len(out) < 400:
        out.append(d1.strftime("%d.%m.%Y"))
        d1 += timedelta(days=1)
    return out


def collect_rows(dates):
    # Очищаем кэш каталога при построении отчетов за периоды, чтобы подтянуть актуальные изменения номенклатуры
    global _CATALOG_CACHE
    _CATALOG_CACHE.clear()
    
    rates = current_rates()
    rows = []
    for d in dates:
        data = bs.load_day(base_for(d), d)
        if not data["receipts"]:
            continue
        for r in bs._rows(data, catalog_for(d), rates, fresh=True):
            r["date"] = d
            rows.append(r)
    return rows


def send_period_report(chat_id, dates, title=None):
    rows = collect_rows(dates)
    if not rows:
        send(chat_id, "За выбранные даты чеков нет.")
        return
    year = dates[0].split(".")[-1]
    out_dir = os.path.join(ROOT_DIR, year, "foto", "_отчёты")
    os.makedirs(out_dir, exist_ok=True)
    name = "отчёт_{}_{}.xlsx".format(dates[0].replace(".", "-"), dates[-1].replace(".", "-"))
    path = os.path.join(out_dir, name)

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Свод"
    
    headers = ["№", "Дата", "Наименование", "Цена", "Процент скидки", "Количество", "Итог",
               "Категория", "Нал.", "Ставка", "Зарплата", "Ставка2", "Зарплата2", "Примечание"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, size=11)
        cell.fill = PatternFill("solid", fgColor="F2E6D0")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        
    for i, r in enumerate(rows, start=2):
        vals = [i - 1, r["date"], r["name"], r["price"], r["discount"], r["qty"], r["total"],
                r["cat"], r["nal"], r["rate"], r["salary"], r["rate2"], r["salary2"], r["note"]]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = Font(size=10)
            if c in (3, 14):
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            elif c in (4, 7, 11, 13):
                cell.number_format = "#,##0.00"
            elif c in (5, 10, 12):
                cell.number_format = "0%"

    tr = len(rows) + 2
    ws.cell(row=tr, column=1, value="ИТОГО")
    ws.cell(row=tr, column=6, value=sum(r["qty"] for r in rows))
    ws.cell(row=tr, column=7, value=round(sum(r["total"] for r in rows), 2))
    ws.cell(row=tr, column=11, value=round(sum(r["salary"] for r in rows), 2))
    ws.cell(row=tr, column=13, value=round(sum(r["salary2"] for r in rows), 2))
    
    for c in range(1, 15):
        cell = ws.cell(row=tr, column=c)
        cell.font = Font(bold=True, size=11)
        cell.fill = PatternFill("solid", fgColor="EDEDED")
    ws.cell(row=tr, column=7).number_format = "#,##0.00"
    ws.cell(row=tr, column=11).number_format = "#,##0.00"
    ws.cell(row=tr, column=13).number_format = "#,##0.00"

    cash = sum(r["total"] for r in rows if r["nal"])
    card = sum(r["total"] for r in rows) - cash
    ws.cell(row=tr + 2, column=1,
            value="Наличными: {:.2f} ₽   |   Безналично: {:.2f} ₽".format(cash, card)).font = Font(bold=True, size=10)

    total = sum(r["total"] for r in rows)
    salary_total = round(sum(r["salary"] for r in rows), 2)
    salary2_total = round(sum(r["salary2"] for r in rows), 2)
    
    plan_lines = []
    try:
        cfg = app_config()
        y, m = dates[0].split(".")[2], dates[0].split(".")[1]
        plan = plan_value(int(y), int(m))
        if plan > 0:
            met = total >= plan
            diff = plan - total
            done = total / plan * 100.0
            plan_lines = [
                "План на {:02d}.{}: {:.2f} ₽".format(int(m), y, plan),
                "Продано: {:.2f} ₽ ({:.1f}% плана)".format(total, done),
                ("До плана: {:.2f} ₽".format(diff) if diff > 0 else "Перевыполнение: {:.2f} ₽".format(-diff)),
                "Сейчас (план не выполнен) → {:.2f} ₽".format(salary_total),
                "Если план выполнить → {:.2f} ₽".format(salary2_total),
            ]
    except (IndexError, ValueError):
        plan_lines = []

    for i, line in enumerate(plan_lines):
        cell = ws.cell(row=tr + 4 + i, column=1, value=line)
        cell.font = Font(size=10, bold=(i == 0))

    for col, w in {"A": 5, "B": 12, "C": 40, "D": 11, "E": 12, "F": 11, "G": 13, "H": 11,
                   "I": 7, "J": 12, "K": 12, "L": 12, "M": 13, "N": 52}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    wb.save(path)

    days = len({r["date"] for r in rows})
    text = "{}: {} дн., {} позиций, {} ₽.\nЗарплата: {} ₽ (при выполнении: {} ₽), из них наличными {} ₽.".format(
        title or "Свод за период", days, len(rows), money(total), money(salary_total), money(salary2_total), money(cash))
    if plan_lines:
        text += "\n\n" + "\n".join(plan_lines)
    send(chat_id, text)
    try:
        send_document(chat_id, path, "Свод за период")
    except Exception as e:
        send(chat_id, "Файл не ушёл: {}".format(e))


def edit_text(chat_id, msg_id, text):
    try:
        api("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": text})
    except Exception:
        pass


def show_delete_list(chat_id):
    day = STATE.get("active_day", {}).get(str(chat_id)) or latest_day()
    if not day:
        send(chat_id, "Чеков нет — удалять нечего.")
        return
    data = bs.load_day(base_for(day), day)
    if not data["receipts"]:
        send(chat_id, "За {} чеков нет.".format(day))
        return
    rows = []
    for i, r in enumerate(data["receipts"]):
        rows.append([{"text": "{} — {} ₽".format(r.get("time", "?"), money(r.get("total"))),
                      "callback_data": "d|{}|{}".format(day, i)}])
    rows.append([{"text": "Отмена", "callback_data": "d|{}|-1".format(day)}])
    send(chat_id, "Какой чек удалить? День {}.".format(day), reply_markup={"inline_keyboard": rows})


def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data_str = str(cb.get("data", ""))
    parts = data_str.split("|")
    try:
        api("answerCallbackQuery", {"callback_query_id": cb["id"]})
    except Exception:
        # кнопка могла устареть (бот был перезапущен) — это не ошибка работы
        pass

    pid = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
    p = find_pending(chat_id, pid) if pid is not None else None

    if parts[0] == "f":
        if len(parts) != 4 or not parts[2].lstrip("-").isdigit() or not parts[3].lstrip("-").isdigit():
            return
        day, ri, ii = parts[1], int(parts[2]), int(parts[3])
        if ri < 0:
            edit_text(chat_id, msg_id, "Правка отменена.")
            return
        data = bs.load_day(base_for(day), day)
        if ri >= len(data["receipts"]) or ii >= len(data["receipts"][ri].get("items", [])):
            edit_text(chat_id, msg_id, "Позиция уже изменилась — откройте «{}» заново.".format(BTN_FIX))
            return
        rec = data["receipts"][ri]
        item = rec["items"][ii]
        np = new_pending(chat_id, step="review", date=day, rec_key=bs.receipt_key(rec), idx=ii,
                         name=item.get("name", ""), total=float(item.get("total") or 0))
        edit_text(chat_id, msg_id, "Правка за {}.".format(day))
        show_step(chat_id, np)
        return

    if parts[0] in ("ok", "fix", "mv", "notmv", "man", "back"):
        if not p:
            edit_text(chat_id, msg_id, "Этот вопрос уже неактуален.")
            return
        if parts[0] == "ok":
            p["answered"] = True
            state_save()
            edit_text(chat_id, msg_id, "Принято: «{}».".format(p.get("name")))
            ask_next(chat_id)
        elif parts[0] == "fix":
            p["step"] = "transfer"
            state_save()
            edit_text(chat_id, msg_id, "Исправляем название.")
            show_step(chat_id, p)
        elif parts[0] == "back":
            p["step"] = "review"
            state_save()
            show_step(chat_id, p)
        elif parts[0] == "mv":
            p["step"] = "source"
            state_save()
            show_step(chat_id, p)
        elif parts[0] == "notmv":
            p["step"] = "select_nom"
            state_save()
            show_step(chat_id, p)
        elif parts[0] == "man":
            p["step"] = "manual_name"
            state_save()
            show_step(chat_id, p)
        return

    if parts[0] == "sel":
        if not p:
            edit_text(chat_id, msg_id, "Этот вопрос уже неактуален.")
            return
        names = similar_names(p["date"], p["name"])
        try:
            i = int(parts[2])
        except (IndexError, ValueError):
            show_step(chat_id, p)
            return
        if i < 0 or i >= len(names):
            show_step(chat_id, p)
            return
        full = names[i]
        set_item_name_safe(base_for(p["date"]), p["date"], p["rec_key"], p["idx"], full)
        bs.build_xlsx(base_for(p["date"]), p["date"], catalog_for(p["date"]), current_rates())
        st = item_state(p["date"], p["rec_key"], p["idx"])
        match = st[2] if st else None
        p["name"] = full
        if match:
            p["answered"] = True
            state_save()
            edit_text(chat_id, msg_id, "Выбрано: «{}» → {}.".format(full, st[4]))
            ask_next(chat_id)
        else:
            p["step"] = "category"
            state_save()
            edit_text(chat_id, msg_id, "Выбрано: «{}».".format(full))
            show_step(chat_id, p)
        return

    if parts[0] == "cat":
        if not p:
            edit_text(chat_id, msg_id, "Этот вопрос уже неактуален.")
            return
        try:
            cat = BTN_CATS[int(parts[2])]
        except (IndexError, ValueError):
            edit_text(chat_id, msg_id, "Не понял категорию.")
            return
        note = "Перемещение: из {} в {}".format(p["source"], HOME) if p.get("source") else ""
        bs.set_override(base_for(p["date"]), p["date"], p["rec_key"], p["idx"], cat, note)
        p["answered"] = True
        state_save()
        path, salary, cash = apply_answer(chat_id, p)
        edit_text(chat_id, msg_id, "Категория: {}.".format(cat))
        send(chat_id, "«{}» → {}.\nЗарплата за день: {} ₽, наличными {} ₽.".format(
            p["name"], cat, money(salary), money(cash)))
        ask_next(chat_id)
        return

    if parts[0] == "pay":
        if not p:
            edit_text(chat_id, msg_id, "Этот вопрос уже неактуален.")
            return
        pay = "наличные" if parts[2] == "0" else "карта"
        p["answered"] = True
        bs.set_payment(base_for(p["date"]), p["date"], p["rec_key"], pay)
        state_save()
        edit_text(chat_id, msg_id, "Оплата: {}.".format(pay))
        if bs.is_pre_split(p["date"]):
            ask_next(chat_id)
        else:
            make_reviews(chat_id, p["date"], p["rec_key"])
            ask_next(chat_id)
        return

    if parts[0] == "p":
        if parts[1] == "cancel":
            edit_text(chat_id, msg_id, "Отменено.")
            return
        from datetime import datetime
        now = datetime.now()
        if parts[1] == "cur":
            y, m = now.year, now.month
        else:
            y, m = now.year, now.month - 1
            if m == 0:
                m, y = 12, y - 1
        d1, d2 = month_days(m, y)
        edit_text(chat_id, msg_id, "Строю свод за {:02d}.{:04d}…".format(m, y))
        send_period_report(chat_id, dates_between(d1, d2),
                           "Свод за {:02d}.{:04d}".format(m, y))
        return

    if len(parts) != 3 or parts[0] != "d":
        return
    day, idx = parts[1], int(parts[2])
    if idx < 0:
        edit_text(chat_id, msg_id, "Удаление отменено.")
        return
    data = bs.load_day(base_for(day), day)
    if idx >= len(data["receipts"]):
        edit_text(chat_id, msg_id, "Этот чек уже удалён.")
        return
    rec = data["receipts"][idx]
    bs.remove_receipt(base_for(day), day, bs.receipt_key(rec))
    for p in STATE["pending"]:
        if p.get("rec_key") == bs.receipt_key(rec):
            p["answered"] = True
    state_save()
    items = catalog_for(day)
    rates = current_rates()
    bs.build_xlsx(base_for(day), day, items, rates)
    edit_text(chat_id, msg_id, "Чек {} удалён из {}.".format(
        receipt_label(rec.get("time"), day), day))
    send(chat_id, "Готово. Если нужно — пришлите его фото заново.")


def show_fix_list(chat_id):
    day = STATE.get("active_day", {}).get(str(chat_id)) or latest_day()
    if not day:
        send(chat_id, "Чеков нет — править нечего.")
        return
    data = bs.load_day(base_for(day), day)
    if not data["receipts"]:
        send(chat_id, "За {} чеков нет.".format(day))
        return
    rows = []
    for ri, rec in enumerate(data["receipts"]):
        for ii, it in enumerate(rec.get("items", [])):
            rows.append([{"text": "{} — {} ₽".format(
                str(it.get("name_override") or it.get("name", ""))[:45], money(it.get("total"))),
                "callback_data": "f|{}|{}|{}".format(day, ri, ii)}])
    rows.append([{"text": "Отмена", "callback_data": "f|{}|-1|-1".format(day)}])
    send(chat_id, "Что исправить? День {}. Выберите позицию.".format(day),
         reply_markup={"inline_keyboard": rows})


def apply_name_choice(chat_id, p, name):
    """Записывает выбранное название; если оно есть в номенклатуре — сразу категория."""
    date = p["date"]
    set_item_name_safe(base_for(date), date, p["rec_key"], p["idx"], name)
    bs.build_xlsx(base_for(date), date, catalog_for(date), current_rates())
    p["name"] = name
    st = item_state(date, p["rec_key"], p["idx"])
    match = st[2] if st else None
    if match:
        p["answered"] = True
        state_save()
        send(chat_id, "Установлено: «{}» → {}.".format(name, st[4]))
        ask_next(chat_id)
    else:
        p["step"] = "category"
        state_save()
        send(chat_id, "Название: «{}». В номенклатуре его нет — выберите категорию.".format(name))
        show_step(chat_id, p)


def send_day_report(chat_id, day=None):
    day = day or STATE.get("active_day", {}).get(str(chat_id)) or latest_day()
    if not day:
        send(chat_id, "Пока нет ни одного чека за день. Пришлите фото.")
        return
    data = bs.load_day(base_for(day), day)
    if not data["receipts"]:
        send(chat_id, "За {} чеков нет.".format(day))
        return
    set_active_day(chat_id, day)
    items = catalog_for(day)
    rates = current_rates()
    path, rows = bs.build_xlsx(base_for(day), day, items, rates)
    total = sum(r["total"] for r in rows)
    salary = sum(r["salary"] for r in rows)
    cash = sum(r["total"] for r in rows if r["nal"])
    unknown = [r for r in rows if r["cat"] == "—"]
    text = "Отчёт за {}: {} позиций, {} ₽.\nЗарплата: {} ₽, из них наличными {} ₽.".format(
        day, len(rows), money(total), money(salary), money(cash))
    if unknown:
        text += "\nБез категории: {}".format(", ".join(r["name"][:30] for r in unknown[:5]))
    send(chat_id, text)
    try:
        send_document(chat_id, path, "Отчёт за " + day)
    except Exception as e:
        send(chat_id, "Файл не ушёл: {}".format(e))


def commit(chat_id, rec, image):
    date = rec["date"]
    base = base_for(date)

    day = bs.day_dir(base, date)
    fname = "photo_{}.jpg".format(time.strftime("%Y%m%d_%H%M%S"))
    with open(os.path.join(day, fname), "wb") as f:
        f.write(image)
    rec["photo"] = fname

    added, why = bs.add_receipt(base, date, rec)
    if not added:
        set_active_day(chat_id, date)
        send(chat_id, "Чек {} уже учтён: {}\nАктивный день — {}.".format(
            receipt_label(rec.get("time"), date), why, date))
        return

    items = catalog_for(date)
    rates = current_rates()
    path, rows = bs.build_xlsx(base, date, items, rates)
    set_active_day(chat_id, date)

    total = sum(r["total"] for r in rows)
    salary = sum(r["salary"] for r in rows)
    
    # ВЫВОДИМ ПОДРОБНЫЙ ОТЧЕТ ПО ПОЗИЦИЯМ ДЛЯ КОНТРОЛЯ
    items_text = []
    for idx_i, item_i in enumerate(rec.get("items", []), 1):
        items_text.append("{}. {} — {} шт. по {} ₽ (итог: {} ₽)".format(
            idx_i, item_i.get("name"), item_i.get("qty", 1), money(item_i.get("price")), money(item_i.get("total"))))
    
    report_msg = "🧾 Распознан чек от {} (время {}):\n{}\n\n💰 Итого сумма: {} ₽".format(
        date, rec.get("time", "—"), "\n".join(items_text), money(rec.get("total") or total))
    send(chat_id, report_msg)

    rec_key = bs.receipt_key(rec)
    new_pending(chat_id, step="payment", date=date, rec_key=rec_key, name=receipt_label(rec.get("time"), date), total=total)
    # Вопросы по позициям создаются после ответа про оплату (см. handle_callback, ветка pay)
    ask_next(chat_id)


def set_item_name_safe(base, date, rec_key, idx, new_name):
    """Пишет исправленное название в name_override, исходную строку чека сохраняет."""
    return bs.set_item_name(base, date, rec_key, idx, new_name)


def apply_answer(chat_id, p):
    day = p.get("date") or p.get("day")
    items = catalog_for(day)
    rates = current_rates()
    path, rows = bs.build_xlsx(base_for(day), day, items, rates)
    salary = sum(r["salary"] for r in rows)
    cash = sum(r["total"] for r in rows if r["nal"])
    return path, salary, cash


def recognize(image):
    if ENGINE == "gemini" and GEMINI_KEY:
        return bv.extract_gemini(image, GEMINI_KEY, GEMINI_MODEL)
    return bv.extract_local(image, MODEL)


_RECENT_PHOTOS = {}

def handle_photo(chat_id, msg):
    photos = msg.get("photo") or []
    if not photos:
        return
    file_id = photos[-1]["file_id"]
    now = time.time()
    if file_id in _RECENT_PHOTOS and now - _RECENT_PHOTOS[file_id] < 10:
        return
    _RECENT_PHOTOS[file_id] = now
    
    send(chat_id, "Распознаю чек…")
    image = download_photo(file_id)
    try:
        rec = recognize(image)
    except Exception as e:
        msg = str(e)
        if "503" in msg or "429" in msg or "UNAVAILABLE" in msg or "overload" in msg.lower():
            send(chat_id, "Gemini сейчас перегружен и не отвечает.\nПодождите минуту и пришлите фото снова — данные не потеряются.")
        else:
            send(chat_id, "Не смог распознать чек: {}".format(msg[:300]))
        return

    if not rec.get("reconciled", True):
        send(chat_id, "Не сходится: сумма позиций {:.2f} ₽, итог чека {:.2f} ₽ (разница {:.2f}).\nПереснимите чек так, чтобы он занимал весь кадр.".format(
            sum(i["total"] for i in rec["items"]), rec["total"], rec.get("reconciled_diff", 0)))
        return

    if not rec.get("date"):
        img_path = store_temp(image)
        new_pending(chat_id, step="date", name="Дата чека", rec=rec, img=img_path)
        ask_next(chat_id)
        return

    commit(chat_id, rec, image)


def handle(update):
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return
    text = (msg.get("text") or "").strip()

    if not allowed(chat_id):
        code = CFG.get("invite_code") or ""
        if code and code.lower() in text.lower():
            grant(chat_id)
            send(chat_id, "Доступ открыт. Присылайте фото чеков.\nКогда закончите — нажмите «{}».".format(BTN_REPORT))
        else:
            send(chat_id, "Это закрытый бот. Отправьте код доступа из приглашения.")
        return

    if text == BTN_REPORT or text.lower() in ("отчет", "отчёт"):
        send_day_report(chat_id)
        return

    if text == BTN_PERIOD:
        show_period_menu(chat_id)
        return

    if text == BTN_HELP:
        send_help(chat_id)
        return

    if text == BTN_SETTINGS:
        show_settings(chat_id)
        return

    if text == BTN_CANCEL:
        show_delete_list(chat_id)
        return

    if text == BTN_FIX:
        show_fix_list(chat_id)
        return

    if text.startswith(BTN_ENGINE_PREFIX):
        global ENGINE
        ENGINE = "local" if ENGINE == "gemini" else "gemini"
        CFG["vision_engine"] = ENGINE
        save_json(CFG_PATH, CFG)
        hint = ("Теперь распознаю через Gemini." if ENGINE == "gemini"
                else "Теперь распознаю локальной моделью — медленнее и менее точно.")
        send(chat_id, hint + "\nКнопка изменится на «{}».".format(engine_label()))
        return

    m = re.fullmatch(r"план\s+(выполнен|не\s*выполнен)\s+(\d+(?:[.,]\d+)?)\s*%?", text.lower().strip())
    if m:
        key = "план_выполнен" if m.group(1) == "выполнен" else "план_не_выполнен"
        cfg = app_config()
        cfg.setdefault("зарплата_от_плана", {})[key] = float(m.group(2).replace(",", "."))
        bs.save_app_config(CONFIG_PATH, cfg)
        send(chat_id, "Записал: план {} = {}%.".format(
            "выполнен" if key == "план_выполнен" else "не выполнен", m.group(2)))
        return

    m = re.fullmatch(r"план\s+([\d\s]+)\s*(?:р|₽|руб\.?)?", text.lower().strip())
    if m:
        val = float(re.sub(r"\D", "", m.group(1)) or 0)
        import datetime
        now = datetime.datetime.now()
        bs.save_plan(ROOT_DIR, now.year, now.month, val)
        send(chat_id, "План на {:02d}.{}: {} ₽.".format(now.month, now.year, money(val)))
        return

    m = re.fullmatch(r"(вывод|матрица|новинк[аи])\s+(\d+(?:[.,]\d+)?)\s*%?", text.lower().strip())
    if m:
        name = {"вывод": "Вывод", "матрица": "Матрица",
                "новинка": "Новинка", "новинки": "Новинка"}[m.group(1)]
        cfg = app_config()
        cfg.setdefault("проценты_категорий", {})[name] = float(m.group(2).replace(",", "."))
        bs.save_app_config(CONFIG_PATH, cfg)
        send(chat_id, "Записал: {} = {}%.".format(name, m.group(2)))
        return

    low = text.lower()
    if "отч" in low and not low.startswith("/"):
        dates = all_dates(text)
        if len(dates) >= 2 and "с " in (" " + low) and "по" in low:
            send_period_report(chat_id, dates_between(dates[0], dates[1]),
                               "Свод с {} по {}".format(dates[0], dates[1]))
            return
        if len(dates) >= 2:
            send_period_report(chat_id, sorted(dates, key=lambda d: d[::-1]),
                               "Свод по выбранным дням")
            return
        if len(dates) == 1:
            send_day_report(chat_id, dates[0])
            return
        mon = find_month(text)
        if mon:
            ym = re.search(r"\b(20\d{2})\b", text)
            year = int(ym.group(1)) if ym else time.localtime().tm_year
            d1, d2 = month_days(mon, year)
            send_period_report(chat_id, dates_between(d1, d2),
                               "Свод за {:02d}.{:04d}".format(mon, year))
            return
        send(chat_id, "Укажите период: «отчёт 05.10.2026», «отчёт за октябрь», «отчёт с 01.10.2026 по 31.10.2026» или «отчёт 01.10.2026, 03.10.2026».")
        return

    if text.startswith("/start"):
        send(chat_id, "Пришлите фото чека целиком, с нижними строками ФД и ФН.\nКогда закончите — нажмите «{}».".format(BTN_REPORT))
        return

    p = current_question(chat_id)

    if p and p["step"] == "date" and text:
        m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
        if not m:
            send(chat_id, "Не понял дату. Напишите в формате ДД.ММ.ГГГГ, например 05.10.2026.")
            return
        date = "{:02d}.{:02d}.{}".format(int(m.group(1)), int(m.group(2)), m.group(3))
        p["answered"] = True
        state_save()
        rec = dict(p.get("rec") or {})
        rec["date"] = date
        image = b""
        if p.get("img") and os.path.exists(p["img"]):
            with open(p["img"], "rb") as f:
                image = f.read()
            try:
                os.remove(p["img"])
            except OSError:
                pass
        commit(chat_id, rec, image)
        return

    if p and p["step"] == "source" and text:
        p["source"] = text[:120]
        p["step"] = "category"
        state_save()
        send(chat_id, "Записал: из {}.".format(p["source"]))
        ask_next(chat_id)
        return

    if p and p["step"] == "payment" and text in BTN_CASH:
        pay = "наличные" if text == "Нал" else "карта"
        p["answered"] = True
        bs.set_payment(base_for(p["date"]), p["date"], p["rec_key"], pay)
        state_save()
        send(chat_id, "Оплата: {}.".format(pay))
        if not bs.is_pre_split(p["date"]):
            make_reviews(chat_id, p["date"], p["rec_key"])
        ask_next(chat_id)
        return

    if p and p["step"] == "transfer" and text in BTN_TRANSFER:
        p["step"] = "source" if text == BTN_TRANSFER[0] else "select_nom"
        state_save()
        show_step(chat_id, p)
        return

    if p and p["step"] == "manual_name" and text:
        apply_name_choice(chat_id, p, text)
        return

    if p and (p["step"] == "transfer" or p["step"] == "select_nom") and text and text not in BTN_TRANSFER and text not in BTN_CATS and text not in BTN_CASH:
        apply_name_choice(chat_id, p, text)
        return

    if p and p["step"] == "category" and text in BTN_CATS:
        p["answered"] = True
        note = "Перемещение: из {} в {}".format(p["source"], HOME) if p.get("source") else ""
        bs.set_override(base_for(p["date"]), p["date"], p["rec_key"], p["idx"], text, note)
        state_save()
        path, salary, cash = apply_answer(chat_id, p)
        send(chat_id, "«{}» → {}.\nЗарплата за день: {} ₽, наличными {} ₽.".format(
            p["name"], text, money(salary), money(cash)))
        ask_next(chat_id)
        return

    if msg.get("photo"):
        handle_photo(chat_id, msg)
        return

    if text:
        send(chat_id, "Не понял. Пришлите фото чека или нажмите кнопку внизу.")


def main():
    if not TOKEN:
        print("print bot_token missing")
        return
    print("бот запущен, модель:", MODEL)
    
    offset = 0
    
    while True:
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 25}, timeout=70)
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle(u)
                except Exception as e:
                    import traceback
                    print("ошибка обработки:", repr(e))
                    traceback.print_exc()
        except Exception as e:
            print("ошибка опроса:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
