# -*- coding: utf-8 -*-
"""
salebot_funnel.py — сборщик воронки из SaleBot.
Запускается на домашнем компе (Windows), рядом кладётся в ту же папку,
что и bitrix_funnel.py. Пишет salebot.json.

РЕЖИМЫ:
  python salebot_funnel.py stages   — РАЗВЕДКА: показывает, сколько клиентов
                                       на каждом этапе (state_id). Названия
                                       этапов подставишь руками в STATE_MAP.
  python salebot_funnel.py          — СБОР: считает воронку и пишет salebot.json

Метод истории/списка всех этапов в API SaleBot отсутствует, поэтому этап
каждого клиента узнаём через get_order_state (текущая стадия, неважно кто
её поставил — бот или менеджер вручную).
"""

import sys
import json
import requests
from concurrent.futures import ThreadPoolExecutor

# ===== НАСТРОЙКИ =====
API_KEY = "223060d419beef5e580d0e0caa2a16ee"
BASE = "https://chatter.salebot.pro/api/" + API_KEY

OUT_FILE = "salebot.json"

# Сколько запросов слать одновременно (параллельно). 20 = быстро.
# Если начнёт ловить ошибки/лимиты — снизь до 10.
WORKERS = 20

# ---- МАППИНГ ЭТАПОВ ----
# ВХОД — это источники трафика (клиент попадает в бота одним из путей):
ENTRY_MAP = {
    66802395: "Личка",                 # Заинтересовались = написали в личку
    66840644: "Комментарии",           # Пришли с комментариев = автоответ на коммент
}
# СТУПЕНИ движения (после входа клиент двигается по ним):
STEP_MAP = {
    66764602: "Узнал цену / нет ответа",
    66764598: "Захотели к менеджеру",
}
STEP_ORDER = [
    "Узнал цену / нет ответа",
    "Захотели к менеджеру",
]
# БОКОВЫЕ (исходы вне потока):
SIDE_MAP = {
    "Отказ": [66809693],
    "Случайные": [66907621],
}

# Родные названия этапов в SaleBot (для тултипов на дашборде).
NATIVE_NAMES_BOT = {
    "Личка":                      "Заинтересовались",
    "Комментарии":                "Пришли с комментариев",
    "Узнал цену / нет ответа":    "Узнал цену / нет ответа",
    "Захотели к менеджеру":       "Захотели связаться с менеджером",
    "Отказ":                      "Отказ",
    "Случайные":                  "Сотрудничество / Случайные срабатывания",
    "Вход в бота":                "Личка + Пришли с комментариев",
}


# ===== Вызовы API =====
def api_get(action, params=None):
    url = BASE + "/" + action
    r = requests.get(url, params=params or {}, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"status": "fail", "raw": r.text[:200]}


def get_all_clients():
    """Тянет всех клиентов постранично (limit 500)."""
    clients = []
    offset = 0
    while True:
        d = api_get("get_clients", {"offset": offset, "limit": 500})
        batch = d.get("clients", []) if isinstance(d, dict) else []
        if not batch:
            break
        clients.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return clients


def get_state(client_id):
    """Текущий state_id клиента. None, если нет сделки/ошибка."""
    d = api_get("get_order_state", {"client_id": client_id})
    if isinstance(d, dict) and d.get("status") == "success":
        return d.get("state_id")
    return None


def get_states_parallel(clients):
    """Возвращает список state_id для всех клиентов, запрашивая параллельно."""
    ids = [c.get("id") for c in clients]
    results = [None] * len(ids)
    done = [0]
    total = len(ids)

    def work(idx_cid):
        idx, cid = idx_cid
        results[idx] = get_state(cid)
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"  ...обработано {done[0]}/{total}")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, enumerate(ids)))
    return results


# ===== РЕЖИМ РАЗВЕДКИ =====
def discover():
    print(">>> salebot_funnel.py  РАЗВЕДКА <<<")
    clients = get_all_clients()
    print(f"Клиентов получено: {len(clients)}")
    if not clients:
        print("Клиентов нет или ключ/права неверные. Проверь API_KEY и доступы.")
        return

    counts = {}      # state_id -> сколько клиентов
    ghosts = 0       # призраки: нет этапа (бот не реагировал / коммент)
    states = get_states_parallel(clients)
    for st in states:
        if st is None:
            ghosts += 1
        else:
            counts[st] = counts.get(st, 0) + 1

    real = sum(counts.values())
    print("\n" + "=" * 50)
    print(f"РЕАЛЬНЫХ клиентов (есть этап): {real}")
    print(f"ПРИЗРАКОВ (нет этапа, коммент/без реакции): {ghosts}")
    print("=" * 50)
    print("ЭТАПЫ ВОРОНКИ (state_id -> сколько клиентов):")
    for st, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  state_id={st:<12} {c} клиент(ов)")
    print("\nОткрой SaleBot, посмотри какие это этапы по номерам,")
    print("и впиши их в ENTRY_MAP / STEP_MAP / SIDE_MAP в начале скрипта.")

    with open("salebot_stages.txt", "w", encoding="utf-8") as f:
        f.write(f"РЕАЛЬНЫХ: {real}, ПРИЗРАКОВ: {ghosts}\n")
        f.write("ЭТАПЫ ВОРОНКИ SaleBot (state_id -> клиентов):\n")
        for st, c in sorted(counts.items(), key=lambda x: -x[1]):
            f.write(f"  state_id={st}  {c}\n")
    print("\n-> Сохранено в salebot_stages.txt")


# Периоды для фильтра по дате создания клиента (дней назад; None = всё время)
PERIODS = {
    "week": 7,
    "month": 30,
    "quarter": 90,
    "year": 365,
    "all": None,
}


