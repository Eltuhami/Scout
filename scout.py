"""
Arbitrage Scout Bot (100% Free Stack) - Stealth & Memory Upgrade
"""

import json
import os
import re
import random
import threading
import time
from dataclasses import dataclass
from typing import Optional

from huggingface_hub import InferenceClient
import requests
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook
from flask import Flask, jsonify

# ─── Flask App ───────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "alive", "bot": "Arbitrage Scout 🔍"}), 200

@app.route("/health")
def health():
    return "OK", 200

# ─── Configuration ──────────────────────────────────────────────────────────────
MAX_BUY_PRICE = 14.0  # Lowered to 14€ to guarantee shipping stays under your 19€ limit
MIN_NET_PROFIT = 10.0
FEE_RATE = 0.15
NUM_LISTINGS = 10
SCAN_INTERVAL_SECONDS = 1200

HF_MODEL = "mistralai/Mistral-7B-Instruct-v0.2"

EBAY_SEARCH_URL = (
    "https://www.ebay.de/sch/i.html"
    "?_nkw=sony&_sop=10&LH_BIN=1&_udhi=15&_ipg=60&rt=nc"
)

# Rotate these to avoid eBay blocking the Render IP
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
]

SEEN_ITEMS = set() # The bot's memory bank

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

def send_alert_to_discord(message: str):
    """Sends a plain text alert to Discord for errors like Captchas."""
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if webhook_url:
        try:
            webhook = DiscordWebhook(url=webhook_url, content=message)
            webhook.execute()
        except Exception:
            pass

def scrape_ebay_listings() -> list[Listing]:
    print(f"[SCRAPER] Fetching newest eBay.de listings (max {MAX_BUY_PRICE} €) …", flush=True)
    
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }

    try:
        response = requests.get(EBAY_SEARCH_URL, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"[SCRAPER] Request failed: {exc}", flush=True)
        return []

    # CAPTCHA DETECTION ALARM
    page_text = response.text.lower()
    if "captcha" in page_text or "pardon our interruption" in page_text or "security measure" in page_text:
        print("!!! [ALERT] eBay Captcha triggered !!!", flush=True)
        send_alert_to_discord("🚨 **eBay Security Block!** The bot hit a Captcha. It is resting until the next cycle.")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select("li.s-item")
    listings: list[Listing] = []

    for item in items:
        if len(listings) >= NUM_LISTINGS: break
        
        # Link & Memory Check
        link_el = item.select_one("a.s-item__link")
        item_url = link_el["href"] if link_el else ""
        clean_url = item_url.split("?")[0] if item_url else "" # Removes tracking junk
        
        if not clean_url or clean_url in SEEN_ITEMS: 
            continue # Skip if already seen

        title_el = item.select_one(".s-item__title")
        if not title_el: continue
        title = title_el.get_text(strip=True)
        if title.lower() in ("shop on ebay", "ergebnisse", ""): continue

        price_el = item.select_one(".s-item__price")
        if not price_el: continue
        price_text = price_el.get_text(strip=True)
        price_match = re.search(r"([\d]+[.,]?\d*)", price_text.replace(".", "").replace(",", "."))
        if not price_match: continue
        price = float(price_match.group(1))
        if price <= 0 or price > MAX_BUY_PRICE: continue

        img_el = item.select_one(".s-item__image-wrapper img")
        image_url = img_el.get("src", "") or img_el.get("data-src", "") or "" if img_el else ""

        # Add to memory
        SEEN_ITEMS.add(clean_url)
        listings.append(Listing(title=title, price=price, image_url=image_url, item_url=item_url))

    # Keep memory from crashing the server
    if len(SEEN_ITEMS) > 1000:
        SEEN_ITEMS.clear()

    print(f"[SCRAPER] Found {len(listings)} NEW listing(s).", flush=True)
    return listings

def _build_batch_prompt(listings: list[Listing]) -> str:
    items_block = "\n".join(f"  {i}. \"{l.title}\"  —  {l.price:.2f} €" for i, l in enumerate(listings, 1))
    return (
        "<s>[INST]\n"
        "You are a resale pricing expert for the European second-hand market (Vinted, Kleinanzeigen).\n\n"
        "I found these items on eBay.de:\n"
        f"{items_block}\n\n"
        "For EACH item, estimate the realistic HIGH resale price on Vinted and give a 1-sentence reasoning.\n\n"
        "Respond ONLY with a JSON array (no markdown fences, no extra text):\n"
        '[{"id": 1, "resale_price": <number>, "reasoning": "<string>"}, ...]\n'
        "[/INST]"
    )

