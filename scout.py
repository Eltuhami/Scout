import os
import re
import json
import random
import requests
import urllib.parse
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

# ─── CORE CONFIG ────────────────────────────────────────────────────────
MAX_BUY_PRICE = 16.0    
MIN_NET_PROFIT = 5.0    
MODEL_ID = "gemini-1.5-flash" 
FEE_RATE = 0.15
HISTORY_FILE = "history.txt"

KEYWORDS = [
    "Pokemon Karte", "Lego Steine Konvolut", "Manga Band 1", 
    "Yugioh Karte", "Nintendo DS Spiel", "Gameboy Spiel"
]
# ────────────────────────────────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_history(url):
    with open(HISTORY_FILE, "a") as f:
        f.write(url + "\n")

def scrape_ebay(keyword, seen):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    safe_keyword = urllib.parse.quote(keyword)
    
    # Put the 16€ budget limit back into the URL so eBay filters the trash for us
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={safe_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    
    # CRITICAL: ScraperAPI parameters to force eBay to load the product grid
    payload = {
        'api_key': scraper_key,
        'url': ebay_url,
        'render': 'true',           # Wait for JavaScript to load the items
        'device_type': 'desktop',   # Force standard Desktop HTML layout
        'country_code': 'de'        # Connect using a German IP
    }
    
    try:
        # Increased timeout to 90s because rendering JS takes a little longer
        resp = requests.get('http://api.scraperapi.com', params=payload, timeout=90)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        print(f"[SCRAPER] Page Title: {soup.title.text if soup.title else 'No Title'}", flush=True)
        
        # Target the info box specifically to avoid hidden background tags
        items = soup.find_all("div", class_=re.compile(r"s-item__info"))
        if not items:
            items = soup.find_all("li", class_=re.compile(r"s-item"))
            
        print(f"[SCRAPER] Found {len(items)} raw eBay elements on the page.", flush=True)
        
        if len(items) <= 2:
            clean_text = re.sub(r'\s+', ' ', soup.text).strip()
            print(f"[DEBUG] eBay Page Dump: {clean_text[:300]}...", flush=True)
            return []
            
        listings = []
        for item in items:
            title_el = item.select_one(".s-item__title") or item.find("h3")
            title = title_el.text.strip() if title_el else ""
            
            if not title or "Shop on eBay" in title or "Neues Angebot" in title:
                continue

            trash = ["seite", "navigation", "feedback", "altersempfehlung", "hülle", "case", "kabel", "adapter", "leerkarton", "anleitung", "defekt"]
            if any(x in title.lower() for x in trash):
                continue

            price_el = item.select_one(".s-item__price")
            if not price_el: continue
            
            match = re.search(r"(\d+[\.,]\d{2}|\d+)", price_el.text)
            if not match: continue
                
            price_str = match.group(1).replace('.', '').replace(',', '.')
            try:
                price = float(price_str)
                if price > MAX_BUY_PRICE or price <= 0:
                    continue 
            except: continue

            # Find the link dynamically depending on which element the scraper grabbed
            link_el = item.select_one(".s-item__link")
            if not link_el and item.parent:
                link_el = item.parent.select_one(".s-item__link")
            if not link_el: continue
                
            item_url = link_el["href"].split("?")[0]
            if item_url in seen: continue

            img_container = item.select_one(".s-item__image-wrapper img")
            if not img_container and item.parent:
                img_container = item.parent.select_one(".s-item__image-wrapper img")
                
            img_url = img_container.get("src") or img_container.get("data-src") or "" if img_container else ""
            
            print(f"  [+] Cheap Item Found: {title[:40]}... ({price}€)", flush=True)
            listings.append({"title": title, "price": price, "url": item_url, "img_url": img_url})
            
            if len(listings) >= 3: 
                break
                
        return listings
    except Exception as e: 
        print(f"[SCRAPE ERROR] {e}", flush=True)
        return []

def run_scout():
    print("--- [START] Bot Active ---", flush=True)
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("[CRITICAL] Missing API Key!", flush=True)
        return

    client = genai.Client(api_key=key)
    history = load_history()
    
    keyword = random.choice(KEYWORDS)
    print(f"[SEARCH] Hunting for: {keyword}", flush=True)
    
    items = scrape_ebay(keyword, history)
    print(f"[INFO] Successfully parsed {len(items)} items matching criteria.", flush=True)

    for item in items:
        print(f"[AI] Analyzing: {item['title'][:40]}...", flush=True)
        try:
            prompt = (
                f"Estimate resale value for '{item['title']}' at {item['price']}€. "
                "Check the image for damage. "
                "Return ONLY valid JSON: [{'resale_price': 30.0, 'reasoning': '...', 'score': 80}]"
            )
            payload = [prompt]
            if item.get("img_url") and item["img_url"].startswith("http"):
                try:
                    img_data = requests.get(item["img_url"], timeout=5).content
                    payload.append(types.Part.from_bytes(data=img_data, mime_type="image/jpeg"))
                except: pass

            res = client.models.generate_content(
                model=MODEL_ID, 
                contents=payload,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            
            data = json.loads(res.text)
            entry = data[0] if isinstance(data, list) else data
            resale = float(entry.get("resale_price", 0))
            profit = round((resale * (1 - FEE_RATE)) - item['price'], 2)
            
            if profit >= MIN_NET_PROFIT:
                webhook = os.getenv("DISCORD_WEBHOOK")
                msg = {"content": f"💰 **DEAL FOUND**\n**Item:** {item['title']}\n**Buy:** {item['price']}€\n**Profit:** {profit}€\n**Link:** {item['url']}"}
                if webhook:
                    requests.post(webhook, json=msg)
                save_history(item['url'])
                print(f"[SUCCESS] Profit: {profit}€", flush=True)
            else:
                print(f"[INFO] Skipped. Est. Profit: {profit}€", flush=True)
        except Exception as e:
            print(f"[AI ERROR] {e}", flush=True)

    print("--- [FINISH] Cycle Complete ---", flush=True)

if __name__ == "__main__":
    run_scout()
