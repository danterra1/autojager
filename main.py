"""
AutoJäger Agent v6 — Autonomous Deal Closer
============================================
The bot hunts, contacts sellers, and closes car deals autonomously.

What it does:
  1. Natural-language AI conversation (Claude-powered) — no wizard menus
  2. Collects buyer profile (name + email) once, reuses forever
  3. Scrapes real listings from AutoScout24 + Mobile.de
  4. Claude ranks top 3 deals by price vs market, km, year, seller type
  5. Autonomously contacts ALL 3 sellers via platform contact-form APIs
  6. Falls back to SMTP email if platform contact fails
  7. Shows buyer exactly what was sent to each seller
  8. Handles counter-offers — buyer pastes seller reply, bot counter-offers
  9. Tracks every deal: contacted → replied → negotiating → viewing → closed
 10. Pursues multiple deals in parallel for competitive leverage
"""

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, List
import asyncio, httpx, json, os, re, time, urllib.parse, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="AutoJäger Agent v6")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── CONFIG ────────────────────────────────────────────────────────────────
TG_TOKEN       = os.getenv("TG_BOT_TOKEN", "")
TG_BASE        = f"https://api.telegram.org/bot{TG_TOKEN}"
CLAUDE_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY", "")
AGENT_EMAIL    = os.getenv("AGENT_EMAIL", "")           # gmail/smtp account
AGENT_EMAIL_PW = os.getenv("AGENT_EMAIL_PASSWORD", "")
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))

SESSIONS: Dict = {}
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "8402310255"))

# ── HELPERS ───────────────────────────────────────────────────────────────
def fe(n):  return f"€{int(n or 0):,}"
def kmf(n): return f"{int(n):,} km" if n else "N/A"
def ts():   return int(time.time())

COUNTRY_LANG = {
    "DE":"de","AT":"de","CH":"de","FR":"fr","BE":"fr","JP":"jp","KR":"kr",
    "CN":"cn","TW":"cn","AE":"ar","SA":"ar","KW":"ar","QA":"ar","PL":"pl",
    "TR":"tr","NL":"nl","IT":"it","ES":"es","GB":"en","US":"en","AU":"en",
}
def slang(loc): return COUNTRY_LANG.get((loc.split(",")[-1].strip().upper()+"  ")[:2],"en")

def wa_link(phone, msg):
    clean = re.sub(r"[^\d]","",phone or "")
    if not clean: return None
    return f"https://wa.me/{clean}?text={urllib.parse.quote(msg)}"

# ── TELEGRAM ─────────────────────────────────────────────────────────────
async def tg(cid, text, buttons=None, parse_mode="HTML"):
    payload = {"chat_id":cid,"text":text,"parse_mode":parse_mode,"disable_web_page_preview":True}
    if buttons:
        kb = []
        for i in range(0, len(buttons), 2):
            row = [{"text":b[0],"callback_data":b[1]} for b in buttons[i:i+2]]
            kb.append(row)
        payload["reply_markup"] = json.dumps({"inline_keyboard":kb})
    async with httpx.AsyncClient() as c:
        try: return (await c.post(f"{TG_BASE}/sendMessage",json=payload,timeout=10)).json()
        except Exception as e: print(f"TG err: {e}"); return {}

async def tg_cb(cbid, text="✅"):
    async with httpx.AsyncClient() as c:
        try: await c.post(f"{TG_BASE}/answerCallbackQuery",json={"callback_query_id":cbid,"text":text},timeout=5)
        except: pass

# ── SESSION ───────────────────────────────────────────────────────────────
def get_sess(cid, username=None, name=None):
    k = str(cid)
    if k not in SESSIONS:
        SESSIONS[k] = {
            "cid":cid,"username":username or "","name":name or "",
            "state":"idle",
            "profile":{},          # {name, email, phone}
            "history":[],          # conversation history for Claude
            "deals":{},            # deal_id → deal object
            "active_deal":None,    # current deal being discussed
            "last_cars":[],
            "last_ai":None,
            "last_query":"",
        }
    if username: SESSIONS[k]["username"]=username
    if name:     SESSIONS[k]["name"]=name
    return SESSIONS[k]

def new_deal(cid, car, ai, offer):
    did = f"d{ts()}"
    deal = {
        "id":did,"cid":cid,"car":car,"ai":ai,
        "offer":offer,"status":"pending_contact",
        "contact_sent":False,"contact_method":None,
        "seller_reply":None,"counter":None,
        "agreed_price":None,"viewing_date":None,
        "history":[],"ts":ts(),
    }
    return did, deal

# ── SCRAPER ───────────────────────────────────────────────────────────────
MAKES = {
    "bmw":"bmw","porsche":"porsche","mercedes":"mercedes-benz","audi":"audi",
    "ferrari":"ferrari","lamborghini":"lamborghini","mclaren":"mclaren",
    "bentley":"bentley","rolls royce":"rolls-royce","aston martin":"aston-martin",
    "maserati":"maserati","jaguar":"jaguar","land rover":"land-rover",
    "volkswagen":"volkswagen","vw":"volkswagen","toyota":"toyota","nissan":"nissan",
    "honda":"honda","ford":"ford","volvo":"volvo","mazda":"mazda",
}

def make_slugs(query):
    ql = query.lower().strip()
    make = next((v for k,v in MAKES.items() if k in ql), None)
    model = ql
    for k in MAKES: model = model.replace(k,"").strip()
    return make, model.replace(" ","-") if model else None

async def scrape_as24(query, filters, n=5):
    make, model = make_slugs(query)
    path = "/lst"
    if make and model: path += f"/{make}/{model}"
    elif make:          path += f"/{make}"
    params = [("atype","C"),("cy","D,A,B,E,F,I,L,NL"),("damaged_listing","exclude"),
              ("sort","standard"),("ustate","N,U")]
    if filters.get("max_price"): params.append(("priceto",str(filters["max_price"])))
    if filters.get("min_year"):  params.append(("fregfrom",str(filters["min_year"])))
    if filters.get("max_km"):    params.append(("kmto",str(filters["max_km"])))
    fm = {"petrol":"B","diesel":"D","electric":"E","hybrid":"H"}
    if filters.get("fuel") in fm: params.append(("fuel",fm[filters["fuel"]]))
    if filters.get("trans") == "automatic": params.append(("gear","A"))
    if filters.get("trans") == "manual":    params.append(("gear","M"))
    if not make: params.append(("q",urllib.parse.quote(query)))
    url = "https://www.autoscout24.com"+path+"?"+"&".join(f"{k}={v}" for k,v in params)
    hdrs = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language":"de-DE,de;q=0.9,en-US;q=0.8","Accept-Encoding":"gzip, deflate, br"}
    try:
        async with httpx.AsyncClient(follow_redirects=True,headers=hdrs,timeout=20) as c:
            r = await c.get(url)
            if r.status_code != 200: return []
            m = re.search(r'id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', r.text)
            if not m: return []
            pp = json.loads(m.group(1)).get("props",{}).get("pageProps",{})
            items = (pp.get("listings",{}).get("items") or
                     pp.get("initialState",{}).get("listings",{}).get("items") or
                     pp.get("listingSearchResult",{}).get("listings") or [])
            cars = []
            for item in items[:n]:
                try:
                    price = (item.get("prices",{}).get("public",{}).get("priceRaw") or
                             item.get("price",{}).get("value") or 0)
                    km    = item.get("mileageInKmRaw") or item.get("mileage",{}).get("value") or 0
                    reg   = item.get("firstRegistrationDate") or item.get("registrationDate","")
                    year  = str(reg)[:4] if reg else ""
                    loc   = item.get("location",{})
                    city  = loc.get("city") or loc.get("cityName","")
                    ctry  = loc.get("countryCode") or loc.get("country","")
                    location = f"{city}, {ctry}".strip(", ")
                    seller   = item.get("seller") or item.get("vendor") or {}
                    sname    = (seller.get("name") or seller.get("companyName","")).strip()
                    stype    = "Dealer" if seller.get("type") in ("D","dealer","DEALER") else "Private"
                    phone    = re.sub(r"[^\d+]","",str(seller.get("phone") or seller.get("phoneNumber","") or ""))
                    lid      = str(item.get("id") or item.get("listingId",""))
                    raw_url  = item.get("url","")
                    if raw_url and not raw_url.startswith("http"):
                        raw_url = "https://www.autoscout24.com" + raw_url
                    elif not raw_url and lid:
                        raw_url = f"https://www.autoscout24.com/annonce/{lid}"
                    title = item.get("name") or item.get("title","") or query
                    if price:
                        cars.append({"source":"autoscout24","title":title,"price":price,
                            "year":year,"km":km,"location":location,"seller_name":sname,
                            "seller_type":stype,"phone":phone,"url":raw_url,
                            "fuel":item.get("fuel") or item.get("fuelTypeText",""),
                            "power":item.get("powerKw",""),"id":lid,"lang":slang(location)})
                except: continue
            return cars
    except Exception as e:
        print(f"AS24 err: {e}"); return []

