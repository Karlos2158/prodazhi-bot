# -*- coding: utf-8 -*-
"""Распознавание чека: подготовка фото, локальная модель (Ollama) и Gemini."""
import base64
import difflib
import io
import json
import re
import urllib.error
import urllib.request

from PIL import Image

PROMPT = '''Распознай кассовый чек на фото. Верни только JSON:
{"date":"ДД.ММ.ГГГГ","time":"ЧЧ:ММ","fd":"номер из строки ФД","fn":"номер из строки ФН","total":0,
 "payment":"карта" или "наличные",
 "items":[{"name":"название строки","price":0,"qty":1,"line_total":0,"discount_amount":0}]}

Правила:
- payment: если на фото виден банковский слип терминала (слова ПАО СБЕРБАНК, КАРТА, RRN, ПИН-код) —
  пиши "карта"; если на фото только кассовый чек — пиши "наличные";
- price — число, напечатанное перед знаком *;
- line_total — число после знака =;
- discount_amount — число в строке СКИДКА (если строки нет, 0);
- числа бери ровно как напечатано, ничего не пересчитывай и не вычитай;
- в начале названия обычно стоит короткий префикс бренда (VV, БМ, Ю, А, К, EB, МБ) —
  обязательно сохраняй его, не отбрасывай;
- название переписывай целиком, не обрезай длинные слова;
- пакеты и конверты пропусти;
- строку «Склад ...» (заголовок склада) в items не включай — это не товар;
- каждая позиция отдельной записью, не склеивай её с соседними строками.

ВАЖНО. В одном чеке часто стоят ПОХОЖИЕ строки с разными префиксами бренда
(VV, БМ, Ю, А, К, EB, МБ) и разным написанием. Например «VV Серьги 800 Золотой 000»
и «К Серьги 800 золото» — это ДВА разных товара.
- Перед каждой строкой смотри на её собственный префикс и переписывай именно его.
- Никогда не копируй название предыдущей строки в следующую.
- Сколько строк с товаром на чеке — столько записей в items; количество записей менять нельзя.
- После каждой записи мысленно сверь: префикс, слово после него и цену.

Пример строки чека:
"БМ Кольцо 1300 Золотой 016
              1209.00 *1 =1209.00
СКИДКА                    -91.00"
даёт: {"name":"БМ Кольцо 1300 Золотой 016","price":1209.00,"qty":1,"line_total":1209.00,"discount_amount":91.00}
'''

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

SKIP_WORDS = ("пакет", "конверт")
HEADER_WORDS = ("склад", "лесная поляна")


def _sim(a, b):
    a = str(a).lower().replace("ё", "е")
    b = str(b).lower().replace("ё", "е")
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------- подготовка изображения ----------

def prepare(image_bytes, top=0.05, bottom=0.98, scale=1.4, max_width=1600):
    """Обрезает низ кадра, увеличивает, повышает контраст и резкость."""
    from PIL import ImageEnhance, ImageFilter
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = im.size
    im = im.crop((0, int(top * h), w, int(bottom * h)))
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)), Image.LANCZOS)
    im = ImageEnhance.Contrast(im).enhance(1.5)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------- разбор и проверка ----------