def filter_by_period(items, days):
    """Оставить только клиентов, созданных не раньше чем `days` дней назад. None = все."""
    if days is None:
        return items
    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    out = []
    for c, st in items:
        ts = c.get("created_at")
        if not ts:
            continue
        dt = None
        # формат "2026-05-28 17:33:42.539976" или ISO
        if isinstance(ts, str):
            s = ts.replace("T", " ").strip()
            # уберём микросекунды (после точки) — strptime без них стабильнее
            if "." in s:
                s = s.split(".")[0]
            try:
                dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    dt = datetime.datetime.strptime(s, "%Y-%m-%d")
                except Exception:
                    pass
        else:
            # на случай если когда-то придёт unix timestamp числом
            try:
                dt = datetime.datetime.fromtimestamp(float(ts))
            except Exception:
                pass
        if dt and dt >= cutoff:
            out.append((c, st))
    return out


def build_block(items):
    """Из пар [(client, state_id), ...] собирает воронку + слепок + боковые."""
    side_codes = {}
    for label, ids in SIDE_MAP.items():
        for sid in ids:
            side_codes[sid] = label

    entry = {name: 0 for name in ENTRY_MAP.values()}
    on_step = {s: 0 for s in STEP_ORDER}
    side = {label: 0 for label in SIDE_MAP}
    for c, st in items:
        if st in ENTRY_MAP:
            entry[ENTRY_MAP[st]] += 1
        elif st in STEP_MAP:
            on_step[STEP_MAP[st]] += 1
        elif st in side_codes:
            side[side_codes[st]] += 1

    entry_total = sum(entry.values())
    on_steps_total = sum(on_step.values())
    base = entry_total + on_steps_total

    funnel = [{
        "step": "Вход в бота",
        "count": base,
        "conv": None,
        "native": NATIVE_NAMES_BOT.get("Вход в бота", "Вход в бота"),
        "children": [{"step": name, "count": entry[name], "native": NATIVE_NAMES_BOT.get(name, name)} for name in entry],
    }]
    for s in STEP_ORDER:
        c = on_step[s]
        conv = round(c / base * 100) if base else 0
        funnel.append({"step": s, "count": c, "conv": conv, "children": None, "native": NATIVE_NAMES_BOT.get(s, s)})

    snapshot = [{"step": name, "count": entry[name], "native": NATIVE_NAMES_BOT.get(name, name)} for name in entry]
    snapshot += [{"step": s, "count": on_step[s], "native": NATIVE_NAMES_BOT.get(s, s)} for s in STEP_ORDER]

    side_list = [{"label": label, "count": side[label], "native": NATIVE_NAMES_BOT.get(label, label)} for label in SIDE_MAP]
    return {"funnel": funnel, "snapshot": snapshot, "side": side_list}


# ===== РЕЖИМ СБОРА =====
def collect():
    print(">>> salebot_funnel.py  ВЕРСИЯ 2 (+ фильтр по дате создания) <<<")
    clients = get_all_clients()
    print(f"Клиентов всего: {len(clients)}")

    states = get_states_parallel(clients)
    # пары (client, state_id) только для реальных (с этапом)
    real = []
    ghosts = 0
    for c, st in zip(clients, states):
        if st is None:
            ghosts += 1
        else:
            real.append((c, st))
    print(f"Реальных (с этапом): {len(real)}, призраков: {ghosts}")

    # Для каждого периода считаем срез
    periods = {}
    for pkey, days in PERIODS.items():
        subset = filter_by_period(real, days)
        periods[pkey] = build_block(subset)

    import datetime
    result = {
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
        "periods": periods,
        "ghosts": ghosts,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # печать сводки
    pnames = {"week": "Неделя", "month": "Месяц", "quarter": "Квартал", "year": "Год", "all": "Всё время"}
    print()
    for pkey in ["all", "year", "quarter", "month", "week"]:
        blk = periods[pkey]
        total_in = blk["funnel"][0]["count"]
        to_manager = next((s["count"] for s in blk["snapshot"] if s["step"]=="Захотели к менеджеру"), 0)
        print(f"  {pnames[pkey]:<12}  вход: {total_in}  → к менеджеру: {to_manager}")

    print(f"\n-> Записано в {OUT_FILE}")


# ===== РЕЖИМ ОСМОТРА (что вообще в данных) =====
def inspect():
    print(">>> salebot_funnel.py  ОСМОТР первых клиентов <<<")
    d = api_get("get_clients", {"offset": 0, "limit": 30})
    clients = d.get("clients", []) if isinstance(d, dict) else []
    print(f"Показываю {len(clients)} клиентов.\n")

    out = []
    for i, c in enumerate(clients, 1):
        cid = c.get("id")
        # есть ли сделка у клиента
        od = api_get("get_orders", {"client_id": cid})
        has_order = isinstance(od, dict) and od.get("status") == "success" and od.get("order_id")
        st = get_state(cid)

        line = (f"#{i} id={cid} | name={c.get('name')!r} | tag={c.get('tag')!r} | "
                f"group={c.get('group')!r} | client_type={c.get('client_type')} | "
                f"сделка={'ДА' if has_order else 'нет'} | state_id={st}")
        print(line)
        out.append(line)
        # покажем ВСЕ ключи первого клиента, чтобы видеть какие поля вообще есть
        if i == 1:
            print("\n   ВСЕ ПОЛЯ первого клиента:")
            for k, v in c.items():
                print(f"     {k} = {v!r}")
            print()

    with open("salebot_inspect.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n-> Сохранено в salebot_inspect.txt")
    print("Скинь это — посмотрим, как отличить реального клиента от призрака.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stages":
        discover()
    elif len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect()
    else:
        collect()