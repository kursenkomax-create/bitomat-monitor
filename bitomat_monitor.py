import asyncio
import json
import os
import re
import time
from pathlib import Path

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------- CONFIG ----------------

CHECK_INTERVAL_SECONDS = 60

# Ігнорувати зміну курсу менше 0.20% від останнього повідомленого значення
PRICE_THRESHOLD_PERCENT = 0.20

# Ігнорувати зміну доступної готівки менше 1000 грн
CASH_THRESHOLD_UAH = 1000

# Ігнорувати зміну розрахункового максимуму менше 25 USDT
MAX_USDT_THRESHOLD = 25

STATE_FILE = Path(os.getenv("STATE_FILE_PATH", "bitomat_state.json"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

LOCATIONS = [
    {
        "id": "kurbasa",
        "name": "Леся Курбаса, 19А",
        "url": "https://www.bitomat.com/uk/bitcoin-bankomat/bitkoin-bankomat-kiev",
    },
    {
        "id": "teremky2",
        "name": "Лятошинського, 14 (Теремки-2)",
        "url": "https://www.bitomat.com/uk/bitcoin-bankomat/bitkoin-bankomat-kiev-teremky-2",
    },
    {
        "id": "ocean_plaza",
        "name": "Антоновича, 176 (Ocean Plaza)",
        "url": "https://www.bitomat.com/uk/bitcoin-bankomat/bankomat-bitkoyn-kiyiv-oushen-plaza",
    },
]


# ---------------- HELPERS ----------------

def normalize_number(value: str) -> float:
    value = (
        value.replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace(",", ".")
    )
    return float(value)


def fmt_uah(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".") + " UAH"


def fmt_usdt(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".") + " USDT"


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 100.0 if new != 0 else 0.0
    return (new - old) / old * 100.0


def extract_uah_values(text: str) -> list[float]:
    # Підтримує: 41.25 UAH / 41,25 UAH / 41 250 UAH
    matches = re.findall(
        r"(?<!\d)(\d[\d \u00a0\u202f]*(?:[.,]\d+)?)\s*UAH\b",
        text,
        flags=re.IGNORECASE,
    )
    return [normalize_number(x) for x in matches]


def extract_cash(body_text: str) -> float:
    """
    Беремо UAH-значення з верхньої частини сторінки до таблиці курсів.
    У цій зоні Bitomat показує доступну готівку конкретного криптомату.
    """
    price_markers = ["Криптомат — ціни", "Криптомат - ціни"]
    top = body_text

    for marker in price_markers:
        if marker in body_text:
            top = body_text.split(marker, 1)[0]
            break

    values = extract_uah_values(top)
    if not values:
        raise ValueError("Не вдалося знайти доступну готівку в UAH")

    # Якщо Bitomat дублює значення у верхньому блоці, останнє все одно буде резервом.
    return values[-1]


def extract_usdt_rates(body_text: str) -> tuple[float, float]:
    """
    Таблиця Bitomat має порядок:
      Продати | Купити

    Bitomat може рендерити курс як:
      41.25 UAH
    або просто:
      41.25

    Тому спочатку пробуємо значення з UAH, а якщо їх немає —
    беремо перші два правдоподібні числові значення у блоці USDT.
    """
    match = re.search(
        r"(?ms)^\s*USDT\s*$([\s\S]*?)(?=^\s*USDC\s*$)",
        body_text,
    )
    if not match:
        raise ValueError("Не вдалося знайти блок USDT")

    block = match.group(1)

    # Варіант 1: сайт явно додає валюту UAH.
    values = extract_uah_values(block)

    # Варіант 2: після JS-рендерингу курс може бути без "UAH".
    if len(values) < 2:
        raw_numbers = re.findall(
            r"(?<![\w])(\d{1,4}(?:[.,]\d{1,6})?)(?![\w])",
            block,
        )

        candidates = []
        for raw in raw_numbers:
            try:
                value = normalize_number(raw)
            except ValueError:
                continue

            # Для UAH/USDT реальний курс має бути в адекватному діапазоні.
            # Це відсіює випадкові числа, які можуть бути в тексті/атрибутах.
            if 10 <= value <= 200:
                candidates.append(value)

        values = candidates

    if len(values) < 2:
        compact = " | ".join(
            line.strip()
            for line in block.splitlines()
            if line.strip()
        )[:500]
        raise ValueError(
            "Не вдалося розпізнати два значення курсу USDT. "
            f"Вміст блоку: {compact}"
        )

    sell_rate = values[0]
    buy_rate = values[1]
    return sell_rate, buy_rate


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


async def scrape_location(page, location: dict) -> dict:
    await page.goto(
        location["url"],
        wait_until="domcontentloaded",
        timeout=45_000,
    )

    # Bitomat підвантажує курс і резерв через JS.
    # Опитуємо сторінку до 25 секунд, замість фіксованого sleep.
    last_error = None

    for _ in range(25):
        body_text = await page.locator("body").inner_text()

        try:
            cash = extract_cash(body_text)
            sell_rate, buy_rate = extract_usdt_rates(body_text)

            if sell_rate <= 0 or buy_rate <= 0 or cash < 0:
                raise ValueError("Отримані некоректні числові значення")

            max_usdt = cash / sell_rate if sell_rate else 0

            return {
                "cash": cash,
                "sell_rate": sell_rate,
                "buy_rate": buy_rate,
                "max_usdt": max_usdt,
                "checked_at": int(time.time()),
            }
        except Exception as exc:
            last_error = exc
            await page.wait_for_timeout(1_000)

    raise RuntimeError(f"Дані не завантажилися: {last_error}")


def initial_message(location: dict, current: dict) -> str:
    return (
        f"✅ Моніторинг запущено\n"
        f"📍 {location['name']}\n\n"
        f"USDT → UAH: {fmt_uah(current['sell_rate'])} за 1 USDT\n"
        f"UAH → USDT: {fmt_uah(current['buy_rate'])} за 1 USDT\n"
        f"Доступна готівка: {fmt_uah(current['cash'])}\n"
        f"≈ максимум до обміну: {fmt_usdt(current['max_usdt'])}"
    )


def build_change_message(
    location: dict,
    notified: dict,
    current: dict,
) -> tuple[str | None, dict]:
    changes = []
    updated = dict(notified)

    # ---- Курс продажу USDT ----
    old_sell = float(notified["sell_rate"])
    sell_pct = pct_change(old_sell, current["sell_rate"])

    if abs(sell_pct) >= PRICE_THRESHOLD_PERCENT:
        changes.append(
            f"USDT → UAH:\n"
            f"{fmt_uah(old_sell)} → {fmt_uah(current['sell_rate'])} "
            f"({sell_pct:+.2f}%)"
        )
        updated["sell_rate"] = current["sell_rate"]

    # ---- Курс купівлі USDT ----
    old_buy = float(notified["buy_rate"])
    buy_pct = pct_change(old_buy, current["buy_rate"])

    if abs(buy_pct) >= PRICE_THRESHOLD_PERCENT:
        changes.append(
            f"UAH → USDT:\n"
            f"{fmt_uah(old_buy)} → {fmt_uah(current['buy_rate'])} "
            f"({buy_pct:+.2f}%)"
        )
        updated["buy_rate"] = current["buy_rate"]

    # ---- Доступна готівка ----
    old_cash = float(notified["cash"])
    cash_diff = current["cash"] - old_cash

    if abs(cash_diff) >= CASH_THRESHOLD_UAH:
        changes.append(
            f"Готівка:\n"
            f"{fmt_uah(old_cash)} → {fmt_uah(current['cash'])} "
            f"({cash_diff:+,.0f} UAH)".replace(",", " ")
        )
        updated["cash"] = current["cash"]

    # ---- Приблизний максимум USDT ----
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

    # Якщо повідомлення вже йде через іншу зміну,
    # показуємо поточний максимум, навіть якщо він сам не перейшов поріг.
    current_summary = (
        f"\n\nЗараз доступно: {fmt_uah(current['cash'])}\n"
        f"≈ {fmt_usdt(current['max_usdt'])} за поточним курсом продажу"
    )

    message = (
        f"🔔 Bitomat — зміна\n"
        f"📍 {location['name']}\n\n"
        + "\n\n".join(changes)
        + current_summary
    )

    return message, updated


async def main():
    state = load_state()

    print("Bitomat monitor started.")
    print(f"Check interval: {CHECK_INTERVAL_SECONDS} sec.")
    print(
        f"Thresholds: rate {PRICE_THRESHOLD_PERCENT}% | "
        f"cash {CASH_THRESHOLD_UAH} UAH | "
        f"max {MAX_USDT_THRESHOLD} USDT"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        while True:
            for location in LOCATIONS:
                loc_id = location["id"]

                try:
                    current = await scrape_location(page, location)
                    print(
                        f"[OK] {location['name']}: "
                        f"sell={current['sell_rate']} "
                        f"buy={current['buy_rate']} "
                        f"cash={current['cash']} "
                        f"max≈{current['max_usdt']:.2f}"
                    )

                    if loc_id not in state:
                        # Перший запуск: зберігаємо базу і надсилаємо стартовий стан.
                        state[loc_id] = {
                            "last_notified": {
                                "sell_rate": current["sell_rate"],
                                "buy_rate": current["buy_rate"],
                                "cash": current["cash"],
                                "max_usdt": current["max_usdt"],
                            },
                            "last_seen": current,
                            "error_count": 0,
                        }

                        save_state(state)
                        send_telegram(initial_message(location, current))
                        continue

                    notified = state[loc_id]["last_notified"]

                    message, updated_notified = build_change_message(
                        location,
                        notified,
                        current,
                    )

                    state[loc_id]["last_seen"] = current
                    state[loc_id]["error_count"] = 0

                    if message:
                        send_telegram(message)
                        state[loc_id]["last_notified"] = updated_notified

                    save_state(state)

                except (PlaywrightTimeoutError, Exception) as exc:
                    print(f"[ERROR] {location['name']}: {exc}")

                    if loc_id not in state:
                        state[loc_id] = {
                            "last_notified": {},
                            "last_seen": {},
                            "error_count": 0,
                        }

                    state[loc_id]["error_count"] = (
                        state[loc_id].get("error_count", 0) + 1
                    )

                    # Не спамимо Telegram одиничними збоями.
                    # Повідомляємо після 5 послідовних невдалих перевірок.
                    if state[loc_id]["error_count"] == 5:
                        send_telegram(
                            f"⚠️ Bitomat monitor\n"
                            f"📍 {location['name']}\n\n"
                            f"Не вдалося отримати дані 5 перевірок поспіль.\n"
                            f"Причина: {exc}"
                        )

                    save_state(state)

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