def clean_json(text):
    text = str(text).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        text = text[i:j + 1]
    text = re.sub(r"[\x00-\x1f]", " ", text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def _digits(v):
    return re.sub(r"\D", "", str(v or ""))


def _num(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


def _fix_year(date_str, now_year):
    """Принимает 02.11.26, 2.11.2026, 02/11/2026 и приводит к ДД.ММ.ГГГГ."""
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", str(date_str or "").strip())
    if not m:
        return ""
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    if not (1 <= d <= 31 and 1 <= mo <= 12 and 2000 <= y <= 2100):
        return ""
    return "{:02d}.{:02d}.{}".format(d, mo, y)


def normalize(rec, now_year=None):
    import time as _t
    now_year = now_year or int(_t.strftime("%Y"))
    rec = dict(rec)

    fd, fn = _digits(rec.get("fd")), _digits(rec.get("fn"))
    if len(fn) == 16:
        rec["fn"] = fn
        rec["fd"] = fd if 1 <= len(fd) <= 6 and fd.strip("0") else ""
    else:
        rec["fn"] = ""
        rec["fd"] = ""

    rec["date"] = _fix_year(str(rec.get("date") or "").strip().replace("/", "."), now_year)
    if rec["date"]:
        try:
            from datetime import datetime
            d = datetime.strptime(rec["date"], "%d.%m.%Y")
            if abs((d - datetime.now()).days) > 90:
                rec["date"] = ""
        except ValueError:
            rec["date"] = ""
    tm = str(rec.get("time") or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", tm)
    rec["time"] = "{:02d}:{}".format(int(m.group(1)), m.group(2)) if m else ""

    items = []
    for it in rec.get("items") or []:
        name = str(it.get("name") or "").strip()
        low = name.lower()
        if not name or any(w in low for w in SKIP_WORDS):
            continue
        # Бренд-префикс (VV, БМ, К, Ю, EB...) НЕ отбрасываем: от него зависит
        # и точность сопоставления с номенклатурой, и категория товара.
        price = _num(it.get("price"))
        qty = _num(it.get("qty")) or 1
        line_total = _num(it.get("line_total")) or round(price * qty, 2)
        if price <= 0 and line_total <= 0:
            continue
        disc_amt = abs(_num(it.get("discount_amount")))
        base_unit = price + (disc_amt / qty if qty else 0)
        frac = round(disc_amt / (base_unit * qty), 4) if base_unit and qty else 0.0
        items.append({
            "name": re.sub(r"\s+", " ", name).strip(" ,-"),
            "price": round(base_unit, 2),
            "discount": frac,
            "qty": int(qty) if qty == int(qty) else qty,
            "total": round(line_total, 2),
        })
    rec["items"] = items
    pay = str(rec.get("payment") or "").strip().lower()
    rec["payment"] = "наличные" if pay.startswith("нал") else ("карта" if pay else "")
    rec["total"] = _num(rec.get("total"))
    if not rec["total"] and items:
        rec["total"] = round(sum(i["total"] for i in items), 2)
    return rec


# ---------- движки ----------

PAY_PROMPT = '''Найди на чеке строку об оплате — она внизу, рядом с ИТОГ.
Верни только JSON: {"text":"строка об оплате ровно как напечатана"}
Если строки не видно, верни пустую строку. Не додумывай.
'''


def prepare_payment(image_bytes, top=0.70, bottom=0.98, scale=1.8, max_width=1600):
    from PIL import ImageEnhance, ImageFilter
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = im.size
    im = im.crop((0, int(top * h), w, int(bottom * h)))
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)), Image.LANCZOS)
    im = ImageEnhance.Contrast(im).enhance(1.5)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


SLIP_PROMPT = '''Посмотри на фото. Рядом с кассовым чеком может лежать узкая бумажная лента —
слип банковского терминала (на нём бывают слова ПАО СБЕРБАНК, КАРТА, RRN, ПИН-код, MIR).
Ответь только JSON: {"terminal_slip": true или false}'''


def detect_slip(image_bytes, model="qwen2.5vl:7b", url=OLLAMA_URL, timeout=300):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > 1280:
        k = 1280 / max(w, h)
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    payload = {"model": model,
               "messages": [{"role": "user", "content": SLIP_PROMPT,
                             "images": [base64.b64encode(buf.getvalue()).decode()]}],
               "stream": False, "format": "json", "options": {"temperature": 0}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        raw = json.loads(clean_json(body["message"]["content"]))
    except Exception:
        return None
    v = raw.get("terminal_slip")
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "да", "yes"):
        return True
    if s in ("false", "нет", "no"):
        return False
    return None


def detect_payment(image_bytes, model="qwen2.5vl:7b", url=OLLAMA_URL, timeout=300):
    slip = detect_slip(image_bytes, model, url)
    if slip is True:
        return "карта", "есть слип терминала"
    if slip is False:
        return "наличные", "слипа терминала нет"
    img = prepare_payment(image_bytes)
    payload = {"model": model,
               "messages": [{"role": "user", "content": PAY_PROMPT,
                             "images": [base64.b64encode(img).decode()]}],
               "stream": False, "format": "json", "options": {"temperature": 0}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        raw = json.loads(clean_json(body["message"]["content"]))
    except Exception:
        return "", ""
    text = str(raw.get("text") or "").upper().replace("Ё", "Е")
    if "БЕЗНАЛИЧН" in text or "КАРТ" in text or "ЭЛЕКТРОНН" in text:
        return "карта", text
    if "НАЛИЧН" in text:
        return "наличные", text
    return "", text


DATE_PROMPT = '''Перепиши строки из нижней части кассового чека, где стоят дата и время.
Формат строки обычно такой: "02.11.26 13:39".
Верни только JSON: {"lines":["строка 1","строка 2", ...]}
Перепиши все строки, которые видишь, даже если не уверен.
'''


def prepare_tail(image_bytes, top=0.60, bottom=0.99, scale=1.8, max_width=1600):
    from PIL import ImageEnhance, ImageFilter
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = im.size
    im = im.crop((0, int(top * h), w, int(bottom * h)))
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)), Image.LANCZOS)
    im = ImageEnhance.Contrast(im).enhance(1.6)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=3))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def detect_datetime(image_bytes, model="qwen2.5vl:7b", url=OLLAMA_URL, timeout=300):
    img = prepare_tail(image_bytes)
    payload = {"model": model,
               "messages": [{"role": "user", "content": DATE_PROMPT,
                             "images": [base64.b64encode(img).decode()]}],
               "stream": False, "format": "json", "options": {"temperature": 0}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        lines = json.loads(clean_json(body["message"]["content"])).get("lines") or []
    except Exception:
        return "", ""
    joined = " ".join(str(x) for x in lines)
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})(?:\s+(\d{1,2}:\d{2}))?", joined)
    if not m:
        return "", ""
    date = _fix_year("{}.{}.{}".format(m.group(1), m.group(2), m.group(3)), 0)
    if not date:
        return "", ""
    try:
        from datetime import datetime
        if abs((datetime.strptime(date, "%d.%m.%Y") - datetime.now()).days) > 90:
            return "", ""
    except ValueError:
        return "", ""
    return date, (m.group(4) or "")


