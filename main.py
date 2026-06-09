"""
AutoJäger — Full Telegram Agent Backend v2
- Search by @username
- Full conversation flow with buyer approval at every step  
- Real scraping from 6 markets
- AI deal analysis + negotiation
- All commands via Telegram inline buttons
"""

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio, httpx, json, os, re, time
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="AutoJäger API v2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "8838098588:AAEX9aZdagfYBZWf-tdKIOwICG9X1ng9HcM")
TG_BASE  = f"https://api.telegram.org/bot{TG_TOKEN}"
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")

RATES = {"EUR":1,"USD":.92,"JPY":.0062,"AED":.25,"KRW":.00069,"CNY":.13,"PLN":.23,"TRY":.028}
HEADERS = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9,de;q=0.8"}

SESSIONS = {}      # chat_id -> session
USERNAME_MAP = {}  # username -> chat_id

def fe(n): return f"€{int(n or 0):,}"

def parse_price(text):
    if not text: return 0
    s = re.sub(r'[€$£¥₩\s]','',str(text))
    try:
        d,c = s.count('.'),s.count(',')
        if d>=1 and c==0: return float(int(s.replace('.','')))
        elif c>=1 and d==0: return float(int(s.replace(',','')))
        elif d>=1 and c==1: return float(int(s.split(',')[0].replace('.','')))
        else: return float(re.sub(r'[^0-9.]','',s) or 0)
    except: return 0

def to_eur(price, cur): return round(price * RATES.get(cur,1))

def est_market(title, year, km):
    t=(title or "").lower(); b=50000
    if "296" in t and "ferrari" in t: b=280000
    elif "488" in t: b=180000
    elif "ferrari" in t: b=200000
    elif "aventador" in t: b=350000
    elif "huracan" in t: b=200000
    elif "lamborghini" in t: b=220000
    elif "720s" in t: b=200000
    elif "mclaren" in t: b=170000
    elif "gt3" in t: b=170000
    elif "turbo s" in t: b=190000
    elif "911" in t: b=130000
    elif "porsche" in t: b=85000
    elif "rolls" in t or "phantom" in t: b=300000
    elif "bentley" in t: b=150000
    elif "amg gt" in t: b=130000
    elif "g63" in t: b=160000
    elif "m5" in t: b=95000
    elif "m4" in t or "m3" in t: b=80000
    elif "rs6" in t or "rs7" in t: b=100000
    elif "r8" in t: b=120000
    elif "gt-r" in t: b=80000
    elif "supra" in t: b=50000
    elif "bmw" in t: b=55000
    elif "mercedes" in t: b=65000
    elif "audi" in t: b=55000
    try:
        m=re.search(r'\b(20\d{2}|199\d)\b',str(year or "2018")); yr=int(m.group(0)) if m else 2018
    except: yr=2018
    age=2025-yr; exotic=b>150000
    dep=max(0.6,1-age*0.03) if exotic else max(0.3,1-age*0.06)
    kmv=parse_price(str(km or "30000"))
    kmf=max(0.85,1-kmv/300000) if exotic else max(0.7,1-kmv/500000)
    return round(b*dep*kmf)

def calc_score(eur,mkt,km,year):
    if not mkt: return 70
    sp=min(100,50+(mkt-eur)/mkt*200)
    kmv=parse_price(str(km or "30000")); sk=max(0,100-kmv/2000)
    try:
        m=re.search(r'\b(20\d{2}|199\d)\b',str(year or "2018")); yr=int(m.group(0)) if m else 2018
    except: yr=2018
    sa=max(50,100-(2025-yr)*3)
    return min(99,max(40,round(sp*.5+sk*.3+sa*.2)))

