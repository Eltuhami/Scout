import json
import os
import re
import requests
from dataclasses import dataclass
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

# ─── CONFIGURATION ──────────────────────────────────────────────────────
MAX_BUY_PRICE = 16.0    
MIN_NET_PROFIT = 5.0    
NUM_LISTINGS = 3        
# 🔥 This exact string is required to fix your 404 Error
MODEL_NAME = "gemini-1.5-flash" 
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
    prompt = f"Suggest ONE specific collectible under {MAX_BUY_PRICE}€. Return ONLY the keyword."
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        keyword = response.text.strip().replace("'", "").replace('"', "")
        print(f"[SEARCH] AI Keyword: {keyword}", flush=True)
        return keyword
    except Exception as e:
        print(f"[ERROR] AI Keyword failed: {e}", flush=True)
        return "Lego Star Wars"

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
                title_el = item.find("h3") or item.find("h2")
                title = title_el.get_text(strip=True) if title_el else ""
                
                # 🚮 Improved Filter to ignore eBay navigation text
                trash = ["seite", "pagination", "navigation", "feedback", "altersempfehlung", "benachrichtigungen"]
                if any(x in title.lower() for x in trash):
                    continue

                link_el = item.find("a", href=re.compile(r"itm/"))
                if not link_el: continue
                item_url = link_el["href"].split("?")[0]
                if item_url in seen_items: continue
                
                price_val = 0.0
                for el in item.find_all(string=re.compile(r"EUR|€|\d+,\d+")):
                    nums = re.sub(r'[^\d.,]', '', el).replace('.', '').replace(',', '.')
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

def analyse_all_gemini(listings: list[Listing], client) -> list[ProfitAnalysis]:
    profitable_deals = []
    for l in listings:
        print(f"[AI] Analyzing: {l.title}...", flush=True)
        prompt = f"Estimate resale for '{l.title}' at {l.price}€. Return JSON: [{{'resale_price': 30.0, 'reasoning': '...', 'score': 80}}]"
        
        payload = [prompt]
        if l.image_url:
            try:
                img_data = requests.get(l.image_url, timeout=5).content
                payload.append(types.Part.from_bytes(data=img_data, mime_type="image/jpeg"))
            except: pass

        try:
            response = client.models.generate_content(
                model=MODEL_NAME, 
                contents=payload, 
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            data = json.loads(response.text)
            entry = data[0] if isinstance(data, list) else data
            resale = float(entry.get("resale_price", 0))
            profit = (resale * (1 - FEE_RATE)) - l.price
            
            if profit >= MIN_NET_PROFIT:
                profitable_deals.append(ProfitAnalysis(l, resale, round(profit, 2), entry.get("reasoning", ""), int(entry.get("score", 50))))
        except Exception as e:
            print(f"[AI ERROR] {e}", flush=True)
            
    return profitable_deals

def send_discord_notification(analyses: list[ProfitAnalysis]):
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if not webhook_url: return
    for a in analyses:
        payload = {
            "embeds": [{
                "title": f"💰 DEAL: {a.listing.title[:100]}", 
                "url": a.listing.item_url, 
                "color": 65450,
                "thumbnail": {"url": a.listing.image_url},
                "fields": [
                    {"name": "Price", "value": f"{a.listing.price}€", "inline": True},
                    {"name": "Profit", "value": f"{a.net_profit}€", "inline": True}
                ]
            }]
        }
        requests.post(webhook_url, json=payload)

if __name__ == "__main__":
    print("--- [START] Bot Active ---", flush=True)
    api_key = os.getenv("GEMINI_API_KEY", "")
    if api_key:
        client = genai.Client(api_key=api_key)
        seen = load_history()
        keyword = get_dynamic_keyword(client)
        items = scrape_ebay_listings(keyword, seen)
        if items:
            deals = analyse_all_gemini(items, client)
            if deals:
                send_discord_notification(deals)
                for d in deals: save_history(d.listing.item_url)
            else: print("[INFO] No profitable items found.", flush=True)
        else: print("[INFO] No new items found on eBay.", flush=True)
    print("--- [FINISH] Cycle Complete ---", flush=True)
