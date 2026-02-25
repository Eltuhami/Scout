import os
import re
import json
import random
import requests
import urllib.parse
from bs4 import BeautifulSoup

# ─── CORE CONFIG ────────────────────────────────────────────────────────
MAX_BUY_PRICE = 16.0    
MIN_NET_PROFIT = 5.0    
FEE_RATE = 0.15
HISTORY_FILE = "history.txt"

KEYWORDS = [
    "Pokemon Karte", "Lego Steine", "Manga Deutsch", 
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
    
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={safe_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}"
    
    payload = {
        'api_key': scraper_key,
        'url': ebay_url,
        'country_code': 'de'
    }
    
    try:
        resp = requests.get('http://api.scraperapi.com', params=payload, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        print(f"[SCRAPER] Page Title: {soup.title.text if soup.title else 'No Title'}", flush=True)
        
        item_links = soup.find_all("a", href=re.compile(r"/itm/"))
        print(f"[SCRAPER] Found {len(item_links)} product links.", flush=True)
        
        if not item_links:
            return []
            
        listings = []
        processed_urls = set()
        
        for link_el in item_links:
            try:
                item_url = link_el["href"].split("?")[0]
                
                if item_url in processed_urls or item_url in seen:
                    continue
                processed_urls.add(item_url)
                
                container = link_el
                for _ in range(10):
                    if container.parent and not re.search(r"\d+,\d{2}", container.text):
                        container = container.parent
                        
                raw_text = container.text.replace('\xa0', ' ')
                
                title = link_el.text.strip()
                if not title:
                    img = container.find("img")
                    title = img.get("alt", "") if img else ""
                title = title.split('\n')[0].strip()
                    
                if not title or "Shop on eBay" in title or "Neues Angebot" in title:
                    continue

                trash = ["seite", "navigation", "feedback", "altersempfehlung", "hülle", "case", "kabel", "adapter", "leerkarton", "anleitung", "defekt"]
                if any(x in title.lower() for x in trash):
                    continue

                match = re.search(r"(\d+[\.,]\d{2})", raw_text)
                if not match: continue
                    
                price_str = match.group(1).replace('.', '').replace(',', '.')
                price = float(price_str)
                
                if price > MAX_BUY_PRICE or price <= 0:
                    continue 

                img_url = ""
                img_el = container.find("img")
                if img_el:
                    img_url = img_el.get("src") or img_el.get("data-src") or ""
                
                print(f"  [+] Valid Item: {title[:40]}... ({price}€)", flush=True)
                listings.append({"title": title, "price": price, "url": item_url, "img_url": img_url})
                
                if len(listings) >= 3: 
                    break
            except:
                continue
            
        return listings
    except Exception as e: 
        print(f"[SCRAPE ERROR] {e}", flush=True)
        return []

def run_scout():
    print("--- [START] Bot Active ---", flush=True)
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        print("[CRITICAL] Missing GROQ_API_KEY!", flush=True)
        return

    history = load_history()
    keyword = random.choice(KEYWORDS)
    print(f"[SEARCH] Hunting for: {keyword}", flush=True)
    
    items = scrape_ebay(keyword, history)
    print(f"[INFO] Successfully parsed {len(items)} items.", flush=True)

    for item in items:
        print(f"[AI] Analyzing: {item['title'][:40]}...", flush=True)
        try:
            prompt = (
                f"Estimate resale value for '{item['title']}' at {item['price']}€. "
                "Check the image for damage. "
                "Return ONLY valid JSON: [{\"resale_price\": 30.0, \"reasoning\": \"...\", \"score\": 80}]"
            )
            
            # ─── GROQ VISION API CONNECTION ───
            headers = {
                "Authorization": f"Bearer {groq_key}",
                "Content-Type": "application/json"
            }
            
            content_list = [{"type": "text", "text": prompt}]
            if item.get("img_url") and item["img_url"].startswith("http"):
                content_list.append({"type": "image_url", "image_url": {"url": item["img_url"]}})
                
            payload = {
                "model": "llama-3.2-90b-vision-preview",
                "messages": [{"role": "user", "content": content_list}],
                "temperature": 0.2
            }
            
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            
            # Parse Groq's response
            response_json = resp.json()
            raw_ai_text = response_json['choices'][0]['message']['content']
            
            # Clean up potential markdown formatting from the AI
            clean_json_str = raw_ai_text.strip().removeprefix("```json").removesuffix("```").strip()
            
            data = json.loads(clean_json_str)
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
            print(f"[AI ERROR] Failed to process with Groq: {e}", flush=True)

    print("--- [FINISH] Cycle Complete ---", flush=True)

if __name__ == "__main__":
    run_scout()
