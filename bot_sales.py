# -*- coding: utf-8 -*-
"""Хранение дневных данных и сборка Excel."""
import json
import os
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

HEADERS = ["№", "Дата", "Наименование", "Цена", "Процент скидки", "Количество", "Итоговая цена",
           "Категория", "Нал.", "Ставка", "Зарплата", "Ставка2", "Зарплата2", "Примечание"]

PACK_RE = re.compile(r"коробк|пакет|конверт", re.I)

SPLIT_DATE = "01.11.2026"

def is_pre_split(date):
    from datetime import datetime
    try:
        return datetime.strptime(str(date), "%d.%m.%Y") < datetime.strptime(SPLIT_DATE, "%d.%m.%Y")
    except ValueError:
        return False

THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="F2E6D0")
TOTAL_FILL = PatternFill("solid", fgColor="EDEDED")
CASH_FILL = PatternFill("solid", fgColor="FFF2CC")

def norm_cat(s):
    s = str(s).strip().lower().replace("ё", "е")
    # Убираем окончание для унификации ("вывод" -> "вывод", "матрица" -> "матриц", "новинка" -> "новинк")
    if s in ("вывод", "вывода", "выводу"):
        return "вывод"
    if s in ("матрица", "матрицы", "матрице", "матриц"):
        return "матриц"
    if s in ("новинка", "новинки", "новинке", "новинок", "новинк"):
        return "новинк"
    return s[:-1] if s.endswith(("а", "и")) else s

def load_app_config(path):
    cfg = {}
    # Проверяем переданный путь
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            pass
    # Также проверяем bot_config.json в корне
    root = os.path.dirname(os.path.abspath(__file__))
    bot_cfg_path = os.path.join(root, "bot_config.json")
    if os.path.exists(bot_cfg_path):
        try:
            with open(bot_cfg_path, encoding="utf-8") as f:
                bcfg = json.load(f)
                if not cfg.get("проценты_категорий") and bcfg.get("проценты_категорий"):
                    cfg["проценты_категорий"] = bcfg["проценты_категорий"]
                elif cfg.get("проценты_категорий") and bcfg.get("проценты_категорий"):
                    # Объединяем, давая приоритет bot_config.json
                    cfg["проценты_категорий"].update(bcfg["проценты_категорий"])
                if not cfg.get("зарплата_от_плана") and bcfg.get("зарплата_от_плана"):
                    cfg.setdefault("зарплата_от_плана", bcfg["зарплата_от_плана"])
        except (OSError, ValueError):
            pass
    return cfg

def rates_from_config(cfg):
    raw = cfg.get("проценты_категорий") or {}
    out = {}
    for k, v in raw.items():
        try:
            out[norm_cat(k)] = float(v) / 100.0
        except (TypeError, ValueError):
            continue
    
    # Жесткие значения по умолчанию согласно требованию: Вывод 5%, Матрица 4%, Новинка 3%
    defaults = {"вывод": 0.05, "матриц": 0.04, "новинк": 0.03}
    for k, v in defaults.items():
        if k not in out:
            out[k] = v
    return out

def plan_for(cfg, year, month):
    plans = cfg.get("план_на_месяц") or {}
    try:
        return float(plans.get("{:04d}-{:02d}".format(int(year), int(month))) or 0)
    except (TypeError, ValueError):
        return 0.0

def plan_percent(cfg, met):
    p = cfg.get("зарплата_от_плана") or {}
    key = "план_выполнен" if met else "план_не_выполнен"
    try:
        return float(p.get(key) or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0

def plan_file(root, year, month):
    return os.path.join(root, str(year), "nom", str(month), "план.json")

def load_plan(root, year, month):
    p = plan_file(root, year, month)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return float(json.load(f).get("план") or 0)
    except (OSError, ValueError, TypeError):
        return None

def save_plan(root, year, month, value):
    p = plan_file(root, year, month)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"план": float(value)}, f, ensure_ascii=False, indent=2)
    return p