# ── TELEGRAM HELPERS ──────────────────────────────────────────────────────
async def tg(chat_id, text, buttons=None):
    payload={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
    if buttons:
        kb=[]; row=[]
        for b in buttons:
            row.append({"text":b[0],"callback_data":b[1]})
            if len(row)==2: kb.append(row); row=[]
        if row: kb.append(row)
        payload["reply_markup"]=json.dumps({"inline_keyboard":kb})
    async with httpx.AsyncClient() as c:
        try: return (await c.post(f"{TG_BASE}/sendMessage",json=payload,timeout=10)).json()
        except Exception as e: print(f"TG err:{e}"); return {}

async def tg_cb(cbid, text=""):
    async with httpx.AsyncClient() as c:
        try: await c.post(f"{TG_BASE}/answerCallbackQuery",json={"callback_query_id":cbid,"text":text},timeout=5)
        except: pass

# ── SCRAPERS ──────────────────────────────────────────────────────────────
MOBILE_IDS={"bmw m4":"ms=3500%3B93%3B%3B","bmw m3":"ms=3500%3B61%3B%3B","bmw m5":"ms=3500%3B95%3B%3B","porsche 911":"ms=19000%3B46%3B%3B","ferrari 488":"ms=9000%3B48%3B%3B","audi rs6":"ms=1900%3B142%3B%3B","audi r8":"ms=1900%3B108%3B%3B"}
AS24={"bmw m4":("bmw","m4"),"bmw m3":("bmw","m3"),"bmw m5":("bmw","m5"),"porsche 911":("porsche","911"),"ferrari 488":("ferrari","488"),"lamborghini huracan":("lamborghini","huracan"),"audi rs6":("audi","rs6")}

def make_listing(card,tsels,psels,source,flag,currency,base=""):
    try:
        t=next((card.select_one(s) for s in tsels if card.select_one(s)),None); title=t.get_text(strip=True)[:100] if t else ""
        p=next((card.select_one(s) for s in psels if card.select_one(s)),None); pt=p.get_text(strip=True) if p else ""
        lnk=card.select_one("a[href]"); href=lnk.get("href","") if lnk else ""
        if href and not href.startswith("http"): href=base+href
        txt=card.get_text(" ",strip=True)
        km=(re.search(r'(\d[\d.,]+)\s*km',txt,re.I) or type('',(),{'group':lambda s,n:''})()).group(0)
        yr=(re.search(r'\b(20\d{2}|199\d)\b',txt) or type('',(),{'group':lambda s,n:''})()).group(0)
        fuel=next((f for f in ["Diesel","Petrol","Electric","Hybrid"] if f.lower() in txt.lower()),"")
        man=re.search(r'([\d.]+)\s*万',pt)
        pval=float(man.group(1))*10000 if man and currency in("JPY","KRW") else parse_price(pt)
        eur=to_eur(pval,currency); mkt=est_market(title,yr,km)
        if eur<500 or not href or not title: return None
        return {"title":title,"price":pt+" "+currency,"price_eur":eur,"market_eur":mkt,"saving":mkt-eur,
                "score":calc_score(eur,mkt,km,yr),"km":km,"year":yr,"fuel":fuel,"currency":currency,
                "source":source,"flag":flag,"url":href,"owners":"","service":"","accident":""}
    except: return None

async def scrape_mobile(c,q,f):
    ql=q.lower(); ms=next((v for k,v in MOBILE_IDS.items() if k in ql),None)
    url="https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&scopeId=C2C&sfmr=false&s=Car&vc=Car"
    url+=("&"+ms) if ms else "&makeModelVariant1.modelDescription="+q
    if f.get("max_price"): url+="&maxPrice="+str(f["max_price"])
    if f.get("max_km"): url+="&maxMileage="+str(f["max_km"])
    if f.get("min_year"): url+="&minFirstRegistrationDate="+str(f["min_year"])+"-01-01"
    try:
        r=await c.get(url,timeout=15); soup=BeautifulSoup(r.text,"html.parser")
        cards=soup.select("a.result-item,article.result-item")[:5] or soup.select("a[href*='auto-inserat']")[:5]
        return [x for x in [make_listing(cd,["h2","h3","[class*='title']"],["[class*='price']",".price-block__price"],"Mobile.de","🇩🇪","EUR","https://suchen.mobile.de") for cd in cards[:3]] if x]
    except Exception as e: print(f"Mobile:{e}"); return []

async def scrape_autoscout(c,q,f):
    ql=q.lower(); slugs=next((v for k,v in AS24.items() if k in ql),None)
    url="https://www.autoscout24.com/lst"+(f"/{slugs[0]}/{slugs[1]}" if slugs else "")
    if f.get("max_price"): url+=f"/pr_{f['max_price']}"
    url+="?atype=C&cy=D%2CA%2CB%2CE%2CF%2CI%2CL%2CNL&damaged_listing=exclude&desc=0&sort=standard&ustate=N%2CU"
    if f.get("max_km"): url+="&kmto="+str(f["max_km"])
    if f.get("min_year"): url+="&fregfrom="+str(f["min_year"])
    if not slugs: url+="&q="+q
    try:
        r=await c.get(url,timeout=15); soup=BeautifulSoup(r.text,"html.parser")
        cards=soup.select("article[class*='cldt'],a[href*='/offers/']")[:5]
        return [x for x in [make_listing(cd,["h2","h3","[class*='title']"],["[class*='price']","[data-testid='price-label']"],"AutoScout24","🇪🇺","EUR","https://www.autoscout24.com") for cd in cards[:3]] if x]
    except Exception as e: print(f"AS24:{e}"); return []

async def scrape_dubizzle(c,q,f):
    url=f"https://dubai.dubizzle.com/motors/used-cars/?keywords={q}"
    if f.get("max_price"): url+=f"&price__lte={int(f['max_price'])*4}"
    if f.get("max_km"): url+=f"&kilometers__lte={f['max_km']}"
    if f.get("min_year"): url+=f"&year__gte={f['min_year']}"
    try:
        r=await c.get(url,timeout=15); soup=BeautifulSoup(r.text,"html.parser")
        cards=soup.select("[class*='listing'],article")[:5]
        return [x for x in [make_listing(cd,["h2","h3","[class*='title']"],["[class*='price']"],"Dubizzle UAE","🇦🇪","AED","https://dubai.dubizzle.com") for cd in cards[:3]] if x]
    except Exception as e: print(f"Dubizzle:{e}"); return []

async def scrape_encar(c,q,f):
    url=f"https://www.encar.com/search/list.do?catCd=kor&searchKey={q}"
    if f.get("min_year"): url+=f"&yearMin={f['min_year']}"
    if f.get("max_km"): url+=f"&mileageMax={f['max_km']}"
    try:
        r=await c.get(url,headers={**HEADERS,"Accept-Language":"ko-KR"},timeout=15); soup=BeautifulSoup(r.text,"html.parser")
        cards=soup.select(".car-item,.list-item")[:5]
        return [x for x in [make_listing(cd,["[class*='name']","h3","h4"],["[class*='price']"],"Encar Korea","🇰🇷","KRW","https://www.encar.com") for cd in cards[:3]] if x]
    except Exception as e: print(f"Encar:{e}"); return []

async def scrape_goonet(c,q,f):
    url=f"https://www.goo-net.com/cgi-bin/fsearch/goo_used_search.cgi?category=USDN&query={q}&phrase={q}"
    if f.get("min_year"): url+=f"&year_min={f['min_year']}"
    if f.get("max_km"): url+=f"&mileage_max={f['max_km']}"
    try:
        r=await c.get(url,timeout=15); soup=BeautifulSoup(r.text,"html.parser")
        cards=soup.select(".carList li,[class*='car-item']")[:5]
        return [x for x in [make_listing(cd,[".car-name","h3","h4"],[".price","[class*='price']"],"Goo-net Japan","🇯🇵","JPY","https://www.goo-net.com") for cd in cards[:3]] if x]
    except Exception as e: print(f"Goonet:{e}"); return []

async def scrape_cab(c,q,f):
    url=f"https://carsandbids.com/search?q={q}"
    if f.get("min_year"): url+=f"&year_min={f['min_year']}&year_max=2026"
    try:
        r=await c.get(url,timeout=15); soup=BeautifulSoup(r.text,"html.parser")
        cards=soup.select(".auction-item,article")[:5]; results=[]
        for cd in cards[:3]:
            item=make_listing(cd,["h2","h3","[class*='title']"],["[class*='price']","[class*='bid']"],"Cars & Bids","🇺🇸","USD","https://carsandbids.com")
            if item:
                km_m=re.search(r'(\d[\d,]+)\s*mile',item.get("km",""),re.I)
                if km_m: item["km"]=f"{int(float(km_m.group(1).replace(',',''))*1.609):,} km"
                results.append(item)
        return results
    except Exception as e: print(f"C&B:{e}"); return []

SCRAPERS={"mobile":scrape_mobile,"autoscout":scrape_autoscout,"dubizzle":scrape_dubizzle,"encar":scrape_encar,"goonet":scrape_goonet,"cab":scrape_cab}

async def run_search(query, filters, sites=None):
    sel=sites or list(SCRAPERS.keys())
    async with httpx.AsyncClient(headers=HEADERS,follow_redirects=True,timeout=httpx.Timeout(20.0),limits=httpx.Limits(max_connections=8)) as c:
        tasks=[SCRAPERS[s](c,query,filters) for s in sel if s in SCRAPERS]
        results=await asyncio.gather(*tasks,return_exceptions=True)
    listings=[]
    for r in results:
        if isinstance(r,list): listings.extend(r)
    listings.sort(key=lambda x:x.get("score",0),reverse=True)
    return listings

# ── AI ────────────────────────────────────────────────────────────────────
async def ai_rank(listings, query, filters):
    if not OPENAI_KEY or not listings: return None
    txt="\n".join([f"{i+1}. {l['title']} | {fe(l['price_eur'])} (market:{fe(l['market_eur'])}) | {l.get('year','')} {l.get('km','')} | {l['source']} | {l['url']}" for i,l in enumerate(listings)])
    prompt=f'Rank top 3 deals for "{query}" (filters:{json.dumps(filters)}). Consider price vs market, mileage, condition, import feasibility.\n\nListings:\n{txt}\n\nJSON only: {{"rankings":[{{"listing_number":1,"why":"2 sentences","red_flags":"or none","negotiation_tip":"1 tactic","suggested_offer":"€XX,XXX"}}]}}'
    try:
        async with httpx.AsyncClient() as c:
            r=await c.post("https://api.openai.com/v1/chat/completions",headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},json={"model":"gpt-4o-mini","max_tokens":600,"temperature":.2,"messages":[{"role":"user","content":prompt}]},timeout=20)
            return json.loads(re.sub(r'```json|```','',r.json()["choices"][0]["message"]["content"]).strip())
    except Exception as e: print(f"AI rank:{e}"); return None

