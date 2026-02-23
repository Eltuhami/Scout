import os
import random
import json
import requests
from bs4 import BeautifulSoup
from google import genai

# ========================
# CONFIG
# ========================

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

MODEL_NAME = "gemini-1.5-flash"

MAX_BUY_PRICE = 16.0
MIN_PROFIT = 5.0
VINTED_FEE = 0.15

LISTINGS_PER_KEYWORD = 8
KEYWORDS_PER_RUN = 5

HISTORY_FILE = "history.txt"
KEYWORD_STATS_FILE = "keyword_stats.json"

client = genai.Client(api_key=GEMINI_API_KEY)

# ========================
# STARTING KEYWORDS
# ========================

BASE_KEYWORDS = [

    "Sony Walkman",
    "iPod Nano",
    "iPod Classic",
    "Nintendo DS",
    "Gameboy",

    "Casio Vintage Uhr",
    "Seiko Uhr",

    "Pokemon Sammlung",
    "YuGiOh Sammlung",

    "Polaroid Kamera",
    "Canon Analog Kamera",

    "Lego Star Wars",
    "Lego Minifiguren",

    "Funko Pop",
    "Hot Wheels Sammlung",

    "Retro Taschenrechner"
]

LOW_VALUE = [
    "hülle", "case", "kabel", "adapter",
    "anleitung", "manual", "dvd", "cd",
    "buch", "ersatzteil"
]

# ========================
# FILE HELPERS
# ========================

def load_set(file):

    if not os.path.exists(file):
        return set()

    with open(file, "r", encoding="utf-8") as f:
        return set(x.strip() for x in f)


def save_line(file, value):

    with open(file, "a", encoding="utf-8") as f:
        f.write(value + "\n")


def load_keyword_stats():

    if not os.path.exists(KEYWORD_STATS_FILE):

        stats = {}

        for k in BASE_KEYWORDS:
            stats[k] = {"profit": 0, "checked": 0}

        return stats

    with open(KEYWORD_STATS_FILE, "r") as f:
        return json.load(f)


def save_keyword_stats(stats):

    with open(KEYWORD_STATS_FILE, "w") as f:
        json.dump(stats, f)


# ========================
# SELF LEARNING KEYWORD SELECTION
# ========================

def choose_keywords():

    stats = load_keyword_stats()

    scored = []

    for k, v in stats.items():

        score = v["profit"] - (v["checked"] * 0.1)

        scored.append((score, k))

    scored.sort(reverse=True)

    best = [k for _, k in scored[:10]]

    selected = random.sample(best, min(KEYWORDS_PER_RUN, len(best)))

    print("[KEYWORDS]", selected)

    return selected, stats


# ========================
# EBAY SCRAPER (FIXED)
# ========================

def scrape_ebay(keyword):

    url = (
        "https://www.ebay.de/sch/i.html"
        f"?_nkw={keyword}"
        "&LH_BIN=1"
        "&_udhi=16"
        "&_sop=15"
    )

    payload = {
        "api_key": SCRAPER_API_KEY,
        "url": url
    }

    try:

        r = requests.get(
            "http://api.scraperapi.com",
            params=payload,
            timeout=60
        )

        soup = BeautifulSoup(r.text, "html.parser")

        listings = []

        items = soup.select("li.s-item")

        for item in items:

            title_tag = item.select_one(".s-item__title")

            price_tag = item.select_one(".s-item__price")

            link_tag = item.select_one("a.s-item__link")

            if not title_tag or not price_tag or not link_tag:
                continue

            title = title_tag.get_text().strip()

            lower = title.lower()

            if any(w in lower for w in LOW_VALUE):
                continue

            price_text = (
                price_tag.get_text()
                .replace("€", "")
                .replace(",", ".")
                .split(" ")[0]
            )

            try:
                price = float(price_text)
            except:
                continue

            if price > MAX_BUY_PRICE:
                continue

            listings.append({

                "title": title,
                "price": price,
                "url": link_tag["href"]
            })

            if len(listings) >= LISTINGS_PER_KEYWORD:
                break

        print(f"[SCRAPER] {keyword}: {len(listings)}")

        return listings

    except Exception as e:

        print("[SCRAPER ERROR]", e)

        return []


# ========================
# GEMINI BATCH PRICE ESTIMATION
# ========================

def estimate_prices(listings):

    if not listings:
        return []

    prompt = "Estimate resale prices in EURO. Return numbers separated by commas.\n\n"

    for i, item in enumerate(listings):
        prompt += f"{i+1}. {item['title']}\n"

    try:

        r = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt
        )

        prices = [float(x.strip()) for x in r.text.split(",")]

        return prices

    except Exception as e:

        print("[GEMINI ERROR]", e)

        return []


# ========================
# PROFIT
# ========================

def profit(buy, resale):

    return round((resale * (1 - VINTED_FEE)) - buy, 2)


# ========================
# DISCORD
# ========================

def alert(item, resale, profit_value):

    msg = (
        f"🔥 Flip Found\n\n"
        f"{item['title']}\n"
        f"Buy: {item['price']}€\n"
        f"Resale: {resale}€\n"
        f"Profit: {profit_value}€\n\n"
        f"{item['url']}"
    )

    try:

        requests.post(
            DISCORD_WEBHOOK,
            json={"content": msg}
        )

        print("[ALERT SENT]")

    except:
        print("[DISCORD FAILED]")


# ========================
# MAIN
# ========================

def main():

    history = load_set(HISTORY_FILE)

    keywords, stats = choose_keywords()

    total = 0
    profitable = 0

    for keyword in keywords:

        listings = scrape_ebay(keyword)

        if not listings:
            continue

        prices = estimate_prices(listings)

        if len(prices) != len(listings):
            continue

        for item, resale in zip(listings, prices):

            total += 1

            stats[keyword]["checked"] += 1

            if item["url"] in history:
                continue

            p = profit(item["price"], resale)

            print("[CHECK]", item["price"], resale, p)

            if p >= MIN_PROFIT:

                profitable += 1

                stats[keyword]["profit"] += 1

                alert(item, resale, p)

                save_line(HISTORY_FILE, item["url"])

    save_keyword_stats(stats)

    print("[SUMMARY]", total, profitable)


# ========================

if __name__ == "__main__":
    main()
