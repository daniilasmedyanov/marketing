"""
VISUVA - Сборщик воронок продаж из Битрикс24 (коробка) -> JSON
================================================================
Запускается локально на твоём компе (IP разрешён в белом списке Битрикса).

Установка (один раз):
  pip install requests
  затем впиши URL вебхука в WEBHOOK ниже.

Использование:
  Шаг 1 (разведка): python bitrix_funnel.py stages
     -> выгрузит стадии СДЕЛОК и статусы ЛИДОВ с кодами в stages.txt
  Шаг 2: проверь коды, при необходимости поправь маппинги ниже.
  Шаг 3 (сбор): python bitrix_funnel.py
     -> посчитает обе воронки + провалы и запишет funnel.json
"""

import sys
import json
import datetime
import os
import requests

# ===== НАСТРОЙКИ =====
# WEBHOOK читается из отдельного файла webhook.txt (лежит рядом со скриптом).
# Так при обновлении скрипта ключ не теряется — он хранится отдельно.
# В webhook.txt должна быть ОДНА строка: https://твой-домен/rest/102/твой-ключ/
def load_webhook():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webhook.txt")
    if not os.path.exists(path):
        raise SystemExit(
            "Не найден файл webhook.txt рядом со скриптом.\n"
            "Создай webhook.txt и впиши в него ОДНУ строку — URL вебхука\n"
            "вида: https://твой-домен/rest/102/твой-ключ/ (со слешем на конце)."
        )
    with open(path, encoding="utf-8") as f:
        url = f.read().strip()
    if not url.startswith("http") or "ТВОЙ" in url:
        raise SystemExit("В webhook.txt не похоже на настоящий URL. Проверь содержимое файла.")
    if not url.endswith("/"):
        url += "/"
    return url

WEBHOOK = None  # заполняется при запуске из webhook.txt

OUT_FILE = "funnel.json"

# ---- ВОРОНКА СДЕЛОК (C1) ----
# код стадии Битрикса -> ступень дашборда
DEAL_MAP = {
    "C1:NEW":           "Лиды",
    "C1:PREPARATION":   "Квалифицированы",
    "C1:FINAL_INVOICE": "КП / переговоры",
    "C1:UC_L5WUDZ":     "Договор",
    "C1:WON":           "Выиграна",
}
DEAL_ORDER = ["Лиды", "Квалифицированы", "КП / переговоры", "Договор", "Выиграна"]

# Родные названия стадий в Битриксе (для тултипов на дашборде).
# Ключ — код стадии Битрикса, значение — то, что в Битриксе видит менеджер.
NATIVE_NAMES = {
    # Сделки
    "C1:NEW":           "Диагностика потребности",
    "C1:PREPARATION":   "Демо / тех. валидация",
    "C1:FINAL_INVOICE": "КП отправлено",
    "C1:UC_L5WUDZ":     "Договор отправлен",
    "C1:WON":           "Договор подписан",
    "C1:UC_8VK6P3":     "Готовы к работе",
    "C1:UC_NQZQP9":     "Отложенный спрос",
    "C1:LOSE":          "Сделка провалена",
    "C1:APOLOGY":       "Анализ причины провала",
    # Лиды
    "IN_PROCESS":       "Связь установлена",
    "NEW":              "Квалификация",
    "CONVERTED":        "Успех — Потенциальный клиент",
    "JUNK":             "Некачественный лид",
    "UC_5Q4J0A":        "Отложенные лиды",
}

# Боковые счётчики сделок: стадии вне основного потока воронки.
# Формат: "ярлык на дашборде" -> [коды стадий Битрикса]
DEAL_SIDE = {
    "Готовы к работе":  ["C1:UC_8VK6P3"],
    "Отложенный спрос": ["C1:UC_NQZQP9"],
    "Провалено":        ["C1:LOSE", "C1:APOLOGY"],
}

# ---- ВОРОНКА ЛИДОВ ----
# Заполняется ПОСЛЕ разведки: коды статусов лидов (столбец STATUS лидов).
# Типовые коды Битрикса: NEW, IN_PROCESS, PROCESSED, JUNK, CONVERTED.
# Названия у тебя: Связь установлена / Квалификация / Отложенные / Некачественный / Успех.
LEAD_MAP = {
    "IN_PROCESS":  "Связь установлена",
    "NEW":         "Квалификация",
    "CONVERTED":   "Успех (в сделку)",
}
LEAD_ORDER = ["Связь установлена", "Квалификация", "Успех (в сделку)"]
# Боковые счётчики лидов: статусы вне основного потока
LEAD_SIDE = {
    "Некачественные":  ["JUNK"],
    "Отложенные":      ["UC_5Q4J0A"],
}