def _parse_batch_response(raw: str, listings: list[Listing]) -> list[ProfitAnalysis]:
    arr_match = re.search(r"\[\s*\{.*\}\s*\]", raw, re.DOTALL)
    if not arr_match:
        print("[AI] Could not locate JSON array in model response.", flush=True)
        return []

    try:
        items_data = json.loads(arr_match.group())
    except json.JSONDecodeError as exc:
        print(f"[AI] JSON decode error: {exc}", flush=True)
        return []

    profitable: list[ProfitAnalysis] = []
    for entry in items_data:
        idx = int(entry.get("id", 0)) - 1
        if idx < 0 or idx >= len(listings): continue

        listing = listings[idx]
        resale_price = float(entry.get("resale_price", 0))
        reasoning = str(entry.get("reasoning", ""))
        if resale_price <= 0: continue

        revenue_after_fees = resale_price * (1 - FEE_RATE)
        fees = resale_price * FEE_RATE
        net_profit = revenue_after_fees - listing.price

        print(f"  [{idx + 1}] {listing.title[:50]}  →  Resale {resale_price:.2f} € | Profit {net_profit:.2f} €", flush=True)

        if net_profit >= MIN_NET_PROFIT:
            profitable.append(ProfitAnalysis(listing=listing, resale_price=resale_price, fees=round(fees, 2), net_profit=round(net_profit, 2), reasoning=reasoning))

    return profitable

def analyse_all(listings: list[Listing]) -> list[ProfitAnalysis]:
    hf_token = os.getenv("HF_TOKEN", "")
    if not hf_token:
        print("[AI] ERROR — HF_TOKEN is not set. Skipping analysis.", flush=True)
        return []

    client = InferenceClient(model=HF_MODEL, token=hf_token)
    prompt = _build_batch_prompt(listings)
    print(f"[AI] Sending single batched prompt for {len(listings)} item(s) …", flush=True)

    try:
        raw = client.text_generation(prompt, max_new_tokens=600, temperature=0.3)
    except Exception as exc:
        print(f"[AI] InferenceClient error: {exc}", flush=True)
        return []

    print(f"[AI] Response received ({len(raw)} chars). Parsing …", flush=True)
    profitable = _parse_batch_response(raw, listings)
    print(f"[AI] {len(profitable)} profitable item(s) out of {len(listings)}.", flush=True)
    return profitable

def send_discord_notification(analyses: list[ProfitAnalysis]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    if not webhook_url:
        return

    for analysis in analyses:
        listing = analysis.listing
        webhook = DiscordWebhook(url=webhook_url, username="Arbitrage Scout 🔍")
        embed = DiscordEmbed(title=f"💰 {listing.title[:200]}", url=listing.item_url, color="03b2f8")
        embed.set_thumbnail(url=listing.image_url)
        embed.add_embed_field(name="🏷️ Buy Price (eBay)", value=f"**{listing.price:.2f} €**", inline=True)
        embed.add_embed_field(name="📈 Resale Price (Vinted)", value=f"**{analysis.resale_price:.2f} €**", inline=True)
        embed.add_embed_field(name="✅ Net Profit", value=f"**{analysis.net_profit:.2f} €**", inline=True)
        embed.add_embed_field(name="🤖 AI Reasoning", value=analysis.reasoning[:1024] or "N/A", inline=False)
        embed.set_footer(text="Arbitrage Scout Bot • HF InferenceClient 🆓")
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
    print("  ARBITRAGE SCOUT — Starting Scan", flush=True)
    print("=" * 60, flush=True)

    listings = scrape_ebay_listings()
    if not listings:
        print("[SCOUT] No new listings found.", flush=True)
        return

    profitable = analyse_all(listings)
    if not profitable:
        print("[SCOUT] No profitable items this cycle.", flush=True)
        return

    send_discord_notification(profitable)
    print(f"[SCOUT] Done — {len(profitable)} alert(s) sent to Discord.", flush=True)

def scout_loop():
    while True:
        try:
            print(f"\n--- [HEARTBEAT] Bot is awake. Time: {time.strftime('%H:%M:%S')} ---", flush=True)
            run_scout_cycle()
        except Exception as exc:
            print(f"[SCOUT] Unhandled error: {exc}", flush=True)
        
        print(f"[SCOUT] Sleeping {SCAN_INTERVAL_SECONDS // 60} min until next scan …\n", flush=True)
        time.sleep(SCAN_INTERVAL_SECONDS)

def start_background_scout():
    thread = threading.Thread(target=scout_loop, daemon=True)
    thread.start()
    print("[MAIN] Scout background thread started.", flush=True)

start_background_scout()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))