# -*- coding: utf-8 -*-
"""Ядро учёта продаж: справочник (только чтение), сверка, реестр чеков, дневной отчёт."""
import csv
import difflib
import os
import re

NOM_PATH = r"G:\prodazhi-bot\2026\nom\11\Лесная поляна ЖЦП(2).xlsx"
BASE_DIR = r"G:\prodazhi-bot\2026\foto"
REG_PATH = r"G:\prodazhi-bot\2026\reestr_chekov.csv"

CATS = ("Вывод", "Матрица", "Новинка", "Новинки")
TYPES = ("Браслет", "Кольцо", "Серьги", "Колье", "Подвеска", "Пусеты", "Брошь", "Цепь", "Текстиль")
COLORS = {"золотой", "золотая", "серебрянный", "серебряный", "белый", "белая", "черный",
          "черная", "голубой", "розовый", "серый", "серая", "коричневый", "красный",
          "синий", "фиолетовый", "бежевый", "мультиколор", "зеленый", "желтый",
          "прозрачный", "дикроико", "черный"}

_WS = re.compile(r"\s+")


def _norm(s):
    s = str(s).lower().replace("ё", "е").replace(",", " ")
    s = re.sub(r"[^0-9a-zа-я\s\-/.]", " ", s)
    return _WS.sub(" ", s).strip()


def _strip_brand(s):
    """Убирает ведущие мусорные токены: «БМ», «VV», «Ю», а также цифры вроде «10»."""
    t = _norm(s).split()
    while t and (t[0].isdigit() or (len(t[0]) <= 3 and t[0][:1].isalpha())):
        t = t[1:]
    return " ".join(t)


def _brand(s):
    """Бренд-токен (VV, БМ, К, Ю, EB...), если он есть; иначе None.

    Бренд значим для категории: «VV Серьги 800 Серебряный» — Новинка,
    «БМ Серьги 800 Серебряный» — Вывод. Поэтому при сопоставлении
    бренд с чека обязан совпадать с брендом в номенклатуре.
    """
    t = _norm(s).split()
    if not t:
        return None
    first = t[0]
    if first.isdigit() or len(first) > 3:
        return None
    if first in {x.lower() for x in TYPES}:
        return None
    return first


def _brand_of(s):
    return _brand(s)


def _signature(s):
    """(бренд, название без бренда и пробелов) — для строгого сравнения."""
    toks = _norm(s).split()
    b = _brand(s)
    if b:
        toks = toks[1:]
    return b, "".join(toks)


def _tokens(s):
    return _norm(s).split()


def _type_of(s):
    toks = _tokens(s)
    for t in toks:
        for typ in TYPES:
            if typ.lower() == t:
                return typ
    # опечатки распознавания: «Серги» вместо «Серьги»
    for t in toks:
        for typ in TYPES:
            if difflib.SequenceMatcher(None, t, typ.lower()).ratio() >= 0.8:
                return typ
    return None


def _pricecode(s):
    for t in _tokens(s):
        if re.fullmatch(r"\d{4}", t) and t[0] != "0":
            return int(t)
    return None


def _colors(s):
    return {t for t in _tokens(s) if t in COLORS}


def load_catalog(path=None):
    """Читает номенклатуру. Только чтение, файл никогда не сохраняется."""
    from openpyxl import load_workbook
    wb = load_workbook(path or NOM_PATH, data_only=True)
    ws = wb.worksheets[0]
    cur, items = None, []
    for r in range(1, ws.max_row + 1):
        a = ws.cell(r, 1).value
        e = ws.cell(r, 5).value
        a_s = str(a).strip() if a is not None else ""
        if a_s in CATS and e in (None, ""):
            cur = a_s
            continue
        if e in (None, "") or cur is None:
            continue
        qty, total = ws.cell(r, 10).value, ws.cell(r, 11).value
        unit = None
        try:
            if qty and total:
                unit = round(float(total) / float(qty), 2)
        except Exception:
            pass
        items.append({"row": r, "name": str(e).rstrip(",").strip(),
                      "cat": cur, "unit": unit, "raw": e})
    return items


