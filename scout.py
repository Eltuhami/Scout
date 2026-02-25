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
CONFIDENCE_THRESHOLD = 80 # Bot only pings if AI is 80%+ sure
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
        prompt = "Reply with exactly ONE German search term for cheap eBay bundles (e.g., 'Sammlung', 'Restposten', 'Dachbodenfund'). Return ONLY the word."
        payload = {
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "temperature": 0.8,
            "max_tokens": 20
        }
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=10)
        word = resp.json()['choices'][0]['message']['content'].strip('."\' \n')
        return word if len(word) > 1 else "Konvolut"
    except Exception: return "Konvolut"

def scrape_ebay_details(item_url):
    """Deep dive into the listing to pull description and high-res images"""
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    payload = {'api_key': scraper_key, 'url': item_url, 'country_code': 'de'}
    try:
        resp = requests.get('http://api.scraperapi.com', params=payload, timeout=40)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Pull description: eBay often hides this in an iframe called 'desc_ifr'
        desc_div = soup.find("div", {"id": "ds_div"}) or soup.find("div", {"class": "d-item-description"})
        description = desc_div.text.strip()[:1500] if desc_div else "No description provided."
        return description
    except: return "Could not load description."

def scrape_ebay_search(keyword, seen):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    safe_keyword = urllib.parse.quote(keyword)
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
                    if container.parent and not re.search(r"\d+,\d{2}", container.text): container = container.parent
                raw_text = container.text.replace('\xa0', ' ')
                match = re.search(r"(\d+[\.,]\d{2})", raw_text)
                if not match: continue
                price = float(match.group(1).replace('.', '').replace(',', '.'))
                if price > MAX_BUY_PRICE or price <= 0: continue 
                img_el = container.find("img")
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
    print(f"[SEARCH] Keyword: {keyword}", flush=True)
    items = scrape_ebay_search(keyword, history)
    for item in items:
        print(f"[AI] Auditing: {item['title']}...", flush=True)
        try:
            # Audit Phase: Get full description
            description = scrape_ebay_details(item['url'])
            
            prompt = (
                f"AUDIT THIS EBAY LISTING:\n"
                f"Title: {item['title']}\n"
                f"Description: {description}\n"
                f"Purchase Price: {item['price']}€\n\n"
                "TASKS:\n"
                "1. Estimate resale value (be conservative).\n"
                "2. Check for keywords like 'defekt', 'ungetestet', 'missing', or 'nur Karton'.\n"
                "3. Scan image for visible damage.\n"
                "4. Provide a confidence score (0-100).\n"
                "Return ONLY valid JSON: [{\"resale_price\": 30.0, \"confidence\": 85, \"reasoning\": \"...\"}]"
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
                "temperature": 0.2, "max_tokens": 800
            }
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            data = json.loads(resp.json()['choices'][0]['message']['content'].strip().removeprefix("```json").removesuffix("```").strip())
            entry = data[0] if isinstance(data, list) else data
            
            resale = float(entry.get("resale_price", 0))
            confidence = int(entry.get("confidence", 0))
            profit = round((resale * (1 - FEE_RATE)) - item['price'], 2)
            
            # THE FILTER: Must meet profit AND confidence
            if profit >= MIN_NET_PROFIT and confidence >= CONFIDENCE_THRESHOLD:
                webhook = os.getenv("DISCORD_WEBHOOK")
                msg = {"content": (
                    f"🛡️ **VERIFIED DEAL FOUND**\n"
                    f"**Item:** {item['title']}\n"
                    f"**Buy:** {item['price']}€ | **Resell:** {resale}€\n"
                    f"**Est. Profit:** {profit}€\n"
                    f"**AI Confidence:** {confidence}%\n"
                    f"**Reasoning:** {entry.get('reasoning')[:200]}...\n"
                    f"**Link:** {item['url']}"
                )}
                if webhook: requests.post(webhook, json=msg)
                save_history(item['url'])
                print(f"[SUCCESS] Audit Pass: {profit}€ profit at {confidence}% confidence.", flush=True)
            else:
                print(f"[INFO] Audit Fail: Profit {profit}€ / Confidence {confidence}%.", flush=True)
        except: continue
    print("--- [FINISH] Cycle Complete ---", flush=True)

if __name__ == "__main__":
    run_scout()