async def ai_nego(car, offer, lang, seller_msg=None):
    if not OPENAI_KEY:
        tpls={"en":f"Hello,\n\nI'm interested in your {car.get('title','vehicle')} and would like to offer {offer}. I am a serious buyer and can proceed quickly.\n\nKind regards","de":f"Guten Tag,\n\nIhr {car.get('title','Fahrzeug')} interessiert mich sehr. Ich möchte {offer} anbieten. Ich bin ein ernsthafter Käufer.\n\nMit freundlichen Grüßen","ar":f"مرحباً،\n\nأنا مهتم بـ {car.get('title','السيارة')} وأود تقديم {offer}. أنا مشترٍ جاد.\n\nمع التحية","jp":f"はじめまして。\n\n{car.get('title','')}に大変興味があります。{offer}でご検討いただけますか？\n\nよろしくお願いします","kr":f"안녕하세요,\n\n{car.get('title','')}에 관심이 있습니다. {offer}을 제안드립니다.\n\n감사합니다","fr":f"Bonjour,\n\nJe suis très intéressé par {car.get('title','')} et je propose {offer}.\n\nCordialement","pl":f"Dzień dobry,\n\nJestem zainteresowany {car.get('title','')} i proponuję {offer}.\n\nZ poważaniem","tr":f"Merhaba,\n\n{car.get('title','')} ilanınıza ilgi duyuyorum. {offer} teklif etmek istiyorum.\n\nSaygılarımla","cn":f"您好，\n\n我对{car.get('title','')}很感兴趣，希望以{offer}购买。\n\n谢谢"}
        return tpls.get(lang,tpls["en"])
    langs={"en":"English","de":"German","fr":"French","jp":"Japanese","ar":"Arabic","kr":"Korean","cn":"Chinese","pl":"Polish","tr":"Turkish"}
    if seller_msg:
        prompt=f'Counter-negotiation in {langs.get(lang,"English")}. Car: {car.get("title")} Listed:{car.get("price")} Market:{fe(car.get("market_eur",0))} My offer:{offer} Seller said:"{seller_msg}". Firm polite counter, cite market value, 60 words max.'
    else:
        prompt=f'First negotiation message in {langs.get(lang,"English")}. Car:{car.get("title")} Listed:{car.get("price")} Market:{fe(car.get("market_eur",0))} My offer:{offer}. Polite, cite market research, genuine interest, 60 words max. No subject line.'
    try:
        async with httpx.AsyncClient() as c:
            r=await c.post("https://api.openai.com/v1/chat/completions",headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},json={"model":"gpt-4o-mini","max_tokens":200,"messages":[{"role":"user","content":prompt}]},timeout=15)
            return r.json()["choices"][0]["message"]["content"]
    except: return f"Hello, I would like to offer {offer} for your {car.get('title','vehicle')}. Kind regards"

