"""
Arbitrage Scout Bot - 2026 Gemini High-Speed Edition
"""

import json
import os
import re
import random
import threading
import time
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types
import requests
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook
from flask import Flask, jsonify

# ─── Flask App for Render ────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "alive", "bot": "Gemini Arbitrage Scout ⚡"}), 200

# ─── Configuration ──────────────────────────────────────────────────────────────
MAX_BUY_PRICE = 10000.0  
MIN_NET_PROFIT = 0.0     # Set to 0.0 for diagnostic mode
FEE_RATE = 0.15
NUM_LISTINGS = 12
SCAN_INTERVAL_SECONDS = 300 

# High-volume diagnostic keywords to guarantee a first result
SEARCH_KEYWORDS = ["iPhone", "Nintendo Switch", "Lego Star Wars", "Pokemon Karten", "GoPro"]

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
    
    # 1. Using eBay.de Mobile URL for much easier scraping
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={current_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    
    # 2. Force mobile device presentation to bypass complex desktop JS
    proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={ebay_url}&device=mobile"
    
    print(f"[SCRAPER] Fetching '{current_keyword}' via Mobile Proxy...", flush=True)
    
    try:
        response = requests.get(proxy_url, timeout=60)
        response.raise_for_status()
    except Exception as exc:
        print(f"[SCRAPER] Proxy failed: {exc}", flush=True)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    
    # Mobile items use simple .item or .s-item wrappers
    items = soup.select(".s-item, .item, li[data-view]")
    print(f"[DEBUG] Raw items detected: {len(items)}", flush=True)

    listings: list[Listing] = []
    for item in items:
        if len(listings) >= NUM_LISTINGS: break
        
        try:
            link_el = item.select_one("a[href*='itm/']")
            title_el = item.select_one(".s-item__title, .item__title, h3")
            price_el = item.select_one(".s-item__price, .item__price, .price")
            
            if not link_el or not title_el: continue
            
            item_url = link_el["href"].split("?")[0]
            if item_url in SEEN_ITEMS: continue
            
            title = title_el.get_text(strip=True)
            if "shop on ebay" in title.lower(): continue

            # Robust Price Parsing (handles 'EUR 20,00' or '20.00 €')
            price_text = price_el.get_text(strip=True) if price_el else "1.0"
            price_clean = re.sub(r'[^\d.,]', '', price_text).replace('.', '').replace(',', '.')
            try:
                price_val = float(re.search(r"(\d+\.\d+|\d+)", price_clean).group(1))
            except:
                price_val = 1.0 # Diagnostic fallback

            img_el = item.find("img")
            image_url = img_el.get("src") or img_el.get("data-src") or ""
            
            print(f"[DEBUG] Validated: {title[:20]} | {price_val}€", flush=True)
            
            listings.append(Listing(title=title, price=price_val, image_url=image_url, item_url=item_url))
            SEEN_ITEMS.add(item_url)
        except:
            continue

    print(f"[SCRAPER] Found {len(listings)} items.", flush=True)
    return listings

def analyse_all_gemini(listings: list[Listing]) -> list[ProfitAnalysis]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key: return []

    # 2026 SDK Syntax
    client = genai.Client(api_key=api_key)
    payload = ["Evaluate these items for resale on Vinted. Check photo condition.\n"]
    
    for i, l in enumerate(listings, 1):
        payload.append(f"Item {i}: '{l.title}' - Price: {l.price} €")
        if l.image_url:
            try:
                img_resp = requests.get(l.image_url, timeout=5)
                if img_resp.status_code == 200:
                    payload.append(types.Part.from_bytes(data=img_resp.content, mime_type="image/jpeg"))
            except: pass

    payload.append("\nReturn JSON array: [{'id', 'resale_price', 'reasoning', 'score'}]")
    
    try:
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
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
                profitable.append(ProfitAnalysis(
                    listing=l, 
                    resale_price=resale, 
                    fees=round(resale*FEE_RATE, 2), 
                    net_profit=round(profit, 2), 
                    reasoning=entry.get("reasoning", ""), 
                    score=int(entry.get("score", 50))
                ))
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
        embed.add_embed_field(name="🤖 AI Reasoning", value=analysis.reasoning[:1024], inline=False)
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
                if profitable: 
                    send_discord_notification(profitable)
                    print(f"[SCOUT] Sent {len(profitable)} alerts.", flush=True)
        except Exception as exc: 
            print(f"[SCOUT] Global Error: {exc}", flush=True)
        time.sleep(SCAN_INTERVAL_SECONDS)

# Start background thread
threading.Thread(target=scout_loop, daemon=True).start()

if __name__ == "__main__":
    # Render requires binding to 0.0.0.0 and port 10000
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))