"""
AutoJäger Backend — Complete Product
Stripe subscriptions + real car scraping + AI analysis + Telegram delivery
"""

from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import asyncio, httpx, json, os, re, stripe
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="AutoJäger API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── CONFIG ────────────────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_live_REPLACE_ME")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY", "")
TG_TOKEN       = os.getenv("TG_BOT_TOKEN", "8838098588:AAEX9aZdagfYBZWf-tdKIOwICG9X1ng9HcM")

RATES = {"EUR":1,"USD":.92,"JPY":.0062,"AED":.25,"KRW":.00069,"CNY":.13,"PLN":.23,"TRY":.028}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

# Plan limits
PLAN_LIMITS = {
    "trial":   {"searches": 10,  "sites": 6,  "ai": False},
    "starter": {"searches": 10,  "sites": 6,  "ai": False},
    "pro":     {"searches": 999, "sites": 13, "ai": True},
    "dealer":  {"searches": 999, "sites": 13, "ai": True},
}

# ── MODELS ────────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query:        str
    max_price:    Optional[int]   = None
    min_year:     Optional[int]   = None
    max_km:       Optional[int]   = None
    fuel:         Optional[str]   = None
    sites:        Optional[list]  = None
    tg_token:     Optional[str]   = None
    tg_chat_id:   Optional[str]   = None
    openai_key:   Optional[str]   = None
    discount_pct: Optional[int]   = 9
    user_id:      Optional[str]   = None

class SubscribeRequest(BaseModel):
    payment_method: str
    email:          str
    plan:           str
    price_id:       str

class NegoRequest(BaseModel):
    car:        dict
    language:   str = "en"
    offer:      str = ""
    openai_key: Optional[str] = None

# ── HELPERS ───────────────────────────────────────────────────────────────
def parse_price(text):
    if not text: return 0
    s = re.sub(r'[€$£¥₩EURUSDAEDKRWCNYJPYPLNTRYAEDkrw\s,]','', str(text))
    try:
        dots = s.count('.'); commas = s.count(',')
        if dots>=1 and commas==0: return float(int(s.replace('.', '')))
        elif commas>=1 and dots==0: return float(int(s.replace(',', '')))
        elif dots>=1 and commas==1: return float(int(s.split(',')[0].replace('.', '')))
        else: return float(re.sub(r'[^0-9.]','',s) or 0)
    except: return 0

def to_eur(price, currency):
    return round(price * RATES.get(currency, 1))

def est_market(title, year, km):
    t = (title or "").lower()
    b = 50000
    if "296" in t and "ferrari" in t: b = 280000
    elif "488" in t: b = 180000
    elif "ferrari" in t: b = 200000
    elif "aventador" in t: b = 350000
    elif "huracan" in t: b = 200000
    elif "lamborghini" in t: b = 220000
    elif "720s" in t: b = 200000
    elif "mclaren" in t: b = 170000
    elif "gt3" in t and "porsche" in t: b = 170000
    elif "turbo s" in t: b = 190000
    elif "911" in t: b = 130000
    elif "porsche" in t: b = 85000
    elif "phantom" in t: b = 350000
    elif "rolls" in t: b = 300000
    elif "bentley" in t: b = 150000
    elif "aston" in t: b = 130000
    elif "amg gt" in t: b = 130000
    elif "g63" in t or "g-class" in t: b = 160000
    elif "m5" in t: b = 95000
    elif "m4" in t or "m3" in t: b = 80000
    elif "rs6" in t or "rs7" in t: b = 100000
    elif "r8" in t: b = 120000
    elif "gt-r" in t: b = 80000
    elif "supra" in t: b = 50000
    elif "bmw" in t: b = 55000
    elif "mercedes" in t or "amg" in t: b = 65000
    elif "audi" in t: b = 55000
    try:
        m = re.search(r'\b(20\d{2}|199\d)\b', str(year or "2018"))
        yr = int(m.group(0)) if m else 2018
    except: yr = 2018
    age = 2025 - yr
    exotic = b > 150000
    dep = max(0.6, 1 - age*0.03) if exotic else max(0.3, 1 - age*0.06)
    kmv = parse_price(str(km or "30000"))
    kmf = max(0.85, 1 - kmv/300000) if exotic else max(0.7, 1 - kmv/500000)
    return round(b * dep * kmf)

