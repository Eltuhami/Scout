import os
import re
import json
import base64
import random
import requests
import urllib.parse
from bs4 import BeautifulSoup

# ─── CORE CONFIG (TIGHTENED) ────────────────────────────────────────────
MAX_BUY_PRICE = 16.0    
MIN_NET_PROFIT = 7.0       # Increased for a larger safety buffer
CONFIDENCE_THRESHOLD = 90  # Strictly 90% or higher. No "maybe" deals.
FEE_RATE = 0.15
HISTORY_FILE = "history.txt"

# Hard-Fail Keywords: If these appear in description, AI must reject.
RED_FLAGS = ["defekt", "kaputt", "ersatzteil", "reparatur", "ungetestet", "gebrochen"]
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
        # Force the AI to pick a new category every time
        categories = ["Vintage Tech", "Board Games", "Retro Gaming", "Tools", "Cameras", "Media"]
        target = random.choice(categories)
        
        prompt = (
            f"Think of ONE specific German search term for used {target} bundles on eBay. "
            "Do NOT use 'Sammlerstücke'. Return ONLY the word (e.g., 'Objektiv', 'Schraubstock', 'Kartenspiel')."
        )
        
        payload = {
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "temperature": 1.0, # High creativity for keywords
            "max_tokens": 20
        }
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=10)
        return resp.json()['choices'][0]['message']['content'].strip('."\' \n')
    except: return "Konvolut"

def scrape_ebay_details(item_url):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    payload = {'api_key': scraper_key, 'url': item_url, 'country_code': 'de'}
    try:
        resp = requests.get('http://api.scraperapi.com', params=payload, timeout=40)
        soup = BeautifulSoup(resp.text, "html.parser")
        desc_div = soup.find("div", {"id": "ds_div"}) or soup.find("div", {"class": "d-item-description"})
        return desc_div.text.strip()[:2000] if desc_div else "No description."
    except: return "Error loading description."

def scrape_ebay_search(keyword, seen):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    safe_keyword = urllib.parse.quote(keyword)
    # Filter: Used condition only.
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={safe_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}&LH_ItemCondition=3000"
    payload = {'api_key': scraper_key, 'url': ebay_url, 'country_code': 'de'}
    try:
        resp = requests.get('http://api.scraperapi.com', params=payload, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        item_links = soup.find_all("a", href=re.compile(r"/itm/"))
        listings = []
        processed_urls = set()
        for link_el in item_links:
            item_url = link_el["href"].split("?")[0]
            if item_url in processed_urls or item_url in seen: continue
            processed_urls.add(item_url)
            
            container = link_el
            for _ in range(8):
                if container.parent and not re.search(r"\d+,\d{2}", container.text): container = container.parent
            
            match = re.search(r"(\d+[\.,]\d{2})", container.text.replace('\xa0', ' '))
            if not match: continue
            price = float(match.group(1).replace('.', '').replace(',', '.'))
            if price > MAX_BUY_PRICE: continue
            
            img_el = container.find("img")
            img_url = img_el.get("src") or img_el.get("data-src") or ""
            listings.append({"title": link_el.text.strip()[:80], "price": price, "url": item_url, "img_url": img_url})
            if len(listings) >= 3: break
        return listings
    except: return []

def run_scout():
    print("--- [START] Strict Audit Mode ---", flush=True)
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key: return
    history = load_history()
    keyword = get_dynamic_keyword(groq_key)
    print(f"[SEARCH] Target: {keyword}", flush=True)
    
    items = scrape_ebay_search(keyword, history)
    for item in items:
        try:
            description = scrape_ebay_details(item['url'])
            
            # Pre-filter for red flags to save AI tokens and time
            if any(flag in description.lower() for flag in RED_FLAGS):
                print(f"[SKIP] Red flag found in text for: {item['title']}", flush=True)
                continue

            prompt = (
                f"STRICT AUDIT - ZERO GAMBLING POLICY.\n"
                f"Item: {item['title']}\n"
                f"Text: {description}\n"
                f"Cost: {item['price']}€\n\n"
                "INSTRUCTIONS:\n"
                "1. If the item is incomplete, damaged, or unconfirmed, confidence MUST be below 50.\n"
                "2. Estimate resale value based ONLY on confirmed contents.\n"
                "3. Flag any potential risk (e.g., stock photos, vague description).\n"
                "Return JSON ONLY: {\"resale_price\": 0.0, \"confidence\": 0, \"risk_factor\": \"high/low\", \"reasoning\": \"...\"}"
            )
            
            headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
            content_list = [{"type": "text", "text": prompt}]
            if item.get("img_url").startswith("http"):
                img_b64 = base64.b64encode(requests.get(item["img_url"]).content).decode('utf-8')
                content_list.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
                
            payload = {"model": "meta-llama/llama-4-scout-17b-16e-instruct", "messages": [{"role": "user", "content": content_list}], "temperature": 0.1}
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            data = json.loads(resp.json()['choices'][0]['message']['content'].strip().removeprefix("```json").removesuffix("```").strip())
            
            resale = float(data.get("resale_price", 0))
            conf = int(data.get("confidence", 0))
            profit = round((resale * (1 - FEE_RATE)) - item['price'], 2)
            
            if profit >= MIN_NET_PROFIT and conf >= CONFIDENCE_THRESHOLD:
                webhook = os.getenv("DISCORD_WEBHOOK")
                msg = {"content": (
                    f"✅ **CERTIFIED DEAL**\n"
                    f"**Item:** {item['title']}\n"
                    f"**Buy:** {item['price']}€ | **Exit:** {resale}€\n"
                    f"**Safety Score:** {conf}%\n"
                    f"**Risk:** {data.get('risk_factor')}\n"
                    f"**Audit:** {data.get('reasoning')}\n"
                    f"**Link:** {item['url']}"
                )}
                if webhook: requests.post(webhook, json=msg)
                save_history(item['url'])
                print(f"[CERTIFIED] {item['title']} - Profit: {profit}€", flush=True)
            else:
                print(f"[REJECTED] Confidence: {conf}% | Profit: {profit}€", flush=True)
        except: continue
    print("--- [FINISH] ---", flush=True)

if __name__ == "__main__":
    run_scout()
