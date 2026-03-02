import os
import re
import json
import base64
import random
import requests
import urllib.parse
from bs4 import BeautifulSoup

# ─── 16€ BUDGET "VOLUME" MODE ───────────────────────────────────────────
MAX_BUY_PRICE = 23.0       
MIN_NET_PROFIT = 4.5       
CONFIDENCE_THRESHOLD = 85  
FEE_RATE = 0.15            # Auf 0.00 setzen, falls du privater Verkäufer bist
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
    """Hybrid-Modell: Python-Zufall gepaart mit KI-Kreativität"""
    try:
        marken = ["Makita", "Bosch", "Nintendo", "Sony", "Lego", "DJI", "Apple", "Festool", "Knipex", "Wera"]
        zustaende = ["Defekt", "Konvolut", "Bastler", "Set", "ungeprüft", "Sammlung", "Ersatzteile"]
        
        zufalls_seed = f"{random.choice(marken)} {random.choice(zustaende)}"
        
        headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
        prompt = f"Erstelle genau EINEN realistischen eBay-Suchbegriff für '{zufalls_seed}'. Nur das Keyword, keine Einleitung! (z.B. 'Makita 18V Schrauber defekt Bastler')"
        
        payload = {
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9 
        }
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=10)
        return resp.json()['choices'][0]['message']['content'].strip('."\' \n')
    except: 
        return "Technik Konvolut Bastler"

def scrape_ebay_details(item_url):
    """Holt die Beschreibung mit JavaScript-Rendering"""
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    payload = {'api_key': scraper_key, 'url': item_url, 'country_code': 'de', 'render': 'true'}
    try:
        resp = requests.get('http://api.scraperapi.com', params=payload, timeout=60)
        soup = BeautifulSoup(resp.text, "html.parser")
        desc_div = soup.select_one("#ds_div, .d-item-description, .x-item-description-child, [class*='description']")
        return desc_div.text.strip()[:2500] if desc_div else "Incomplete description."
    except: return "Scraper error."

def scrape_ebay_search(keyword, seen):
    scraper_key = os.getenv("SCRAPER_API_KEY", "")
    safe_keyword = urllib.parse.quote(keyword)
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={safe_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}&LH_ItemCondition=3000|7000"
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
            
            # Verwandelt winzige Thumbnails (s-l140 oder s-l225) in scharfe HD-Bilder (s-l1600)
            img_url = re.sub(r's-l\d+\.', 's-l1600.', img_url)
            
            listings.append({"title": link_el.text.strip()[:80], "price": price, "url": item_url, "img_url": img_url})
            if len(listings) >= 3: break
        return listings
    except: return []

def run_scout():
    print("--- [START] 16€ Hybrid Scout ---", flush=True)
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key: return
    history = load_history()
    keyword = get_dynamic_keyword(groq_key)
    print(f"[SEARCH] Target: {keyword}", flush=True)
    
    items = scrape_ebay_search(keyword, history)
    for item in items:
        try:
            description = scrape_ebay_details(item['url'])
            save_history(item['url'])

            prompt = (
                f"RETAIL MARKET AUDIT - {MAX_BUY_PRICE} EURO MAX BUDGET.\n"
                f"Item: {item['title']}\n"
                f"Description: {description}\n"
                f"Cost: {item['price']}€\n\n"
                "RULE 1: Estimate the HIGHEST realistic retail price a buyer would initially see before negotiating.\n"
                "RULE 2: If the item is clearly defective, estimate the fair market value for hobbyists.\n"
                "RULE 3: If you cannot identify the exact brand or model, 'confidence' MUST be strictly 0.\n"
                "Return JSON ONLY: {\"resale_price\": 0.0, \"confidence\": 0, \"reasoning\": \"Retail market evaluation...\"}"
            )
            
            headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
            content_list = [{"type": "text", "text": prompt}]
            if item.get("img_url").startswith("http"):
                try:
                    img_b64 = base64.b64encode(requests.get(item["img_url"]).content).decode('utf-8')
                    content_list.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
                except: pass
                
            payload = {"model": "meta-llama/llama-4-scout-17b-16e-instruct", "messages": [{"role": "user", "content": content_list}], "temperature": 0.1}
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            
            raw_content = resp.json()['choices'][0]['message']['content']
            json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
            if not json_match: raise ValueError("No JSON found")
            data = json.loads(json_match.group())
            
            resale = float(data.get("resale_price", 0))
            conf = int(data.get("confidence", 0))
            profit = round((resale * (1 - FEE_RATE)) - item['price'], 2)
            
            if profit >= MIN_NET_PROFIT and conf >= CONFIDENCE_THRESHOLD:
                webhook = os.getenv("DISCORD_WEBHOOK")
                msg = {"content": f"🎯 **CERTIFIED WIN**\n**Item:** {item['title']}\n**Buy:** {item['price']}€ | **Exit:** {resale}€\n**Safety:** {conf}%\n**Profit:** {profit}€\n**Logic:** {data.get('reasoning')}\n**Link:** {item['url']}"}
                if webhook: requests.post(webhook, json=msg)
                print(f"[WIN] {item['title']} - Profit: {profit}€ | Conf: {conf}%", flush=True)
            else:
                if profit > 0:
                    print(f"[REJECT] Conf: {conf}% | Profit: {profit}€", flush=True)
                else:
                    print(f"[REJECT] Kein Profit ({profit}€) - Item wird ignoriert", flush=True)
                    
        except Exception as e:
            print(f"[ERROR] Skipping item: {str(e)}", flush=True)
            continue
    print("--- [FINISH] ---", flush=True)

if __name__ == "__main__":
    run_scout()
