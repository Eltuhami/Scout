import os
import re
import json
import base64
import random
import requests
import urllib.parse
from bs4 import BeautifulSoup

# ─── CORE CONFIG ────────────────────────────────────────────────────────
MAX_BUY_PRICE = 16.0    
MIN_NET_PROFIT = 5.0    
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

def get_dynamic_keyword(groq_key):
    try:
        headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
        prompt = "Reply with exactly ONE German search term for cheap eBay bundles (e.g., 'Sammlung', 'Kellerfund', 'Restposten'). Return ONLY the word."
        
        payload = {
            "model": "meta-llama/llama-4-scout-17b-16e-instruct", 
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "temperature": 0.8,
            "max_tokens": 20 
        }
        
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=10)
        
        if not resp.ok:
            return "Konvolut"

        word = resp.json()['choices'][0]['message']['content'].strip('."\' \n')
        return word if len(word) > 1 else "Konvolut"
    except Exception:
        return "Konvolut"

def scrape_ebay(keyword, seen):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    safe_keyword = urllib.parse.quote(keyword)
    # Filter: Used/Defective only to avoid dropshipping clutter
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={safe_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}&LH_ItemCondition=3000|7000"
    
    payload = {'api_key': scraper_key, 'url': ebay_url, 'country_code': 'de'}
    
    try:
        resp = requests.get('http://api.scraperapi.com', params=payload, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        item_links = soup.find_all("a", href=re.compile(r"/itm/"))
        if not item_links: return []
            
        listings = []
        processed_urls = set()
        
        for link_el in item_links:
            try:
                item_url = link_el["href"].split("?")[0]
                if item_url in processed_urls or item_url in seen: continue
                processed_urls.add(item_url)
                
                container = link_el
                for _ in range(10):
                    if container.parent and not re.search(r"\d+,\d{2}", container.text):
                        container = container.parent
                
                raw_text = container.text.replace('\xa0', ' ')
                match = re.search(r"(\d+[\.,]\d{2})", raw_text)
                if not match: continue
                    
                price = float(match.group(1).replace('.', '').replace(',', '.'))
                if price > MAX_BUY_PRICE or price <= 0: continue 

                img_url = ""
                img_el = container.find("img")
                if img_el:
                    img_url = img_el.get("src") or img_el.get("data-src") or ""
                
                title = link_el.text.strip().split('\n')[0][:80]
                listings.append({"title": title, "price": price, "url": item_url, "img_url": img_url})
                if len(listings) >= 3: break
            except: continue
        return listings
    except: return []

def run_scout():
    print("--- [START] Bot Active ---", flush=True)
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key: return

    history = load_history()
    keyword = get_dynamic_keyword(groq_key)
    print(f"[SEARCH] Hunting for: {keyword}", flush=True)
    
    items = scrape_ebay(keyword, history)
    print(f"[INFO] Found {len(items)} items to analyze.", flush=True)

    for item in items:
        print(f"[AI] Analyzing: {item['title']}...", flush=True)
        try:
            # Updated prompt to ask for target resale price
            prompt = (
                f"Estimate the resale value for '{item['title']}' at {item['price']}€. "
                "Visually check the image for item condition. "
                "Return ONLY valid JSON: [{\"resale_price\": 30.0, \"reasoning\": \"...\", \"score\": 80}]"
            )
            
            headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
            content_list = [{"type": "text", "text": prompt}]
            
            if item.get("img_url") and item["img_url"].startswith("http"):
                img_resp = requests.get(item["img_url"], timeout=5)
                img_b64 = base64.b64encode(img_resp.content).decode('utf-8')
                content_list.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
                
            payload = {
                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "messages": [{"role": "user", "content": content_list}],
                "temperature": 0.2,
                "max_tokens": 512 
            }
            
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            if not resp.ok: continue
                
            raw_ai_text = resp.json()['choices'][0]['message']['content']
            clean_json = raw_ai_text.strip().removeprefix("```json").removesuffix("```").strip()
            
            data = json.loads(clean_json)
            entry = data[0] if isinstance(data, list) else data
            resale = float(entry.get("resale_price", 0))
            
            # Profit = (Resale * 0.85) - Buy Price
            profit = round((resale * (1 - FEE_RATE)) - item['price'], 2)
            
            if profit >= MIN_NET_PROFIT:
                webhook = os.getenv("DISCORD_WEBHOOK")
                msg = {
                    "content": (
                        f"💰 **DEAL FOUND**\n"
                        f"**Search Key:** {keyword}\n"
                        f"**Item:** {item['title']}\n"
                        f"**Buy Price:** {item['price']}€\n"
                        f"**Target Resale:** {resale}€\n"
                        f"**Est. Net Profit:** {profit}€\n"
                        f"**Link:** {item['url']}"
                    )
                }
                if webhook: requests.post(webhook, json=msg)
                save_history(item['url'])
                print(f"[SUCCESS] Profit: {profit}€", flush=True)
            else:
                print(f"[INFO] Skipped. Profit: {profit}€", flush=True)
        except: continue
    print("--- [FINISH] Cycle Complete ---", flush=True)

if __name__ == "__main__":
    run_scout()