def calc_score(eur, mkt, km, year):
    if not mkt: return 70
    sp = min(100, 50 + (mkt - eur) / mkt * 200)
    kmv = parse_price(str(km or "30000"))
    sk = max(0, 100 - kmv / 2000)
    try:
        m = re.search(r'\b(20\d{2}|199\d)\b', str(year or "2018"))
        yr = int(m.group(0)) if m else 2018
    except: yr = 2018
    sa = max(50, 100 - (2025 - yr) * 3)
    return min(99, max(40, round(sp*.5 + sk*.3 + sa*.2)))

def make_listing(card, title_sels, price_sels, source, flag, currency, base_url=""):
    try:
        title_el = None
        for s in title_sels:
            title_el = card.select_one(s)
            if title_el: break
        title = title_el.get_text(strip=True)[:100] if title_el else ""

        price_el = None
        for s in price_sels:
            price_el = card.select_one(s)
            if price_el: break
        price_text = price_el.get_text(strip=True) if price_el else ""

        link_el = card.select_one("a[href]")
        href = link_el.get("href","") if link_el else ""
        if href and not href.startswith("http"): href = base_url + href

        text = card.get_text(" ", strip=True)
        km_m = re.search(r'(\d[\d.,]+)\s*km', text, re.I)
        km = km_m.group(0) if km_m else ""
        yr_m = re.search(r'\b(20\d{2}|199\d)\b', text)
        year = yr_m.group(0) if yr_m else ""
        fuel_kws = ["Diesel","Petrol","Electric","Hybrid","Benzin","Gasoline"]
        fuel = next((f for f in fuel_kws if f.lower() in text.lower()), "")
        img_el = card.select_one("img")
        img = img_el.get("src","") if img_el else ""

        # Special JPY/KRW handling (万)
        man_m = re.search(r'([\d.]+)\s*万', price_text)
        if man_m and currency in ("JPY","KRW"):
            pval = float(man_m.group(1)) * 10000
        else:
            pval = parse_price(price_text)

        eur = to_eur(pval, currency)
        mkt = est_market(title, year, km)

        if eur < 500 or not href: return None
        return {"title":title,"price":price_text+" "+currency,"price_eur":eur,
                "market_eur":mkt,"saving":mkt-eur,"score":calc_score(eur,mkt,km,year),
                "km":km,"year":year,"fuel":fuel,"currency":currency,
                "source":source,"flag":flag,"url":href,"image":img,
                "location":"","seller":"","owners":"","service":"","accident":""}
    except: return None

# ── SCRAPERS ──────────────────────────────────────────────────────────────
MOBILE_IDS = {
    "bmw m4":"ms=3500%3B93%3B%3B","bmw m3":"ms=3500%3B61%3B%3B","bmw m5":"ms=3500%3B95%3B%3B",
    "porsche 911":"ms=19000%3B46%3B%3B","porsche cayenne":"ms=19000%3B50%3B%3B",
    "ferrari 488":"ms=9000%3B48%3B%3B","lamborghini huracan":"ms=13900%3B11%3B%3B",
    "audi rs6":"ms=1900%3B142%3B%3B","audi r8":"ms=1900%3B108%3B%3B",
}

AS24_SLUGS = {
    "bmw m4":("bmw","m4"),"bmw m3":("bmw","m3"),"bmw m5":("bmw","m5"),
    "porsche 911":("porsche","911"),"porsche cayenne":("porsche","cayenne"),
    "ferrari 488":("ferrari","488"),"lamborghini huracan":("lamborghini","huracan"),
    "audi rs6":("audi","rs6"),"audi r8":("audi","r8"),
    "mercedes amg gt":("mercedes-benz","amg-gt"),"nissan gt-r":("nissan","gt-r"),
}

async def scrape_mobile(client, query, f):
    ql = query.lower()
    ms = next((v for k,v in MOBILE_IDS.items() if k in ql), None)
    url = "https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&scopeId=C2C&sfmr=false&s=Car&vc=Car"
    url += ("&"+ms) if ms else "&makeModelVariant1.modelDescription="+query
    if f.get("max_price"): url += "&maxPrice="+str(f["max_price"])
    if f.get("max_km"):    url += "&maxMileage="+str(f["max_km"])
    if f.get("min_year"):  url += "&minFirstRegistrationDate="+str(f["min_year"])+"-01-01"
    try:
        r = await client.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("a.result-item, article.result-item, [class*='result-item']")[:5] or soup.select("a[href*='auto-inserat']")[:5]
        results = []
        for card in cards[:3]:
            item = make_listing(card,["h2","h3",".listing-headline","[class*='title']"],
                ["[class*='price']",".price-block__price"],"Mobile.de","🇩🇪","EUR","https://suchen.mobile.de")
            if item: results.append(item)
        return results
    except Exception as e:
        print(f"Mobile.de: {e}"); return []