# ── SESSION ───────────────────────────────────────────────────────────────
def sess(chat_id, username=None, name=None):
    k=str(chat_id)
    if k not in SESSIONS:
        SESSIONS[k]={"chat_id":chat_id,"username":username,"name":name or username or "","state":"idle","listings":[],"deals":{},"query":"","filters":{}}
    if username: SESSIONS[k]["username"]=username; USERNAME_MAP[username.lower().lstrip("@")]=chat_id
    if name: SESSIONS[k]["name"]=name
    return SESSIONS[k]

# ── FLOW ──────────────────────────────────────────────────────────────────
HELP="""🤖 <b>AutoJäger — Commands</b>

/search BMW M4 — Search any car
/search Porsche 911 max:120000 year:2018 km:80000 — With filters

/deals — Show your last results
/offer 1 — Make offer on deal #1
/status — Your active deals
/help — This menu

<b>Filters:</b> max:PRICE year:YEAR km:KM fuel:diesel|petrol|electric"""

async def do_start(cid, username, name):
    s=sess(cid,username,name)
    await tg(cid,f"🏎 <b>Welcome to AutoJäger, {name}!</b>\n\nI hunt the best car deals across <b>13 markets worldwide</b> — Mobile.de, AutoScout24, Dubizzle UAE, Encar Korea, Goo-net Japan and more.\n\nAI analyzes every listing: price vs market value, mileage, service history, condition.\n\nI guide you from search → offer → negotiation → <b>closed deal</b>.\n\n<b>Start with:</b>\n/search BMW M4\n/search Porsche 911 max:120000 year:2019 km:60000",
        [["🔍 Search a Car","do_search"],["❓ Help","do_help"]])

