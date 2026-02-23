import json
import os
import re
import random
import time
from dataclasses import dataclass
from google import genai
from google.genai import types
import requests
from bs4 import BeautifulSoup

# ─── EDIT ONLY THESE VARIABLES ──────────────────────────────────────────
MAX_BUY_PRICE = 16.0  # Your current bank balance
MIN_NET_PROFIT = 5.0  # Min profit after 15% fees
NUM_LISTINGS = 3      
# ────────────────────────────────────────────────────────────────────────

FEE_RATE = 0.15
HISTORY_FILE = "history.txt"

@dataclass
class Listing:
    title: str
    price: float
    image_url: str
    item_url: str

@dataclass
class ProfitAnalysis:
    listing: Listing
    resale_price: float
    net_profit: float
    reasoning: str
    score: int

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_history(item_url):
    with open(HISTORY_FILE, "a") as f:
        f.write(item_url + "\n")

def get_dynamic_keyword(client):
    prompt = (
        f"You are a professional reseller. My current budget is {MAX_BUY_PRICE}€. "
        f"Suggest ONE specific item or brand that is REALISTICALLY found for under {MAX_BUY_PRICE}€ "
        "on eBay and can be flipped for a profit. DO NOT suggest consoles or iPhones. "
        "Return ONLY the keyword."
    )
    try:
        response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
        return response.text.strip().replace("'", "").replace('"', "")
    except:
        return "Lego Minifigure"

def scrape_ebay_listings(keyword, seen_items) -> list[Listing]:
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={ebay_url}&device=mobile&render=true"
    
    try:
        response = requests.get(proxy_url, timeout=60)
        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.find_all(["li", "div"], class_=re.compile(r"item|s-item|result"))
        
        listings = []
        for item in items:
            if len(listings) >= NUM_LISTINGS: break
            try:
                link_el = item.find("a", href=re.compile(r"itm/"))
                if not link_el: continue
                
                item_url = link_el["href"].split("?")[0]
                if item_url in seen_items: continue # 🔥 Skip already pinged items
                
                title_el = item.find("h3") or item.find("h2")
                title = title_el.get_text(strip=True).replace("Neues Angebot", "") if title_el else "Unknown"
                
                # Price extraction logic
                price_val = 0.0
                for el in item.find_all(string=re.compile(r"EUR|€|\d+,\d+")):
                    t = el.get_text(strip=True)
                    if "10000" in t: continue
                    nums = re.sub(r'[^\d.,]', '', t).replace('.', '').replace(',', '.')
                    match = re.search(r"(\d+\.\d+|\d+)", nums)
                    if match:
                        price_val = float(match.group(1))
                        break
                
                if 0 < price_val <= MAX_BUY_PRICE:
                    img = item.find("img")
                    img_url = img.get("src") or img.get("data-src") or ""
                    listings.append(Listing(title=title, price=price_val, image_url=img_url, item_url=item_url))
            except: continue
        return listings
    except: return []

# ... (analyse_all_gemini and send_discord_notification stay the same)

if __name__ == "__main__":
    api_key = os.getenv("GEMINI_API_KEY", "")
    if api_key:
        client = genai.Client(api_key=api_key)
        seen_items = load_history() # 🔥 Load memory
        keyword = get_dynamic_keyword(client)
        listings = scrape_ebay_listings(keyword, seen_items)
        
        if listings:
            profitable = analyse_all_gemini(listings, client)
            if profitable:
                for p in profitable:
                    send_discord_notification([p])
                    save_history(p.listing.item_url) # 🔥 Save memory