async def scrape_autoscout(client, query, f):
    ql = query.lower()
    slugs = next((v for k,v in AS24_SLUGS.items() if k in ql), None)
    url = "https://www.autoscout24.com/lst"
    if slugs: url += f"/{slugs[0]}/{slugs[1]}"
    if f.get("max_price"): url += f"/pr_{f['max_price']}"
    url += "?atype=C&cy=D%2CA%2CB%2CE%2CF%2CI%2CL%2CNL&damaged_listing=exclude&desc=0&sort=standard&ustate=N%2CU"
    if f.get("max_km"):   url += "&kmto="+str(f["max_km"])
    if f.get("min_year"): url += "&fregfrom="+str(f["min_year"])
    if not slugs: url += "&q="+query
    try:
        r = await client.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("article[class*='cldt'], [class*='ListItem'], a[href*='/offers/']")[:5]
        results = []
        for card in cards[:3]:
            item = make_listing(card,["h2","h3","[class*='title']","[class*='headline']"],
                ["[class*='price']","[data-testid='price-label']"],"AutoScout24","🇪🇺","EUR","https://www.autoscout24.com")
            if item: results.append(item)
        return results
    except Exception as e:
        print(f"AutoScout24: {e}"); return []

async def scrape_dubizzle(client, query, f):
    url = f"https://dubai.dubizzle.com/motors/used-cars/?keywords={query}"
    if f.get("max_price"): url += f"&price__lte={int(f['max_price'])*4}"
    if f.get("max_km"):    url += f"&kilometers__lte={f['max_km']}"
    if f.get("min_year"):  url += f"&year__gte={f['min_year']}"
    try:
        r = await client.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("[class*='listing'], article, [data-testid*='listing']")[:5]
        results = []
        for card in cards[:3]:
            item = make_listing(card,["h2","h3","[class*='title']"],
                ["[class*='price']","[data-testid*='price']"],"Dubizzle UAE","🇦🇪","AED","https://dubai.dubizzle.com")
            if item: results.append(item)
        return results
    except Exception as e:
        print(f"Dubizzle: {e}"); return []

