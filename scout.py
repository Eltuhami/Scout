import os
import re
import json
import base64
import random
import requests
import urllib.parse
from bs4 import BeautifulSoup

# ─── 16€ BUDGET "REALISTIC VOLUME" MODE ─────────────────────────────────
MAX_BUY_PRICE = 23.0       
MIN_NET_PROFIT = 2.0       
CONFIDENCE_THRESHOLD = 85  
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
    # SMARTE LÖSUNG: Wir nutzen extrem breite, kurze Keywords für maximales Volumen.
    marken = ["Makita", "Bosch", "Nintendo", "Sony", "Lego", "DJI", "Apple", "Festool", "Knipex", "Wera", "Playstation"]
    zustaende = ["Defekt", "Konvolut", "Bastler", "Ersatzteile", "ungeprüft"]
    keyword = f"{random.choice(marken)} {random.choice(zustaende)}"
    return keyword

def scrape_ebay_details(item_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(item_url, headers=headers, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        desc_div = soup.select_one("#ds_div, .d-item-description, .x-item-description-child, [class*='description']")
        return desc_div.text.strip()[:2500] if desc_div else "Incomplete description."
    except: return "Scraper error."

def scrape_ebay_search(keyword, seen):
    safe_keyword = urllib.parse.quote(keyword)
    ebay_url = f"https://www.ebay.de/sch/i.html?_nkw={safe_keyword}&_sop=10&LH_BIN=1&_udhi={int(MAX_BUY_PRICE)}&LH_ItemCondition=3000|7000&_rss=1"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        resp = requests.get(ebay_url, headers=headers, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.find_all("item")
        
        print(f"[DEBUG] RSS Feed hat {len(items)} Items für '{keyword}' geliefert.", flush=True)
        
        listings = []
        for item in items:
            title = item.title.text if item.title else "Unbekannt"
            link = item.link.text if item.link else ""
            if not link or link in seen: continue
            
            desc_text = item.description.text if item.description else ""
            
            # Verbesserte Preis-Erkennung für den RSS Feed
            price_match = re.search(r"EUR\s*(\d+[\.,]\d{2})", desc_text)
            if not price_match: continue
            price = float(price_match.group(1).replace('.', '').replace(',', '.'))
            
            if price > MAX_BUY_PRICE: continue
            
            img_match = re.search(r'src="(https://i\.ebayimg\.com/[^"]+)"', desc_text)
            img_url = img_match.group(1) if img_match else ""
            if img_url:
                img_url = re.sub(r's-l\d+\.', 's-l1600.', img_url)
            
            listings.append({"title": title[:80], "price": price, "url": link.split("?")[0], "img_url": img_url})
            if len(listings) >= 3: break
        return listings
    except Exception as e:
        print(f"[DEBUG] Fehler beim RSS-Scrapen: {e}", flush=True)
        return []

def run_scout():
    print("--- [START] 16€ Hybrid Scout ---", flush=True)
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key: 
        print("[ERROR] Groq API Key fehlt!", flush=True)
        return
        
    history = load_history()
    keyword = get_dynamic_keyword(groq_key)
    print(f"[SEARCH] Target: {keyword}", flush=True)
    
    items = scrape_ebay_search(keyword, history)
    
    if not items:
        print("[INFO] Keine passenden Items gefunden.", flush=True)

    for item in items:
        try:
            description = scrape_ebay_details(item['url'])

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
                if webhook: 
                    requests.post(webhook, json=msg)
                    print(">>> DISCORD WEBHOOK ERFOLGREICH GESENDET! <<<", flush=True)
                print(f"[WIN] {item['title']} - Profit: {profit}€ | Conf: {conf}%", flush=True)
            else:
                if profit > 0:
                    print(f"[REJECT] Conf: {conf}% | Profit: {profit}€", flush=True)
                else:
                    print(f"[REJECT] Kein Profit ({profit}€) - Item wird ignoriert", flush=True)
            
            save_history(item['url'])
                    
        except Exception as e:
            print(f"[ERROR] Skipping item: {str(e)}", flush=True)
            continue
    print("--- [FINISH] ---", flush=True)

if __name__ == "__main__":
    run_scout()
