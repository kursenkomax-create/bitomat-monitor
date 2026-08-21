import json
import os
import time
from pathlib import Path
from typing import Any

import requests


# ---------------- CONFIG ----------------

CHECK_INTERVAL_SECONDS = 3600

# Сигнал по курсу лише при зміні >= 0.20%
PRICE_THRESHOLD_PERCENT = 0.20

# Сигнал по готівці лише при зміні >= 1000 UAH
CASH_THRESHOLD_UAH = 1000

# Окремий поріг для розрахункового максимуму
MAX_USDT_THRESHOLD = 25

# Після скількох невдалих погодинних перевірок надіслати ОДНЕ попередження
ERROR_NOTIFY_AFTER = 5

STATE_FILE = Path(os.getenv("STATE_FILE_PATH", "bitomat_state.json"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Один endpoint повинен повертати список усіх ATM.
# Пробуємо по черзі; перший успішний використовується.
ATM_API_URLS = [
    "https://shitcoins.club/atms/getAtmsData",
    "https://api.bitomat.com/atms/getAtmsData",
    "https://www.bitomat.com/atms/getAtmsData",
]

LOCATIONS = [
    {
        "id": "1395",
        "state_id": "kurbasa",
        "name": "Леся Курбаса, 19А",
    },
    {
        "id": "1521",
        "state_id": "teremky2",
        "name": "Лятошинського, 14 (Теремки-2)",
    },
    {
        "id": "1538",
        "state_id": "ocean_plaza",
        "name": "Антоновича, 176 (Ocean Plaza)",
    },
]


# ---------------- BASIC HELPERS ----------------

def as_float(value: Any) -> float | None:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        text = (
            value.strip()
            .replace("\u00a0", "")
            .replace("\u202f", "")
            .replace(" ", "")
            .replace(",", ".")
        )
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    return None


def fmt_uah(value: float) -> str:
    return f"{value:,.4f}".replace(",", " ").rstrip("0").rstrip(".") + " UAH"


def fmt_cash(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " UAH"


def fmt_usdt(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".") + " USDT"


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 100.0 if new != 0 else 0.0
    return (new - old) / old * 100.0


def normalize_key(key: str) -> str:
    return (
        str(key)
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
        .replace(".", "_")
    )


def flatten_dict(obj: Any, prefix: str = "") -> dict[str, Any]:
    """
    Перетворює вкладений JSON у словник:
      balances.UAH -> value
      prices.Tether_sell_price -> value
    Це дає змогу пережити невеликі зміни структури API.
    """
    out = {}

    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                out.update(flatten_dict(value, path))
            else:
                out[path] = value

    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            path = f"{prefix}[{i}]"
            if isinstance(value, (dict, list)):
                out.update(flatten_dict(value, path))
            else:
                out[path] = value

    return out


# ---------------- API PARSING ----------------

def get_atm_id(item: dict) -> str | None:
    for key in ("id", "atm_id", "atmId", "machine_id", "machineId"):
        if key in item and item[key] is not None:
            return str(item[key])

    flat = flatten_dict(item)
    for path, value in flat.items():
        last = normalize_key(path.split(".")[-1])
        if last in {"id", "atm_id", "atmid", "machine_id", "machineid"}:
            if value is not None:
                return str(value)

    return None


def find_cash_uah(item: dict) -> float:
    # Найтиповіший формат: "balances": {"UAH": 12345}
    balances = item.get("balances")
    if isinstance(balances, dict):
        for key, value in balances.items():
            if str(key).upper() == "UAH":
                num = as_float(value)
                if num is not None:
                    return num

    # Fallback — шукаємо UAH у вкладеній структурі
    flat = flatten_dict(item)

    priority = []
    fallback = []

    for path, value in flat.items():
        p = normalize_key(path)
        num = as_float(value)
        if num is None:
            continue

        if "uah" in p and any(word in p for word in ("balance", "cash", "available")):
            priority.append(num)
        elif p.endswith(".uah") or p == "uah":
            fallback.append(num)

    if priority:
        return priority[0]
    if fallback:
        return fallback[0]

    raise ValueError("Не знайдено UAH balance")


def find_usdt_rates(item: dict) -> tuple[float, float]:
    """
    Шукає USDT/Tether buy/sell rate незалежно від невеликих змін назв ключів.
    Підтримує, наприклад:
      Tether_sell_price
      Tether_buy_price
      USDT_sell_price
      USDT_buy_price
      prices.USDT.sell
      rates.tether.buy
    """
    flat = flatten_dict(item)

    sell_candidates = []
    buy_candidates = []

    for path, value in flat.items():
        p = normalize_key(path)
        num = as_float(value)

        if num is None:
            continue

        is_usdt = ("usdt" in p) or ("tether" in p)
        if not is_usdt:
            continue

        # UAH/USDT має бути в реалістичному діапазоні.
        if not (10 <= num <= 200):
            continue

        if "sell" in p:
            sell_candidates.append((path, num))
        if "buy" in p:
            buy_candidates.append((path, num))

    if not sell_candidates or not buy_candidates:
        keys = [k for k in flat if ("usdt" in k.lower() or "tether" in k.lower())]
        raise ValueError(
            "Не знайдено buy/sell курс USDT. "
            f"USDT/Tether keys: {keys[:20]}"
        )

    raw_sell = sell_candidates[0][1]
    raw_buy = buy_candidates[0][1]

    # Для користувача:
    # USDT -> UAH зазвичай нижчий курс,
    # UAH -> USDT зазвичай вищий курс.
    # Це також страхує від протилежного трактування buy/sell самим backend.
    user_sell_rate = min(raw_sell, raw_buy)
    user_buy_rate = max(raw_sell, raw_buy)

    return user_sell_rate, user_buy_rate


def unwrap_atm_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        # Часті wrapper-и API
        for key in ("data", "atms", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

        # Іноді data -> atms
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("atms", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]

    raise ValueError(
        f"Невідомий формат API: root={type(payload).__name__}"
    )


def fetch_all_atms(session: requests.Session) -> tuple[list[dict], str]:
    errors = []

    for url in ATM_API_URLS:
        for method in ("GET", "POST"):
            try:
                kwargs = dict(
                    timeout=25,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"
                        ),
                        "Referer": "https://www.bitomat.com/",
                    },
                )

                if method == "GET":
                    response = session.get(url, **kwargs)
                else:
                    response = session.post(url, **kwargs)

                if response.status_code != 200:
                    errors.append(f"{method} {url} -> HTTP {response.status_code}")
                    continue

                try:
                    payload = response.json()
                except Exception:
                    errors.append(
                        f"{method} {url} -> HTTP 200, але не JSON "
                        f"(content-type={response.headers.get('content-type')})"
                    )
                    continue

                atms = unwrap_atm_list(payload)

                if not atms:
                    errors.append(f"{method} {url} -> порожній список")
                    continue

                print(f"[API OK] {method} {url}: {len(atms)} ATM")
                return atms, url

            except Exception as exc:
                errors.append(f"{method} {url} -> {type(exc).__name__}: {exc}")

    raise RuntimeError(" | ".join(errors))


def parse_location(atms: list[dict], location: dict) -> dict:
    target_id = str(location["id"])

    atm = None
    for item in atms:
        if get_atm_id(item) == target_id:
            atm = item
            break

    if atm is None:
        raise ValueError(f"ATM id={target_id} не знайдено в API")

    cash = find_cash_uah(atm)
    sell_rate, buy_rate = find_usdt_rates(atm)

    if sell_rate <= 0 or buy_rate <= 0 or cash < 0:
        raise ValueError("API повернув некоректні значення")

    max_usdt = cash / sell_rate if sell_rate else 0

    # Додатково читаємо статус, якщо є
    flat = flatten_dict(atm)
    status = None

    for path, value in flat.items():
        p = normalize_key(path)
        if p.endswith("is_enabled") or p.endswith("enabled") or p.endswith("status"):
            if isinstance(value, (bool, int, str)):
                status = value
                break

    return {
        "cash": cash,
        "sell_rate": sell_rate,
        "buy_rate": buy_rate,
        "max_usdt": max_usdt,
        "status": status,
        "checked_at": int(time.time()),
    }


# ---------------- STATE ----------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


# ---------------- TELEGRAM ----------------

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n--- TELEGRAM MESSAGE ---")
        print(text)
        print("------------------------\n")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()


# ---------------- NOTIFICATIONS ----------------

def initial_message(location: dict, current: dict) -> str:
    status_line = ""
    if current.get("status") is not None:
        status_line = f"\nСтатус API: {current['status']}"

    return (
        f"✅ Моніторинг запущено\n"
        f"📍 {location['name']}\n\n"
        f"USDT → UAH: {fmt_uah(current['sell_rate'])} за 1 USDT\n"
        f"UAH → USDT: {fmt_uah(current['buy_rate'])} за 1 USDT\n"
        f"Доступна готівка: {fmt_cash(current['cash'])}\n"
        f"≈ максимум до обміну: {fmt_usdt(current['max_usdt'])}"
        f"{status_line}"
    )


def build_change_message(
    location: dict,
    notified: dict,
    current: dict,
) -> tuple[str | None, dict]:

    changes = []
    updated = dict(notified)

    old_sell = float(notified["sell_rate"])
    sell_pct = pct_change(old_sell, current["sell_rate"])

    if abs(sell_pct) >= PRICE_THRESHOLD_PERCENT:
        changes.append(
            f"USDT → UAH:\n"
            f"{fmt_uah(old_sell)} → {fmt_uah(current['sell_rate'])} "
            f"({sell_pct:+.2f}%)"
        )
        updated["sell_rate"] = current["sell_rate"]

    old_buy = float(notified["buy_rate"])
    buy_pct = pct_change(old_buy, current["buy_rate"])

    if abs(buy_pct) >= PRICE_THRESHOLD_PERCENT:
        changes.append(
            f"UAH → USDT:\n"
            f"{fmt_uah(old_buy)} → {fmt_uah(current['buy_rate'])} "
            f"({buy_pct:+.2f}%)"
        )
        updated["buy_rate"] = current["buy_rate"]

    old_cash = float(notified["cash"])
    cash_diff = current["cash"] - old_cash

    if abs(cash_diff) >= CASH_THRESHOLD_UAH:
        changes.append(
            f"Готівка:\n"
            f"{fmt_cash(old_cash)} → {fmt_cash(current['cash'])} "
            f"({cash_diff:+,.0f} UAH)".replace(",", " ")
        )
        updated["cash"] = current["cash"]

    old_max = float(notified["max_usdt"])
    max_diff = current["max_usdt"] - old_max

    if abs(max_diff) >= MAX_USDT_THRESHOLD:
        changes.append(
            f"≈ максимум USDT:\n"
            f"{fmt_usdt(old_max)} → {fmt_usdt(current['max_usdt'])} "
            f"({max_diff:+.2f} USDT)"
        )
        updated["max_usdt"] = current["max_usdt"]

    if not changes:
        return None, notified

    message = (
        f"🔔 Bitomat — зміна\n"
        f"📍 {location['name']}\n\n"
        + "\n\n".join(changes)
        + f"\n\nЗараз доступно: {fmt_cash(current['cash'])}"
        + f"\n≈ {fmt_usdt(current['max_usdt'])} за поточним курсом продажу"
    )

    return message, updated


# ---------------- MAIN ----------------

def main():
    state = load_state()
    state.setdefault("_monitor", {})
    monitor = state["_monitor"]

    print("Bitomat API-only monitor started.")
    print(f"Check interval: {CHECK_INTERVAL_SECONDS} sec.")
    print(
        f"Thresholds: rate {PRICE_THRESHOLD_PERCENT}% | "
        f"cash {CASH_THRESHOLD_UAH} UAH | "
        f"max {MAX_USDT_THRESHOLD} USDT"
    )

    session = requests.Session()

    while True:
        try:
            atms, source_url = fetch_all_atms(session)

            previous_errors = int(monitor.get("api_error_count", 0))
            monitor["api_error_count"] = 0
            monitor["last_api_source"] = source_url
            monitor["last_api_success"] = int(time.time())

            if previous_errors >= ERROR_NOTIFY_AFTER:
                send_telegram(
                    "✅ Bitomat monitor відновив роботу\n\n"
                    f"Джерело даних знову відповідає після "
                    f"{previous_errors} невдалих перевірок."
                )

            for location in LOCATIONS:
                state_id = location["state_id"]

                try:
                    current = parse_location(atms, location)

                    print(
                        f"[OK] {location['name']}: "
                        f"sell={current['sell_rate']} "
                        f"buy={current['buy_rate']} "
                        f"cash={current['cash']} "
                        f"max≈{current['max_usdt']:.2f} "
                        f"status={current.get('status')}"
                    )

                    loc_state = state.get(state_id)

                    if (
                        not isinstance(loc_state, dict)
                        or not loc_state.get("last_notified")
                        or not all(
                            k in loc_state["last_notified"]
                            for k in ("sell_rate", "buy_rate", "cash", "max_usdt")
                        )
                    ):
                        state[state_id] = {
                            "last_notified": {
                                "sell_rate": current["sell_rate"],
                                "buy_rate": current["buy_rate"],
                                "cash": current["cash"],
                                "max_usdt": current["max_usdt"],
                            },
                            "last_seen": current,
                        }

                        save_state(state)
                        send_telegram(initial_message(location, current))
                        continue

                    notified = loc_state["last_notified"]
                    message, updated_notified = build_change_message(
                        location,
                        notified,
                        current,
                    )

                    loc_state["last_seen"] = current

                    if message:
                        send_telegram(message)
                        loc_state["last_notified"] = updated_notified

                    save_state(state)

                except Exception as exc:
                    # Одна проблемна точка не повинна ламати інші.
                    print(f"[LOCATION ERROR] {location['name']}: {exc}")

            save_state(state)

        except Exception as exc:
            count = int(monitor.get("api_error_count", 0)) + 1
            monitor["api_error_count"] = count
            monitor["last_api_error"] = str(exc)
            monitor["last_api_error_at"] = int(time.time())
            save_state(state)

            print(f"[API ERROR] attempt={count}: {exc}")

            # Одне повідомлення на порозі; далі не спамимо щогодини.
            if count == ERROR_NOTIFY_AFTER:
                send_telegram(
                    "⚠️ Bitomat monitor\n\n"
                    f"Не вдалося отримати API-дані "
                    f"{ERROR_NOTIFY_AFTER} перевірок поспіль.\n"
                    f"Причина: {exc}"
                )

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
