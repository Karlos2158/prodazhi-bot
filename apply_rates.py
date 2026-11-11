# -*- coding: utf-8 -*-
"""Ставки зарплаты по категориям: применяет их ко всем дневным отчётам.

Ставки хранятся в stavki.json (доли: 0.05 = 5%) и не меняются от дня ко дню.
Зарплата пишется в столбец J числом: Итоговая цена × ставка.

Запуск:  python apply_rates.py          — применить к файлам
         python apply_rates.py --demo   — показать на копии, не трогая файлы
"""
import glob
import json
import os
import shutil
import sys
import tempfile

from openpyxl import load_workbook

BASE = r"D:\ADeepSeek\Sanator\2026\foto"
CFG = r"D:\ADeepSeek\stavki.json"

COL_RATE, COL_TOTAL, COL_CATEGORY, COL_SALARY = 6, 8, 9, 10
NOTE = "Зарплата = Итоговая цена × Процент зарплатный (ставка категории)"


def norm_cat(s):
    """«Новинки» и «Новинка» — одна категория."""
    s = str(s).strip().lower().replace("ё", "е")
    if s.endswith(("а", "и")):
        s = s[:-1]
    return s


def load_rates():
    with open(CFG, encoding="utf-8") as f:
        data = json.load(f)
    return {norm_cat(k): v for k, v in data.items() if not k.startswith("_")}


def day_files():
    return sorted(glob.glob(os.path.join(BASE, "**", "prodazhi_*.xlsx"), recursive=True))


def data_rows(ws):
    for r in range(2, ws.max_row + 1):
        if not isinstance(ws.cell(r, 1).value, int):
            break
        yield r


def apply_to(path, rates, save=True):
    wb = load_workbook(path)
    ws = wb["Продажи"]
    filled, salary, rows = 0, 0.0, []
    for r in rows_of(ws):
        cat = ws.cell(r, COL_CATEGORY).value
        total = float(ws.cell(r, COL_TOTAL).value or 0)
        rate = rates.get(norm_cat(cat))
        fcell, jcell = ws.cell(r, COL_RATE), ws.cell(r, COL_SALARY)
        if rate is None:
            fcell.value = None
            jcell.value = 0.0
        else:
            fcell.value = float(rate)
            jcell.value = round(total * float(rate), 2)
            filled += 1
            salary += jcell.value
        fcell.number_format = "0%"
        jcell.number_format = "#,##0.00"
        rows.append((cat, total, rate, jcell.value))

    salary = round(salary, 2)
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(r, 1).value).strip() == "ИТОГО":
            ws.cell(r, COL_SALARY).value = salary
            ws.cell(r, COL_SALARY).number_format = "#,##0.00"
            break

    if save:
        wb.save(path)
    return filled, salary, rows


def rows_of(ws):
    return data_rows(ws)


def demo(files, rates):
    print("\nдемонстрация на копии со ставками Вывод 5%, Матрица 3%, Новинка 2%")
    rates = {"Вывод": 0.05, "Матрица": 0.03, "Новинка": 0.02}
    tmp = os.path.join(tempfile.gettempdir(), "demo_" + os.path.basename(files[0]))
    shutil.copy2(files[0], tmp)
    filled, salary, rows = apply_to(tmp, rates, save=True)
    for cat, total, rate, sal in rows:
        print("  {:<9} {:>9.2f} × {:>4} = {:>8.2f}".format(
            str(cat), total, "{:.0%}".format(rate or 0), sal))
    print("  строк со ставкой: {}, зарплата за день: {:.2f} ₽".format(filled, salary))
    os.remove(tmp)


def main():
    rates = load_rates()
    files = day_files()
    print("ставки из stavki.json:", rates)
    if not files:
        print("дневных файлов не найдено")
        return
    if all(v is None for v in rates.values()):
        print("ставки ещё не заданы — обнуляю зарплату в файлах")
        for path in files:
            filled, salary, _ = apply_to(path, rates, save=True)
            print("  {}: зарплата {:.2f} ₽".format(os.path.basename(path), salary))
        if "--demo" in sys.argv:
            demo(files, rates)
        return
    for path in files:
        filled, salary, _ = apply_to(path, rates, save=True)
        print("  {}: строк со ставкой {}, зарплата {:.2f} ₽".format(
            os.path.basename(path), filled, salary))


if __name__ == "__main__":
    main()
