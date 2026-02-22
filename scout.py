"""
Arbitrage Scout Bot - Gemini High-Speed Edition
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

# ─── Flask App ───────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "alive", "bot": "Gemini Arbitrage Scout ⚡"}), 200

@app.route("/health")
def health():
    return "OK", 200

# ─── Configuration ──────────────────────────────────────────────────────────────
MAX_BUY_PRICE = 10000
MIN_NET_PROFIT = 0
FEE_RATE = 0.15
NUM_LISTINGS = 12
SCAN_INTERVAL_SECONDS = 180  # 🔥 Upgraded: Scans every 3 minutes now!

SEARCH_KEYWORDS = [
    "nintendo", "sony", "ipod", "gameboy", "playstation",
    "logitech", "sennheiser", "garmin", "jbl", "bose",
    "kindle", "polaroid", "tamagotchi", "gopro"
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
]

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

def send_alert_to_discord(message: str):
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if webhook_url:
        try:
            DiscordWebhook(url=webhook_url, content=message).execute()
        except Exception:
            pass

def scrape_ebay_listings() -> list[Listing]:
    current_keyword = random.choice(SEARCH_KEYWORDS)
    # 🔥 FIX: Force the price to be a clean integer in the URL to prevent eBay errors
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    
    # 🔥 Switch to eBay Austria for local deals
    ebay_url = f"https://www.ebay.at/sch/i.html?_nkw={current_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}&_ipg=60&rt=nc"
    
    # 🔥 Route the request through the automated proxy
    proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={ebay_url}"
    
    print(f"[SCRAPER] Fetching '{current_keyword}' via Proxy …", flush=True)
    
    try:
        # 🔥 Drop the fake headers, the proxy handles all Captchas automatically
        response = requests.get(proxy_url, timeout=45)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    
    # 🔥 DEBUGGER: This will print exactly what page eBay is forcing the bot to look at
    page_title = soup.title.get_text(strip=True) if soup.title else "No Title Found"
    print(f"[DEBUG] eBay Page Title: {page_title}", flush=True)

    if any(warn in response.text.lower() for warn in ["captcha", "pardon our interruption", "security measure"]):
        print("!!! [ALERT] eBay Captcha triggered !!!", flush=True)
        send_alert_to_discord("🚨 **eBay Security Block!** The bot hit a Captcha. Resting until next cycle.")
        return []

    items = soup.select("li.s-item")
    listings: list[Listing] = []

    for item in items:
        if len(listings) >= NUM_LISTINGS: break
        
        link_el = item.select_one("a.s-item__link")
        item_url = link_el["href"] if link_el else ""
        clean_url = item_url.split("?")[0] if item_url else ""
        
        if not clean_url or clean_url in SEEN_ITEMS: continue

        title_el = item.select_one(".s-item__title")
        if not title_el: continue
        title = title_el.get_text(strip=True)
        if title.lower() in ("shop on ebay", "ergebnisse", ""): continue

        price_el = item.select_one(".s-item__price")
        if not price_el: continue
        price_match = re.search(r"([\d]+[.,]?\d*)", price_el.get_text(strip=True).replace(".", "").replace(",", "."))
        if not price_match: continue
        price = float(price_match.group(1))
        if price <= 0 or price > MAX_BUY_PRICE: continue

        img_el = item.select_one(".s-item__image-wrapper img")
        image_url = img_el.get("src", "") or img_el.get("data-src", "") or "" if img_el else ""

        SEEN_ITEMS.add(clean_url)
        listings.append(Listing(title=title, price=price, image_url=image_url, item_url=item_url))

    if len(SEEN_ITEMS) > 1000: SEEN_ITEMS.clear()
    print(f"[SCRAPER] Found {len(listings)} NEW listing(s).", flush=True)
    return listings

def analyse_all_gemini(listings: list[Listing]) -> list[ProfitAnalysis]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        print("[AI] ERROR — GEMINI_API_KEY is not set.", flush=True)
        return []

    # 🔥 FIX: Using the new SDK syntax here
    client = genai.Client(api_key=api_key)
    
    # 🔥 The payload now holds BOTH text instructions and raw image files
    payload = [
        "You are a strict resale pricing expert for the European second-hand market.\n"
        "Evaluate the following items. Look closely at the provided images to spot defects (scratches, cracks, missing parts, or if it is just an empty 'OVP' box).\n"
        "If the image is blurry, does not show the actual item, or looks like a stock photo from Google, severely lower the 'score'.\n\n"
    ]
    
    for i, l in enumerate(listings, 1):
        payload.append(f"Item {i}: Title: '{l.title}' — Buy Price: {l.price:.2f} €")
        
        # 🔥 Download the eBay image secretly and give it to the AI
        if l.image_url:
            try:
                img_resp = requests.get(l.image_url, timeout=5)
                if img_resp.status_code == 200:
                    # 🔥 FIX: New SDK image format
                    payload.append(
                        types.Part.from_bytes(data=img_resp.content, mime_type="image/jpeg")
                    )
            except Exception:
                pass

    payload.append(
        "\nReturn ONLY a JSON array of objects with strictly these keys:\n"
        "- 'id' (integer matching the item number)\n"
        "- 'resale_price' (float)\n"
        "- 'reasoning' (2 sentences: Who is the buyer? What did you see in the photo regarding condition?)\n"
        "- 'score' (1-100 rating. Deduct massive points for empty boxes, damage, or stock photos)."
    )

    print(f"[AI] Handing Gemini {len(listings)} items WITH images to evaluate...", flush=True)
    
    try:
        # 🔥 FIX: New SDK generate_content syntax
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=payload,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2
            )
        )
        items_data = json.loads(response.text)
    except Exception as exc:
        print(f"[AI] Gemini API error: {exc}", flush=True)
        return []

    profitable: list[ProfitAnalysis] = []
    for entry in items_data:
        idx = int(entry.get("id", 0)) - 1
        if idx < 0 or idx >= len(listings): continue

        listing = listings[idx]
        resale_price = float(entry.get("resale_price", 0))
        reasoning = str(entry.get("reasoning", ""))
        score = int(entry.get("score", 50))
        
        if resale_price <= 0: continue

        revenue_after_fees = resale_price * (1 - FEE_RATE)
        fees = resale_price * FEE_RATE
        net_profit = revenue_after_fees - listing.price

        if net_profit >= MIN_NET_PROFIT:
            profitable.append(ProfitAnalysis(listing=listing, resale_price=resale_price, fees=round(fees, 2), net_profit=round(net_profit, 2), reasoning=reasoning, score=score))

    return profitable

def send_discord_notification(analyses: list[ProfitAnalysis]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if not webhook_url: return

    for analysis in analyses:
        listing = analysis.listing
        webhook = DiscordWebhook(url=webhook_url, username="Gemini Scout ⚡")
        embed = DiscordEmbed(title=f"💰 {listing.title[:200]}", url=listing.item_url, color="00FFAA")
        embed.set_thumbnail(url=listing.image_url)
        
        # 🔥 Add the new score to the top of the embed
        embed.add_embed_field(name="🔥 Flip Score", value=f"**{analysis.score}/100**", inline=False)
        
        embed.add_embed_field(name="🏷️ Buy Price", value=f"**{listing.price:.2f} €**", inline=True)
        embed.add_embed_field(name="📈 Resale Price", value=f"**{analysis.resale_price:.2f} €**", inline=True)
        embed.add_embed_field(name="✅ Net Profit", value=f"**{analysis.net_profit:.2f} €**", inline=True)
        embed.add_embed_field(name="🤖 Gemini Strategy", value=analysis.reasoning[:1024] or "N/A", inline=False)
        
        embed.set_footer(text="Arbitrage Scout Bot • Gemini API ⚡")
        embed.set_timestamp()
        webhook.add_embed(embed)

        try:
            webhook.execute()
            print(f"[DISCORD] Embed sent for '{listing.title[:50]}'", flush=True)
        except Exception as exc:
            print(f"[DISCORD] Failed to send embed: {exc}", flush=True)
        time.sleep(1)

def run_scout_cycle():
    print("=" * 60, flush=True)
    listings = scrape_ebay_listings()
    if not listings:
        print("[SCOUT] No new listings found.", flush=True)
        return

    profitable = analyse_all_gemini(listings)
    if not profitable:
        print("[SCOUT] No profitable items this cycle.", flush=True)
        return

    send_discord_notification(profitable)
    print(f"[SCOUT] Done — {len(profitable)} alert(s) sent.", flush=True)

def scout_loop():
    while True:
        try:
            print(f"\n--- [⚡] Gemini Bot awake. Time: {time.strftime('%H:%M:%S')} ---", flush=True)
            run_scout_cycle()
        except Exception as exc:
            print(f"[SCOUT] Unhandled error: {exc}", flush=True)
        
        print(f"[SCOUT] Sleeping {SCAN_INTERVAL_SECONDS // 60} min …\n", flush=True)
        time.sleep(SCAN_INTERVAL_SECONDS)

def start_background_scout():
    threading.Thread(target=scout_loop, daemon=True).start()

start_background_scout()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))