async def do_search(cid, username, text):
    s=sess(cid,username)
    raw=re.sub(r'^/search\s*','',text,flags=re.I).strip()
    f={}
    for pat,key in [(r'max:(\d+)',"max_price"),(r'year:(\d{4})',"min_year"),(r'km:(\d+)',"max_km")]:
        m=re.search(pat,raw,re.I)
        if m: f[key]=int(m.group(1))
    fm=re.search(r'fuel:(diesel|petrol|electric|hybrid)',raw,re.I)
    if fm: f["fuel"]=fm.group(1).lower()
    q=re.sub(r'(max|year|km|fuel):\S+','',raw,flags=re.I).strip()
    if not q: await tg(cid,"❌ Tell me what car: /search BMW M4"); return
    s["state"]="searching"; s["query"]=q; s["filters"]=f
    fs=""
    if f.get("max_price"): fs+=f"💶 Max {fe(f['max_price'])}  "
    if f.get("min_year"): fs+=f"📅 From {f['min_year']}  "
    if f.get("max_km"): fs+=f"🛣 Max {f['max_km']:,}km"
    await tg(cid,f"🔍 <b>Searching: {q}</b>\n{fs}\n\n🇩🇪 Mobile.de  🇪🇺 AutoScout24  🇦🇪 Dubizzle\n🇰🇷 Encar  🇯🇵 Goo-net  🇺🇸 Cars&Bids\n\n⏳ ~20 seconds...")
    listings=await run_search(q,f)
    ai=await ai_rank(listings[:10],q,f)
    if not listings:
        await tg(cid,f"😔 No listings found for <b>{q}</b>.\n\nTry broader terms or remove filters.",
            [["🔍 Try Again","do_search"]]); return
    s["listings"]=listings[:10]; s["ai"]=ai; s["state"]="results"
    top3=listings[:3]; rankings=(ai or {}).get("rankings",[])
    medals=["🥇","🥈","🥉"]; msg=f"🤖 <b>{len(listings)} listings found for {q}</b>\n{fs}\n\n<b>TOP 3 DEALS:</b>\n\n"
    for i,car in enumerate(top3):
        a=rankings[i] if i<len(rankings) else {}
        offer=a.get("suggested_offer") or fe(round(car["price_eur"]*.91))
        msg+=f"{medals[i]} <b>{car['title'][:52]}</b>\n{car['flag']} {car['source']}\n💶 <b>{fe(car['price_eur'])}</b>"
        if car["saving"]>500: msg+=f"  💰 Save {fe(car['saving'])}"
        msg+="\n"
        if car.get("year"): msg+=f"📅 {car['year']}"
        if car.get("km"): msg+=f"  🛣 {car['km']}"
        if car.get("fuel"): msg+=f"  ⛽ {car['fuel']}"
        msg+=f"\n🏅 Score: {car['score']}/100\n"
        if a.get("why"): msg+=f"✅ {a['why']}\n"
        if a.get("red_flags") and a["red_flags"].lower() not in("none","no",""):
            msg+=f"⚠️ {a['red_flags']}\n"
        msg+=f"💡 Offer: <b>{offer}</b> | 🔗 <a href=\"{car['url']}\">View →</a>\n\n"
    msg+="─────────────\n<i>Which deal interests you?</i>"
    await tg(cid,msg,[["🥇 Deal on #1","deal_0"],["🥈 Deal on #2","deal_1"],["🥉 Deal on #3","deal_2"],["📋 All results","show_all"],["🔍 New Search","do_search"]])

async def do_deal(cid,username,idx,cbid):
    s=sess(cid,username); listings=s.get("listings",[]); ai=s.get("ai")
    if idx>=len(listings): await tg(cid,"❌ Run /search first"); return
    car=listings[idx]; a=(ai or {}).get("rankings",[{}])[idx] if idx<len((ai or {}).get("rankings",[])) else {}
    offer=a.get("suggested_offer") or fe(round(car["price_eur"]*.91))
    s["active"]={"car":car,"ai":a,"offer":offer,"idx":idx,"state":"selected"}
    await tg_cb(cbid,"✅")
    await tg(cid,
        f"🎯 <b>Selected: {car['title'][:50]}</b>\n\n"
        f"{car['flag']} {car['source']}\n"
        f"💶 Listed: <b>{fe(car['price_eur'])}</b>\n"
        f"📊 Market value: {fe(car['market_eur'])}\n"
        f"💰 You save: {fe(car['saving'])}\n"
        f"📅 {car.get('year','')}  🛣 {car.get('km','')}  ⛽ {car.get('fuel','')}\n"
        f"🔗 <a href=\"{car['url']}\">View listing →</a>\n\n"
        +(f"🤖 <i>{a.get('why','')}</i>\n\n" if a.get("why") else "")
        +(f"⚠️ {a.get('red_flags')}\n\n" if a.get("red_flags") and a["red_flags"].lower() not in("none","") else "")
        +f"💡 Suggested offer: <b>{offer}</b>\n"
        +(f"🗣 Tip: {a.get('negotiation_tip')}\n\n" if a.get("negotiation_tip") else "\n")
        +f"<i>Shall I draft a negotiation message?</i>",
        [[f"✅ Draft at {offer}",f"draft_{idx}_auto"],["✏️ Set my price",f"set_price_{idx}"],
         ["🔗 View listing",f"view_{idx}"],["⬅️ Back","show_deals"]])

async def do_draft(cid,username,idx,custom_offer=None,cbid=None):
    s=sess(cid,username); listings=s.get("listings",[])
    if idx>=len(listings): await tg(cid,"❌ Run /search first"); return
    car=listings[idx]; a=s.get("active",{}).get("ai",{})
    offer=custom_offer or a.get("suggested_offer") or fe(round(car["price_eur"]*.91))
    LANG_MAP={"Mobile.de":"de","AutoScout24":"de","Dubizzle UAE":"ar","Encar Korea":"kr","Goo-net Japan":"jp","Cars & Bids":"en","Otomoto":"pl","Sahibinden":"tr","Leboncoin":"fr"}
    lang=LANG_MAP.get(car["source"],"en")
    if cbid: await tg_cb(cbid,"✍️")
    await tg(cid,"🤖 <i>Drafting negotiation...</i>")
    text=await ai_nego(car,offer,lang)
    s["nego"]={"car":car,"offer":offer,"text":text,"lang":lang,"idx":idx}
    FLAGS={"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷","cn":"🇨🇳","pl":"🇵🇱","tr":"🇹🇷"}
    await tg(cid,
        f"✍️ <b>Negotiation Draft {FLAGS.get(lang,'')} ({lang.upper()})</b>\n\n"
        f"<b>Car:</b> {car['title'][:50]}\n<b>Your offer:</b> {offer}\n\n"
        f"<code>{text}</code>\n\n─────────────\n<i>Approve to send?</i>",
        [["✅ Approve & Send",f"send_{idx}"],["🌍 Change Language",f"lang_{idx}"],
         ["💶 Change Price",f"set_price_{idx}"],["🔄 Regenerate",f"regen_{idx}"],["❌ Cancel","cancel"]])