def match_item(items, receipt_name, price=None):
    """Возвращает (item, способ) или (None, причина).

    Бренд обязателен: «VV Серьги 800» и «БМ Серьги 800» — разные категории,
    поэтому без совпадения бренда позиция считается ненайденной и уходит
    на ручной выбор продавцу.
    """
    raw = str(receipt_name or "")
    brand, rest = _signature(raw)
    target_s = rest

    # 1. точное совпадение — совпадают и бренд, и название (без пробелов и знаков)
    exact = [it for it in items if _signature(it["name"]) == (brand, rest)]
    if len(exact) == 1:
        return exact[0], "точное совпадение названия"

    typ = _type_of(raw)
    code = _pricecode(_strip_brand(raw))

    # 2. похожее название (опечатки, лишний пробел) среди того же бренда и типа
    cand = []
    for it in items:
        if _brand(it["name"]) != brand:
            continue
        if _type_of(it["name"]) != typ:
            continue
        if code is not None and _pricecode(it["name"]) != code:
            continue
        if code is None and price is not None:
            if it["unit"] is None or abs(it["unit"] - price) > 0.01:
                continue
        ratio = difflib.SequenceMatcher(
            None, target_s, _signature(it["name"])[1]).ratio()
        cand.append((ratio, it))

    if cand:
        cand.sort(key=lambda x: x[0], reverse=True)
        best_ratio, best = cand[0]
        rivals = [c for c in cand if c[0] >= best_ratio - 0.04 and c[1]["row"] != best["row"]]
        if best_ratio >= 0.85 and not rivals:
            return best, "похожее название"

    # 3. по бренду, цене и цвету — тоже только внутри своего бренда
    cols = _colors(raw)
    cand2 = []
    for it in items:
        if _brand(it["name"]) != brand:
            continue
        if _type_of(it["name"]) != typ:
            continue
        ok_price = (code is not None and _pricecode(it["name"]) == code) or \
                   (code is None and price is not None and it["unit"] is not None
                    and abs(it["unit"] - price) < 0.01)
        if not ok_price:
            continue
        ic = _colors(it["name"])
        if not cols or not ic:
            sim = 0.5
        else:
            fwd = sum(max((difflib.SequenceMatcher(None, a, b2).ratio() for b2 in ic), default=0.0)
                      for a in cols) / len(cols)
            bwd = sum(max((difflib.SequenceMatcher(None, b2, a).ratio() for a in cols), default=0.0)
                      for b2 in ic) / len(ic)
            sim = min(fwd, bwd)
        cand2.append((sim, it))

    if not cand2:
        return None, "нет кандидатов по бренду, типу и цене"
    cand2.sort(key=lambda x: x[0], reverse=True)
    best_sim, best2 = cand2[0]
    if best_sim < 0.75:
        return None, "цвет не совпал (лучшее сходство {:.2f})".format(best_sim)
    rivals = [c for c in cand2 if c[0] >= best_sim - 0.05 and c[1]["row"] != best2["row"]]
    if rivals:
        return None, "неоднозначно: {} кандидатов".format(len(rivals) + 1)
    return best2, "по бренду, типу, цене и цвету"


def similarity(receipt_name, catalog_name):
    """0..1 — насколько название из номенклатуры похоже на строку чека.

    Используется для подсказок продавцу. Совпадение бренда добавляет вес,
    но не скрывает варианты с другим брендом.
    """
    b1, r1 = _signature(receipt_name)
    b2, r2 = _signature(catalog_name)
    base = difflib.SequenceMatcher(None, r1, r2).ratio()
    if b1 and b2:
        base += 0.15 if b1 == b2 else -0.10
    elif b1 or b2:
        base -= 0.05
    if _type_of(receipt_name) and _type_of(receipt_name) == _type_of(catalog_name):
        base += 0.05
    return max(0.0, min(1.0, base))



# ---------- реестр чеков (защита от дублей) ----------

REG_FIELDS = ["key", "date", "time", "fd", "fn", "total", "items", "photo", "added_at"]


def make_key(fd, fn, date, time, total):
    if fd and str(fd).strip() and fn and str(fn).strip():
        return "FN{}:FD{}".format(str(fn).strip(), str(fd).strip())
    return "FALLBACK:{}:{}:{}".format(date, time, total)


def load_registry():
    reg = {}
    if not os.path.exists(REG_PATH):
        return reg
    with open(REG_PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row.get("key"):
                reg[row["key"]] = row
    return reg


def register_receipt(key, date, time, fd, fn, total, items_count, photo):
    """True — чек новый и записан. False — уже был (дубль)."""
    reg = load_registry()
    if key in reg:
        return False, reg[key]
    new = not os.path.exists(REG_PATH)
    with open(REG_PATH, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REG_FIELDS, delimiter=";")
        if new:
            w.writeheader()
        w.writerow({"key": key, "date": date, "time": time, "fd": fd or "", "fn": fn or "",
                    "total": total, "items": items_count, "photo": photo,
                    "added_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return True, None


def day_dir(date_str):
    d = os.path.join(BASE_DIR, date_str)
    os.makedirs(d, exist_ok=True)
    return d


def day_report_path(date_str):
    iso = "-".join(reversed(date_str.split(".")))
    return os.path.join(day_dir(date_str), "prodazhi_{}.xlsx".format(iso))
