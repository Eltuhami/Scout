import json
import os
import re
import random
import threading
import time
from dataclasses import dataclass
from google import genai
from google.genai import types
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "alive", "bot": "Gemini Scout ⚡"}), 200

# ─── Configuration ──────────────────────────────────────────────────────────────
MAX_BUY_PRICE = 16.0  # Adjusted to fit your 16.81€ balance
MIN_NET_PROFIT = 5.0   # Only ping if you make at least 5€ profit
FEE_RATE = 0.15
NUM_LISTINGS = 3         
SCAN_INTERVAL_SECONDS = 300 

# Keywords focused on high-turnover items in your price range
SEARCH_KEYWORDS = ["Lego Minifigure", "Pokemon Card Holo", "Vintage Casio", "Gameboy Game"]
SEEN_ITEMS = set()

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

def scrape_ebay_listings() -> list[Listing]:
    current_keyword = random.choice(SEARCH_KEYWORDS)
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={current_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={ebay_url}&device=mobile&render=true"
    
    print(f"[SCRAPER] Fetching '{current_keyword}'...", flush=True)
    try:
        response = requests.get(proxy_url, timeout=60)
        response.raise_for_status()
    except Exception as exc:
        print(f"[SCRAPER] Proxy failed: {exc}", flush=True)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.find_all(["li", "div"], class_=re.compile(r"item|s-item|result"))
    print(f"[DEBUG] Containers found: {len(items)}", flush=True)

    listings: list[Listing] = []
    for item in items:
        if len(listings) >= NUM_LISTINGS: break
        try:
            link_el = item.find("a", href=re.compile(r"itm/"))
            if not link_el: continue
            
            item_url = link_el["href"].split("?")[0]
            if item_url in SEEN_ITEMS: continue
            
            # 🔥 FIXED TITLE DETECTION
            title = "Unknown Item"
            title_el = item.find("h3") or item.find("h2")
            if title_el:
                title = title_el.get_text(strip=True).replace("Neues Angebot", "")
            
            price_val = 0.0
            for el in item.find_all(string=re.compile(r"EUR|€|\d+,\d+")):
                text_val = el.get_text(strip=True)
                if "10000" in text_val or "10.000" in text_val: continue
                
                clean_str = re.sub(r'[^\d.,]', '', text_val).replace('.', '').replace(',', '.')
                match = re.search(r"(\d+\.\d+|\d+)", clean_str)
                if match:
                    price_val = float(match.group(1))
                    break 
            
            if price_val == 0.0 or price_val > MAX_BUY_PRICE: continue

            img_el = item.find("img")
            image_url = img_el.get("src") or img_el.get("data-src") or ""
            
            listings.append(Listing(title=title, price=price_val, image_url=image_url, item_url=item_url))
            SEEN_ITEMS.add(item_url)
        except: continue

    print(f"[SCRAPER] Successfully parsed {len(listings)} items.", flush=True)
    return listings

def analyse_all_gemini(listings: list[Listing]) -> list[ProfitAnalysis]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key: return []

    client = genai.Client(api_key=api_key)
    payload = ["Analyze resale value for Vinted. Return JSON array.\n"]
    for i, l in enumerate(listings, 1):
        payload.append(f"Item {i}: '{l.title}' - Price: {l.price} €")
        
        if l.image_url.startswith("http"):
            try:
                img_resp = requests.get(l.image_url, timeout=5)
                if img_resp.status_code == 200:
                    payload.append(types.Part.from_bytes(data=img_resp.content, mime_type="image/jpeg"))
            except Exception as e:
                print(f"[AI] Skipping image fetch: {e}", flush=True)

    payload.append("\nReturn JSON array: [{'id': 1, 'resale_price': 50.0, 'reasoning': '...', 'score': 85}]")
    
    max_retries = 3
    delay = 5
    items_data = []
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash', 
                contents=payload,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
            )
            print(f"[AI] Raw Output: {response.text}", flush=True) 
            items_data = json.loads(response.text)
            break 
            
        except Exception as e:
            if "503" in str(e):
                time.sleep(delay)
                delay *= 2 
            else:
                return []

    profitable = []
    for entry in items_data:
        try:
            raw_id = str(entry.get("id", "0"))
            clean_id = int(re.search(r'\d+', raw_id).group())
            idx = clean_id - 1
            if 0 <= idx < len(listings):
                l = listings[idx]
                resale = float(entry.get("resale_price", 0))
                profit = (resale * (1 - FEE_RATE)) - l.price
                print(f"[DEBUG] Math: {resale}€ resale - {l.price}€ buy = {profit}€ profit", flush=True)
                if profit >= MIN_NET_PROFIT:
                    profitable.append(ProfitAnalysis(
                        listing=l, resale_price=resale, net_profit=round(profit, 2),
                        reasoning=entry.get("reasoning", ""), score=int(entry.get("score", 50))
                    ))
        except: continue
    return profitable

def send_discord_notification(analyses: list[ProfitAnalysis]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if not webhook_url: return
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    }

    for analysis in analyses:
        listing = analysis.listing
        print(f"[DISCORD] Sending stealth ping for: {listing.title[:30]}...", flush=True)
        payload = {
            "username": "Gemini Scout ⚡",
            "embeds": [{
                "title": f"💰 {listing.title[:200]}",
                "url": listing.item_url,
                "color": 65450, 
                "thumbnail": {"url": listing.image_url} if listing.image_url else {},
                "fields": [
                    {"name": "🏷️ Buy Price", "value": f"**{listing.price:.2f} €**", "inline": True},
                    {"name": "✅ Net Profit", "value": f"**{analysis.net_profit:.2f} €**", "inline": True},
                    {"name": "🤖 AI Reason", "value": analysis.reasoning[:1000], "inline": False}
                ]
            }]
        }
        try:
            requests.post(webhook_url, json=payload, headers=headers, timeout=10)
        except: pass

def scout_loop():
    # Only run ONCE per execution for GitHub Actions compatibility
    try:
        print(f"\n--- [⚡] Gemini Bot awake. ---", flush=True)
        listings = scrape_ebay_listings()
        if listings:
            profitable = analyse_all_gemini(listings)
            if profitable: 
                send_discord_notification(profitable)
    except Exception as exc: print(f"[SCOUT] Error: {exc}", flush=True)

if __name__ == "__main__":
    scout_loop()