def save_app_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    root = os.path.dirname(os.path.abspath(__file__))
    bot_cfg_path = os.path.join(root, "bot_config.json")
    if os.path.exists(bot_cfg_path):
        try:
            with open(bot_cfg_path, "r", encoding="utf-8") as f:
                bcfg = json.load(f)
            if "проценты_категорий" in cfg:
                bcfg["проценты_категорий"] = cfg["проценты_категорий"]
            with open(bot_cfg_path, "w", encoding="utf-8") as f:
                json.dump(bcfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def load_rates(path):
    defaults = {"вывод": 0.05, "матриц": 0.04, "новинк": 0.03}
    if not path or not os.path.exists(path):
        return defaults
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rates = {norm_cat(k): v for k, v in data.items() if not k.startswith("_")}
        for k, v in defaults.items():
            if k not in rates:
                rates[k] = v
        return rates
    except Exception:
        return defaults

def month_of(date):
    try:
        m = str(date).split(".")[1]
        if len(m) == 2 and m.isdigit():
            return m
    except (AttributeError, IndexError):
        pass
    import time
    return time.strftime("%m")

def day_dir(base, date):
    d = os.path.join(base, month_of(date), str(date))
    os.makedirs(d, exist_ok=True)
    return d

def data_path(base, date):
    return os.path.join(day_dir(base, date), "data.json")

def xlsx_path(base, date):
    iso = "-".join(reversed(date.split(".")))
    return os.path.join(day_dir(base, date), "prodazhi_{}.xlsx".format(iso))

def receipt_key(rec):
    fd, fn = str(rec.get("fd") or "").strip(), str(rec.get("fn") or "").strip()
    if fd and fn:
        return "FN{}:FD{}".format(fn, fd)
    return "FALLBACK:{}:{}:{}".format(rec.get("date"), rec.get("time"), rec.get("total"))

def load_day(base, date):
    p = data_path(base, date)
    if not os.path.exists(p):
        return {"date": date, "receipts": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def save_day(base, date, data):
    with open(data_path(base, date), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _keys(rec):
    ks = {receipt_key(rec)}
    total = float(rec.get("total") or 0)
    if total:
        ks.add("TOTAL:{}:{:.2f}".format(rec.get("date"), total))
        ks.add("FALLBACK:{}:{}:{:.2f}".format(rec.get("date"), rec.get("time"), total))
    return ks

def add_receipt(base, date, rec):
    rec = dict(rec)
    rec["date"] = date
    new_keys = _keys(rec)
    data = load_day(base, date)
    for old in data["receipts"]:
        if new_keys & _keys(old):
            return False, "чек уже учтён ({} {}), данные не задвоены".format(
                old.get("time", ""), old.get("total", ""))
    data["receipts"].append(rec)
    data["receipts"].sort(key=lambda r: str(r.get("time", "")))
    save_day(base, date, data)
    return True, "чек добавлен"

def set_override(base, date, rec_key, idx, cat, note=None):
    data = load_day(base, date)
    for rec in data["receipts"]:
        if receipt_key(rec) != rec_key:
            continue
        items = rec.get("items", [])
        if 0 <= idx < len(items):
            items[idx]["cat_override"] = cat
            if note is not None:
                items[idx]["note"] = note
            save_day(base, date, data)
            return True
    return False

def set_item_name(base, date, rec_key, idx, name):
    """Запоминает исправленное название, НЕ трогая строку чека (name)."""
    data = load_day(base, date)
    for rec in data["receipts"]:
        if receipt_key(rec) != rec_key:
            continue
        items = rec.get("items", [])
        if 0 <= idx < len(items):
            items[idx]["name_override"] = str(name)
            save_day(base, date, data)
            return True
    return False

def remove_receipt(base, date, rec_key):
    data = load_day(base, date)
    before = len(data["receipts"])
    data["receipts"] = [r for r in data["receipts"] if receipt_key(r) != rec_key]
    if len(data["receipts"]) == before:
        return False
    save_day(base, date, data)
    return True

def set_payment(base, date, rec_key, payment):
    data = load_day(base, date)
    for rec in data["receipts"]:
        if receipt_key(rec) == rec_key:
            rec["payment"] = payment
            rec["payment_confirmed"] = True
            save_day(base, date, data)
            return True
    return False

def unconfirmed_payments(base, date):
    out = []
    for rec in load_day(base, date)["receipts"]:
        if not rec.get("payment_confirmed"):
            out.append(rec)
    return out

def sc_match(items, name, price):
    import sales_core
    return sales_core.match_item(items, name, price)

def _rows(data, items, rates, fresh=False):
    """Строки отчёта.

    fresh=True — пересчёт по текущей номенклатуре: категория берётся из свежего
    сопоставления, старые ручные категории не «замораживают» результат.
    Ручная категория остаётся только для позиций, которых в номенклатуре нет
    (например, перемещение с другой точки).
    """
    out = []
    pre = is_pre_split(data.get("date"))
    for rec in data["receipts"]:
        cash = rec.get("payment") == "наличные"
        for idx, it in enumerate(rec.get("items", [])):
            orig = it.get("name", "")
            raw = it.get("name_override") or orig
            match, how = sc_match(items, raw, it.get("price"))
            if fresh:
                cat = (match["cat"] if match else "") or it.get("cat_override") or "—"
            else:
                cat = it.get("cat_override") or (match["cat"] if match else "—")
            name = it.get("name_override") or (match["name"] if match else orig)
            total = float(it.get("total") or 0)

            note = it.get("note", "")
            if pre:
                cat = it.get("cat_override") or "Матрица"
                note = ""
            elif not it.get("cat_override"):
                if match is None and PACK_RE.search(raw):
                    cat = "Матрица"
                    note = ""
                elif match is None:
                    note = "Нет в остатках: «{}»".format(raw)

            # Получаем ставку с надежным преобразованием ключа категории
            norm_c = norm_cat(cat)
            rate = rates.get(norm_c)
            if rate is None:
                # Если вдруг ключ не нашелся, подстрахуемся по прямому совпадению текста
                low_cat = cat.strip().lower()
                if "вывод" in low_cat:
                    rate = 0.05
                elif "новинк" in low_cat:
                    rate = 0.03
                else:
                    rate = 0.04 # матрица по умолчанию

            rate2 = rate + 0.01
            salary = round(total * float(rate), 2)
            salary2 = round(total * float(rate2), 2)

            out.append({
                "date": data.get("date"),
                "name": name, "price": it.get("price"), "discount": it.get("discount") or 0,
                "rate": rate, "salary": salary, "rate2": rate2, "salary2": salary2,
                "qty": it.get("qty") or 1, "total": total, "cat": cat,
                "nal": "нал" if cash else "", "note": note,
                "rec_key": receipt_key(rec), "idx": idx, "raw_name": raw,
                "exact": bool(match) and how == "точное совпадение названия",
            })
    return out

def build_xlsx(base, date, items, rates):
    data = load_day(base, date)
    rows = _rows(data, items, rates)

    wb = Workbook()
    ws = wb.active
    ws.title = "Продажи"

    for c, h in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, size=11)
        cell.fill = HEAD_FILL
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for i, row in enumerate(rows, start=2):
        values = [i - 1, row["date"], row["name"], row["price"], row["discount"], row["qty"], row["total"],
                  row["cat"], row["nal"], row["rate"], row["salary"], row["rate2"], row["salary2"], row["note"]]
        for c, v in enumerate(values, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.border = BORDER
            cell.font = Font(size=10)
            if c in (3, 14):
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            elif c in (4, 7, 11, 13):
                cell.alignment = Alignment(horizontal="right", vertical="top")
                cell.number_format = "#,##0.00"
            elif c in (5, 10, 12):
                cell.alignment = Alignment(horizontal="center", vertical="top")
                cell.number_format = "0%"
            elif c == 9:
                cell.alignment = Alignment(horizontal="center", vertical="top")
                if row["nal"]:
                    cell.fill = CASH_FILL
                    cell.font = Font(size=10, bold=True)
            else:
                cell.alignment = Alignment(horizontal="center", vertical="top")

    total_row = len(rows) + 2
    cash_total = round(sum(r["total"] for r in rows if r["nal"]), 2)
    card_total = round(sum(r["total"] for r in rows if not r["nal"]), 2)
    ws.cell(row=total_row, column=1, value="ИТОГО")
    ws.cell(row=total_row, column=6, value=sum(r["qty"] for r in rows))
    ws.cell(row=total_row, column=7, value=round(sum(r["total"] for r in rows), 2))
    ws.cell(row=total_row, column=11, value=round(sum(r["salary"] for r in rows), 2))
    ws.cell(row=total_row, column=13, value=round(sum(r["salary2"] for r in rows), 2))
    for c in range(1, 15):
        cell = ws.cell(row=total_row, column=c)
        cell.font = Font(bold=True, size=11)
        cell.fill = TOTAL_FILL
        cell.border = Border(left=THIN, right=THIN, top=Side(style="double", color="808080"), bottom=THIN)
    ws.cell(row=total_row, column=7).number_format = "#,##0.00"
    ws.cell(row=total_row, column=11).number_format = "#,##0.00"
    ws.cell(row=total_row, column=13).number_format = "#,##0.00"

    ws.cell(row=total_row + 2, column=1, 
            value="Наличными: {:,.2f} ₽   |   Безналично: {:.2f} ₽".format(cash_total, card_total).replace(",", " ")).font =         Font(bold=True, size=10, color="333333")

    ws.cell(row=total_row + 3, column=1,
            value="Зарплата = Итоговая цена × Ставка (или Ставка2 при выполнении плана)").font =         Font(italic=True, size=9, color="666666")

    for col, w in {"A": 5, "B": 12, "C": 40, "D": 11, "E": 12, "F": 11, "G": 13, "H": 11,
                   "I": 7, "J": 12, "K": 12, "L": 12, "M": 13, "N": 52}.items():
        ws.column_dimensions[col].width = w
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:N{}".format(total_row - 1)

    s = wb.create_sheet("Сверка")
    s["A1"] = "Сверка с чеками за " + date
    s["A1"].font = Font(bold=True, size=13)
    for c, h in enumerate(["Чек (время)", "Оплата", "Позиций", "Итого по чеку"], 1):
        cell = s.cell(row=3, column=c, value=h)
        cell.font = Font(bold=True, size=10)
        cell.fill = HEAD_FILL
        cell.border = BORDER
    r_i = 4
    for rec in data["receipts"]:
        vals = [rec.get("time", ""), rec.get("payment") or "—",
                len(rec.get("items", [])), rec.get("total", 0)]
        for c, v in enumerate(vals, 1):
            cell = s.cell(row=r_i, column=c, value=v)
            cell.border = BORDER
            cell.font = Font(size=10)
            if c == 4:
                cell.number_format = "#,##0.00"
        r_i += 1
    for col, w in {"A": 16, "B": 14, "C": 12, "D": 16}.items():
        s.column_dimensions[col].width = w

    out = xlsx_path(base, date)
    wb.save(out)
    return out, rows