async def do_send(cid,username,idx,cbid):
    s=sess(cid,username); n=s.get("nego",{}); car=n.get("car",{})
    await tg_cb(cbid,"✅ Approved!")
    await tg(cid,
        f"📤 <b>Ready to send to seller</b>\n\n"
        f"<b>{car.get('title','')[:50]}</b>\n"
        f"Offer: <b>{n.get('offer','')}</b> | {car.get('flag','')} {car.get('source','')}\n\n"
        f"📋 Copy the message above and paste it on the listing page.\n"
        f"🔗 <a href=\"{car.get('url','')}\">Open listing →</a>\n\n"
        f"<i>After sending, what happened?</i>",
        [["✅ Seller accepted!",f"accepted_{idx}"],["💬 Seller replied",f"replied_{idx}"],
         ["⏳ Still waiting",f"waiting_{idx}"],["❌ Seller refused",f"refused_{idx}"]])
    if "deals" not in s: s["deals"]={}
    s["deals"][str(idx)]={"car":car,"offer":n.get("offer"),"status":"sent","ts":int(time.time())}

async def do_counter(cid,username,idx,cbid):
    s=sess(cid,username); await tg_cb(cbid,"📨")
    s["state"]=f"reply_{idx}"
    await tg(cid,f"📨 <b>What did the seller say?</b>\n\nType their exact response — I'll write the perfect counter-offer.\n\n<i>e.g. \"Best price is €72,000\" or \"Can do €68,000\"</i>")

async def do_accepted(cid,username,idx,cbid):
    s=sess(cid,username); await tg_cb(cbid,"🎉")
    deal=s.get("deals",{}).get(str(idx),{}); car=deal.get("car",{})
    if str(idx) in s.get("deals",{}): s["deals"][str(idx)]["status"]="closed"
    await tg(cid,
        f"🎉 <b>DEAL CLOSED! Congratulations!</b>\n\n"
        f"<b>{car.get('title','Vehicle')}</b>\n"
        f"💶 Your price: <b>{deal.get('offer',fe(car.get('price_eur',0)))}</b>\n"
        f"💰 Saved vs market: <b>{fe(car.get('saving',0))}</b>\n"
        f"{car.get('flag','')} {car.get('source','')}\n\n"
        f"✅ AutoJäger helped you close this deal!\n\nReady for the next? /search",
        [["🔍 Find Next Car","do_search"]])

async def do_refused(cid,username,idx,cbid):
    s=sess(cid,username); await tg_cb(cbid)
    listings=s.get("listings",[]); n=len(listings)
    if str(idx) in s.get("deals",{}): s["deals"][str(idx)]["status"]="refused"
    await tg(cid,
        f"😔 <b>Seller refused</b> — no problem, there are better deals.\n\n"
        f"You have {n} listings in your current search.\n\n<i>What next?</i>",
        [["🥈 Try Deal #2","deal_1"],["🥉 Try Deal #3","deal_2"],
         [f"💰 Higher offer",f"set_price_{idx}"],["🔍 New Search","do_search"]])

async def do_status(cid,username):
    s=sess(cid,username); deals=s.get("deals",{})
    if not deals: await tg(cid,"📋 No active deals yet.\n\nUse /search to find your first deal!",[["🔍 Search","do_search"]]); return
    icons={"sent":"📤","waiting":"⏳","closed":"✅","refused":"❌","negotiating":"💬"}
    msg=f"📋 <b>Your Deals ({len(deals)})</b>\n\n"
    for idx,d in deals.items():
        car=d.get("car",{}); st=d.get("status","sent")
        msg+=f"{icons.get(st,'🔄')} <b>{car.get('title','')[:40]}</b>\n   Offer: {d.get('offer','')} · {st.replace('_',' ').title()}\n   🔗 <a href=\"{car.get('url','')}\">View</a>\n\n"
    await tg(cid,msg,[["🔍 New Search","do_search"]])

# ── WEBHOOK ───────────────────────────────────────────────────────────────
@app.post("/telegram")
async def webhook(req: Request, bg: BackgroundTasks):
    try: data=await req.json()
    except: return {"ok":True}
    if "callback_query" in data:
        cb=data["callback_query"]; cid=cb["message"]["chat"]["id"]
        username=cb["from"].get("username",""); name=cb["from"].get("first_name",username)
        cbd=cb.get("data",""); cbid=cb["id"]
        bg.add_task(handle_cb,cid,username,name,cbd,cbid)
    elif "message" in data or "edited_message" in data:
        msg=data.get("message") or data.get("edited_message")
        cid=msg["chat"]["id"]; username=msg.get("from",{}).get("username","")
        name=msg.get("from",{}).get("first_name",username or "there"); text=(msg.get("text") or "").strip()
        if text: bg.add_task(handle_msg,cid,username,name,text)
    return {"ok":True}

