import os
import re
import json
import requests
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

# ─── CORE CONFIG ────────────────────────────────────────────────────────
MAX_BUY_PRICE = 16.0    
MIN_NET_PROFIT = 5.0    
MODEL_ID = "gemini-2.0-flash" 
FEE_RATE = 0.15
HISTORY_FILE = "history.txt"
# ────────────────────────────────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_history(url):
    with open(HISTORY_FILE, "a") as f:
        f.write(url + "\n")

def get_keyword(client):
    try:
        res = client.models.generate_content(
            model=MODEL_ID, 
            contents=f"Suggest ONE specific collectible (e.g., 'Nintendo DS', 'Lego') under {MAX_BUY_PRICE}€. Return ONLY the name."
        )
        keyword = res.text.strip().replace('"', '')
        print(f"[SEARCH] AI selected keyword: {keyword}", flush=True)
        return keyword
    except Exception as e:
        print(f"[ERROR] Keyword fail: {e}", flush=True)
        return "Gameboy" 

def scrape_ebay(keyword, seen):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    url = f"https://www.ebay.de/sch/i.html?_nkw={keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    proxy = f"http://api.scraperapi.com?api_key={scraper_key}&url={url}"
    
    print(f"[SCRAPER] Contacting eBay for '{keyword}'...", flush=True)
    try:
        resp = requests.get(proxy, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Broadest possible search to ensure we don't miss items
        items = soup.find_all("li", class_=re.compile(r"s-item"))
        print(f"[SCRAPER] Found {len(items)} raw HTML elements.", flush=True)
        
        listings = []
        for item in items:
            title_el = item.find(class_="s-item__title")
            title = title_el.text.strip() if title_el else ""
            
            # Skip the hidden "Shop on eBay" dummy item eBay injects
            if not title or "Shop on eBay" in title:
                continue

            link_el = item.find("a", class_="s-item__link")
            if not link_el: continue
            item_url = link_el["href"].split("?")[0]
            if item_url in seen: continue

            price_el = item.find(class_="s-item__price")
            if not price_el: continue
            
            price_str = re.sub(r'[^\d.,]', '', price_el.text).replace(',', '.')
            try:
                price = float(price_str)
                if 0 < price <= MAX_BUY_PRICE:
                    img_el = item.find("img")
                    img_url = img_el.get("src") or img_el.get("data-src") or "" if img_el else ""
                    listings.append({"title": title, "price": price, "url": item_url, "img_url": img_url})
            except: continue
            
            if len(listings) >= 3: break
            
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
    
    keyword = get_keyword(client)
    items = scrape_ebay(keyword, history)
    print(f"[INFO] Successfully parsed {len(items)} items under budget.", flush=True)

    for item in items:
        print(f"[AI] Analyzing: {item['title']}", flush=True)
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
                print(f"[SUCCESS] Sent to Discord. Profit: {profit}€", flush=True)
            else:
                print(f"[INFO] Skipped. Profit too low ({profit}€).", flush=True)
        except Exception as e:
            print(f"[AI ERROR] {e}", flush=True)

    print("--- [FINISH] Cycle Complete ---", flush=True)

if __name__ == "__main__":
    run_scout()