async def scrape_mobilede(query, filters, n=5):
    """Scrape Mobile.de search results."""
    qe = urllib.parse.quote(query)
    url = f"https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&scopeId=C2C&vc=Car&makeModelVariant1.modelDescription={qe}"
    if filters.get("max_price"): url += f"&maxPrice={filters['max_price']}"
    if filters.get("max_km"):    url += f"&maxMileage={filters['max_km']}"
    if filters.get("min_year"):  url += f"&minFirstRegistrationDate={filters['min_year']}-01-01"
    hdrs = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language":"de-DE,de;q=0.9","Accept":"text/html,*/*"}
    try:
        async with httpx.AsyncClient(follow_redirects=True,headers=hdrs,timeout=20) as c:
            r = await c.get(url)
            if r.status_code != 200: return []
            items = re.findall(r'"id":"(\d+)"[^}]*?"price":\{"amount":(\d+)[^}]*?"mileage":\{"value":(\d+)[^}]*?"firstRegistration":"(\d{4})', r.text)
            cars = []
            for item_id, price, km, year in items[:n]:
                cars.append({
                    "source":"mobilede","title":query,
                    "price":int(price),"year":year,"km":int(km),
                    "location":"Germany","seller_name":"","seller_type":"",
                    "phone":"","url":f"https://suchen.mobile.de/fahrzeuge/details.html?id={item_id}",
                    "fuel":"","power":"","id":item_id,"lang":"de",
                })
            return cars
    except Exception as e:
        print(f"Mobile.de err: {e}"); return []

# ── AUTONOMOUS SELLER CONTACT ─────────────────────────────────────────────
async def contact_via_as24(listing_id, buyer, message):
    """Submit AutoScout24 contact form on behalf of buyer."""
    if not listing_id: return False, "no listing id"
    urls_to_try = [
        f"https://www.autoscout24.com/api/listings/{listing_id}/contact",
        f"https://www.autoscout24.com/_next/api/contact/listings/{listing_id}",
        f"https://www.autoscout24.com/api/v1/listings/{listing_id}/contact-seller",
    ]
    payload = {
        "name":    buyer.get("name","Interested Buyer"),
        "email":   buyer.get("email",""),
        "phone":   buyer.get("phone",""),
        "message": message,
        "locale":  "de_DE",
    }
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Origin": "https://www.autoscout24.com",
        "Referer": f"https://www.autoscout24.com/annonce/{listing_id}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as c:
        for url in urls_to_try:
            try:
                r = await c.post(url, json=payload, headers=hdrs)
                if r.status_code in (200, 201, 204):
                    return True, "autoscout24_form"
            except: continue
    return False, "as24_blocked"

async def contact_via_mobilede(listing_id, buyer, message):
    """Submit Mobile.de contact form."""
    if not listing_id: return False, "no id"
    url = f"https://m.mobile.de/svc/a/{listing_id}/contact"
    payload = {"name":buyer.get("name",""),"email":buyer.get("email",""),
               "text":message,"phone":buyer.get("phone","")}
    hdrs = {"User-Agent":"Mozilla/5.0","Content-Type":"application/json",
            "Origin":"https://suchen.mobile.de","Referer":f"https://suchen.mobile.de/fahrzeuge/details.html?id={listing_id}"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json=payload, headers=hdrs)
            if r.status_code in (200,201,204):
                return True, "mobilede_form"
    except: pass
    return False, "mobilede_blocked"

def send_email_smtp(to_email, subject, body, from_name="AutoJäger"):
    """Send email via SMTP (gmail/etc). Returns True on success."""
    if not AGENT_EMAIL or not AGENT_EMAIL_PW or not to_email:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{from_name} <{AGENT_EMAIL}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo(); srv.starttls(); srv.login(AGENT_EMAIL, AGENT_EMAIL_PW)
            srv.sendmail(AGENT_EMAIL, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"SMTP err: {e}"); return False

async def extract_seller_email_from_listing(url):
    """Try to extract seller email directly from listing page."""
    if not url: return None
    try:
        hdrs = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language":"de-DE,de;q=0.9"}
        async with httpx.AsyncClient(follow_redirects=True,headers=hdrs,timeout=15) as c:
            r = await c.get(url)
            emails = re.findall(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b', r.text)
            # filter out platform emails (noreply etc)
            skip = {"noreply","no-reply","info@autoscout","info@mobile","donotreply"}
            valid = [e for e in emails if not any(s in e.lower() for s in skip)]
            return valid[0] if valid else None
    except: return None

async def contact_seller_autonomous(car, buyer, message):
    """
    Try every available method to contact seller. Returns (success, method, detail).
    Order: platform form → email extraction+smtp → wa link only
    """
    results = []
    source = car.get("source","autoscout24")
    lid    = car.get("id","")
    url    = car.get("url","")

    # Method 1: Platform contact form
    if source == "autoscout24" and lid:
        ok, method = await contact_via_as24(lid, buyer, message)
        if ok:
            return True, method, "Message sent via AutoScout24 contact form"

    if source == "mobilede" and lid:
        ok, method = await contact_via_mobilede(lid, buyer, message)
        if ok:
            return True, method, "Message sent via Mobile.de contact form"

    # Method 2: Extract seller email from listing page → SMTP
    seller_email = await extract_seller_email_from_listing(url)
    if seller_email and AGENT_EMAIL:
        subj = f"Inquiry: {car.get('title','')} — {fe(car.get('price',0))}"
        ok   = send_email_smtp(seller_email, subj, message, from_name=buyer.get("name","Buyer"))
        if ok:
            return True, "email_smtp", f"Email sent to {seller_email}"

    # Method 3: WhatsApp (requires user tap — last resort)
    phone = car.get("phone","")
    if phone:
        wl = wa_link(phone, message)
        return False, "whatsapp_manual", wl or ""

    return False, "failed", "Could not contact seller automatically"

# ── AI ────────────────────────────────────────────────────────────────────
SYS_PROMPT = """You are AutoJäger, an expert autonomous car deal agent. You help buyers find and close the best car deals globally.

Your personality: direct, expert, proactive. You don't ask unnecessary questions — you take action.

You have these capabilities:
- Search 9+ markets worldwide (EU, Japan, Korea, UAE, USA)
- Autonomously contact sellers on the buyer's behalf
- Negotiate counter-offers in the seller's language
- Track multiple deals simultaneously
- Close deals end-to-end

When a buyer tells you what car they want, you:
1. Extract: make/model, budget, year range, max km, fuel preference
2. Confirm their name+email (needed to contact sellers) — ask once, remember forever
3. Search the market and find top 3 deals
4. Tell buyer you're contacting those sellers NOW
5. Handle all negotiation

Keep replies concise. Use bullet points for car specs. Be proactive — if you have enough info, act rather than ask.
Always reply in the same language the user writes in."""

async def _call_claude(messages, max_tokens=600):
    if not CLAUDE_KEY: return None
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-haiku-4-5-20251001","max_tokens":max_tokens,
                      "system":SYS_PROMPT,"messages":messages},
                timeout=30,
            )
            return r.json()["content"][0]["text"]
    except Exception as e: print(f"Claude err: {e}"); return None

async def _call_openai(messages, max_tokens=600):
    if not OPENAI_KEY: return None
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},
                json={"model":"gpt-4o-mini","max_tokens":max_tokens,"temperature":0.3,
                      "messages":[{"role":"system","content":SYS_PROMPT}]+messages},
                timeout=30,
            )
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e: print(f"OpenAI err: {e}"); return None

async def _ai(messages, max_tokens=600):
    r = await _call_claude(messages, max_tokens)
    return r if r else await _call_openai(messages, max_tokens)

async def ai_extract_search_params(text):
    """Extract structured search params from natural language."""
    prompt = f"""Extract car search parameters from this text. Respond ONLY in JSON, nothing else.
Text: "{text}"
JSON schema: {"make":"string or null","model":"string or null","max_price":"number_or_null","min_year":"number_or_null","max_km":"number_or_null","fuel":"petrol|diesel|electric|hybrid|null","trans":"automatic|manual|null","query":"full search string"}
Example: {"make":"BMW","model":"M4","max_price":70000,"min_year":2019,"max_km":60000,"fuel":"null","trans":"automatic","query":"BMW M4"}"""
    try:
        r = await _ai([{"role":"user","content":prompt}], 200)
        if r: return json.loads(re.sub(r'```json|```','',r).strip())
    except: pass
    return {"query": text, "make":None,"model":None,"max_price":None,"min_year":None,"max_km":None,"fuel":None,"trans":None}

async def ai_rank_deals(cars, query, filters):
    """Claude scores and ranks the listings."""
    if not cars: return cars
    if not CLAUDE_KEY and not OPENAI_KEY: return cars[:3]
    summary = "\n".join(f"{i+1}. {c['title']} | {fe(c['price'])} | {c['year']} | {kmf(c['km'])} | {c['seller_type']} | {c['location']}" for i,c in enumerate(cars))
    prompt  = f"""Rank these {len(cars)} car listings from best to worst deal for a buyer searching "{query}".
Criteria: price vs market, low km, recent year, private > dealer (better negotiation), EU location preferred.
Listings:
{summary}
Reply ONLY with JSON: {"ranked":[0,2,1]}  (0-indexed, best first, max 3)"""
    try:
        r = await _ai([{"role":"user","content":prompt}], 100)
        if r:
            d = json.loads(re.sub(r'```json|```','',r).strip())
            ranked = d.get("ranked",[])[:3]
            return [cars[i] for i in ranked if i < len(cars)]
    except: pass
    return cars[:3]

async def ai_market_analysis(query, filters):
    if not CLAUDE_KEY and not OPENAI_KEY: return None
    p = filters.get("max_price"); y = filters.get("min_year"); k = filters.get("max_km")
    prompt = f"""Market analysis for: {query}
Budget: {fe(p) if p else 'open'} | From year: {y or 'any'} | Max km: {kmf(k) if k else 'any'}
Respond ONLY in JSON: {"market_low":"int","market_high":"int","market_mid":"int","price_trend":"Rising|Stable|Falling","demand":"High|Medium|Low","negotiation_tip":"one sentence"}"""
    try:
        r = await _ai([{"role":"user","content":prompt}], 200)
        if r: return json.loads(re.sub(r'```json|```','',r).strip())
    except: pass
    return None

async def ai_opening_message(car, buyer, offer, lang):
    """Generate opening negotiation message in seller's language."""
    lang_names = {"en":"English","de":"German","fr":"French","jp":"Japanese","ar":"Arabic",
                  "kr":"Korean","cn":"Chinese","pl":"Polish","tr":"Turkish","nl":"Dutch","it":"Italian"}
    prompt = (f"Write a professional car purchase inquiry in {lang_names.get(lang,'English')}.\n"
              f"Car: {car['title']} listed at {fe(car['price'])}\n"
              f"My offer: {offer}\n"
              f"My name: {buyer.get('name','')}\n"
              f"Mention I've done market research. Be polite but firm. 80 words max. No subject line.")
    r = await _ai([{"role":"user","content":prompt}], 150)
    return r or _fallback_msg(car, buyer, offer, lang)

async def ai_counter(car, buyer, seller_says, my_offer, lang):
    """Generate counter-offer based on seller reply."""
    lang_names = {"en":"English","de":"German","fr":"French","jp":"Japanese","ar":"Arabic",
                  "kr":"Korean","cn":"Chinese","pl":"Polish","tr":"Turkish","nl":"Dutch","it":"Italian"}
    prompt = (f"Write a counter-offer in {lang_names.get(lang,'English')}.\n"
              f"Car: {car['title']} | My offer: {my_offer} | Seller said: \"{seller_says}\"\n"
              f"Be firm, cite market research, mention I'm looking at multiple cars. 60 words max.")
    r = await _ai([{"role":"user","content":prompt}], 120)
    return r or _fallback_counter(car, my_offer, lang)

def _fallback_msg(car, buyer, offer, lang):
    q = car.get("title","vehicle"); n = buyer.get("name","")
    t = {
        "en":f"Hello,\n\nMy name is {n}. I'm interested in your {q} (listed at {fe(car.get('price',0))}). Based on current market research I would like to offer {offer}. I am a serious buyer and can proceed quickly. I'm currently evaluating several options.\n\nKind regards,\n{n}",
        "de":f"Guten Tag,\n\nMein Name ist {n}. Ich interessiere mich für Ihr {q} (Preis: {fe(car.get('price',0))}). Basierend auf meiner Marktrecherche biete ich {offer} an. Ich bin ein ernsthafter Käufer und schaue mir mehrere Fahrzeuge an.\n\nMit freundlichen Grüßen,\n{n}",
        "ar":f"مرحباً,\n\nاسمي {n}. أنا مهتم بـ {q} وأقدم {offer} بناءً على أسعار السوق. أنا مشترٍ جاد.\n\nمع التحية,\n{n}",
        "fr":f"Bonjour,\n\nJe m'appelle {n} et je suis intéressé par votre {q}. Je propose {offer} selon les prix du marché.\n\nCordialement,\n{n}",
        "jp":f"はじめまして。{n}と申します。{q}に興味があり、市場調査に基づいて{offer}を提案します。\n\nよろしくお願いします。",
    }
    return t.get(lang, t["en"])

def _fallback_counter(car, offer, lang):
    t = {
        "en":f"Thank you for your response. I understand your position, however based on current market prices for comparable vehicles, {offer} remains my best offer. I have other options I'm considering as well. I hope we can find an agreement.",
        "de":f"Danke für Ihre Antwort. Basierend auf aktuellen Marktpreisen ist {offer} mein bestes Angebot. Ich prüfe auch andere Fahrzeuge.",
        "ar":f"شكراً للرد. بناءً على أسعار السوق الحالية، {offer} هو أفضل عرض لدي.",
        "fr":f"Merci pour votre réponse. Selon les prix du marché actuel, {offer} reste ma meilleure offre.",
    }
    return t.get(lang, t["en"])

async def ai_conversation(s, user_msg):
    """Main conversational AI — handles anything not caught by structured handlers."""
    history = s.get("history",[])[-10:]  # last 10 turns
    history.append({"role":"user","content":user_msg})
    resp = await _ai(history, 400)
    if resp:
        s["history"] = s.get("history",[])[-18:] + [{"role":"user","content":user_msg},{"role":"assistant","content":resp}]
    return resp

# ── DEAL PIPELINE ─────────────────────────────────────────────────────────
async def hunt_and_contact(cid, query, filters):
    """Core: search → rank → contact top 3 sellers autonomously."""
    s   = get_sess(cid)
    buyer = s.get("profile",{})

    await tg(cid,
        f"🎯 <b>On it. Hunting for: {query}</b>\n\n"
        f"⏳ Scanning AutoScout24 + Mobile.de...\n"
        f"🤖 Ranking deals by price vs market, km, condition..."
    )

    # Scrape both sources in parallel
    as24_task   = asyncio.create_task(scrape_as24(query, filters, n=8))
    mde_task    = asyncio.create_task(scrape_mobilede(query, filters, n=5))
    ai_task     = asyncio.create_task(ai_market_analysis(query, filters))
    all_cars, mde_cars, ai = await asyncio.gather(as24_task, mde_task, ai_task)

    all_cars = all_cars + [c for c in mde_cars if c.get("price")]
    s["last_ai"]    = ai
    s["last_query"] = query

    if not all_cars:
        await tg(cid,
            "⚠️ <b>No live listings found right now.</b>\n\n"
            "AutoScout24 may be blocking the scraper from Render's IP.\n"
            "Try: /links — to open search pages directly.",
            [["🌍 Open Search Links","show_links"],["🔍 Try Different Car","do_search"]]
        ); return

    # AI ranks top 3
    await tg(cid,"🤖 <i>Claude is ranking the best deals...</i>")
    top3 = await ai_rank_deals(all_cars, query, filters)
    s["last_cars"] = top3

    mid = ai.get("market_mid",0) if ai else 0

    # Show what was found
    msg = f"🏆 <b>Top {len(top3)} deals found for: {query}</b>\n"
    if ai:
        msg += f"💶 Market: {fe(ai.get('market_low',0))} – {fe(ai.get('market_high',0))} · Mid {fe(mid)}\n"
        if ai.get("negotiation_tip"):
            msg += f"💡 Tip: {ai['negotiation_tip']}\n"
    msg += "\n"

    for i, car in enumerate(top3, 1):
        vs_market = ""
        if mid and car.get("price"):
            diff = round((car["price"] - mid) / mid * 100)
            vs_market = f" <b>({'+'if diff>0 else ''}{diff}% vs market)</b>"
        msg += f"<b>{i}. {car['title']}</b>{vs_market}\n"
        msg += f"   💶 {fe(car['price'])} · 📅 {car.get('year','?')} · 🛣 {kmf(car.get('km'))}\n"
        msg += f"   📍 {car.get('location','')} · {car.get('seller_type','')}\n"
        if car.get("phone"): msg += f"   📞 Has phone\n"
        msg += "\n"

    # If buyer has profile, auto-contact. Otherwise ask first.
    if buyer.get("name") and buyer.get("email"):
        msg += "🤖 <b>I'm contacting all 3 sellers now on your behalf...</b>"
        await tg(cid, msg)
        await contact_all_top3(cid, top3, ai, mid)
    else:
        msg += "👇 <b>Should I contact all 3 sellers now?</b>\n<i>I'll need your name + email to send the messages.</i>"
        await tg(cid, msg, [
            ["✅ Yes — contact them all","confirm_contact_all"],
            ["📝 Pick one car","pick_one"],
            ["🌍 Just give links","show_links"],
        ])

async def contact_all_top3(cid, cars, ai, mid):
    s     = get_sess(cid)
    buyer = s.get("profile",{})
    results = []

    for i, car in enumerate(cars, 1):
        # Calculate offer: 91% of market mid or 91% of asking
        base  = mid if mid else car.get("price",0)
        offer = fe(round(base * 0.91 / 500) * 500)
        lang  = car.get("lang","en")

        # Generate opening message
        msg_text = await ai_opening_message(car, buyer, offer, lang)

        # Contact seller
        ok, method, detail = await contact_seller_autonomous(car, buyer, msg_text)

        # Create deal record
        did, deal = new_deal(cid, car, ai, offer)
        deal["message_sent"] = msg_text
        deal["contact_method"] = method
        deal["contact_ok"]     = ok
        deal["status"] = "contacted" if ok else "contact_manual"
        deal["lang"]   = lang
        s["deals"][did] = deal

        if ok:
            icon = "✅"
            how  = {"autoscout24_form":"via AS24 form","mobilede_form":"via Mobile.de form",
                    "email_smtp":f"email ({detail.split('to ')[-1] if 'to ' in detail else 'email'})"}
            results.append(f"{icon} <b>#{i} {car['title'][:35]}</b>\n   Contacted {how.get(method,'directly')} · Offer: {offer}\n   Awaiting reply...")
        else:
            if method == "whatsapp_manual" and detail:
                icon = "📲"
                s["deals"][did]["wa_link"] = detail
                results.append(f"{icon} <b>#{i} {car['title'][:35]}</b>\n   No direct contact available. <a href=\"{detail}\">Tap to WhatsApp</a> · Offer: {offer}\n")
            else:
                icon = "⚠️"
                results.append(f"{icon} <b>#{i} {car['title'][:35]}</b>\n   Could not contact automatically. Use /deals to get WhatsApp link.\n")

    summary = "🚀 <b>Done! Here's what I did:</b>\n\n" + "\n".join(results)
    summary += "\n\n<i>When a seller replies, paste their message here and I'll handle the negotiation.</i>"
    await tg(cid, summary, [
        ["📋 View My Deals","view_deals"],
        ["💬 Seller Replied","seller_replied_menu"],
        ["🔍 New Search","do_search"],
    ])

# ── PROFILE COLLECTION ────────────────────────────────────────────────────
async def ask_for_profile(cid):
    s = get_sess(cid)
    s["state"] = "awaiting_name"
    await tg(cid,
        "👤 <b>Quick setup — I need your details to contact sellers.</b>\n\n"
        "This is sent to sellers so they can reply directly to you.\n\n"
        "<i>What's your full name?</i>"
    )

async def handle_profile_steps(cid, text):
    s     = get_sess(cid)
    state = s.get("state","")

    if state == "awaiting_name":
        s["profile"]["name"] = text.strip()
        s["state"] = "awaiting_email"
        await tg(cid, f"✅ Got it, <b>{text.strip()}</b>.\n\n<i>Your email address?</i> (sellers reply to this)")

    elif state == "awaiting_email":
        if "@" not in text:
            await tg(cid,"❌ That doesn't look like a valid email. Try again:"); return
        s["profile"]["email"] = text.strip().lower()
        s["state"] = "awaiting_phone"
        await tg(cid, "✅ Email saved.\n\n<i>Phone number? (optional — skip by typing 'skip')</i>")

    elif state == "awaiting_phone":
        if text.lower() not in ("skip","no","none","-"):
            s["profile"]["phone"] = re.sub(r"[^\d+]","",text)
        s["state"] = "idle"
        profile = s["profile"]
        await tg(cid,
            f"✅ <b>Profile saved!</b>\n\n"
            f"👤 {profile.get('name','')}\n"
            f"📧 {profile.get('email','')}\n"
            f"📞 {profile.get('phone','') or 'Not provided'}\n\n"
            f"Now tell me what car you're looking for and I'll hunt it down! 🏎"
        )
        # If there's a pending search, run it now
        pending = s.pop("pending_search", None)
        if pending:
            await hunt_and_contact(cid, pending["query"], pending["filters"])

# ── MESSAGE HANDLER ─────────────────────────────────────────────────────────────────
HELP = """🤖 <b>AutoJäger — Autonomous Car Deal Agent</b>

<b>How it works:</b>
1️⃣ Tell me what car you want (natural language)
2️⃣ I search AutoScout24 + Mobile.de for best deals
3️⃣ I contact all top sellers <b>automatically</b>
4️⃣ I negotiate counter-offers when they reply
5️⃣ Deal closed 🎉

<b>Examples:</b>
• "Find me a BMW M4 under €65k, max 60k km"
• "I want a Porsche 911 GT3, 2019+, automatic"
• "Looking for Mercedes E63 AMG under €80k"

<b>Commands:</b>
/deals — view active deals
/profile — update your contact info
/help — this menu"""

async def handle_msg(cid, username, name, text):
    s  = get_sess(cid, username, name)
    tl = text.lower().strip()

    # Profile collection flow
    if s.get("state") in ("awaiting_name","awaiting_email","awaiting_phone"):
        await handle_profile_steps(cid, text); return

    if tl.startswith("/start"):
        profile = s.get("profile",{})
        has_profile = bool(profile.get("name") and profile.get("email"))
        await tg(cid,
            f"🏎 <b>AutoJäger — Autonomous Car Deal Agent</b>\n\n"
            f"Hi <b>{name}</b>! I hunt car deals globally and contact sellers for you automatically.\n\n"
            ("✅ Profile: " + profile.get("name","") + " | " + profile.get("email","") if has_profile else "⚠️ Profile not set — I need your name + email to contact sellers."),
            [["🔍 Find a Car","do_search"],["👤 Set Profile","setup_profile"],["❓ Help","do_help"]]
        ); return

    if tl in ("/help","/menu"):
        await tg(cid, HELP, [["🔍 Find a Car","do_search"]]); return

    if tl.startswith("/profile"):
        await ask_for_profile(cid); return

    if tl.startswith("/deals"):
        await show_deals(cid); return

    if s.get("state") == "awaiting_seller_reply":
        await handle_seller_reply(cid, text); return

    if s.get("state") == "awaiting_custom_offer":
        m = re.search(r'\d[\d,.]*',text.replace(' ',''))
        if m:
            val = int(float(m.group(0).replace(',','')))
            s["state"] = "idle"
            did = s.get("active_deal_id")
            if did and did in s["deals"]:
                deal = s["deals"][did]
                deal["offer"] = fe(val)
                await regenerate_contact_msg(cid, did)
        else:
            await tg(cid,"❌ Enter a number, e.g. 62000"); return

    # ── Main: try to extract car search intent ──────────────────────────────
    # Detect if this is a search request
    search_signals = any(w in tl for w in [
        "find","search","looking for","want","need","buy","bmw","porsche","audi","mercedes",
        "ferrari","lambo","volkswagen","ford","toyota","honda","budget","price","max","under",
        "mileage","year","automatic","manual","diesel","petrol","electric","suv","coupe"
    ])

    if search_signals or len(text) > 5:
        params = await ai_extract_search_params(text)
        query  = params.get("query") or text
        filters = {k:v for k,v in {
            "max_price": params.get("max_price"),
            "min_year":  params.get("min_year"),
            "max_km":    params.get("max_km"),
            "fuel":      params.get("fuel"),
            "trans":     params.get("trans"),
        }.items() if v}

        profile = s.get("profile",{})
        if not profile.get("name") or not profile.get("email"):
            # Save search, collect profile first
            s["pending_search"] = {"query": query, "filters": filters}
            await tg(cid,
                f"🔍 Got it — <b>{query}</b>\n\n"
                f"Before I contact sellers I need your details (name + email).\n"
                f"Sellers will reply directly to you.",
                [["👤 Set up profile","setup_profile"]]
            )
        else:
            await hunt_and_contact(cid, query, filters)
    else:
        # Conversational fallback
        resp = await ai_conversation(s, text)
        if resp:
            await tg(cid, resp, [["🔍 Search a Car","do_search"],["📋 My Deals","view_deals"]])
        else:
            await tg(cid,"👋 Tell me what car you're looking for and I'll hunt it down!",
                [["🔍 Search","do_search"],["📋 Deals","view_deals"],["❓ Help","do_help"]])

async def show_deals(cid):
    s     = get_sess(cid)
    deals = s.get("deals",{})
    if not deals:
        await tg(cid,"📋 No active deals yet.",[["🔍 Search a Car","do_search"]]); return
    STATUS_ICON = {
        "contacted":"📤","contact_manual":"📲","negotiating":"💬",
        "agreed":"🤝","viewing":"📅","closed":"✅","refused":"❌","pending_contact":"⏳"
    }
    msg = "📋 <b>Your Active Deals</b>\n\n"
    buttons = []
    for did, d in sorted(deals.items(), key=lambda x: x[1].get("ts",0), reverse=True)[:8]:
        car    = d.get("car",{})
        icon   = STATUS_ICON.get(d.get("status",""),"🔄")
        price  = fe(car.get("price",0)); offer = d.get("offer","")
        status = d.get("status","").replace("_"," ").title()
        msg   += f"{icon} <b>{car.get('title','')[:35]}</b>\n"
        msg   += f"   Ask: {price} · My offer: {offer} · {status}\n"
        if d.get("wa_link"):
            msg += f"   <a href=\"{d['wa_link']}\">📲 Contact via WhatsApp</a>\n"
        msg += "\n"
        buttons.append([f"💬 Reply #{did[-4:]}", f"reply_{did}"])
    buttons += [["🔍 New Search","do_search"],["❌ Clear All","clear_deals"]]
    await tg(cid, msg, buttons[:10])

async def handle_seller_reply(cid, text):
    s   = get_sess(cid)
    did = s.get("active_deal_id")
    if not did or did not in s.get("deals",{}):
        s["state"] = "idle"
        await tg(cid,"❌ No active deal. Use /deals to pick one.",[["📋 Deals","view_deals"]]); return

    deal  = s["deals"][did]
    car   = deal.get("car",{})
    offer = deal.get("offer","")
    lang  = deal.get("lang","en")
    ai    = deal.get("ai",{}) or {}

    await tg(cid,"🤖 <i>Generating counter-offer...</i>")
    counter = await ai_counter(car, s.get("profile",{}), text, offer, lang)
    deal["seller_reply"] = text
    deal["counter"]      = counter
    deal["status"]       = "negotiating"
    s["state"]           = "idle"

    FLAGS = {"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷","cn":"🇨🇳","pl":"🇵🇱","tr":"🇹🇷"}
    wl = wa_link(car.get("phone",""), counter)
    if wl: deal["wa_link"] = wl; s["deals"][did] = deal

    buttons = []
    if wl: buttons.append(["📲 Send on WhatsApp","wa_counter_"+did])
    buttons += [["✅ Mark Agreed","agreed_"+did],["🔄 Regenerate","regen_counter_"+did],
                ["💶 Change Offer","change_offer_"+did],["❌ Walk Away","refuse_"+did]]

    await tg(cid,
        f"🔄 <b>Counter-Offer {FLAGS.get(lang,'')} ({lang.upper()})</b>\n\n"
        f"Seller said: <i>\"{text[:120]}\"</i>\n\n"
        f"My response:\n<code>{counter}</code>",
        buttons
    )

async def regenerate_contact_msg(cid, did):
    s    = get_sess(cid)
    deal = s["deals"].get(did)
    if not deal: return
    car  = deal.get("car",{}); offer = deal.get("offer",""); lang = deal.get("lang","en")
    msg  = await ai_opening_message(car, s.get("profile",{}), offer, lang)
    deal["message_sent"] = msg
    wl = wa_link(car.get("phone",""), msg)
    if wl: deal["wa_link"] = wl
    FLAGS = {"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷","cn":"🇨🇳","pl":"🇵🇱","tr":"🇹🇷"}
    buttons = []
    if wl: buttons.append([f"📲 WhatsApp ({offer})","wa_deal_"+did])
    if car.get("url"): buttons.append(["🔗 Listing","view_listing_"+did])
    buttons += [["💶 Change Offer","change_offer_"+did],["📋 All Deals","view_deals"]]
    await tg(cid,
        f"✍️ <b>Message {FLAGS.get(lang,'')} ({lang.upper()})</b>\n\n"
        f"Car: {car.get('title','')}\nOffer: <b>{offer}</b>\n\n"
        f"<code>{msg}</code>",
        buttons
    )

# ── CALLBACK HANDLER ──────────────────────────────────────────────────────
async def handle_cb(cid, username, name, cbd, cbid):
    s = get_sess(cid, username, name)

    if cbd == "setup_profile" or cbd == "do_profile":
        await tg_cb(cbid); await ask_for_profile(cid)

    elif cbd == "confirm_contact_all":
        await tg_cb(cbid,"🚀 Contacting sellers...")
        cars = s.get("last_cars",[])
        ai   = s.get("last_ai")
        mid  = ai.get("market_mid",0) if ai else 0
        if not s.get("profile",{}).get("name"):
            s["state"]="awaiting_name"; await ask_for_profile(cid); return
        await contact_all_top3(cid, cars, ai, mid)

    elif cbd == "view_deals" or cbd == "do_status":
        await tg_cb(cbid); await show_deals(cid)

    elif cbd.startswith("reply_"):
        did = cbd[6:]
        await tg_cb(cbid,"💬")
        if did in s.get("deals",{}):
            s["state"] = "awaiting_seller_reply"
            s["active_deal_id"] = did
            deal = s["deals"][did]
            await tg(cid,
                f"💬 <b>What did the seller say?</b>\n\n"
                f"Deal: {deal.get('car',{}).get('title','')}\n"
                f"My offer: {deal.get('offer','')}\n\n"
                f"<i>Paste their reply and I'll draft a counter:</i>"
            )

    elif cbd == "seller_replied_menu":
        await tg_cb(cbid)
        deals = {k:v for k,v in s.get("deals",{}).items() if v.get("status") in ("contacted","negotiating")}
        if not deals:
            await tg(cid,"No active contacted deals.",[["📋 All Deals","view_deals"]]); return
        buttons = [[f"💬 {v.get('car',{}).get('title','')[:25]} (#{k[-4:]})", f"reply_{k}"] for k,v in list(deals.items())[:5]]
        await tg(cid,"Which deal got a reply?", buttons)

    elif cbd.startswith("wa_counter_"):
        did  = cbd[11:]
        deal = s.get("deals",{}).get(did,{})
        wl   = deal.get("wa_link")
        await tg_cb(cbid,"📲")
        if wl: await tg(cid,f"📲 <a href=\"{wl}\">Open WhatsApp to send counter-offer →</a>\n\nMessage is pre-written. Just tap Send.",
            [["✅ Mark Agreed","agreed_"+did],["💬 They replied again","reply_"+did]])
        else: await tg(cid,"No phone for this seller.")

    elif cbd.startswith("wa_deal_"):
        did  = cbd[8:]
        deal = s.get("deals",{}).get(did,{})
        wl   = deal.get("wa_link")
        await tg_cb(cbid,"📲")
        if wl: await tg(cid,f"📲 <a href=\"{wl}\">Open WhatsApp to contact seller →</a>")

    elif cbd.startswith("agreed_"):
        did = cbd[7:]
        await tg_cb(cbid,"🤝 Deal agreed!")
        if did in s.get("deals",{}):
            s["deals"][did]["status"] = "agreed"
            car = s["deals"][did].get("car",{})
            offer = s["deals"][did].get("offer","")
            await tg(cid,
                f"🤝 <b>DEAL AGREED!</b>\n\nCar: <b>{car.get('title','')}</b>\nPrice: <b>{offer}</b>\n\n"
                f"Next: confirm viewing/inspection date with the seller.",
                [["📋 All Deals","view_deals"],["🔍 Find Another","do_search"]]
            )

    elif cbd.startswith("refuse_"):
        did = cbd[7:]
        await tg_cb(cbid)
        if did in s.get("deals",{}): s["deals"][did]["status"] = "refused"
        await tg(cid,"Deal closed. On to the next! 🔍",[["🔍 New Search","do_search"],["📋 Deals","view_deals"]])

    elif cbd.startswith("regen_counter_"):
        did = cbd[14:]
        await tg_cb(cbid,"🔄")
        if did in s.get("deals",{}):
            deal  = s["deals"][did]
            s["state"]="awaiting_seller_reply"; s["active_deal_id"]=did
            await handle_seller_reply(cid, deal.get("seller_reply",""))

    elif cbd.startswith("change_offer_"):
        did = cbd[13:]
        await tg_cb(cbid)
        s["state"]="awaiting_custom_offer"; s["active_deal_id"]=did
        deal = s.get("deals",{}).get(did,{})
        await tg(cid,f"💶 New offer amount for {deal.get('car',{}).get('title','')[:30]}?\n<i>Type a number e.g. 62000</i>")

    elif cbd.startswith("view_listing_"):
        did = cbd[13:]
        deal = s.get("deals",{}).get(did,{})
        url  = deal.get("car",{}).get("url","")
        await tg_cb(cbid)
        if url: await tg(cid,f'🔗 <a href="{url}">View listing →</a>')

    elif cbd == "show_links":
        await tg_cb(cbid)
        q = s.get("last_query","")
        f = {}
        links = build_links(q, f)
        msg   = f"🌍 <b>Search links for: {q}</b>\n\n"
        for site,url in links.items(): msg += f"🔗 <a href=\"{url}\">{site}</a>\n"
        await tg(cid, msg)

    elif cbd == "clear_deals":
        await tg_cb(cbid)
        s["deals"] = {}
        await tg(cid,"✅ Deals cleared.",[["🔍 New Search","do_search"]])

    elif cbd in ("do_search","refine"):
        await tg_cb(cbid); s["state"]="idle"
        await tg(cid,"🔍 <b>What car are you looking for?</b>\n\n<i>Just describe it naturally:</i>\n\"BMW M4 under €70k, max 50k km, 2020+\"")

    elif cbd == "do_help":
        await tg_cb(cbid); await tg(cid, HELP, [["🔍 Search","do_search"]])

    elif cbd == "pick_one":
        await tg_cb(cbid)
        cars = s.get("last_cars",[])
        if not cars: await tg(cid,"No results.",[["🔍 Search","do_search"]]); return
        buttons = [[f"{i+1}. {c.get('title','')[:28]} {fe(c.get('price',0))}", f"contact_one_{i}"] for i,c in enumerate(cars[:3])]
        await tg(cid,"Which car should I contact?", buttons)

    elif cbd.startswith("contact_one_"):
        idx = int(cbd[12:])
        await tg_cb(cbid,"📞")
        cars = s.get("last_cars",[])
        if idx >= len(cars): return
        car   = cars[idx]
        ai    = s.get("last_ai")
        mid   = ai.get("market_mid",0) if ai else 0
        offer = fe(round((mid if mid else car["price"]) * 0.91 / 500) * 500)
        if not s.get("profile",{}).get("name"):
            s["pending_search"] = {"single_car":car,"offer":offer}
            await ask_for_profile(cid); return
        await do_single_contact(cid, car, ai, offer)


    # ── IMPORT ARBITRAGE CALLBACKS ────────────────────────────────────────
    elif cbd.startswith("import_orig_"):
        await tg_cb(cbid)
        orig = cbd.split("_")[2]
        pending = s.get("pending_import", {})
        if pending.get("query"):
            await run_import_calc(cid, pending["query"], orig)

    elif cbd.startswith("import_dest_"):
        await tg_cb(cbid)
        dest = cbd.split("_")[2]
        imp = s.get("last_import", {})
        if imp:
            await run_import_calc(cid, imp["query"], imp["origin"], dest=dest, car_price=imp["car_price"])

    elif cbd.startswith("import_full_"):
        await tg_cb(cbid)
        parts = cbd.split("_", 3)
        if len(parts) >= 4:
            orig, query = parts[2], parts[3].replace("_"," ")
            await run_import_calc(cid, query, orig)

    elif cbd in ("import_search","import_search_now"):
        await tg_cb(cbid)
        s["state"] = "awaiting_query"
        await tg(cid, "🔍 What car to search and import?")

    elif cbd == "import_proceed":
        await tg_cb(cbid)
        imp = s.get("last_import", {})
        if imp:
            await tg(cid,
                f"✅ <b>Starting import: {imp['query']}</b>\n\n"
                f"1. Search listings: /search {imp['query']}\n"
                f"2. Contact seller → I handle negotiation\n"
                f"3. Request COC from manufacturer\n"
                f"4. Book shipping from {imp['origin']}\n"
                f"5. I generate all paperwork\n\nReady?",
                [["🔍 Search Now","import_search_now"],["📄 Paperwork","import_paperwork"]]
            )

    elif cbd == "import_howto":
        await tg_cb(cbid)
        await tg(cid,
            "🛃 <b>Import Arbitrage Guide</b>\n\n"
            "1️⃣ Find car cheaper abroad\n"
            "2️⃣ Exact landed cost = shipping + customs + VAT + homologation\n"
            "3️⃣ Compare to EU price = your profit\n\n"
            "🇰🇷 Korea: 0% duty (EU-Korea FTA)\n"
            "🇯🇵 Japan: 0% duty (EU-Japan EPA)\n"
            "🇦🇪 UAE: 6.5% duty but very cheap stock\n"
            "🇺🇸 USA: 6.5% duty, good for muscle cars\n\n"
            "Try: /import BMW M4 from Korea",
            [["📊 Start Calc","import_search"]]
        )

    elif cbd == "import_compare":
        await tg_cb(cbid)
        imp = s.get("last_import", {})
        if imp:
            await handle_import_compare(cid, imp["query"])

    elif cbd == "import_paperwork":
        await tg_cb(cbid)
        await tg(cid, "📄 Which document?",
            [["🛃 Customs declaration","doc_customs"],
             ["📋 COC request","doc_coc"],
             ["💶 VAT reclaim","doc_vat"]]
        )

    elif cbd.startswith("doc_"):
        await tg_cb(cbid)
        doc_map = {"doc_customs":"customs_declaration","doc_coc":"coc_request","doc_vat":"vat_reclaim"}
        doc_type = doc_map.get(cbd, "customs_declaration")
        imp = s.get("last_import", {})
        profile = s.get("profile", {})
        car_info = {"title":imp.get("query",""),"price":imp.get("car_price",0),"origin":imp.get("origin","")}
        await generate_paperwork(cid, doc_type, car_info, profile)

    elif cbd.startswith("open_encar_"):
        await tg_cb(cbid)
        q = cbd[11:].replace("_"," ")
        link = f"https://www.encar.com/search/list.do?catCd=kor&searchKey={q.replace(' ','+')}"
        await tg(cid, f"🇰🇷 <b>Encar Korea: {q}</b>\n🔗 <a href=\"{link}\">Search Encar →</a>\n\nFind a car, copy the price, then:\n/import {q} from Korea [PRICE]")

    elif cbd.startswith("open_goonet_"):
        await tg_cb(cbid)
        q = cbd[12:].replace("_"," ")
        link = f"https://www.goo-net.com/cgi-bin/fsearch/goo_used_search.cgi?category=USDN&query={q.replace(' ','+')}"
        await tg(cid, f"🇯🇵 <b>Goo-net Japan: {q}</b>\n🔗 <a href=\"{link}\">Search Goo-net →</a>\n\nFind a car, copy the price, then:\n/import {q} from Japan [PRICE]")



    # ── WATCHLIST CALLBACKS ───────────────────────────────────────────────
    elif cbd == "do_watchlist":
        await tg_cb(cbid)
        await handle_watchlist_command(cid, username)

    elif cbd == "watch_check_now":
        await handle_watch_check_now(cid, cbid)

    elif cbd == "watch_clear_sold":
        await handle_watch_clear_sold(cid, cbid)

    elif cbd == "watch_report":
        await tg_cb(cbid)
        if CHANNEL_IDS:
            for ch in list(CHANNEL_IDS)[:1]:
                await send_daily_report(ch)
        else:
            await send_daily_report(cid)

    elif cbd == "watchlist_howto":
        await handle_watchlist_howto(cid, cbid)

    elif cbd.startswith("watch_remove_"):
        await tg_cb(cbid)
        lid = cbd[13:]
        await handle_watch_remove(cid, lid)


    elif cbd == "do_cancel":
        await tg_cb(cbid,"❌"); s["state"]="idle"
        await tg(cid,"Cancelled.",[["🔍 Search","do_search"]])

def build_links(q, filters):
    qu = urllib.parse.quote(q); qp = q.replace(" ","+")
    return {
        "🇩🇪 Mobile.de":       f"https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&scopeId=C2C&vc=Car&makeModelVariant1.modelDescription={qu}",
        "🇪🇺 AutoScout24":     f"https://www.autoscout24.com/lst?atype=C&sort=standard&ustate=N%2CU&q={qu}",
        "🇦🇪 Dubizzle UAE":    f"https://dubai.dubizzle.com/motors/used-cars/?keywords={qp}",
        "🇰🇷 Encar Korea":     f"https://www.encar.com/search/list.do?catCd=kor&searchKey={qu}",
        "🇯🇵 Goo-net Japan":   f"https://www.goo-net.com/cgi-bin/fsearch/goo_used_search.cgi?category=USDN&query={qu}",
        "🇺🇸 Cars & Bids":     f"https://carsandbids.com/search?q={qp}",
        "🇵🇱 Otomoto":         f"https://www.otomoto.pl/osobowe?search%5Bq%5D={qu}",
        "🇹🇷 Sahibinden":      f"https://www.sahibinden.com/vasita-otomobil?query_text={qu}",
    }

async def do_single_contact(cid, car, ai, offer):
    s     = get_sess(cid)
    buyer = s.get("profile",{})
    lang  = car.get("lang","en")
    msg_text = await ai_opening_message(car, buyer, offer, lang)
    ok, method, detail = await contact_seller_autonomous(car, buyer, msg_text)
    did, deal = new_deal(cid, car, ai, offer)
    deal["message_sent"]=msg_text; deal["contact_method"]=method; deal["contact_ok"]=ok
    deal["status"] = "contacted" if ok else "contact_manual"; deal["lang"]=lang
    if method=="whatsapp_manual" and detail: deal["wa_link"]=detail
    s["deals"][did] = deal
    FLAGS = {"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷"}
    buttons = []
    wl = wa_link(car.get("phone",""), msg_text)
    if wl: deal["wa_link"]=wl; buttons.append(["📲 Send via WhatsApp","wa_deal_"+did])
    if car.get("url"): buttons.append(["🔗 View Listing","view_listing_"+did])
    buttons += [["💬 Seller Replied","reply_"+did],["📋 All Deals","view_deals"]]
    status_line = "✅ Message sent automatically!" if ok else "📲 Use WhatsApp button to send manually."
    await tg(cid,
        f"{'✅' if ok else '📲'} <b>{car.get('title','')}</b>\n"
        f"{status_line}\n\nOffer: <b>{offer}</b>\n\n"
        f"Message {FLAGS.get(lang,'')}:\n<code>{msg_text}</code>",
        buttons
    )


# ════════════════════════════════════════════════════════════════════════════
# IMPORT ARBITRAGE MODULE — AutoJäger v7
# ════════════════════════════════════════════════════════════════════════════

# ── SHIPPING RATES (EUR, port-to-port, updated 2025) ────────────────────────
SHIPPING_RATES = {
    # (origin_country, dest_country): EUR
    ("KR","DE"): 1150, ("KR","NL"): 1100, ("KR","BE"): 1100,
    ("KR","FR"): 1200, ("KR","PL"): 1250, ("KR","IT"): 1300,
    ("KR","ES"): 1350, ("KR","EU"): 1200,  # fallback
    ("JP","DE"): 1400, ("JP","NL"): 1350, ("JP","BE"): 1350,
    ("JP","FR"): 1450, ("JP","EU"): 1400,
    ("AE","DE"):  850, ("AE","NL"):  800, ("AE","FR"):  900,
    ("AE","PL"):  950, ("AE","EU"):  880,
    ("US","DE"):  950, ("US","NL"):  900, ("US","EU"):  930,
    ("GB","DE"):  350, ("GB","NL"):  280, ("GB","FR"):  320, ("GB","EU"): 340,
    ("CN","DE"): 1600, ("CN","NL"): 1550, ("CN","EU"): 1580,
}

# ── EU CUSTOMS DUTY RATES ──────────────────────────────────────────────────
# Passenger cars (HS 8703): 6.5% of CIF (cost+insurance+freight)
# Source: EU Combined Nomenclature
EU_CUSTOMS_DUTY_PCT = 6.5  # %

# VAT by destination country
VAT_RATES = {
    "DE": 19.0, "FR": 20.0, "NL": 21.0, "BE": 21.0, "PL": 23.0,
    "IT": 22.0, "ES": 21.0, "PT": 23.0, "AT": 20.0, "CH": 7.7,
    "SE": 25.0, "NO": 25.0, "DK": 25.0, "FI": 24.0, "CZ": 21.0,
    "RO": 19.0, "HU": 27.0, "SK": 20.0, "HR": 25.0, "BG": 20.0,
}

# ── REGISTRATION FEES ──────────────────────────────────────────────────────
REGISTRATION_FEES = {
    "DE": 150,  "FR": 200, "NL": 650, "BE": 200, "PL": 300,
    "IT": 350,  "ES": 250, "AT": 200, "CH": 400, "SE": 350,
    "DK": 800,  "NO": 600, "FI": 400, "CZ": 300, "RO": 200,
    "EU": 250,  # default
}

# ── HOMOLOGATION DATABASE ─────────────────────────────────────────────────
# Key: make_model_pattern (lowercase), value: homologation info
HOMOLOG_DB = {
    # Korean cars — generally need individual approval for EU
    "hyundai genesis":  {"coc":False,"lights":False,"emission":"Euro6","approval":"individual","cost":1800,"time_days":21},
    "kia stinger":      {"coc":False,"lights":False,"emission":"Euro6","approval":"individual","cost":1800,"time_days":21},
    "genesis g70":      {"coc":False,"lights":False,"emission":"Euro6","approval":"individual","cost":1800,"time_days":21},
    "genesis g80":      {"coc":False,"lights":False,"emission":"Euro6","approval":"individual","cost":1800,"time_days":21},

    # Japanese cars — JDM spec often needs conversion
    "nissan gt-r":      {"coc":False,"lights":True, "emission":"Euro5","approval":"individual","cost":2500,"time_days":30,"note":"Speed limiter removal needed"},
    "toyota supra":     {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "honda nsx":        {"coc":False,"lights":True, "emission":"Euro5","approval":"individual","cost":3000,"time_days":35},
    "lexus lfa":        {"coc":False,"lights":True, "emission":"Euro5","approval":"individual","cost":3500,"time_days":40},
    "mazda rx-7":       {"coc":False,"lights":True, "emission":"Euro3","approval":"individual","cost":3000,"time_days":45,"note":"Emissions may fail — check year"},
    "toyota land cruiser": {"coc":True,"lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "toyota alphard":   {"coc":False,"lights":True, "emission":"Euro6","approval":"individual","cost":2200,"time_days":25},

    # UAE spec — often same as EU but check AC refrigerant, emissions
    "mercedes g63":     {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "mercedes amg gt":  {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "porsche 911":      {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "porsche cayenne":  {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "lamborghini huracan": {"coc":True,"lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "ferrari 488":      {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "bmw m4":           {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "bmw m5":           {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "bmw m3":           {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "audi rs6":         {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
    "audi r8":          {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},

    # US spec — often need headlight conversion, speedometer, mph→km
    "ford mustang":     {"coc":False,"lights":True, "emission":"EPA","approval":"individual","cost":2800,"time_days":35,"note":"MPH speedo conversion + headlights"},
    "chevrolet corvette":{"coc":False,"lights":True,"emission":"EPA","approval":"individual","cost":3200,"time_days":40},
    "dodge challenger": {"coc":False,"lights":True, "emission":"EPA","approval":"individual","cost":2800,"time_days":35},
    "dodge charger":    {"coc":False,"lights":True, "emission":"EPA","approval":"individual","cost":2800,"time_days":35},

    # Default fallbacks by origin
    "_korea_default":   {"coc":False,"lights":False,"emission":"Euro6","approval":"individual","cost":1800,"time_days":21},
    "_japan_default":   {"coc":False,"lights":True, "emission":"varies","approval":"individual","cost":2500,"time_days":30},
    "_uae_default":     {"coc":True, "lights":False,"emission":"Euro6","approval":"likely_coc","cost":300,"time_days":7,"note":"Verify COC with importer"},
    "_us_default":      {"coc":False,"lights":True, "emission":"EPA","approval":"individual","cost":3000,"time_days":40},
    "_eu_default":      {"coc":True, "lights":False,"emission":"Euro6","approval":"coc_available","cost":0,"time_days":0},
}

ORIGIN_FROM_SOURCE = {
    "autoscout24": "EU", "mobilede": "DE", "encar": "KR", "goonet": "JP",
    "dubizzle": "AE", "cab": "US", "bat": "US", "otomoto": "PL",
    "sahibinden": "TR",
}

def get_homolog(title, origin):
    """Match car to homologation database."""
    tl = title.lower()
    # Try exact matches first
    for key, data in HOMOLOG_DB.items():
        if key.startswith("_"): continue
        if key in tl:
            return data
    # Fallback to origin defaults
    defaults = {
        "KR": HOMOLOG_DB["_korea_default"], "JP": HOMOLOG_DB["_japan_default"],
        "AE": HOMOLOG_DB["_uae_default"],   "US": HOMOLOG_DB["_us_default"],
        "GB": HOMOLOG_DB["_eu_default"],    "EU": HOMOLOG_DB["_eu_default"],
        "DE": HOMOLOG_DB["_eu_default"],
    }
    return defaults.get(origin, HOMOLOG_DB["_eu_default"])

def calc_import(car_price_eur, origin, dest="DE", car_title=""):
    """
    Calculate full landed cost for importing a car.
    Returns detailed breakdown dict.
    """
    dest_cc = dest.upper()[:2]
    origin_cc = origin.upper()[:2]

    # 1. Shipping
    shipping = (SHIPPING_RATES.get((origin_cc, dest_cc)) or
                SHIPPING_RATES.get((origin_cc, "EU")) or 1200)

    # 2. Insurance (0.5% of car value, standard for shipping)
    insurance = round(car_price_eur * 0.005)

    # 3. CIF = Car + Insurance + Freight (for customs calculation)
    cif = car_price_eur + insurance + shipping

    # 4. EU Customs duty (6.5% of CIF for passenger cars)
    # Free Trade Agreement: Korea → EU: 0% since 2016!
    # Japan → EU: 0% since 2019 (EPA)!
    # UAE → EU: 6.5%
    # US → EU: 6.5%
    fta_zero = {"KR", "JP", "CA", "SG", "VN"}
    customs_pct = 0.0 if origin_cc in fta_zero else EU_CUSTOMS_DUTY_PCT
    customs_duty = round(cif * customs_pct / 100)

    # 5. VAT (on car + shipping + insurance + customs duty)
    vat_pct = VAT_RATES.get(dest_cc, 19.0)
    vat_base = cif + customs_duty
    vat = round(vat_base * vat_pct / 100)

    # 6. Registration
    reg_fee = REGISTRATION_FEES.get(dest_cc, REGISTRATION_FEES["EU"])

    # 7. Homologation
    homo = get_homolog(car_title, origin_cc)
    homo_cost = homo.get("cost", 0)

    # 8. Pre-inspection (TÜV/DEKRA — standard for import)
    inspection = 350

    # 9. Transport within EU (port to dealer)
    inland_transport = 250

    # Total
    total_extra = shipping + insurance + customs_duty + vat + reg_fee + homo_cost + inspection + inland_transport
    landed_cost = car_price_eur + total_extra

    return {
        "car_price":        car_price_eur,
        "shipping":         shipping,
        "insurance":        insurance,
        "customs_pct":      customs_pct,
        "customs_duty":     customs_duty,
        "vat_pct":          vat_pct,
        "vat":              vat,
        "registration":     reg_fee,
        "homologation":     homo_cost,
        "inspection":       inspection,
        "inland_transport": inland_transport,
        "total_extra":      total_extra,
        "landed_cost":      landed_cost,
        "homo_info":        homo,
        "fta_applies":      origin_cc in fta_zero,
        "origin":           origin_cc,
        "destination":      dest_cc,
    }

def format_import_report(car, calc, eu_market_price):
    """Format a full import analysis report for Telegram."""
    profit = eu_market_price - calc["landed_cost"]
    profit_pct = (profit / eu_market_price * 100) if eu_market_price else 0
    homo = calc["homo_info"]

    flag_map = {"DE":"🇩🇪","KR":"🇰🇷","JP":"🇯🇵","AE":"🇦🇪","US":"🇺🇸","GB":"🇬🇧",
                "FR":"🇫🇷","PL":"🇵🇱","NL":"🇳🇱","BE":"🇧🇪","IT":"🇮🇹","ES":"🇪🇸"}
    orig_flag = flag_map.get(calc["origin"],"🌍")
    dest_flag = flag_map.get(calc["destination"],"🇪🇺")

    fta_note = "✅ 0% duty (Free Trade Agreement)" if calc["fta_applies"] else f"❗ {calc['customs_pct']}% EU customs duty"

    # Homologation summary
    if homo.get("approval") == "coc_available":
        homo_line = "✅ COC available — no extra approval needed"
    elif homo.get("approval") == "likely_coc":
        homo_line = f"🟡 COC likely available — verify (est. {fe(homo.get('cost',0))} if needed)"
    else:
        homo_line = f"⚠️ Individual approval needed — {fe(homo.get('cost',0))}, ~{homo.get('time_days',0)} days"
        if homo.get("note"):
            homo_line += f"\n   Note: {homo['note']}"

    lights_note = "⚠️ Headlight conversion required" if homo.get("lights") else "✅ Lights: OK"

    profit_emoji = "🟢" if profit > 5000 else "🟡" if profit > 0 else "🔴"

    report = (
        f"🛃 <b>Import Analysis: {orig_flag} → {dest_flag}</b>\n"
        f"<b>{car.get('title','Vehicle')}</b>\n\n"
        f"<b>💰 Cost Breakdown</b>\n"
        f"  Car price:           {fe(calc['car_price'])}\n"
        f"  Shipping:            {fe(calc['shipping'])}\n"
        f"  Insurance:           {fe(calc['insurance'])}\n"
        f"  Customs ({calc['customs_pct']}%):      {fe(calc['customs_duty'])}  {fta_note}\n"
        f"  VAT ({calc['vat_pct']}%):           {fe(calc['vat'])}\n"
        f"  Homologation:        {fe(calc['homologation'])}\n"
        f"  Registration:        {fe(calc['registration'])}\n"
        f"  Inspection:          {fe(calc['inspection'])}\n"
        f"  Inland transport:    {fe(calc['inland_transport'])}\n"
        f"  ─────────────────────\n"
        f"  <b>Total extra:         {fe(calc['total_extra'])}</b>\n"
        f"  <b>Landed cost:         {fe(calc['landed_cost'])}</b>\n\n"
        f"<b>📊 Profit Potential</b>\n"
        f"  EU market price:     {fe(eu_market_price)}\n"
        f"  Landed cost:         {fe(calc['landed_cost'])}\n"
        f"  {profit_emoji} <b>Net profit:          {fe(profit)} ({profit_pct:.1f}%)</b>\n\n"
        f"<b>🔧 Homologation</b>\n"
        f"  {homo_line}\n"
        f"  {lights_note}\n"
        f"  Emission: {homo.get('emission','?')}\n\n"
        f"<b>⏱ Timeline</b>\n"
        f"  Shipping:  2-6 weeks\n"
    )
    if homo.get("time_days",0) > 0:
        report += f"  Homologation: ~{homo['time_days']} days\n"
    report += (
        f"  Total: ~6-10 weeks\n\n"
        f"<i>Calculations based on import to {calc['destination']}. "
        f"VAT is reclaimable if registered as business.</i>"
    )
    return report

# ── IMPORT FLOW HANDLERS ──────────────────────────────────────────────────

DEST_COUNTRIES = {
    "🇩🇪 Germany": "DE", "🇫🇷 France": "FR", "🇳🇱 Netherlands": "NL",
    "🇧🇪 Belgium": "BE", "🇵🇱 Poland": "PL", "🇮🇹 Italy": "IT",
    "🇪🇸 Spain": "ES", "🇦🇹 Austria": "AT", "🇨🇭 Switzerland": "CH",
    "🇸🇪 Sweden": "SE", "🇨🇿 Czech Republic": "CZ",
}

async def handle_import_command(cid, username, text):
    """Handle /import command — start import calc flow."""
    s = get_sess(cid, username)
    raw = re.sub(r'^/import\s*', '', text, flags=re.I).strip()

    if not raw:
        await tg(cid,
            "🛃 <b>Import Arbitrage Calculator</b>\n\n"
            "Tell me the car and source country, e.g.:\n\n"
            "• /import BMW M4 from Korea\n"
            "• /import Nissan GT-R from Japan\n"
            "• /import Mercedes G63 from UAE\n"
            "• /import Ford Mustang from USA\n\n"
            "I'll calculate the exact landed cost, profit potential, "
            "homologation requirements and full import roadmap.",
            [["🔍 Search + Import", "import_search"], ["❓ How it works", "import_howto"]]
        )
        return

    # Parse origin from text
    origin_map = {
        "korea": "KR", "korean": "KR", "encar": "KR",
        "japan": "JP", "japanese": "JP", "jdm": "JP",
        "uae": "AE", "dubai": "AE", "abu dhabi": "AE",
        "usa": "US", "us": "US", "america": "US", "american": "US",
        "uk": "GB", "england": "GB", "britain": "GB",
        "china": "CN", "chinese": "CN",
        "germany": "DE", "german": "DE",
        "europe": "EU", "european": "EU",
    }
    origin = None
    car_query = raw
    for word, cc in origin_map.items():
        if word in raw.lower():
            origin = cc
            car_query = re.sub(rf'\bfrom\s+{word}\b|\b{word}\b', '', raw, flags=re.I).strip()
            break

    if not origin:
        # Ask for origin
        s["pending_import"] = {"query": raw}
        await tg(cid,
            f"🌍 Where is the <b>{raw}</b> located?",
            [["🇰🇷 Korea","import_orig_KR"],["🇯🇵 Japan","import_orig_JP"],
             ["🇦🇪 UAE","import_orig_AE"],["🇺🇸 USA","import_orig_US"],
             ["🇬🇧 UK","import_orig_GB"],["🇨🇳 China","import_orig_CN"]]
        )
        return

    await run_import_calc(cid, car_query, origin)

async def run_import_calc(cid, car_query, origin, dest="DE", car_price=None):
    """Run full import calculation and send report."""
    s = get_sess(cid)

    # If we have a real car from search results, use its price
    # Otherwise use AI to estimate
    if not car_price:
        await tg(cid, f"🔍 <i>Analyzing {car_query} from {origin}...</i>")

    # Get AI market estimate if no price
    if not car_price:
        ai_est = await ai_import_estimate(car_query, origin)
        if not ai_est:
            await tg(cid, "❌ Could not estimate price. Try: /import BMW M4 from Korea 65000")
            return
        car_price = ai_est.get("source_price_eur", 0)
        eu_market  = ai_est.get("eu_market_eur", 0)
        s["last_import_ai"] = ai_est
    else:
        eu_market = 0
        ai_est = {}

    # If still no EU market price, estimate from homolog db
    if not eu_market:
        eu_market = round(car_price * 1.35)  # rough EU premium

    calc = calc_import(car_price, origin, dest, car_query)

    # Store for follow-up
    s["last_import"] = {
        "query": car_query, "origin": origin, "dest": dest,
        "car_price": car_price, "eu_market": eu_market,
        "calc": calc, "ai": ai_est,
    }

    car_obj = {"title": car_query}
    report = format_import_report(car_obj, calc, eu_market)

    await tg(cid, report, [
        ["🇩🇪 Calc for Germany",  "import_dest_DE"],
        ["🇳🇱 Calc for Netherlands","import_dest_NL"],
        ["🇫🇷 Calc for France",   "import_dest_FR"],
        ["🇵🇱 Calc for Poland",   "import_dest_PL"],
        ["✅ I want to buy this",  "import_proceed"],
        ["🔍 Search this car now", "import_search_now"],
    ])

async def ai_import_estimate(query, origin):
    """Use AI to estimate car price in source country and EU market price."""
    origin_names = {"KR":"South Korea","JP":"Japan","AE":"UAE","US":"USA","GB":"UK","CN":"China","DE":"Germany"}
    prompt = f"""Expert car market analyst. Estimate prices for: {query} from {origin_names.get(origin, origin)}.

Respond ONLY in JSON:
{{"source_price_eur": 52000, "eu_market_eur": 78000, "price_range_source": "48000-56000", "price_range_eu": "72000-85000", "typical_year": "2020-2022", "typical_km": "30000-60000", "market_notes": "brief note on this car in this market"}}

Source price = typical asking price in {origin_names.get(origin,origin)} converted to EUR.
EU market price = what this car sells for in Europe today.
Be accurate — use real market knowledge."""

    try:
        r = await _ai([{"role":"user","content":prompt}], 300)
        if r:
            return json.loads(re.sub(r'```json|```','',r).strip())
    except Exception as e:
        print(f"AI import est: {e}")
    return None

# ── IMPORT COMPARISON: Find best source country ──────────────────────────

async def handle_import_compare(cid, query):
    """Compare importing same car from multiple countries."""
    await tg(cid, f"🌍 <i>Comparing import costs for {query} from all markets...</i>")

    origins = ["KR","JP","AE","US"]
    results = []

    for orig in origins:
        ai_est = await ai_import_estimate(query, orig)
        if not ai_est: continue
        car_price = ai_est.get("source_price_eur", 0)
        eu_market  = ai_est.get("eu_market_eur", 0)
        if not car_price: continue
        calc = calc_import(car_price, orig, "DE", query)
        profit = eu_market - calc["landed_cost"]
        results.append({
            "origin": orig, "car_price": car_price,
            "landed": calc["landed_cost"], "profit": profit,
            "calc": calc, "eu_market": eu_market, "ai": ai_est,
        })

    if not results:
        await tg(cid, "❌ Could not compare markets. Try a more specific car name.")
        return

    results.sort(key=lambda x: x["profit"], reverse=True)

    flags = {"KR":"🇰🇷","JP":"🇯🇵","AE":"🇦🇪","US":"🇺🇸"}
    names = {"KR":"Korea","JP":"Japan","AE":"UAE","US":"USA"}

    msg = f"🌍 <b>Import Comparison: {query}</b>\n"
    msg += f"Destination: 🇩🇪 Germany\n\n"

    medals = ["🥇","🥈","🥉","4️⃣"]
    for i, r in enumerate(results[:4]):
        profit_emoji = "🟢" if r["profit"] > 5000 else "🟡" if r["profit"] > 0 else "🔴"
        homo = get_homolog(query, r["origin"])
        fta = "✅ 0% duty" if r["origin"] in ("KR","JP") else f"❗ 6.5% duty"
        msg += (
            f"{medals[i]} {flags.get(r['origin'],'🌍')} <b>{names.get(r['origin'],r['origin'])}</b>\n"
            f"   Source: {fe(r['car_price'])} → Landed: {fe(r['landed'])}\n"
            f"   {profit_emoji} Profit: <b>{fe(r['profit'])}</b> | {fta}\n"
            f"   Homolog: {fe(homo.get('cost',0))} ({'COC ok' if homo.get('approval')=='coc_available' else 'Individual approval'})\n\n"
        )

    best = results[0]
    msg += f"💡 <b>Best deal: {flags.get(best['origin'],'')}{names.get(best['origin'],'')} at {fe(best['profit'])} profit</b>"

    buttons = [[f"📊 Full {names.get(r['origin'],'')} breakdown", f"import_full_{r['origin']}_{query.replace(' ','_')}"]
               for r in results[:3]]
    buttons.append(["🔍 Search in best market", "import_search_best"])
    await tg(cid, msg, buttons)

# ── PAPERWORK GENERATOR ──────────────────────────────────────────────────

PAPERWORK_TEMPLATES = {
    "customs_declaration": {
        "title": "EU Customs Declaration Helper (C88)",
        "fields": ["importer_name", "importer_address", "vehicle_vin", "vehicle_make_model",
                   "engine_cc", "year", "purchase_price_eur", "country_of_origin",
                   "shipping_cost", "invoice_number"],
        "notes": "Submit at port of entry customs office. Attach invoice + bill of lading.",
    },
    "coc_request": {
        "title": "Certificate of Conformity Request",
        "fields": ["vehicle_vin", "make", "model", "year", "engine_type",
                   "your_name", "your_email", "your_address"],
        "notes": "Send to manufacturer's EU headquarters. BMW: coc@bmwgroup.com, Mercedes: coc-service@mercedes-benz.com, Porsche: coc@porsche.de",
        "manufacturer_emails": {
            "bmw": "customercare@bmw.de",
            "mercedes": "customer.service@mercedes-benz.com",
            "porsche": "info@porsche.de",
            "audi": "info@audi.de",
        }
    },
    "vat_reclaim": {
        "title": "VAT Reclaim (for registered dealers)",
        "fields": ["business_name", "vat_number", "invoice_date", "seller_name",
                   "seller_vat", "vehicle_description", "purchase_price_excl_vat", "vat_amount"],
        "notes": "File with your local tax authority within the same quarter. Keep all original invoices.",
    },
}

async def generate_paperwork(cid, doc_type, car_info, buyer_info):
    """Generate AI-filled paperwork document."""
    template = PAPERWORK_TEMPLATES.get(doc_type)
    if not template:
        await tg(cid, "❌ Unknown document type")
        return

    prompt = f"""Generate a professional {template['title']} template.

Vehicle: {car_info.get('title','Unknown vehicle')}
VIN: {car_info.get('vin','[VIN TO BE FILLED]')}
Year: {car_info.get('year','[YEAR]')}
Price: {fe(car_info.get('price',0))}
Origin: {car_info.get('origin','[COUNTRY]')}

Buyer: {buyer_info.get('name','[NAME]')}
Email: {buyer_info.get('email','[EMAIL]')}

Generate a clean, ready-to-use template with all standard fields. 
Mark fields needing manual completion with [FILL IN].
Include the document title, all required fields, and any important notes.
Keep it professional and complete."""

    doc = await _ai([{"role":"user","content":prompt}], 600)
    if not doc:
        doc = f"=== {template['title']} ===\n\n" + "\n".join([f"{f}: [FILL IN]" for f in template["fields"]])
        doc += f"\n\nNotes: {template['notes']}"

    await tg(cid,
        f"📄 <b>{template['title']}</b>\n\n<code>{doc[:3000]}</code>\n\n"
        f"<i>{template.get('notes','')}</i>",
        [["📋 Copy", "copy_doc"], ["📧 Email to me", "email_doc"]]
    )

# ── IMPORT SEARCH INTEGRATION ─────────────────────────────────────────────

async def search_with_import_calc(cid, query, filters, dest_country="DE"):
    """Search for cars and immediately show import cost for each result."""
    cars_eu, cars_kr = await asyncio.gather(
        scrape_as24(query, filters, n=3),
        # For non-EU sources we use the search links (can't scrape directly)
        asyncio.sleep(0),
    )
    cars_eu = cars_eu or []

    if not cars_eu:
        await tg(cid, f"No listings found for {query}. Try broader search terms.")
        return

    msg = f"🌍 <b>Search + Import Analysis: {query}</b>\n\n"
    msg += f"Found {len(cars_eu)} EU listings. Showing import potential from other markets:\n\n"

    for i, car in enumerate(cars_eu[:3]):
        medals = ["🥇","🥈","🥉"]
        price = car.get("price", 0)
        msg += f"{medals[i]} <b>{car.get('title','')[:50]}</b>\n"
        msg += f"🇪🇺 EU price: {fe(price)} · {car.get('year','')} · {kmf(car.get('km',0))}\n"
        msg += f"🔗 <a href=\"{car.get('url','')}\">View listing</a>\n\n"

    # Add import opportunity analysis
    ai_est_kr = await ai_import_estimate(query, "KR")
    ai_est_jp = await ai_import_estimate(query, "JP")

    if ai_est_kr:
        calc_kr = calc_import(ai_est_kr.get("source_price_eur",0), "KR", dest_country, query)
        profit_kr = (cars_eu[0].get("price",0) if cars_eu else 0) - calc_kr["landed_cost"]
        msg += f"🇰🇷 <b>Korea import opportunity:</b>\n"
        msg += f"   Source ~{fe(ai_est_kr.get('source_price_eur',0))} → Landed: {fe(calc_kr['landed_cost'])}\n"
        msg += f"   💰 Potential profit vs EU: <b>{fe(profit_kr)}</b>\n\n"

    if ai_est_jp:
        calc_jp = calc_import(ai_est_jp.get("source_price_eur",0), "JP", dest_country, query)
        profit_jp = (cars_eu[0].get("price",0) if cars_eu else 0) - calc_jp["landed_cost"]
        msg += f"🇯🇵 <b>Japan import opportunity:</b>\n"
        msg += f"   Source ~{fe(ai_est_jp.get('source_price_eur',0))} → Landed: {fe(calc_jp['landed_cost'])}\n"
        msg += f"   💰 Potential profit vs EU: <b>{fe(profit_jp)}</b>\n\n"

    get_sess(cid)["last_cars"] = cars_eu

    await tg(cid, msg, [
        ["📊 Full Korea import calc", "import_full_KR_"+query.replace(" ","_")],
        ["📊 Full Japan import calc", "import_full_JP_"+query.replace(" ","_")],
        ["🇰🇷 Search Encar Korea",   "open_encar_"+query.replace(" ","_")],
        ["🇯🇵 Search Goo-net Japan", "open_goonet_"+query.replace(" ","_")],
        ["📄 Generate paperwork",     "import_paperwork"],
        ["✅ Contact EU seller",      "contact_0"],
    ])




# ════════════════════════════════════════════════════════════════════════════
# CHANNEL MONITOR — AutoJäger v8
# ════════════════════════════════════════════════════════════════════════════
# How it works:
# 1. You post cars to your Telegram channel in any format
# 2. Bot receives every channel post via webhook
# 3. AI parses the listing: make, model, price, year, km, source, URL
# 4. Stores all parsed listings in WATCHLIST
# 5. Every 24h: checks if each listing is still available
# 6. Posts a daily availability report back to your channel
# 7. Alerts you immediately when a listing disappears (sold!)
# ════════════════════════════════════════════════════════════════════════════

import json, re, time, asyncio, httpx
from typing import Optional

# ── WATCHLIST STORAGE ─────────────────────────────────────────────────────
# In-memory store: listing_id → listing object
# Format:
# {
#   "id": "wl_1234567",
#   "title": "BMW M4 Competition 2021",
#   "price": 78000,
#   "year": "2021",
#   "km": 35000,
#   "url": "https://...",
#   "source": "mobile.de",
#   "channel_id": -1001234567890,
#   "message_id": 42,
#   "added_ts": 1700000000,
#   "last_checked_ts": 1700000000,
#   "last_status": "available",   # available | sold | unknown
#   "check_count": 5,
#   "sold_at_ts": None,
#   "raw_text": "original message text",
# }
WATCHLIST = {}          # listing_id → listing
CHANNEL_IDS = set()     # set of channel_ids we monitor
NOTIFY_CHAT_IDS = set() # who gets the daily report (owner + subscribed users)

# Owner chat ID — gets all alerts
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "8402310255"))

# ── LISTING PARSER ────────────────────────────────────────────────────────
URL_PATTERN = re.compile(
    r'https?://(?:www\.)?'
    r'(?:autoscout24|mobile\.de|suchen\.mobile\.de|dubizzle|encar|goo-net|goonet|'
    r'carsandbids|bringatrailer|otomoto|sahibinden|leboncoin|mobile|autotrader|'
    r'cars\.com|autotrader\.co\.uk|caranddriver|hemmings|bat\.ms)'
    r'[^\s\)\]>\"\']+',
    re.I
)

PRICE_PATTERN = re.compile(
    r'(?:€|EUR|euro|USD|\$|AED|KRW|JPY|PLN|TRY|GBP|£)\s*'
    r'([\d]{3,}[\d.,]*)'
    r'|'
    r'([\d]{3,}[\d.,]*)\s*(?:€|EUR|euro|USD|\$|AED|KRW|JPY|PLN|TRY|GBP|£)',
    re.I
)

YEAR_PATTERN  = re.compile(r'\b(20[0-2]\d|199\d)\b')
KM_PATTERN    = re.compile(r'([\d]{1,3}(?:[,.][\d]{3})*(?:[,.][\d]{1,2})?)\s*km', re.I)
KM_K_PATTERN  = re.compile(r'([\d]+)[kK]\s*km', re.I)

SOURCE_MAP = {
    "autoscout24": "AutoScout24", "mobile.de": "Mobile.de",
    "suchen.mobile.de": "Mobile.de", "dubizzle": "Dubizzle UAE",
    "encar": "Encar Korea", "goo-net": "Goo-net Japan",
    "goonet": "Goo-net Japan", "carsandbids": "Cars & Bids",
    "bringatrailer": "Bring a Trailer", "bat.ms": "Bring a Trailer",
    "otomoto": "Otomoto", "sahibinden": "Sahibinden",
    "leboncoin": "Leboncoin", "autotrader": "AutoTrader",
}

def detect_source(url):
    for domain, name in SOURCE_MAP.items():
        if domain in url.lower():
            return name
    return "Unknown"

def parse_price(text):
    m = PRICE_PATTERN.search(text)
    if not m: return 0
    raw = (m.group(1) or m.group(2) or "0").replace(",","").replace(".","")
    try:
        val = int(raw)
        # Sanity check: car prices 1000–5000000
        return val if 1000 <= val <= 5_000_000 else 0
    except: return 0

def parse_km(text):
    m = KM_K_PATTERN.search(text)
    if m: return int(m.group(1)) * 1000
    m = KM_PATTERN.search(text)
    if m:
        raw = m.group(1).replace(",","").replace(".","")
        try: return int(raw)
        except: return 0
    return 0

async def ai_parse_listing(text):
    """Use AI to parse a car listing from channel message."""
    prompt = f"""Parse this car listing message and extract structured data.

Message:
\"\"\"{text[:1000]}\"\"\"

Extract: make, model, year, price (number only in EUR or original currency), km (number only), 
color, fuel type, transmission, seller type (private/dealer), location, any notes.

Respond ONLY in JSON:
{{"make":"BMW","model":"M4 Competition","year":2021,"price":78000,"currency":"EUR",
"km":35000,"color":"black","fuel":"petrol","transmission":"automatic",
"seller_type":"private","location":"Munich, Germany","title":"BMW M4 Competition 2021",
"notes":"Full service history, no accidents"}}

If a field is unknown, use null. Price must be a number (no symbols)."""

    try:
        r = await _ai([{"role":"user","content":prompt}], 300)
        if r:
            return json.loads(re.sub(r'```json|```','',r).strip())
    except Exception as e:
        print(f"AI parse: {e}")
    return None

def parse_listing_basic(text):
    """Fast regex-based fallback parser."""
    urls = URL_PATTERN.findall(text)
    url = urls[0] if urls else ""
    price = parse_price(text)
    km = parse_km(text)
    years = YEAR_PATTERN.findall(text)
    year = years[0] if years else ""
    source = detect_source(url) if url else "Channel"
    # Try to extract a title from first line
    first_line = text.strip().split('\n')[0][:100]
    return {
        "url": url, "price": price, "km": km,
        "year": year, "source": source,
        "title": first_line, "currency": "EUR",
        "make": None, "model": None, "color": None,
        "fuel": None, "transmission": None,
        "seller_type": None, "location": None, "notes": None,
    }

async def parse_channel_message(text, channel_id, message_id):
    """Parse a channel post into a watchlist entry."""
    # Quick check: does it look like a car listing?
    has_url   = bool(URL_PATTERN.search(text))
    has_price = bool(PRICE_PATTERN.search(text))
    has_year  = bool(YEAR_PATTERN.search(text))

    # Need at least 2 of 3 signals to be a listing
    signals = sum([has_url, has_price, has_year])
    if signals < 2:
        return None

    # Try AI parse first, fall back to regex
    parsed = await ai_parse_listing(text)
    if not parsed:
        parsed = parse_listing_basic(text)

    # Merge URL from regex if AI missed it
    if not parsed.get("url"):
        urls = URL_PATTERN.findall(text)
        parsed["url"] = urls[0] if urls else ""

    listing_id = f"wl_{channel_id}_{message_id}"
    now = int(time.time())

    return {
        "id":           listing_id,
        "title":        parsed.get("title") or f"{parsed.get('make','')} {parsed.get('model','')}".strip() or "Vehicle",
        "make":         parsed.get("make"),
        "model":        parsed.get("model"),
        "year":         str(parsed.get("year","")) if parsed.get("year") else "",
        "price":        parsed.get("price") or 0,
        "currency":     parsed.get("currency","EUR"),
        "km":           parsed.get("km") or 0,
        "color":        parsed.get("color"),
        "fuel":         parsed.get("fuel"),
        "transmission": parsed.get("transmission"),
        "seller_type":  parsed.get("seller_type"),
        "location":     parsed.get("location"),
        "notes":        parsed.get("notes"),
        "url":          parsed.get("url",""),
        "source":       detect_source(parsed.get("url","")) if parsed.get("url") else "Channel",
        "channel_id":   channel_id,
        "message_id":   message_id,
        "added_ts":     now,
        "last_checked_ts": now,
        "last_status":  "available",
        "check_count":  0,
        "sold_at_ts":   None,
        "raw_text":     text[:500],
    }

# ── AVAILABILITY CHECKER ──────────────────────────────────────────────────
async def check_listing_available(listing):
    """Check if a listing URL is still live. Returns (available, status_detail)."""
    url = listing.get("url","")
    if not url:
        return True, "no_url"  # No URL to check, assume available

    try:
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        }
        async with httpx.AsyncClient(headers=hdrs, follow_redirects=True, timeout=15) as c:
            r = await c.get(url)

            # 404 = definitely gone
            if r.status_code == 404:
                return False, "404_not_found"

            # 200 but check content for "sold" signals
            if r.status_code == 200:
                text_lower = r.text.lower()

                # AutoScout24 sold signals
                sold_signals = [
                    "listing is no longer available",
                    "dieses inserat ist nicht mehr verfügbar",
                    "annonce n'est plus disponible",
                    "this listing has been removed",
                    "already sold",
                    "bereits verkauft",
                    "déjà vendu",
                    "sold out",
                    "verkauft",
                    "404",
                    "not found",
                    "no longer active",
                    "this vehicle has been sold",
                    "차량이 판매되었습니다",  # Korean: "vehicle has been sold"
                    "売却済み",  # Japanese: "sold"
                    "has ended",  # auction ended
                    "auction ended",
                    "bidding has closed",
                ]
                for sig in sold_signals:
                    if sig in text_lower:
                        return False, f"sold_signal:{sig[:30]}"

                # Check title for sold indicators
                title_match = re.search(r'<title[^>]*>([^<]{5,100})</title>', r.text, re.I)
                if title_match:
                    title = title_match.group(1).lower()
                    if any(s in title for s in ["404","not found","sold","removed","expired","ended"]):
                        return False, f"title_sold:{title[:40]}"

                return True, "available"

            # Redirected to homepage = likely sold
            if str(r.url) != url and any(x in str(r.url) for x in ["/home","/search","/lst","/start"]):
                return False, "redirected_to_home"

            return True, f"http_{r.status_code}"

    except httpx.TimeoutException:
        return True, "timeout"  # Don't mark as sold on timeout
    except Exception as e:
        return True, f"error:{str(e)[:30]}"

async def run_availability_check():
    """Check all watchlist items. Called every 24h."""
    if not WATCHLIST:
        return

    now = int(time.time())
    newly_sold = []
    still_available = []
    unknown = []

    print(f"🔄 Availability check: {len(WATCHLIST)} listings")

    # Check all listings concurrently (max 5 at a time to avoid rate limiting)
    sem = asyncio.Semaphore(5)

    async def check_one(lid, listing):
        async with sem:
            available, detail = await check_listing_available(listing)
            listing["last_checked_ts"] = now
            listing["check_count"] = listing.get("check_count",0) + 1

            was_available = listing.get("last_status") != "sold"

            if available:
                listing["last_status"] = "available"
                still_available.append(listing)
            else:
                listing["last_status"] = "sold"
                if was_available:
                    listing["sold_at_ts"] = now
                    newly_sold.append(listing)
                else:
                    newly_sold.append(listing)  # already was sold

            print(f"  {'✅' if available else '❌'} {listing['title'][:40]} → {detail}")

    await asyncio.gather(*[check_one(lid, lst) for lid, lst in list(WATCHLIST.items())])

    return newly_sold, still_available, unknown

async def send_daily_report(channel_id):
    """Send the daily availability report to the channel."""
    now = int(time.time())
    total = len(WATCHLIST)
    if total == 0:
        return

    available_list = [l for l in WATCHLIST.values() if l.get("last_status") == "available"]
    sold_list      = [l for l in WATCHLIST.values() if l.get("last_status") == "sold"]
    available_count = len(available_list)
    sold_count      = len(sold_list)

    msg = (
        f"📊 <b>AutoJäger — Daily Watch Report</b>\n"
        f"<i>{time.strftime('%d %b %Y, %H:%M UTC', time.gmtime(now))}</i>\n\n"
        f"📋 Watching: <b>{total} listings</b>\n"
        f"✅ Available: <b>{available_count}</b>\n"
        f"❌ Sold/Gone: <b>{sold_count}</b>\n\n"
    )

    if available_list:
        msg += "✅ <b>Still Available:</b>\n"
        for lst in sorted(available_list, key=lambda x: x.get("price",0))[:10]:
            price_str = fe(lst["price"]) if lst.get("price") else "?"
            km_str    = f"{lst['km']:,} km" if lst.get("km") else "?"
            yr_str    = lst.get("year","?")
            url       = lst.get("url","")
            title     = lst.get("title","")[:45]
            age_days  = (now - lst.get("added_ts", now)) // 86400
            msg += f"  {'🔗 ' if url else ''}{'<a href=\"'+url+'\">'+title+'</a>' if url else title}\n"
            msg += f"   {price_str} · {yr_str} · {km_str}"
            if age_days > 0:
                msg += f" · <i>{age_days}d on watch</i>"
            msg += "\n"
        if len(available_list) > 10:
            msg += f"  <i>...and {len(available_list)-10} more</i>\n"
        msg += "\n"

    if sold_list:
        msg += "❌ <b>Sold / Gone:</b>\n"
        for lst in sold_list[:5]:
            price_str = fe(lst["price"]) if lst.get("price") else "?"
            sold_ago  = (now - lst.get("sold_at_ts",now)) // 3600
            msg += f"  {lst.get('title','')[:45]}\n"
            msg += f"   {price_str}"
            if sold_ago < 48:
                msg += f" · sold ~{sold_ago}h ago"
            msg += "\n"
        if len(sold_list) > 5:
            msg += f"  <i>...and {len(sold_list)-5} more</i>\n"

    msg += "\n<i>Next check in 24h · Reply /watchlist to see full list</i>"

    await tg(channel_id, msg)

async def alert_sold(listing):
    """Immediately alert when a listing goes sold."""
    url = listing.get("url","")
    price_str = fe(listing["price"]) if listing.get("price") else "price unknown"
    title = listing.get("title","")[:60]

    msg = (
        f"🔴 <b>SOLD ALERT</b>\n\n"
        f"<b>{title}</b>\n"
        f"💶 {price_str}"
        + (f" · {listing['year']}" if listing.get("year") else "")
        + (f" · {listing['km']:,} km" if listing.get("km") else "")
        + "\n"
        + (f"🔗 <a href=\"{url}\">{listing.get('source','Listing')}</a>\n" if url else "")
        + f"\n⚠️ This listing is no longer available."
    )

    # Alert the channel + owner
    for cid in list(NOTIFY_CHAT_IDS) + [OWNER_CHAT_ID]:
        await tg(cid, msg)

# ── 24H BACKGROUND TASK ───────────────────────────────────────────────────
CHECK_INTERVAL = 24 * 60 * 60  # 24 hours in seconds

async def availability_loop():
    """Background loop — runs forever, checks every 24h."""
    await asyncio.sleep(60)  # Wait 1 min after startup
    while True:
        try:
            if WATCHLIST:
                print(f"🔄 Starting 24h availability check ({len(WATCHLIST)} listings)...")
                result = await run_availability_check()
                if result:
                    newly_sold, still_available, unknown = result

                    # Alert on newly sold items
                    for lst in newly_sold:
                        if lst.get("sold_at_ts") and (int(time.time()) - lst["sold_at_ts"]) < 86400:
                            await alert_sold(lst)

                    # Send daily report to all monitored channels
                    for ch_id in list(CHANNEL_IDS):
                        await send_daily_report(ch_id)

                    # Also send to owner
                    if CHANNEL_IDS:
                        await send_daily_report(OWNER_CHAT_ID)

                print(f"✅ Check complete. Next check in 24h.")
        except Exception as e:
            print(f"❌ Availability check error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

# ── CHANNEL MESSAGE HANDLER ───────────────────────────────────────────────
async def handle_channel_post(channel_id, message_id, text, caption=""):
    """Handle incoming channel post — parse and add to watchlist."""
    full_text = f"{text or ''} {caption or ''}".strip()
    if not full_text:
        return

    # Register this channel
    CHANNEL_IDS.add(channel_id)
    NOTIFY_CHAT_IDS.add(OWNER_CHAT_ID)

    # Try to parse as listing
    listing = await parse_channel_message(full_text, channel_id, message_id)

    if listing:
        WATCHLIST[listing["id"]] = listing
        print(f"📋 Added to watchlist: {listing['title']} | {fe(listing['price'])} | {listing['url'][:50]}")

        # Confirm to channel
        price_str = fe(listing["price"]) if listing.get("price") else "price unknown"
        km_str    = f"{listing['km']:,} km" if listing.get("km") else ""
        yr_str    = listing.get("year","")
        title     = listing.get("title","")[:50]

        confirm = (
            f"👁 <b>Added to watch list</b>\n"
            f"<b>{title}</b>\n"
            f"💶 {price_str}"
            + (f" · {yr_str}" if yr_str else "")
            + (f" · {km_str}" if km_str else "")
            + f"\n📊 Watching {len(WATCHLIST)} listings total\n"
            f"<i>Availability checked every 24h</i>"
        )
        await tg(channel_id, confirm)
    else:
        # Not a listing — ignore silently (don't spam the channel)
        pass

# ── WATCHLIST COMMANDS ────────────────────────────────────────────────────
async def handle_watchlist_command(cid, username):
    """Show full watchlist to user."""
    if not WATCHLIST:
        await tg(cid,
            "📋 <b>Watchlist is empty</b>\n\n"
            "Post cars to your channel and I'll track them!\n\n"
            "I auto-detect listings from any format — just include a URL, price, or year.",
            [["➕ How to add listings","watchlist_howto"]]
        )
        return

    available = [l for l in WATCHLIST.values() if l.get("last_status") != "sold"]
    sold      = [l for l in WATCHLIST.values() if l.get("last_status") == "sold"]
    now = int(time.time())

    msg = f"📋 <b>Watchlist ({len(WATCHLIST)} total)</b>\n\n"

    if available:
        msg += f"✅ <b>Available ({len(available)})</b>\n"
        for lst in sorted(available, key=lambda x: x.get("added_ts",0), reverse=True)[:8]:
            age = (now - lst.get("added_ts",now)) // 86400
            url = lst.get("url","")
            t   = lst.get("title","")[:40]
            p   = fe(lst["price"]) if lst.get("price") else "?"
            checked_ago = (now - lst.get("last_checked_ts",now)) // 3600
            msg += f"• {'<a href=\"'+url+'\">'+t+'</a>' if url else t}\n"
            msg += f"  {p}"
            if lst.get("year"): msg += f" · {lst['year']}"
            if lst.get("km"):   msg += f" · {lst['km']:,}km"
            msg += f" · checked {checked_ago}h ago\n"

    if sold:
        msg += f"\n❌ <b>Sold/Gone ({len(sold)})</b>\n"
        for lst in sold[:4]:
            t = lst.get("title","")[:40]
            p = fe(lst["price"]) if lst.get("price") else "?"
            msg += f"• {t} — {p}\n"

    last_check = max((l.get("last_checked_ts",0) for l in WATCHLIST.values()), default=0)
    if last_check:
        h = (now - last_check) // 3600
        msg += f"\n<i>Last check: {h}h ago · Next: in {max(0,24-h)}h</i>"

    await tg(cid, msg, [
        ["🔄 Check Now",    "watch_check_now"],
        ["🗑 Clear Sold",   "watch_clear_sold"],
        ["📊 Daily Report", "watch_report"],
        ["➕ How to Add",   "watchlist_howto"],
    ])

async def handle_watch_check_now(cid, cbid):
    """Manual check triggered by user."""
    await tg_cb(cbid, "🔄 Checking...")
    if not WATCHLIST:
        await tg(cid, "📋 Watchlist is empty. Post cars to your channel first!")
        return

    await tg(cid, f"🔄 <i>Checking {len(WATCHLIST)} listings...</i>")
    result = await run_availability_check()
    if not result:
        await tg(cid, "❌ Check failed. Try again.")
        return

    newly_sold, still_available, _ = result
    now = int(time.time())

    msg = f"✅ <b>Check Complete — {len(WATCHLIST)} listings</b>\n\n"
    msg += f"✅ Available: {len(still_available)}\n"
    msg += f"❌ Sold/Gone: {len(newly_sold)}\n\n"

    if newly_sold:
        msg += "🔴 <b>Newly Sold:</b>\n"
        for lst in newly_sold[:5]:
            msg += f"• {lst.get('title','')[:40]} — {fe(lst.get('price',0))}\n"

    if still_available:
        msg += "\n✅ <b>Still Available:</b>\n"
        for lst in sorted(still_available, key=lambda x: x.get("price",0))[:5]:
            url = lst.get("url","")
            t   = lst.get("title","")[:40]
            p   = fe(lst.get("price",0))
            msg += f"• {'<a href=\"'+url+'\">'+t+'</a>' if url else t} — {p}\n"

    await tg(cid, msg, [["📋 Full Watchlist","do_watchlist"],["📊 Daily Report","watch_report"]])

    # Alert for newly sold
    for lst in newly_sold:
        if lst.get("sold_at_ts") and (now - lst["sold_at_ts"]) < 3600:
            await alert_sold(lst)

async def handle_watch_clear_sold(cid, cbid):
    """Remove sold listings from watchlist."""
    await tg_cb(cbid, "🗑 Clearing...")
    sold_ids = [lid for lid, l in WATCHLIST.items() if l.get("last_status") == "sold"]
    for sid in sold_ids:
        del WATCHLIST[sid]
    await tg(cid, f"🗑 Cleared {len(sold_ids)} sold listings. {len(WATCHLIST)} remaining.")

async def handle_watch_remove(cid, listing_id):
    """Remove a specific listing from watchlist."""
    if listing_id in WATCHLIST:
        title = WATCHLIST[listing_id].get("title","")
        del WATCHLIST[listing_id]
        await tg(cid, f"✅ Removed: {title}\n📋 {len(WATCHLIST)} listings remaining.")
    else:
        await tg(cid, "❌ Listing not found.")

async def handle_watchlist_howto(cid, cbid=None):
    if cbid: await tg_cb(cbid)
    await tg(cid,
        "➕ <b>How to Add Cars to Watch List</b>\n\n"
        "Just post any car listing to your channel — in any format:\n\n"
        "<b>Option 1: Paste the URL</b>\n"
        "https://www.autoscout24.com/annonce/...\n\n"
        "<b>Option 2: Full listing details</b>\n"
        "BMW M4 Competition 2021\n"
        "€72,000 · 35,000 km\n"
        "https://mobile.de/...\n\n"
        "<b>Option 3: Quick note</b>\n"
        "Check this M4 — looks cheap!\n"
        "€68,000 · 2020 · 40k km\n"
        "https://autoscout24.com/...\n\n"
        "I auto-detect listings and start watching them. "
        "Availability is checked every 24h and you get a daily report.\n\n"
        "✅ Supported sites: Mobile.de, AutoScout24, Dubizzle, Encar, Goo-net, Cars & Bids, Otomoto and more.",
        [["📋 View Watchlist","do_watchlist"]]
    )



# ── WEBHOOK ───────────────────────────────────────────────────────────────
@app.post("/telegram")
async def webhook(req: Request, bg: BackgroundTasks):
    try: data = await req.json()
    except: return {"ok":True}
    if "callback_query" in data:
        cb = data["callback_query"]
        cid=cb["message"]["chat"]["id"]; username=cb["from"].get("username","")
        name=cb["from"].get("first_name",username or "there"); cbd=cb.get("data",""); cbid=cb["id"]
        bg.add_task(handle_cb,cid,username,name,cbd,cbid)
    elif "message" in data or "edited_message" in data:
        msg=data.get("message") or data.get("edited_message")
        cid=msg["chat"]["id"]; username=msg.get("from",{}).get("username","")
        name=msg.get("from",{}).get("first_name",username or "there")
        text=(msg.get("text") or "").strip()
        if text: bg.add_task(handle_msg,cid,username,name,text)
    return {"ok":True}

@app.get("/health")
async def health():
    return {"ok":True,"version":"v6","ai":"claude" if CLAUDE_KEY else "openai" if OPENAI_KEY else "none",
            "email": bool(AGENT_EMAIL),"sessions":len(SESSIONS)}

@app.post("/setup-webhook")
async def setup_wh():
    url=os.getenv("RENDER_EXTERNAL_URL","https://autojager.onrender.com")+"/telegram"
    async with httpx.AsyncClient() as c:
        r=await c.post(f"{TG_BASE}/setWebhook",json={"url":url,"drop_pending_updates":True})
        return r.json()

@app.get("/webhook-info")
async def wh_info():
    async with httpx.AsyncClient() as c:
        r=await c.get(f"{TG_BASE}/getWebhookInfo"); return r.json()

@app.on_event("startup")
async def startup():
    if not TG_TOKEN: print("⚠️  TG_BOT_TOKEN not set"); return
    await asyncio.sleep(3)
    wh_url=os.getenv("RENDER_EXTERNAL_URL","https://autojager.onrender.com")+"/telegram"
    async with httpx.AsyncClient() as c:
        try:
            r=await c.post(f"{TG_BASE}/setWebhook",json={"url":wh_url,"drop_pending_updates":False},timeout=10)
            print(f"Webhook: {r.json()}")
        except Exception as e: print(f"Webhook err: {e}")
    # Start 24h availability check loop
    asyncio.create_task(availability_loop())
    print("✅ 24h availability monitor started")

if __name__=="__main__":
    import uvicorn; uvicorn.run("main:app",host="0.0.0.0",port=8000,reload=True)