# ===== Вызов метода Битрикса =====
def b24(method, params=None):
    url = WEBHOOK + method + ".json"
    r = requests.post(url, json=params or {}, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data.get('error_description', data['error'])}")
    return data


def b24_all(method, params=None):
    """Постраничная выгрузка (Битрикс отдаёт по 50).
    Большинство методов кладут данные в result (список).
    Методы истории (*history.list) кладут в result.items (словарь)."""
    out, start = [], 0
    while True:
        p = dict(params or {}, start=start)
        d = b24(method, p)
        res = d.get("result", [])
        if isinstance(res, dict):       # history-методы: {"items": [...]}
            res = res.get("items", [])
        out.extend(res)
        nxt = d.get("next")
        if not nxt:
            break
        start = nxt
    return out


# ===== РЕЖИМ 1: разведка =====
def discover_stages():
    statuses = b24_all("crm.status.list", {})
    lines = ["=" * 60, "СТАДИИ СДЕЛОК (для DEAL_MAP):", "=" * 60]
    for s in statuses:
        if str(s.get("ENTITY_ID", "")).startswith("DEAL_STAGE"):
            lines.append(f"  ENTITY_ID={s['ENTITY_ID']:<16} код={s['STATUS_ID']:<24} \"{s['NAME']}\"")

    lines += ["", "=" * 60, "СТАТУСЫ ЛИДОВ (для LEAD_MAP):", "=" * 60]
    for s in statuses:
        if s.get("ENTITY_ID") == "STATUS":
            lines.append(f"  код={s['STATUS_ID']:<24} \"{s['NAME']}\"")

    # Справочник источников (ENTITY_ID=SOURCE)
    lines += ["", "=" * 60, "ИСТОЧНИКИ (поле SOURCE_ID сделок):", "=" * 60]
    src_names = {}
    for s in statuses:
        if s.get("ENTITY_ID") == "SOURCE":
            src_names[s["STATUS_ID"]] = s["NAME"]
            lines.append(f"  код={s['STATUS_ID']:<24} \"{s['NAME']}\"")

    # Реальное распределение источников по сделкам (заполнено ли поле)
    lines += ["", "-" * 60, "Сколько СДЕЛОК по каждому источнику (факт):", "-" * 60]
    deals = b24_all("crm.deal.list", {"filter": {}, "select": ["ID", "SOURCE_ID"]})
    counts = {}
    for dl in deals:
        sid = dl.get("SOURCE_ID") or "(пусто)"
        counts[sid] = counts.get(sid, 0) + 1
    for sid, c in sorted(counts.items(), key=lambda x: -x[1]):
        name = src_names.get(sid, sid)
        lines.append(f"  {name:<30} {c}  [код: {sid}]")

    # Распределение источников по ЛИДАМ (заполнено ли поле SOURCE_ID у лидов)
    lines += ["", "-" * 60, "Сколько ЛИДОВ по каждому источнику (факт):", "-" * 60]
    leads = b24_all("crm.lead.list", {"filter": {}, "select": ["ID", "SOURCE_ID"]})
    lcounts = {}
    for ld in leads:
        sid = ld.get("SOURCE_ID") or "(пусто)"
        lcounts[sid] = lcounts.get(sid, 0) + 1
    for sid, c in sorted(lcounts.items(), key=lambda x: -x[1]):
        name = src_names.get(sid, sid)
        lines.append(f"  {name:<30} {c}  [код: {sid}]")

    text = "\n".join(lines + ["", "Скопируй нужные коды в DEAL_MAP и LEAD_MAP в скрипте."])
    print(text)
    with open("stages.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print('\n-> Сохранено в stages.txt')


# ===== ЕДИНЫЙ СЧЁТЧИК (снимок текущего состояния) =====
# Работает одинаково для сделок и лидов. Возвращает ДВЕ метрики:
#   snapshot — сколько объектов СЕЙЧАС на каждой стадии (как канбан);
#   funnel   — накопительно (ступень N = N и все последующие), всегда убывает.
# Плюс боковые счётчики (side) для стадий вне основного потока.
def snapshot_funnel(method, code_field, stage_map, order, side_map):
    items = b24_all(method, {"filter": {}, "select": ["ID", code_field]})
    codes = [it.get(code_field) for it in items]
    return funnel_from_codes(codes, stage_map, order, side_map)


# Считает воронку из готового списка кодов стадий (без обращения к API)
def funnel_from_codes(codes, stage_map, order, side_map):
    on_step = {s: 0 for s in order}
    side = {label: 0 for label in side_map}
    side_codes = {}
    for label, cs in side_map.items():
        for c in cs:
            side_codes[c] = label

    for code in codes:
        if code in stage_map:
            on_step[stage_map[code]] += 1
        elif code in side_codes:
            side[side_codes[code]] += 1

    # Обратный индекс: имя ступени -> код Битрикса -> родное название
    step_to_code = {v: k for k, v in stage_map.items()}
    def native_for(step):
        code = step_to_code.get(step)
        return NATIVE_NAMES.get(code, step) if code else step

    snapshot = [{"step": s, "count": on_step[s], "native": native_for(s)} for s in order]

    funnel, prev = [], None
    n = len(order)
    for i, s in enumerate(order):
        cum = sum(on_step[order[j]] for j in range(i, n))
        conv = None if prev in (None, 0) else round(cum / prev * 100)
        funnel.append({"step": s, "count": cum, "conv": conv, "native": native_for(s)})
        prev = cum

    side_list = []
    for label, cs in side_map.items():
        natives = [NATIVE_NAMES.get(c, c) for c in cs]
        side_list.append({"label": label, "count": side[label], "native": " / ".join(natives)})
    return snapshot, funnel, side_list


# Загружает справочник имён источников: код -> "название"
def load_source_names():
    statuses = b24_all("crm.status.list", {})
    names = {}
    for s in statuses:
        if s.get("ENTITY_ID") == "SOURCE":
            names[s["STATUS_ID"]] = s["NAME"]
    return names


# ===== РЕЖИМ 2: сбор =====
# Разбивает объекты по источникам и считает воронку для каждого
def split_by_source(objects, code_field, src_names, stage_map, order, side_map):
    by_source = {}
    for o in objects:
        sid = o.get("SOURCE_ID")
        name = src_names.get(sid) if sid else None
        if not name:
            name = "Не указан"
        by_source.setdefault(name, []).append(o.get(code_field))

    out = []
    for name, codes in by_source.items():
        s_snap, s_funnel, s_side = funnel_from_codes(codes, stage_map, order, side_map)
        # для лидов: считаем "ушло в сделку" = количество с кодом CONVERTED
        converted = sum(1 for c in codes if c == "CONVERTED")
        total = len(codes)
        conv_pct = round(converted / total * 100) if total else 0
        out.append({"name": name, "total": total,
                    "converted": converted, "conv_pct": conv_pct,
                    "snapshot": s_snap, "funnel": s_funnel, "side": s_side})
    out.sort(key=lambda x: -x["total"])
    return out


# Периоды для фильтра по дате создания (дней назад; None = всё время)
PERIODS = {
    "week": 7,
    "month": 30,
    "quarter": 90,
    "year": 365,
    "all": None,
}


def filter_by_period(objects, days):
    """Оставляет объекты, созданные за последние `days` дней. days=None -> все."""
    if days is None:
        return objects
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    out = []
    for o in objects:
        dc = o.get("DATE_CREATE")  # формат ISO от Битрикса
        if not dc:
            continue
        try:
            # Битрикс отдаёт вида 2026-05-28T14:00:00+03:00
            dt = datetime.datetime.fromisoformat(dc)
            # приводим к naive для сравнения
            dt = dt.replace(tzinfo=None)
        except Exception:
            continue
        if dt >= cutoff:
            out.append(o)
    return out


def build_block(objects, code_field, src_names, stage_map, order, side_map):
    """Считает общую воронку + по источникам для заданного набора объектов."""
    codes = [o.get(code_field) for o in objects]
    snap, funnel, side = funnel_from_codes(codes, stage_map, order, side_map)
    by_src = split_by_source(objects, code_field, src_names, stage_map, order, side_map)
    return {"snapshot": snap, "funnel": funnel, "side": side, "by_source": by_src}


def timeseries_weekly(objects):
    """Группирует объекты по неделям (понедельник). Возвращает [{week:'YYYY-MM-DD', count:N}, ...]."""
    import datetime
    buckets = {}
    for o in objects:
        dc = o.get("DATE_CREATE")
        if not dc:
            continue
        try:
            dt = datetime.datetime.fromisoformat(dc).replace(tzinfo=None)
        except Exception:
            continue
        # понедельник этой недели
        monday = (dt - datetime.timedelta(days=dt.weekday())).date()
        buckets[monday] = buckets.get(monday, 0) + 1
    return [{"week": k.isoformat(), "count": v} for k, v in sorted(buckets.items())]


def timeseries_monthly_by_source(objects, src_names):
    """Помесячный временной ряд по каждому источнику.
    Возвращает {имя_источника: [{month:'YYYY-MM', count:N}, ...]}.
    """
    import datetime
    by_src = {}  # name -> {month: count}
    for o in objects:
        dc = o.get("DATE_CREATE")
        if not dc:
            continue
        try:
            dt = datetime.datetime.fromisoformat(dc).replace(tzinfo=None)
        except Exception:
            continue
        sid = o.get("SOURCE_ID")
        name = src_names.get(sid) if sid else None
        if not name:
            name = "Не указан"
        month = f"{dt.year:04d}-{dt.month:02d}"
        by_src.setdefault(name, {}).setdefault(month, 0)
        by_src[name][month] += 1
    return {name: [{"month": m, "count": c} for m, c in sorted(months.items())]
            for name, months in by_src.items()}


def collect_funnel():
    if not DEAL_MAP:
        raise SystemExit("DEAL_MAP пуст. Запусти разведку и заполни маппинг.")

    src_names = load_source_names()

    # Тянем сделки и лиды один раз, с датой создания
    deals = b24_all("crm.deal.list", {"filter": {}, "select": ["ID", "STAGE_ID", "SOURCE_ID", "DATE_CREATE"]})
    leads = []
    if LEAD_MAP:
        try:
            leads = b24_all("crm.lead.list", {"filter": {}, "select": ["ID", "STATUS_ID", "SOURCE_ID", "DATE_CREATE"]})
        except Exception as e:
            print(f"[!] Лиды собрать не удалось ({e}).")

    # Для каждого периода считаем сделки и лиды
    periods = {}
    for pkey, days in PERIODS.items():
        d_subset = filter_by_period(deals, days)
        block = {"deals": build_block(d_subset, "STAGE_ID", src_names, DEAL_MAP, DEAL_ORDER, DEAL_SIDE)}
        block["deals"]["timeseries"] = timeseries_weekly(d_subset)
        if LEAD_MAP and leads:
            l_subset = filter_by_period(leads, days)
            block["leads"] = build_block(l_subset, "STATUS_ID", src_names, LEAD_MAP, LEAD_ORDER, LEAD_SIDE)
            block["leads"]["timeseries"] = timeseries_weekly(l_subset)
        periods[pkey] = block

    result = {
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
        "periods": periods,   # все срезы: период -> {deals, leads}, каждый с by_source
        "leads_monthly_by_source": timeseries_monthly_by_source(leads, src_names) if leads else {},
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # печать в консоль: краткая сводка по периодам
    pnames = {"week": "Неделя", "month": "Месяц", "quarter": "Квартал", "year": "Год", "all": "Всё время"}
    print()
    for pkey in ["all", "year", "quarter", "month", "week"]:
        blk = periods.get(pkey, {})
        dfun = blk.get("deals", {}).get("funnel", [])
        lfun = blk.get("leads", {}).get("funnel", [])
        deals_in = dfun[0]["count"] if dfun else 0
        deals_won = dfun[-1]["count"] if dfun else 0
        leads_in = lfun[0]["count"] if lfun else 0
        print(f"  {pnames[pkey]:<10}: лидов {leads_in:>4} | сделок {deals_in:>4} | выиграно {deals_won:>3}")

    print(f"\n-> Записано в {OUT_FILE}")


if __name__ == "__main__":
    print(">>> bitrix_funnel.py  ВЕРСИЯ 15 (+ лиды помесячно по источникам) <<<")
    WEBHOOK = load_webhook()
    if len(sys.argv) > 1 and sys.argv[1] == "stages":
        discover_stages()
    else:
        collect_funnel()