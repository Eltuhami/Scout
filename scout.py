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
    return jsonify({"status": "alive", "bot": "Gemini Scout ⚡"}), 200

# ─── Configuration ──────────────────────────────────────────────────────────────
MAX_BUY_PRICE = 10000.0  
MIN_NET_PROFIT = -100.0   
FEE_RATE = 0.15
NUM_LISTINGS = 1         
SCAN_INTERVAL_SECONDS = 300 

SEARCH_KEYWORDS = ["iPhone", "Nintendo Switch", "Lego Star Wars", "GoPro"]
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
            
            texts = [t.get_text(strip=True) for t in item.find_all(["h3", "h2", "span"]) if len(t.get_text(strip=True)) > 10]
            title = texts[0] if texts else "Unknown Item"
            
            price_text = "1.0"
            for s in item.find_all(string=re.compile(r"EUR|€|\d+,\d+")):
                price_text = s
                break
            
            price_clean = re.sub(r'[^\d.,]', '', price_text).replace('.', '').replace(',', '.')
            price_val = float(re.search(r"(\d+\.\d+|\d+)", price_clean).group(1))

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
        
        # 🔥 THE FIX: Download the image first, then send bytes to Gemini
        if l.image_url.startswith("http"):
            try:
                img_resp = requests.get(l.image_url, timeout=5)
                if img_resp.status_code == 200:
                    payload.append(types.Part.from_bytes(data=img_resp.content, mime_type="image/jpeg"))
            except Exception as e:
                print(f"[AI] Skipping image fetch: {e}", flush=True)

    payload.append("\nReturn JSON array: [{'id': 1, 'resale_price': 50.0, 'reasoning': '...', 'score': 85}]")
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=payload,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
        )
        items_data = json.loads(response.text)
    except Exception as e:
        if "429" in str(e):
            print("[AI] Quota hit. Sleeping 60s...", flush=True)
            time.sleep(60) 
        print(f"[AI] Gemini Error: {e}", flush=True)
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
    for analysis in analyses:
        listing = analysis.listing
        print(f"[DISCORD] Sending ping for: {listing.title[:30]}", flush=True)
        webhook = DiscordWebhook(url=webhook_url, username="Gemini Scout ⚡")
        embed = DiscordEmbed(title=f"💰 {listing.title[:200]}", url=listing.item_url, color="00FFAA")
        if listing.image_url.startswith("http"):
            embed.set_thumbnail(url=listing.image_url)
        embed.add_embed_field(name="🏷️ Buy Price", value=f"**{listing.price:.2f} €**", inline=True)
        embed.add_embed_field(name="✅ Net Profit", value=f"**{analysis.net_profit:.2f} €**", inline=True)
        embed.add_embed_field(name="🤖 AI Reason", value=analysis.reasoning[:1000], inline=False)
        webhook.add_embed(embed)
        
        try:
            webhook.execute()
            print("[DISCORD] Ping successful!", flush=True)
        except Exception as e:
            print(f"[DISCORD] Send failed: {e}", flush=True)

def scout_loop():
    while True:
        try:
            print(f"\n--- [⚡] Gemini Bot awake. ---", flush=True)
            listings = scrape_ebay_listings()
            if listings:
                profitable = analyse_all_gemini(listings)
                if profitable: 
                    send_discord_notification(profitable)
        except Exception as exc: print(f"[SCOUT] Error: {exc}", flush=True)
        time.sleep(SCAN_INTERVAL_SECONDS)

threading.Thread(target=scout_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))