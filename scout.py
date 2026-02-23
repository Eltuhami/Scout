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
MAX_BUY_PRICE = 16.0  # Change this as your bank account grows
MIN_NET_PROFIT = 5.0  # Minimum profit you want to make per flip
NUM_LISTINGS = 3      # How many items to check per 5-minute run
# ────────────────────────────────────────────────────────────────────────

FEE_RATE = 0.15

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

def get_dynamic_keyword(client):
    """AI Brain: Automatically picks a niche based on your CURRENT budget."""
    prompt = (
        f"You are a professional reseller. My current budget is {MAX_BUY_PRICE}€. "
        f"Suggest ONE specific item or brand that is REALISTICALLY found for under {MAX_BUY_PRICE}€ "
        "on eBay and can be flipped for a profit. DO NOT suggest items that usually cost more "
        "(like consoles or iPhones). Think of collectibles, specific toys, or media. "
        "Return ONLY the keyword."
    )
    try:
        response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
        keyword = response.text.strip().replace("'", "").replace('"', "")
        print(f"[AI] Budget-Aware Search: {keyword}", flush=True)
        return keyword
    except:
        return "Lego Minifigure"

def scrape_ebay_listings(keyword) -> list[Listing]:
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    # Scraper strictly respects your MAX_BUY_PRICE variable
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
                
                title_el = item.find("h3") or item.find("h2")
                title = title_el.get_text(strip=True).replace("Neues Angebot", "") if title_el else "Unknown"
                
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
                    listings.append(Listing(title=title, price=price_val, image_url=img_url, item_url=link_el["href"].split("?")[0]))
            except: continue
        return listings
    except: return []

def analyse_all_gemini(listings: list[Listing], client) -> list[ProfitAnalysis]:
    payload = ["Analyze resale value for Vinted. Return JSON array: [{'id': 1, 'resale_price': 50.0, 'reasoning': '...', 'score': 85}]"]
    for i, l in enumerate(listings, 1):
        payload.append(f"Item {i}: '{l.title}' - Price: {l.price} €")
        if l.image_url:
            try:
                img_resp = requests.get(l.image_url, timeout=5)
                payload.append(types.Part.from_bytes(data=img_resp.content, mime_type="image/jpeg"))
            except: pass

    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash', 
            contents=payload,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
        )
        items_data = json.loads(response.text)
        
        profitable = []
        for entry in items_data:
            idx = int(entry.get("id", 1)) - 1
            if 0 <= idx < len(listings):
                l = listings[idx]
                resale = float(entry.get("resale_price", 0))
                profit = (resale * (1 - FEE_RATE)) - l.price
                # Logic strictly respects your MIN_NET_PROFIT variable
                if profit >= MIN_NET_PROFIT:
                    profitable.append(ProfitAnalysis(
                        listing=l, resale_price=resale, net_profit=round(profit, 2),
                        reasoning=entry.get("reasoning", ""), score=int(entry.get("score", 50))
                    ))
        return profitable
    except: return []

def send_discord_notification(analyses: list[ProfitAnalysis]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

    for analysis in analyses:
        l = analysis.listing
        payload = {
            "username": "Gemini Scout ⚡",
            "embeds": [{
                "title": f"💰 {l.title[:200]}",
                "url": l.item_url,
                "color": 65450, 
                "thumbnail": {"url": l.image_url} if l.image_url else {},
                "fields": [
                    {"name": "🏷️ Buy Price", "value": f"**{l.price:.2f} €**", "inline": True},
                    {"name": "📈 Sell Price", "value": f"**{analysis.resale_price:.2f} €**", "inline": True},
                    {"name": "✅ Net Profit", "value": f"**{analysis.net_profit:.2f} €**", "inline": True},
                    {"name": "⭐ Deal Score", "value": f"**{analysis.score}/100**", "inline": True},
                    {"name": "🤖 AI Reason", "value": analysis.reasoning[:1000], "inline": False}
                ]
            }]
        }
        requests.post(webhook_url, json=payload, headers=headers)

if __name__ == "__main__":
    api_key = os.getenv("GEMINI_API_KEY", "")
    if api_key:
        client = genai.Client(api_key=api_key)
        keyword = get_dynamic_keyword(client)
        listings = scrape_ebay_listings(keyword)
        if listings:
            profitable = analyse_all_gemini(listings, client)
            if profitable:
                send_discord_notification(profitable)
