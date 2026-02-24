import os
import random
import json
import time
import requests
from bs4 import BeautifulSoup
from google import genai

# ========================
# CORE CONFIG
# ========================

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

MODEL = "gemini-1.5-flash"

MAX_BUY = 16.0
MIN_PROFIT = 5.0
FEE = 0.15

CYCLES_PER_RUN = 4
KEYWORDS_PER_CYCLE = 3
LISTINGS_PER_KEYWORD = 6

COOLDOWN_MIN = 20
COOLDOWN_MAX = 40

HISTORY_FILE = "history.txt"
STATS_FILE = "keyword_stats.json"

client = genai.Client(api_key=GEMINI_API_KEY)

# ========================
# KEYWORDS
# ========================

BASE_KEYWORDS = [

    "Sony Walkman",
    "iPod Nano",
    "iPod Classic",

    "Nintendo DS",
    "Gameboy",

    "Pokemon Sammlung",
    "YuGiOh Sammlung",

    "Casio Uhr Vintage",
    "Seiko Uhr",

    "Polaroid Kamera",

    "Lego Star Wars",
    "Lego Minifiguren",

    "Funko Pop",

    "Retro Taschenrechner"
]

BAD_WORDS = [
    "hülle","case","kabel",
    "adapter","manual",
    "anleitung","dvd",
    "cd","buch","ersatzteil"
]

# ========================
# FILE HELPERS
# ========================

def load_set(file):

    if not os.path.exists(file):
        return set()

    with open(file,"r") as f:
        return set(f.read().splitlines())


def save_line(file,line):

    with open(file,"a") as f:
        f.write(line+"\n")


def load_stats():

    if not os.path.exists(STATS_FILE):

        return {
            k:{"profit":0,"checked":0}
            for k in BASE_KEYWORDS
        }

    with open(STATS_FILE,"r") as f:
        return json.load(f)


def save_stats(stats):

    with open(STATS_FILE,"w") as f:
        json.dump(stats,f)

# ========================
# SMART KEYWORD SELECTION
# ========================

def choose_keywords(stats):

    scored=[]

    for k,v in stats.items():

        score=v["profit"]-(v["checked"]*0.05)

        scored.append((score,k))

    scored.sort(reverse=True)

    best=[k for _,k in scored[:10]]

    return random.sample(best,min(KEYWORDS_PER_CYCLE,len(best)))

# ========================
# SCRAPER
# ========================

def scrape(keyword):

    url=(
        "https://www.ebay.de/sch/i.html"
        f"?_nkw={keyword}"
        "&LH_BIN=1"
        "&_udhi=16"
        "&_sop=15"
    )

    try:

        r=requests.get(
            "http://api.scraperapi.com",
            params={
                "api_key":SCRAPER_API_KEY,
                "url":url
            },
            timeout=60
        )

        soup=BeautifulSoup(r.text,"html.parser")

        listings=[]

        for item in soup.select("li.s-item"):

            title=item.select_one(".s-item__title")

            price=item.select_one(".s-item__price")

            link=item.select_one("a.s-item__link")

            if not title or not price or not link:
                continue

            title_text=title.text.strip()

            if any(x in title_text.lower() for x in BAD_WORDS):
                continue

            try:
                price_value=float(
                    price.text
                    .replace("€","")
                    .replace(",",".")
                    .split()[0]
                )
            except:
                continue

            if price_value>MAX_BUY:
                continue

            listings.append({
                "title":title_text,
                "price":price_value,
                "url":link["href"]
            })

            if len(listings)>=LISTINGS_PER_KEYWORD:
                break

        print("[SCRAPE]",keyword,len(listings))

        return listings

    except Exception as e:

        print("[SCRAPE ERROR]",e)
        return []

# ========================
# GEMINI
# ========================

def estimate(listings):

    if not listings:
        return []

    prompt="Estimate resale prices in EURO. Only numbers comma separated.\n\n"

    for i,x in enumerate(listings):
        prompt+=f"{i+1}. {x['title']}\n"

    try:

        r=client.models.generate_content(
            model=MODEL,
            contents=prompt
        )

        return [
            float(x.strip())
            for x in r.text.split(",")
        ]

    except:

        return []

# ========================
# PROFIT
# ========================

def profit(buy,resale):

    return round(resale*(1-FEE)-buy,2)

# ========================
# ALERT
# ========================

def alert(item,resale,p):

    msg=(
        f"🔥 Flip Found\n\n"
        f"{item['title']}\n"
        f"Buy: {item['price']}€\n"
        f"Resale: {resale}€\n"
        f"Profit: {p}€\n\n"
        f"{item['url']}"
    )

    try:
        requests.post(DISCORD_WEBHOOK,json={"content":msg})
    except:
        pass

# ========================
# ONE CYCLE
# ========================

def cycle(stats,history):

    keywords=choose_keywords(stats)

    print("[CYCLE]",keywords)

    for keyword in keywords:

        listings=scrape(keyword)

        if not listings:
            continue

        prices=estimate(listings)

        if len(prices)!=len(listings):
            continue

        for item,resale in zip(listings,prices):

            stats[keyword]["checked"]+=1

            if item["url"] in history:
                continue

            p=profit(item["price"],resale)

            print("[CHECK]",p)

            if p>=MIN_PROFIT:

                stats[keyword]["profit"]+=1

                alert(item,resale,p)

                save_line(HISTORY_FILE,item["url"])

# ========================
# MAIN LOOP
# ========================

def main():

    stats=load_stats()

    history=load_set(HISTORY_FILE)

    for i in range(CYCLES_PER_RUN):

        print(f"[RUN CYCLE {i+1}/{CYCLES_PER_RUN}]")

        cycle(stats,history)

        save_stats(stats)

        cooldown=random.randint(
            COOLDOWN_MIN,
            COOLDOWN_MAX
        )

        print("[COOLDOWN]",cooldown)

        time.sleep(cooldown)

# ========================

if __name__=="__main__":
    main()
