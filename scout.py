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
# Use exact ID; the SDK handles the 'models/' prefix internally
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
        # No 'models/' prefix needed for this specific SDK call
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        keyword = response.text.strip().replace("'", "").replace('"', "")
        print(f"[SEARCH] AI Keyword: {keyword}", flush=True)
        return keyword
    except Exception as e:
        print(f"[ERROR] AI Keyword failed: {e}", flush=True)
        return "Vintage Lego"

def scrape_ebay_listings(keyword, seen_items) -> list[Listing]:
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={ebay_url}&device=mobile&render=true"
    
    try:
        response = requests.get(proxy_url, timeout=60)
        soup = BeautifulSoup(response.text, "html.parser")
        # 🎯 TARGET ONLY REAL PRODUCT BOXES
        items = soup.find_all("div", class_="s-item__info")
        
        listings = []
        for item in items:
            if len(listings) >= NUM_LISTINGS: break
            try:
                title_el = item.find("div", class_="s-item__title") or item.find("h3")
                title = title_el.get_text(strip=True) if title_el else ""
                
                # 🚮 TRASH FILTER
                trash = ["seite", "pagination", "navigation", "feedback", "altersempfehlung", "benachrichtigungen"]
                if not title or any(x in title.lower() for x in trash):
                    continue

                link_el = item.find("a", class_="s-item__link")
                if not link_el: continue
                item_url = link_el["href"].split("?")[0]
                if item_url in seen_items: continue
                
                price_el = item.find("span", class_="s-item__price")
                if not price_el: continue
                nums = re.sub(r'[^\d.,]', '', price_el.get_text()).replace('.', '').replace(',', '.')
                match = re.search(r"(\d+\.\d+|\d+)", nums)
                price_val = float(match.group(1)) if match else 0.0
                
                if 0 < price_val <= MAX_BUY_PRICE:
                    img_container = item.parent.find("div", class_="s-item__image-wrapper")
                    img = img_container.find("img") if img_container else None
                    img_url = img.get("src") or img.get("data-src") or "" if img else ""
                    listings.append(Listing(title=title, price=price_val, image_url=img_url, item_url=item_url))
            except: continue
        return listings
    except: return []

def analyse_all_gemini(listings: list[Listing], client) -> list:
    profitable_deals = []
    for l in listings:
        print(f"[AI] Analyzing real item: {l.title}...", flush=True)
        prompt = f"Estimate resale for '{l.title}' at {l.price}€. Return JSON: [{{'resale_price': 35.0, 'reasoning': '...', 'score': 85}}]"
        
        payload = [prompt]
        if l.image_url and l.image_url.startswith("http"):
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
                profitable_deals.append({"listing": l, "profit": round(profit, 2)})
        except Exception as e:
            print(f"[AI ERROR] {e}", flush=True)
            
    return profitable_deals

if __name__ == "__main__":
    print("--- [START] Bot Active ---", flush=True)
    api_key = os.getenv("GEMINI_API_KEY", "")
    if api_key:
        # Force the stable version
        client = genai.Client(api_key=api_key)
        seen = load_history()
        keyword = get_dynamic_keyword(client)
        items = scrape_ebay_listings(keyword, seen)
        
        if items:
            print(f"[INFO] Scanning {len(items)} real products...", flush=True)
            deals = analyse_all_gemini(items, client)
            if deals:
                webhook_url = os.getenv("DISCORD_WEBHOOK", "")
                for d in deals:
                    l = d["listing"]
                    payload = {"embeds": [{"title": f"💰 DEAL: {l.title[:100]}", "url": l.item_url, "color": 65450,
                               "fields": [{"name": "Price", "value": f"{l.price}€", "inline": True},
                                          {"name": "Profit", "value": f"{d['profit']}€", "inline": True}]}]}
                    requests.post(webhook_url, json=payload)
                    save_history(l.item_url)
            else: print("[INFO] No profitable items found.", flush=True)
        else: print("[INFO] No new items found on eBay.", flush=True)
    print("--- [FINISH] Cycle Complete ---", flush=True)
