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
NUM_LISTINGS = 5

HISTORY_FILE = "history.txt"
KEYWORD_HISTORY_FILE = "keyword_history.txt"

client = genai.Client(api_key=GEMINI_API_KEY)

# ========================
# HIGH-PERFORMANCE KEYWORDS
# ========================

KEYWORDS = [

    # electronics
    "Sony Walkman",
    "iPod Classic",
    "iPod Nano",
    "Nintendo DS Konsole",
    "Gameboy Konsole",
    "PSP Konsole",
    "Retro Taschenrechner",

    # watches
    "Casio Vintage Uhr",
    "Seiko Uhr Vintage",

    # collectibles
    "Pokemon Sammlung",
    "YuGiOh Sammlung",
    "Funko Pop Limited",
    "Hot Wheels Sammlung",

    # cameras
    "Polaroid Kamera",
    "Canon Analog Kamera",
    "Vintage Kamera",

    # toys
    "Lego Star Wars",
    "Lego Minifiguren Sammlung",
    "Playmobil Sammlung",

    # misc
    "Retro Elektronik",
    "Vintage Elektronik"
]

# words that indicate LOW resale value
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
    "defekt nur teile",
    "ersatzteil"
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
# SMART KEYWORD ROTATION
# ========================

def get_keyword():

    used = load_file_set(KEYWORD_HISTORY_FILE)

    available = [k for k in KEYWORDS if k not in used]

    if not available:
        open(KEYWORD_HISTORY_FILE, "w").close()
        available = KEYWORDS

    keyword = random.choice(available)

    save_to_file(KEYWORD_HISTORY_FILE, keyword)

    print(f"[KEYWORD] {keyword}")

    return keyword


# ========================
# EBAY SCRAPER
# ========================

def scrape_ebay(keyword):

    url = (
        "https://www.ebay.de/sch/i.html"
        f"?_nkw={keyword}"
        "&LH_BIN=1"
        "&LH_PrefLoc=1"
        "&LH_ItemCondition=3000"
        "&_udhi=16"
        "&_sop=15"
    )

    payload = {
        "api_key": SCRAPER_API_KEY,
        "url": url
    }

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

        if len(listings) >= NUM_LISTINGS:
            break

    print(f"[SCRAPER] Found {len(listings)} candidates")

    return listings


# ========================
# GEMINI BATCH RESALE ESTIMATION
# ========================

def estimate_resale_prices(listings):

    if not listings:
        return []

    prompt = (
        "Estimate realistic resale prices on Vinted Germany.\n"
        "Return ONLY numbers separated by commas.\n\n"
    )

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
# PROFIT CALCULATION
# ========================

def calculate_profit(buy, resale):

    net = resale * (1 - VINTED_FEE)

    return round(net - buy, 2)


# ========================
# DISCORD ALERT
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

        print("[DISCORD] Alert sent")

    except Exception as e:

        print(f"[DISCORD ERROR] {e}")


# ========================
# MAIN LOOP
# ========================

def main():

    history = load_file_set(HISTORY_FILE)

    keyword = get_keyword()

    listings = scrape_ebay(keyword)

    if not listings:
        return

    resale_prices = estimate_resale_prices(listings)

    if len(resale_prices) != len(listings):
        print("[ERROR] Gemini mismatch")
        return

    for item, resale in zip(listings, resale_prices):

        if item["url"] in history:
            continue

        profit = calculate_profit(
            item["price"],
            resale
        )

        print(
            f"[CHECK] buy {item['price']} | resale {resale} | profit {profit}"
        )

        if profit >= MIN_PROFIT:

            send_discord(
                item,
                resale,
                profit
            )

            save_to_file(
                HISTORY_FILE,
                item["url"]
            )

            print("[SUCCESS] PROFITABLE")


# ========================
# ENTRY POINT
# ========================

if __name__ == "__main__":
    main()
