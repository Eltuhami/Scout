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

# ─── CONFIGURATION (EDITABLE) ──────────────────────────────────────────
MAX_BUY_PRICE = 16.0    # Your starting budget
MIN_NET_PROFIT = 5.0    # Minimum profit after fees
NUM_LISTINGS = 3        # Increased to find deals 3x faster
MODEL_NAME = "gemini-1.5-flash" # 1,500 free daily requests + Vision
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
        f"You are a professional reseller. My budget is {MAX_BUY_PRICE}€. "
        "Suggest ONE specific collectible or item (e.g. 'Lego Star Wars', 'Pokemon card') "
        "that often sells for under my budget. No phones. Return ONLY the keyword."
    )
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        keyword = response.text.strip().replace("'", "").replace('"', "")
        print(f"[SEARCH] AI suggested keyword: {keyword}", flush=True)
        return keyword
    except Exception as e:
        print(f"[ERROR] Keyword generation failed: {e}", flush=True)
        return "Lego Minifigure"

def scrape_ebay_listings(keyword, seen_items) -> list[Listing]:
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={ebay_url}&device=mobile&render=true"
    
    print(f"[SCRAPER] Fetching items for '{keyword}'...", flush=True)
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
                if item_url in seen_items: continue
                
                title_el = item.find("h3") or item.find("h2")
                title = title_el.get_text(strip=True).replace("Neues Angebot", "") if title_el else "Unknown"
                
                price_val = 0.0
                for el in item.find_all(string=re.compile(r"EUR|€|\d+,\d+")):
                    t = el.get_text(strip=True)
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
        
        print(f"[SCRAPER] Found {len(listings)} new items under budget.", flush=True)
        return listings
    except Exception as e:
        print(f"[ERROR] Scraper failed: {e}", flush=True)
        return []

def analyse_all_gemini(listings: list[Listing], client) -> list[ProfitAnalysis]:
    profitable_deals = []
    for l in listings:
        print(f"[AI] Analyzing: {l.title} ({l.price}€)", flush=True)
        
        prompt = (
            f"Inspect this item: '{l.title}' listed for {l.price}€. "
            "1. Check the image for damage or missing parts. "
            "2. Estimate the resale price on Vinted. "
            "3. If profitable, return JSON: [{'resale_price': 40.0, 'reasoning': '...', 'score': 90}] "
            "Otherwise return an empty list []."
        )
        
        payload = [prompt]
        if l.image_url:
            try:
                img_data = requests.get(l.image_url, timeout=5).content
                payload.append(types.Part.from_bytes(data=img_data, mime_type="image/jpeg"))
            except: 
                print(f"[WARNING] Could not load image for {l.title}", flush=True)

        try:
            response = client.models.generate_content(
                model=MODEL_NAME, 
                contents=payload,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            data = json.loads(response.text)
            if not data: continue
            
            entry = data[0] if isinstance(data, list) else data
            resale = float(entry.get("resale_price", 0))
            profit = (resale * (1 - FEE_RATE)) - l.price
            
            print(f"[DEBUG] Predicted Profit: {profit:.2f}€", flush=True)
            
            if profit >= MIN_NET_PROFIT:
                profitable_deals.append(ProfitAnalysis(
                    listing=l, resale_price=resale, net_profit=round(profit, 2),
                    reasoning=entry.get("reasoning", ""), score=int(entry.get("score", 50))
                ))
        except Exception as e:
            print(f"[AI ERROR] Analysis failed for {l.title}: {e}", flush=True)
            
    return profitable_deals

def send_discord_notification(analyses: list[ProfitAnalysis]):
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if not webhook_url: return

    for analysis in analyses:
        l = analysis.listing
        payload = {
            "embeds": [{
                "title": f"💰 DEAL FOUND: {l.title[:100]}",
                "url": l.item_url,
                "color": 65450,
                "thumbnail": {"url": l.image_url},
                "fields": [
                    {"name": "🏷️ Buy", "value": f"{l.price}€", "inline": True},
                    {"name": "📈 Sell", "value": f"{analysis.resale_price}€", "inline": True},
                    {"name": "✅ Profit", "value": f"**{analysis.net_profit}€**", "inline": True},
                    {"name": "🤖 Reason", "value": analysis.reasoning[:200]}
                ]
            }]
        }
        requests.post(webhook_url, json=payload)
        print(f"[DISCORD] Notification sent for {l.title}", flush=True)

if __name__ == "__main__":
    print("--- [START] Gemini Scout Bot Waking Up ---", flush=True)
    api_key = os.getenv("GEMINI_API_KEY", "")
    
    if not api_key:
        print("[CRITICAL] No GEMINI_API_KEY found!", flush=True)
    else:
        client = genai.Client(api_key=api_key)
        seen_items = load_history()
        
        keyword = get_dynamic_keyword(client)
        listings = scrape_ebay_listings(keyword, seen_items)
        
        if listings:
            deals = analyse_all_gemini(listings, client)
            if deals:
                send_discord_notification(deals)
                for d in deals:
                    save_history(d.listing.item_url)
            else:
                print("[INFO] No deals met the profit threshold.", flush=True)
        else:
            print("[INFO] No new items found to analyze.", flush=True)

    print("--- [FINISH] Bot Cycle Complete ---", flush=True)