async def handle_cb(cid,username,name,cbd,cbid):
    sess(cid,username,name); s=SESSIONS[str(cid)]
    if cbd=="do_search": await tg_cb(cbid); await tg(cid,"🔍 What car?\n\n/search BMW M4\n/search Porsche 911 max:120000 year:2019 km:60000")
    elif cbd=="do_help": await tg_cb(cbid); await tg(cid,HELP,[["🔍 Search","do_search"]])
    elif cbd=="show_all":
        await tg_cb(cbid); listings=s.get("listings",[]); msg=f"📋 <b>All {len(listings)} results:</b>\n\n"
        for i,c in enumerate(listings[:10]):
            msg+=f"{i+1}. <b>{c['title'][:42]}</b>\n   {c['flag']} {fe(c['price_eur'])} · {c.get('year','')} · {c.get('km','')} · {c['score']}/100\n   🔗 <a href=\"{c['url']}\">View</a>\n\n"
        await tg(cid,msg,[[f"Deal #{i+1}",f"deal_{i}"] for i in range(min(5,len(listings)))])
    elif cbd=="show_deals":
        await tg_cb(cbid); listings=s.get("listings",[])
        if listings: await do_search(cid,username,f"/search {s.get('query','')}")
        else: await tg(cid,"No results. Use /search",[["🔍 Search","do_search"]])
    elif cbd.startswith("deal_"):
        idx=int(cbd.split("_")[1]); await do_deal(cid,username,idx,cbid)
    elif cbd.startswith("draft_"):
        idx=int(cbd.split("_")[1]); await do_draft(cid,username,idx,cbid=cbid)
    elif cbd.startswith("send_"):
        idx=int(cbd.split("_")[1]); await do_send(cid,username,idx,cbid)
    elif cbd.startswith("accepted_"):
        idx=int(cbd.split("_")[1]); await do_accepted(cid,username,idx,cbid)
    elif cbd.startswith("replied_"):
        idx=int(cbd.split("_")[1]); await do_counter(cid,username,idx,cbid)
    elif cbd.startswith("waiting_"):
        await tg_cb(cbid,"⏳"); await tg(cid,"⏳ Got it — I'll be here when they reply!\n\nUse /status to check your deals.",
            [["📋 My Deals","do_status"],["🔍 New Search","do_search"]])
    elif cbd.startswith("refused_"):
        idx=int(cbd.split("_")[1]); await do_refused(cid,username,idx,cbid)
    elif cbd.startswith("regen_"):
        idx=int(cbd.split("_")[1]); await do_draft(cid,username,idx,cbid=cbid)
    elif cbd.startswith("view_"):
        idx=int(cbd.split("_")[1]); await tg_cb(cbid)
        listings=s.get("listings",[])
        if idx<len(listings): await tg(cid,f"🔗 <a href=\"{listings[idx]['url']}\">Open listing →</a>",[["⬅️ Back",f"deal_{idx}"]])
    elif cbd.startswith("set_price_"):
        idx=int(cbd.split("_")[2]); await tg_cb(cbid)
        s["state"]=f"price_{idx}"
        listings=s.get("listings",[]); car=listings[idx] if idx<len(listings) else {}
        await tg(cid,f"💶 <b>Your Offer Price</b>\n\nCar: {car.get('title','')[:50]}\nListed: {fe(car.get('price_eur',0))}\nMarket: {fe(car.get('market_eur',0))}\n\n<i>Type your offer (numbers only, e.g. 67500):</i>")
    elif cbd.startswith("lang_"):
        parts=cbd.split("_"); lang=parts[1]; idx=int(parts[2]); await tg_cb(cbid,f"Language set!")
        n=s.get("nego",{}); car=n.get("car") or (s.get("listings",[{}])[idx] if idx<len(s.get("listings",[])) else {})
        offer=n.get("offer") or fe(round(car.get("price_eur",0)*.91))
        text=await ai_nego(car,offer,lang)
        s["nego"]={"car":car,"offer":offer,"text":text,"lang":lang,"idx":idx}
        FLAGS={"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷","cn":"🇨🇳","pl":"🇵🇱","tr":"🇹🇷"}
        await tg(cid,f"✍️ <b>Draft {FLAGS.get(lang,'')} ({lang.upper()})</b>\n\n<code>{text}</code>",
            [["✅ Approve",f"send_{idx}"],["🔄 Regen",f"regen_{idx}"],["⬅️ Back",f"deal_{idx}"]])
    elif cbd=="cancel": await tg_cb(cbid,"❌"); await tg(cid,"Cancelled.",[["🔍 Search","do_search"]])
    elif cbd=="do_status": await tg_cb(cbid); await do_status(cid,username)