def _dup_count(rec):
    """Сколько названий повторилось (одинаковые позиции в одном чеке — почти всегда ошибка OCR)."""
    seen, dups = set(), 0
    for it in rec.get("items") or []:
        key = re.sub(r"\s+", " ", str(it.get("name") or "").lower()).strip()
        if key in seen:
            dups += 1
        seen.add(key)
    return dups


DUP_NOTE = '''

ВНИМАНИЕ: в этом чеке несколько строк с похожими названиями. Перечитай каждую строку
с товаром по отдельности и выпиши её собственный префикс бренда (VV, БМ, К, Ю, EB...).
Названия записей в items не должны повторяться — если они совпали, значит строка прочитана неверно.
'''


def _call_ollama(img, model, url, timeout, temperature, prompt=PROMPT):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(img).decode()]}],
        "stream": False, "format": "json",
        "options": {"temperature": temperature},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("Ollama: ошибка {}: {}".format(
            e.code, e.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as e:
        raise RuntimeError("Ollama недоступен: {}".format(e.reason))
    return json.loads(clean_json(body["message"]["content"]))


def extract_local(image_bytes, model="qwen2.5vl:7b", url=OLLAMA_URL, timeout=600):
    img = prepare(image_bytes)
    best, best_score, last = None, None, None
    for temp in (0, 0.3, 0.6):
        # если попытка не первая и в ней были повторы названий — просим перечитать префиксы
        prompt = PROMPT if best is None else PROMPT + DUP_NOTE
        try:
            rec = normalize(_call_ollama(img, model, url, timeout, temp, prompt))
        except Exception as e:
            last = e
            continue
        err = abs(round(sum(i["total"] for i in rec["items"]), 2) - rec["total"])
        dups = _dup_count(rec)
        # важнее сходимость по сумме, но при равной сходимости выбираем вариант без повторов
        score = (err > 1.0, dups, round(err, 2))
        if best is None or score < best_score:
            best, best_score = rec, score
        if err <= 1.0 and dups == 0:
            break
    if best is None:
        raise RuntimeError("Не удалось распознать: {}".format(last))
    best_err = best_score[2]
    best["reconciled"] = best_err <= 1.0
    best["reconciled_diff"] = round(best_err, 2)
    if not best.get("payment"):
        pay, pay_text = detect_payment(image_bytes, model, url)
        best["payment"] = pay
        best["payment_text"] = pay_text
    if not best.get("date"):
        d, t = detect_datetime(image_bytes, model, url)
        if d:
            best["date"] = d
            best["date_from_tail"] = True
            if t and not best.get("time"):
                best["time"] = t
    return best


def extract_gemini(image_bytes, api_key, model="gemini-2.5-flash", mime="image/jpeg",
                   relay=None, relay_token=None):
    """Распознавание через Gemini.

    relay — адрес релеевого сервера на GHO (вида
    "http://192.168.1.194:8714/v1beta/models/{model}:generateContent"). Если он
    задан, запрос идёт туда, а ключ Gemini подставляет сам релей на GHO.
    Без relay ходим в Google напрямую (с SPQR Google отказывает по географии).
    """
    img = prepare(image_bytes)
    payload = {
        "contents": [{"parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(img).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    if relay:
        url = relay.format(model=model)
        headers = {"Content-Type": "application/json"}
        if relay_token:
            headers["X-Relay-Token"] = relay_token
    else:
        url = GEMINI_URL.format(model=model, key=api_key)
        headers = {"Content-Type": "application/json"}
    last_err = None
    for attempt in range(5):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = json.loads(r.read().decode("utf-8"))
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            rec = normalize(json.loads(clean_json(text)))
            err = abs(round(sum(i["total"] for i in rec["items"]), 2) - rec["total"])
            rec["reconciled"] = err <= 1.0
            rec["reconciled_diff"] = round(err, 2)
            return rec
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = "HTTP {}: {}".format(e.code, detail)
            if e.code in (429, 500, 502, 503) and attempt < 4:
                import time
                time.sleep(3 + attempt * 4)
                continue
            raise RuntimeError("Gemini: {}".format(last_err))
        except Exception as e:
            last_err = repr(e)
            if attempt < 4:
                import time
                time.sleep(3)
                continue
            raise
    raise RuntimeError("Gemini: {}".format(last_err))