async def scrape_encar(client, query, f):
    url = f"https://www.encar.com/search/list.do?catCd=kor&searchKey={query}"
    if f.get("min_year"): url += f"&yearMin={f['min_year']}"
    if f.get("max_km"):   url += f"&mileageMax={f['max_km']}"
    try:
        r = await client.get(url, headers={**HEADERS,"Accept-Language":"ko-KR"}, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select(".car-item, [class*='car'], .list-item")[:5]
        results = []
        for card in cards[:3]:
            item = make_listing(card,["[class*='name']","h3","h4"],
                ["[class*='price']",".price"],"Encar Korea","🇰🇷","KRW","https://www.encar.com")
            if item: results.append(item)
        return results
    except Exception as e:
        print(f"Encar: {e}"); return []

async def scrape_goonet(client, query, f):
    url = f"https://www.goo-net.com/cgi-bin/fsearch/goo_used_search.cgi?category=USDN&query={query}&phrase={query}"
    if f.get("min_year"): url += f"&year_min={f['min_year']}"
    if f.get("max_km"):   url += f"&mileage_max={f['max_km']}"
    try:
        r = await client.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select(".carList li, .result-item, [class*='car-item']")[:5]
        results = []
        for card in cards[:3]:
            item = make_listing(card,[".car-name","h3","h4","[class*='name']"],
                [".price","[class*='price']"],"Goo-net Japan","🇯🇵","JPY","https://www.goo-net.com")
            if item: results.append(item)
        return results
    except Exception as e:
        print(f"Goo-net: {e}"); return []

async def scrape_carsandbids(client, query, f):
    url = f"https://carsandbids.com/search?q={query}"
    if f.get("min_year"): url += f"&year_min={f['min_year']}&year_max=2026"
    try:
        r = await client.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select(".auction-item, article, [class*='listing']")[:5]
        results = []
        for card in cards[:3]:
            item = make_listing(card,["h2","h3","[class*='title']"],
                ["[class*='price']","[class*='bid']"],"Cars & Bids","🇺🇸","USD","https://carsandbids.com")
            if item:
                # Convert miles to km
                km_m = re.search(r'(\d[\d,]+)\s*mile', item.get("km",""), re.I)
                if km_m: item["km"] = f"{int(float(km_m.group(1).replace(',',''))*1.609):,} km"
                results.append(item)
        return results
    except Exception as e:
        print(f"C&B: {e}"); return []

SCRAPER_MAP = {
    "mobile": scrape_mobile, "autoscout": scrape_autoscout,
    "dubizzle": scrape_dubizzle, "encar": scrape_encar,
    "goonet": scrape_goonet, "cab": scrape_carsandbids,
}

# ── AI ANALYSIS ───────────────────────────────────────────────────────────
async def ai_analyze(listings, query, filters, openai_key):
    key = openai_key or OPENAI_KEY
    if not key or not listings: return None
    txt = "\n\n".join([
        f"{i+1}. {l['title']}\n   Price: {l['price']} (≈€{int(l['price_eur']):,})\n"
        f"   Market: €{int(l['market_eur']):,} | Saving: €{int(l['saving']):,}\n"
        f"   Year: {l.get('year','?')} | KM: {l.get('km','?')} | Fuel: {l.get('fuel','?')}\n"
        f"   Service: {l.get('service','?')} | Accident: {l.get('accident','?')}\n"
        f"   Owners: {l.get('owners','?')} | Source: {l['source']} | Location: {l.get('location','?')}\n"
        f"   URL: {l['url']}"
        for i,l in enumerate(listings)
    ])
    prompt = f"""Expert car dealer analyzing listings for "{query}".
Buyer: {f"max €{filters.get('max_price'):,}" if filters.get('max_price') else "no budget"}, {f"from {filters.get('min_year')}" if filters.get('min_year') else ""}, {f"max {filters.get('max_km'):,}km" if filters.get('max_km') else ""}

Consider: price vs mid-market, mileage/year ratio, service history, accidents, owners, import costs.

Listings:
{txt}

Rank top 3. Respond ONLY JSON:
{{"rankings":[{{"listing_number":1,"why":"2 sentences why best deal","red_flags":"specific issues or none","negotiation_tip":"one specific tactic","suggested_offer":"€XX,XXX"}},{{"listing_number":2,...}},{{"listing_number":3,...}}]}}"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
                json={"model":"gpt-4o-mini","max_tokens":700,"temperature":.2,
                      "messages":[{"role":"user","content":prompt}]}, timeout=20)
            text = r.json()["choices"][0]["message"]["content"]
            return json.loads(re.sub(r'```json|```','',text).strip())
    except Exception as e:
        print(f"AI: {e}"); return None

# ── TELEGRAM ──────────────────────────────────────────────────────────────
async def tg_send(token, chat_id, text):
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}, timeout=10)
    except Exception as e: print(f"TG: {e}")

# ── ENDPOINTS ─────────────────────────────────────────────────────────────
@app.get("/")
def root(): return {"status":"AutoJäger API","version":"2.0"}

@app.get("/health")
def health(): return {"ok":True}

@app.post("/search")
async def search(req: SearchRequest):
    filters = {k:v for k,v in {"max_price":req.max_price,"min_year":req.min_year,"max_km":req.max_km,"fuel":req.fuel}.items() if v}
    selected = req.sites or list(SCRAPER_MAP.keys())

    tok = req.tg_token or TG_TOKEN
    if tok and req.tg_chat_id:
        await tg_send(tok, req.tg_chat_id,
            f"🔍 <b>Search started: {req.query}</b>\n\nScanning {len(selected)} markets...\n"
            f"{'💶 Max €'+f'{req.max_price:,}' if req.max_price else ''} "
            f"{'📅 From '+str(req.min_year) if req.min_year else ''}\n"
            "Results in ~30 seconds...")

    all_listings = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                  timeout=httpx.Timeout(20.0), limits=httpx.Limits(max_connections=8)) as client:
        tasks = [SCRAPER_MAP[s](client, req.query, filters) for s in selected if s in SCRAPER_MAP]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list): all_listings.extend(r)

    all_listings.sort(key=lambda x: x.get("score",0), reverse=True)
    top10 = all_listings[:10]
    ai = await ai_analyze(top10, req.query, filters, req.openai_key)
    top3 = top10[:3]

    # Build Telegram message
    if tok and req.tg_chat_id and top3:
        medals = ["🥇","🥈","🥉"]
        rankings = (ai or {}).get("rankings", [])
        msg = f"🤖 <b>AutoJäger: {req.query}</b>\n"
        if req.max_price: msg += f"💶 Max €{req.max_price:,}  "
        if req.min_year:  msg += f"📅 From {req.min_year}  "
        if req.max_km:    msg += f"🛣 Max {req.max_km:,}km\n"
        msg += f"Analyzed <b>{len(all_listings)}</b> real listings\n\n"
        for i, car in enumerate(top3):
            ai_item = rankings[i] if i < len(rankings) else {}
            offer = ai_item.get("suggested_offer") or f"€{int(car['price_eur']*(1-(req.discount_pct or 9)/100)):,}"
            msg += f"{medals[i]} <b>{car['title'][:60]}</b>\n{car['flag']} {car['source']}\n"
            msg += f"💶 <b>€{int(car['price_eur']):,}</b>"
            if car['saving'] > 0: msg += f"  ·  Save €{int(car['saving']):,}"
            msg += "\n"
            if ai_item.get("why"): msg += f"✅ {ai_item['why']}\n"
            if ai_item.get("red_flags") and ai_item["red_flags"].lower() != "none":
                msg += f"⚠️ {ai_item['red_flags']}\n"
            msg += f"💡 Offer: <b>{offer}</b>\n"
            if ai_item.get("negotiation_tip"): msg += f"🗣 {ai_item['negotiation_tip']}\n"
            if car.get("year"): msg += f"📅 {car['year']}"
            if car.get("km"):   msg += f"  🛣 {car['km']}"
            if car.get("fuel"): msg += f"  ⛽ {car['fuel']}"
            msg += f"\n🔗 <a href=\"{car['url']}\">View real listing →</a>\n\n"
        msg += "─────────────────\n🤖 AI ranked by: price vs market, mileage, service history, accidents, condition"
        await tg_send(tok, req.tg_chat_id, msg)

    return {"query":req.query,"total_found":len(all_listings),"listings":top10,"top3":top3,"ai_analysis":ai}

@app.post("/negotiate")
async def negotiate(req: NegoRequest):
    key = req.openai_key or OPENAI_KEY
    if not key:
        templates = {
            "en": f"Hello,\n\nI'm interested in your {req.car.get('title','vehicle')}.\n\nBased on market research, I'd like to offer {req.offer}.\n\nKind regards",
            "de": f"Guten Tag,\n\nIhr {req.car.get('title','Fahrzeug')} interessiert mich sehr.\n\nIch möchte {req.offer} anbieten.\n\nMit freundlichen Grüßen",
        }
        return {"text": templates.get(req.language, templates["en"])}
    lang_names = {"en":"English","de":"German","fr":"French","jp":"Japanese","ar":"Arabic","kr":"Korean","cn":"Chinese","pl":"Polish","tr":"Turkish"}
    prompt = f"""Write a professional car purchase negotiation message in {lang_names.get(req.language,'English')}.
Car: {req.car.get('title','vehicle')} | Year: {req.car.get('year','?')} | KM: {req.car.get('km','?')}
Listed: {req.car.get('price','?')} | Market value: €{int(req.car.get('market_eur',0)):,}
My offer: {req.offer} | Source: {req.car.get('source','?')}
60-80 words. Polite, mention market research, express genuine interest. No subject line."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
                json={"model":"gpt-4o-mini","max_tokens":200,"messages":[{"role":"user","content":prompt}]}, timeout=15)
            return {"text": r.json()["choices"][0]["message"]["content"]}
    except Exception as e: return {"text": f"Error: {e}"}