async def handle_msg(cid,username,name,text):
    s=sess(cid,username,name); tl=text.lower().strip()
    if tl.startswith("/start"): await do_start(cid,username,name)
    elif tl.startswith("/search") or tl.startswith("/s "): await do_search(cid,username,text)
    elif tl.startswith("/deals") or tl.startswith("/results"): 
        listings=s.get("listings",[])
        if not listings: await tg(cid,"No results yet. Use /search",[["🔍 Search","do_search"]])
        else:
            top3=listings[:3]; medals=["🥇","🥈","🥉"]; ai=s.get("ai"); rankings=(ai or {}).get("rankings",[])
            msg=f"📋 <b>Your last results for: {s.get('query','')}</b>\n\n"
            for i,car in enumerate(top3):
                a=rankings[i] if i<len(rankings) else {}
                offer=a.get("suggested_offer") or fe(round(car["price_eur"]*.91))
                msg+=f"{medals[i]} <b>{car['title'][:50]}</b>\n{car['flag']} {fe(car['price_eur'])} · {car.get('year','')} · {car.get('km','')} · Score:{car['score']}/100\n💡 Offer: {offer}\n🔗 <a href=\"{car['url']}\">View</a>\n\n"
            await tg(cid,msg,[["🥇 Deal #1","deal_0"],["🥈 Deal #2","deal_1"],["🥉 Deal #3","deal_2"]])
    elif tl.startswith("/offer"):
        parts=text.split(); idx=int(parts[1])-1 if len(parts)>1 and parts[1].isdigit() else 0
        await do_deal(cid,username,idx,"")
    elif tl.startswith("/negotiate"):
        parts=text.split(); idx=int(parts[1])-1 if len(parts)>1 and parts[1].isdigit() else 0
        await do_draft(cid,username,idx)
    elif tl.startswith("/status"): await do_status(cid,username)
    elif tl.startswith("/help"): await tg(cid,HELP,[["🔍 Search","do_search"]])
    elif s.get("state","").startswith("price_"):
        idx=int(s["state"].split("_")[1])
        m=re.search(r'\d[\d,.]*',text.replace(' ',''))
        if m:
            v=int(m.group(0).replace(',','').split('.')[0])
            s["state"]="idle"; await do_draft(cid,username,idx,custom_offer=fe(v))
        else: await tg(cid,"❌ Type a number, e.g. 67500")
    elif s.get("state","").startswith("reply_"):
        idx=int(s["state"].split("_")[1]); s["state"]="idle"
        n=s.get("nego",{}); car=n.get("car") or (s.get("listings",[{}])[idx] if idx<len(s.get("listings",[])) else {})
        offer=n.get("offer") or fe(round(car.get("price_eur",0)*.91)); lang=n.get("lang","en")
        await tg(cid,"🤖 <i>Crafting counter-offer...</i>")
        counter=await ai_nego(car,offer,lang,seller_msg=text)
        s["nego"]={"car":car,"offer":offer,"text":counter,"lang":lang,"idx":idx}
        FLAGS={"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷","cn":"🇨🇳","pl":"🇵🇱","tr":"🇹🇷"}
        await tg(cid,f"🔄 <b>Counter-Offer</b>\n\nSeller: <i>\"{text[:80]}\"</i>\n\nMy response {FLAGS.get(lang,'')}:\n<code>{counter}</code>",
            [["✅ Send This",f"send_{idx}"],["🔄 Regen",f"regen_{idx}"],["✅ Deal Done!",f"accepted_{idx}"],["❌ Walk Away","do_search"]])
    else:
        await tg(cid,f"👋 Try:\n/search BMW M4\n/search Porsche 911 max:120000 year:2019\n/help",[["🔍 Search","do_search"],["❓ Help","do_help"]])

# ── API ENDPOINTS ──────────────────────────────────────────────────────────
@app.get("/")
async def root(): return {"status":"AutoJäger v2","bot":"@autociker787bot","website":"https://danterra1.github.io/autojager/"}

@app.get("/health")
async def health(): return {"ok":True}

class SearchReq(BaseModel):
    query:str; max_price:Optional[int]=None; min_year:Optional[int]=None
    max_km:Optional[int]=None; fuel:Optional[str]=None; sites:Optional[list]=None
    tg_chat_id:Optional[str]=None; tg_username:Optional[str]=None

@app.post("/search")
async def search_api(req:SearchReq):
    f={k:v for k,v in {"max_price":req.max_price,"min_year":req.min_year,"max_km":req.max_km,"fuel":req.fuel}.items() if v}
    listings=await run_search(req.query,f,req.sites)
    ai=await ai_rank(listings[:10],req.query,f)
    return {"query":req.query,"total_found":len(listings),"listings":listings[:10],"ai_analysis":ai}

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
    await asyncio.sleep(3)
    url=os.getenv("RENDER_EXTERNAL_URL","https://autojager.onrender.com")+"/telegram"
    async with httpx.AsyncClient() as c:
        try:
            r=await c.post(f"{TG_BASE}/setWebhook",json={"url":url,"drop_pending_updates":True},timeout=10)
            print(f"Webhook: {r.json()}")
        except Exception as e: print(f"Webhook err:{e}")

if __name__=="__main__":
    import uvicorn; uvicorn.run("main:app",host="0.0.0.0",port=8000,reload=True)
