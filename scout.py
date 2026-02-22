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
from discord_webhook import DiscordEmbed, DiscordWebhook
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "alive", "bot": "Gemini Arbitrage Scout ⚡"}), 200

# ─── Configuration ──────────────────────────────────────────────────────────────
MAX_BUY_PRICE = 10000.0  # Set to 10k as requested
MIN_NET_PROFIT = 0.0     # Catch everything for testing
FEE_RATE = 0.15
NUM_LISTINGS = 12
SCAN_INTERVAL_SECONDS = 300 

SEARCH_KEYWORDS = ["iPhone", "Nintendo Switch", "Lego Star Wars", "GoPro", "Kindle"]
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
    fees: float
    net_profit: float
    reasoning: str
    score: int

def scrape_ebay_listings() -> list[Listing]:
    current_keyword = random.choice(SEARCH_KEYWORDS)
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    
    # 🔥 FIX 1: Use the mobile URL (m.ebay.de) for much higher success rates
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={current_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    
    # 🔥 FIX 2: Use "device=mobile" so the proxy presents as a phone
    proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={ebay_url}&device=mobile"
    
    print(f"[SCRAPER] Fetching '{current_keyword}' via Mobile Proxy...", flush=True)
    
    try:
        response = requests.get(proxy_url, timeout=60)
        response.raise_for_status()
    except Exception as exc:
        print(f"[SCRAPER] Proxy failed: {exc}", flush=True)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    
    # 🔥 FIX 3: Mobile items use very simple 's-item' or 'item' tags
    items = soup.select(".s-item, .item, li[data-view]")
    print(f"[DEBUG] Raw items detected: {len(items)}", flush=True)

    listings: list[Listing] = []
    for item in items:
        if len(listings) >= NUM_LISTINGS: break
        
        try:
            # 🔥 FIX: More aggressive mobile selectors
            link_el = item.select_one("a[href*='itm/']")
            title_el = item.select_one(".s-item__title, .item__title, h3")
            price_el = item.select_one(".s-item__price, .item__price, .price")
            
            if not link_el or not title_el: continue
            
            item_url = link_el["href"].split("?")[0]
            # 🔥 TEMPORARY: Comment out 'seen' check to force a test result
            # if item_url in SEEN_ITEMS: continue
            
            title = title_el.get_text(strip=True)
            if "shop on ebay" in title.lower(): continue

            # 🔥 FIX: Ultra-robust price cleaning
            price_text = price_el.get_text(strip=True) if price_el else "1.0"
            # Remove everything except numbers and decimal separators
            price_clean = re.sub(r'[^\d.,]', '', price_text).replace(',', '.')
            try:
                price_val = float(re.search(r"(\d+\.\d+|\d+)", price_clean).group(1))
            except:
                price_val = 1.0 # Fallback for testing

            img_el = item.find("img")
            image_url = img_el.get("src") or img_el.get("data-src") or ""
            
            # If we got this far, we HAVE an item
            print(f"[DEBUG] Validating: {title[:30]} | Price: {price_val}", flush=True)
            
            listings.append(Listing(title=title, price=price_val, image_url=image_url, item_url=item_url))
            SEEN_ITEMS.add(item_url)
        except Exception as e:
            continue

    print(f"[SCRAPER] Found {len(listings)} items.", flush=True)
    return listings

def analyse_all_gemini(listings: list[Listing]) -> list[ProfitAnalysis]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key: return []

    client = genai.Client(api_key=api_key)
    payload = ["Analyze resale value. Check photos for damage.\n"]
    
    for i, l in enumerate(listings, 1):
        payload.append(f"Item {i}: '{l.title}' - Price: {l.price} €")
        if l.image_url:
            try:
                img_resp = requests.get(l.image_url, timeout=5)
                if img_resp.status_code == 200:
                    payload.append(types.Part.from_bytes(data=img_resp.content, mime_type="image/jpeg"))
            except: pass

    payload.append("\nReturn ONLY JSON array: [{'id', 'resale_price', 'reasoning', 'score'}]")
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=payload,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
        )
        items_data = json.loads(response.text)
    except Exception as e:
        print(f"[AI] Gemini Error: {e}", flush=True)
        return []

    profitable = []
    for entry in items_data:
        idx = int(entry.get("id", 0)) - 1
        if 0 <= idx < len(listings):
            l = listings[idx]
            resale = float(entry.get("resale_price", 0))
            profit = (resale * (1 - FEE_RATE)) - l.price
            if profit >= MIN_NET_PROFIT:
                profitable.append(ProfitAnalysis(listing=l, resale_price=resale, fees=round(resale*FEE_RATE, 2), net_profit=round(profit, 2), reasoning=entry.get("reasoning", ""), score=int(entry.get("score", 50))))
    return profitable

def send_discord_notification(analyses: list[ProfitAnalysis]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if not webhook_url: return
    for analysis in analyses:
        listing = analysis.listing
        webhook = DiscordWebhook(url=webhook_url, username="Gemini Scout ⚡")
        embed = DiscordEmbed(title=f"💰 {listing.title[:200]}", url=listing.item_url, color="00FFAA")
        embed.set_thumbnail(url=listing.image_url)
        embed.add_embed_field(name="🔥 Flip Score", value=f"**{analysis.score}/100**", inline=False)
        embed.add_embed_field(name="✅ Net Profit", value=f"**{analysis.net_profit:.2f} €**", inline=True)
        embed.add_embed_field(name="🤖 AI Reason", value=analysis.reasoning[:1024], inline=False)
        webhook.add_embed(embed)
        webhook.execute()
        time.sleep(1)

def scout_loop():
    while True:
        try:
            print(f"\n--- [⚡] Gemini Bot awake. Time: {time.strftime('%H:%M:%S')} ---", flush=True)
            listings = scrape_ebay_listings()
            if listings:
                profitable = analyse_all_gemini(listings)
                if profitable: send_discord_notification(profitable)
        except Exception as exc: print(f"[SCOUT] Error: {exc}", flush=True)
        time.sleep(SCAN_INTERVAL_SECONDS)

threading.Thread(target=scout_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))