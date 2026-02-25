import os
import re
import json
import requests
import google.generativeai as genai
from bs4 import BeautifulSoup

# ─── CORE CONFIG ────────────────────────────────────────────────────────
MAX_BUY_PRICE = 16.0    
MIN_NET_PROFIT = 5.0    
# Back to 1.5 Flash using the stable SDK to avoid 404s and 429s
MODEL_ID = "gemini-1.5-flash" 
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

def get_keyword(model):
    try:
        res = model.generate_content(f"Suggest ONE specific collectible (e.g., 'Nintendo DS', 'Lego Set') under {MAX_BUY_PRICE}€. Return ONLY the name.")
        keyword = res.text.strip().replace('"', '')
        print(f"[SEARCH] AI selected keyword: {keyword}", flush=True)
        return keyword
    except Exception as e:
        print(f"[ERROR] Keyword fail: {e}", flush=True)
        # Broad backup keyword to ensure we don't get 0 items
        return "Lego" 

def scrape_ebay(keyword, seen):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    url = f"https://www.ebay.de/sch/i.html?_nkw={keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    proxy = f"http://api.scraperapi.com?api_key={scraper_key}&url={url}"
    
    print(f"[SCRAPER] Contacting eBay for '{keyword}'...", flush=True)
    try:
        resp = requests.get(proxy, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Broadest possible search for items
        items = soup.select(".s-item")
        print(f"[SCRAPER] Found {len(items)} raw HTML elements.", flush=True)
        
        listings = []
        for item in items:
            title_el = item.select_one(".s-item__title")
            title = title_el.text.strip() if title_el else ""
            
            # Skip hidden dummy items
            if not title or "Shop on eBay" in title:
                continue

            link_el = item.select_one(".s-item__link")
            if not link_el: continue
            item_url = link_el["href"].split("?")[0]
            if item_url in seen: continue

            price_el = item.select_one(".s-item__price")
            if not price_el: continue
            
            price_str = re.sub(r'[^\d.,]', '', price_el.text).replace(',', '.')
            try:
                price = float(price_str)
                if 0 < price <= MAX_BUY_PRICE:
                    listings.append({"title": title, "price": price, "url": item_url})
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

    # Configure the stable SDK
    genai.configure(api_key=key)
    model = genai.GenerativeModel(MODEL_ID)
    
    history = load_history()
    keyword = get_keyword(model)
    items = scrape_ebay(keyword, history)
    print(f"[INFO] Successfully parsed {len(items)} new items.", flush=True)

    for item in items:
        print(f"[AI] Analyzing: {item['title'][:50]}...", flush=True)
        try:
            prompt = (
                f"Estimate resale value for '{item['title']}' at {item['price']}€. "
                "Return ONLY valid JSON: [{'resale_price': 30.0, 'reasoning': '...', 'score': 80}]"
            )
            
            res = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(response_mime_type="application/json")
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
                print(f"[INFO] Skipped. Profit: {profit}€", flush=True)
        except Exception as e:
            print(f"[AI ERROR] {e}", flush=True)

    print("--- [FINISH] Cycle Complete ---", flush=True)

if __name__ == "__main__":
    run_scout()