@app.post("/subscribe")
async def subscribe(req: SubscribeRequest):
    """Create Stripe subscription"""
    if not stripe.api_key or "REPLACE" in stripe.api_key:
        return {"error": "Stripe not configured. Add STRIPE_SECRET_KEY to .env file."}
    try:
        customer = stripe.Customer.create(email=req.email, payment_method=req.payment_method,
            invoice_settings={"default_payment_method": req.payment_method})
        subscription = stripe.Subscription.create(
            customer=customer.id, items=[{"price": req.price_id}],
            payment_settings={"payment_method_types":["card"],"save_default_payment_method":"on_subscription"},
            expand=["latest_invoice.payment_intent"])
        pi = subscription.latest_invoice.payment_intent
        return {"subscription_id":subscription.id,"status":subscription.status,
                "client_secret":pi.client_secret if pi and pi.status=="requires_action" else None}
    except stripe.error.StripeError as e:
        return {"error": str(e.user_message)}

@app.post("/webhook")
async def webhook(request: Request):
    """Stripe webhook for subscription events"""
    payload = await request.body()
    sig = request.headers.get("stripe-signature","")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET","")
    try:
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret) if webhook_secret else json.loads(payload)
        if event["type"] == "customer.subscription.deleted":
            print(f"Subscription cancelled: {event['data']['object']['id']}")
        elif event["type"] == "invoice.payment_succeeded":
            print(f"Payment succeeded: {event['data']['object']['amount_paid']}")
    except Exception as e: raise HTTPException(400, str(e))
    return {"received": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
