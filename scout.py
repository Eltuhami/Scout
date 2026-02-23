import os
import random
import requests
from bs4 import BeautifulSoup
from google import genai

# ========================
# CONFIGURATION
# ========================

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

MODEL_NAME = "gemini-1.5-flash"

MAX_BUY_PRICE = 16.0
MIN_PROFIT = 5.0
VINTED_FEE = 0.15

NUM_LISTINGS_PER_KEYWORD = 6
KEYWORDS_PER_RUN = 5

HISTORY_FILE = "history.txt"
KEYWORD_HISTORY_FILE = "keyword_history.txt"

client = genai.Client(api_key=GEMINI_API_KEY)

# ========================
# HIGH SUCCESS KEYWORDS
# ========================

KEYWORDS = [

    "Sony Walkman",
    "iPod Classic",
    "iPod Nano",
    "Nintendo DS Konsole",
    "Gameboy Konsole",
    "PSP Konsole",

    "Casio Vintage Uhr",
    "Seiko Uhr Vintage",

    "Pokemon Sammlung",
    "YuGiOh Sammlung",

    "Funko Pop Limited",
    "Hot Wheels Sammlung",

    "Polaroid Kamera",
    "Canon Analog Kamera",

    "Lego Star Wars",
    "Lego Minifiguren Sammlung",

    "Playmobil Sammlung",

    "Retro Taschenrechner",

    "Vintage Kamera",

    "Retro Elektronik"
]

LOW_VALUE_WORDS = [

    "hülle",
    "case",
    "kabel",
    "adapter",
    "manual",
    "anleitung",
    "dvd",
    "cd",
    "buch",
    "ersatzteil",
    "defekt nur teile"
]

TRASH_WORDS = [

    "seite",
    "pagination",
    "navigation",
    "feedback",
    "altersempfehlung",
    "benachrichtigungen"
]

# ========================
# FILE HELPERS
# ========================

def load_file_set(filename):

    if not os.path.exists(filename):
        return set()

    with open(filename, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)


def save_to_file(filename, value):

    with open(filename, "a", encoding="utf-8") as f:
        f.write(value + "\n")


# ========================
# KEYWORD ROTATION
# ========================

def get_next_keywords():

    used = load_file_set(KEYWORD_HISTORY_FILE)

    available = [k for k in KEYWORDS if k not in used]

    if len(available) < KEYWORDS_PER_RUN:
        open(KEYWORD_HISTORY_FILE, "w").close()
        available = KEYWORDS.copy()

    selected = random.sample(available, KEYWORDS_PER_RUN)

    for k in selected:
        save_to_file(KEYWORD_HISTORY_FILE, k)

    print(f"[KEYWORDS] {selected}")

    return selected


# ========================
# EBAY SCRAPER
# ========================

def scrape_ebay(keyword):

    url = (
        "https://www.ebay.de/sch/i.html"
        f"?_nkw={keyword}"
        "&LH_BIN=1"
        "&LH_PrefLoc=3"
        "&LH_ItemCondition=3000"
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

        for item in soup.select(".s-item"):

            title_tag = item.select_one(".s-item__title")
            price_tag = item.select_one(".s-item__price")
            link_tag = item.select_one(".s-item__link")

            if not title_tag or not price_tag or not link_tag:
                continue

            title = title_tag.get_text().strip().lower()

            if any(word in title for word in TRASH_WORDS):
                continue

            if any(word in title for word in LOW_VALUE_WORDS):
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

                "title": title_tag.get_text().strip(),
                "price": price,
                "url": link_tag.get("href")
            })

            if len(listings) >= NUM_LISTINGS_PER_KEYWORD:
                break

        print(f"[SCRAPER] {keyword}: {len(listings)} candidates")

        return listings

    except Exception as e:

        print(f"[SCRAPER ERROR] {e}")

        return []


# ========================
# GEMINI BATCH VALUATION
# ========================

def estimate_resale_prices(listings):

    if not listings:
        return []

    prompt = "Estimate realistic Vinted resale prices in EURO.\nReturn ONLY numbers separated by commas.\n\n"

    for i, item in enumerate(listings):

        prompt += f"{i+1}. {item['title']}\n"

    try:

        response = client.models.generate_content(

            model=MODEL_NAME,
            contents=prompt
        )

        text = response.text.strip()

        prices = [

            float(x.strip())
            for x in text.split(",")
        ]

        print(f"[GEMINI] {prices}")

        return prices

    except Exception as e:

        print(f"[GEMINI ERROR] {e}")

        return []


# ========================
# PROFIT
# ========================

def calculate_profit(buy, resale):

    net = resale * (1 - VINTED_FEE)

    return round(net - buy, 2)


# ========================
# DISCORD
# ========================

def send_discord(item, resale, profit):

    message = (

        f"🔥 Flip Found\n\n"
        f"{item['title']}\n\n"
        f"Buy: {item['price']}€\n"
        f"Resale: {resale}€\n"
        f"Profit: {profit}€\n\n"
        f"{item['url']}"
    )

    try:

        requests.post(

            DISCORD_WEBHOOK,
            json={"content": message},
            timeout=30
        )

        print("[DISCORD] SENT")

    except Exception as e:

        print(f"[DISCORD ERROR] {e}")


# ========================
# MAIN LOOP
# ========================

def main():

    history = load_file_set(HISTORY_FILE)

    keywords = get_next_keywords()

    total_checked = 0
    total_profitable = 0

    for keyword in keywords:

        listings = scrape_ebay(keyword)

        if not listings:
            continue

        resale_prices = estimate_resale_prices(listings)

        if len(resale_prices) != len(listings):
            continue

        for item, resale in zip(listings, resale_prices):

            total_checked += 1

            if item["url"] in history:
                continue

            profit = calculate_profit(item["price"], resale)

            print(
                f"[CHECK] buy {item['price']} resale {resale} profit {profit}"
            )

            if profit >= MIN_PROFIT:

                send_discord(item, resale, profit)

                save_to_file(HISTORY_FILE, item["url"])

                total_profitable += 1

    print(f"[SUMMARY] checked={total_checked} profitable={total_profitable}")


# ========================
# ENTRY
# ========================

if __name__ == "__main__":
    main()
