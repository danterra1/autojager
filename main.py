"""
AutoJäger Bot v3 — Telegram-first, Claude AI powered
- Full filter wizard via inline buttons
- Claude AI generates real market analysis
- Direct filtered search links to real listing sites
- Complete A-Z negotiation flow
"""

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio, httpx, json, os, re, time
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="AutoJäger v3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TG_TOKEN    = os.getenv("TG_BOT_TOKEN", "8838098588:AAEX9aZdagfYBZWf-tdKIOwICG9X1ng9HcM")
TG_BASE     = f"https://api.telegram.org/bot{TG_TOKEN}"
OPENAI_KEY  = os.getenv("OPENAI_API_KEY", "")
CLAUDE_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

SESSIONS = {}
USERNAME_MAP = {}

RATES = {"EUR":1.0,"USD":0.92,"JPY":0.0062,"AED":0.25,"KRW":0.00069,"CNY":0.13,"PLN":0.23,"TRY":0.028,"GBP":1.17}

def fe(n): return f"€{int(n or 0):,}"
def fp(n): return f"€{int(n or 0):,}" if n else "Any"

# ── TELEGRAM ─────────────────────────────────────────────────────────────
async def tg(chat_id, text, buttons=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if buttons:
        kb = []
        for i in range(0, len(buttons), 2):
            row = [{"text": b[0], "callback_data": b[1]} for b in buttons[i:i+2]]
            kb.append(row)
        payload["reply_markup"] = json.dumps({"inline_keyboard": kb})
    async with httpx.AsyncClient() as c:
        try: return (await c.post(f"{TG_BASE}/sendMessage", json=payload, timeout=10)).json()
        except Exception as e: print(f"TG: {e}"); return {}

async def tg_cb(cbid, text="✅"):
    async with httpx.AsyncClient() as c:
        try: await c.post(f"{TG_BASE}/answerCallbackQuery", json={"callback_query_id": cbid, "text": text}, timeout=5)
        except: pass

async def tg_edit(chat_id, msg_id, text, buttons=None):
    payload = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        kb = []
        for i in range(0, len(buttons), 2):
            row = [{"text": b[0], "callback_data": b[1]} for b in buttons[i:i+2]]
            kb.append(row)
        payload["reply_markup"] = json.dumps({"inline_keyboard": kb})
    async with httpx.AsyncClient() as c:
        try: await c.post(f"{TG_BASE}/editMessageText", json=payload, timeout=10)
        except: pass

# ── SESSION ───────────────────────────────────────────────────────────────
def get_sess(chat_id, username=None, name=None):
    k = str(chat_id)
    if k not in SESSIONS:
        SESSIONS[k] = {
            "chat_id": chat_id, "username": username or "", "name": name or "",
            "state": "idle", "search": {}, "results": [], "deals": {}, "nego": {}
        }
    if username:
        SESSIONS[k]["username"] = username
        USERNAME_MAP[username.lower().lstrip("@")] = chat_id
    if name: SESSIONS[k]["name"] = name
    return SESSIONS[k]

# ── REAL SEARCH LINKS BUILDER ─────────────────────────────────────────────
def build_search_links(query, filters):
    """Build real filtered search URLs for each site"""
    q = query.strip()
    qu = q.replace(' ', '+')
    qe = q.replace(' ', '%20')
    price = filters.get("max_price")
    year  = filters.get("min_year")
    km    = filters.get("max_km")
    fuel  = filters.get("fuel", "")
    trans = filters.get("transmission", "")
    color = filters.get("color", "")

    # Mobile.de - Germany
    mobile_url = f"https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&scopeId=C2C&sfmr=false&s=Car&vc=Car&makeModelVariant1.modelDescription={qe}"
    if price: mobile_url += f"&maxPrice={price}"
    if km:    mobile_url += f"&maxMileage={km}"
    if year:  mobile_url += f"&minFirstRegistrationDate={year}-01-01"
    if fuel == "diesel":   mobile_url += "&fuel=DIESEL"
    if fuel == "petrol":   mobile_url += "&fuel=PETROL"
    if fuel == "electric": mobile_url += "&fuel=ELECTRICITY"
    if trans == "automatic": mobile_url += "&transmission=AUTOMATIC_GEAR"
    if trans == "manual":    mobile_url += "&transmission=MANUAL_GEAR"

    # AutoScout24 - Europe
    # Try to build slug from query
    makes = {"bmw":"bmw","porsche":"porsche","mercedes":"mercedes-benz","audi":"audi","ferrari":"ferrari",
             "lamborghini":"lamborghini","mclaren":"mclaren","bentley":"bentley","rolls royce":"rolls-royce",
             "aston martin":"aston-martin","maserati":"maserati","jaguar":"jaguar","land rover":"land-rover",
             "volkswagen":"volkswagen","toyota":"toyota","nissan":"nissan","honda":"honda","ford":"ford"}
    ql = q.lower()
    make_slug = next((v for k,v in makes.items() if k in ql), None)
    # Model slug - clean the query after make
    model_part = ql
    for k in makes:
        model_part = model_part.replace(k, "").strip()
    model_slug = model_part.replace(" ", "-") if model_part else None

    as24_url = "https://www.autoscout24.com/lst"
    if make_slug and model_slug:
        as24_url += f"/{make_slug}/{model_slug}"
    elif make_slug:
        as24_url += f"/{make_slug}"
    if price: as24_url += f"/pr_{price}"
    as24_url += "?atype=C&cy=D%2CA%2CB%2CE%2CF%2CI%2CL%2CNL&damaged_listing=exclude&desc=0&sort=standard&ustate=N%2CU"
    if km:   as24_url += f"&kmto={km}"
    if year: as24_url += f"&fregfrom={year}"
    if fuel == "diesel":   as24_url += "&fuel=D"
    if fuel == "petrol":   as24_url += "&fuel=B"
    if fuel == "electric": as24_url += "&fuel=E"
    if trans == "automatic": as24_url += "&gear=A"
    if trans == "manual":    as24_url += "&gear=M"
    if not make_slug: as24_url += f"&q={qu}"

    # Dubizzle UAE
    dub_url = f"https://dubai.dubizzle.com/motors/used-cars/?keywords={qu}"
    if price: dub_url += f"&price__lte={int(price)*4}"
    if km:    dub_url += f"&kilometers__lte={km}"
    if year:  dub_url += f"&year__gte={year}"

    # Encar Korea
    encar_url = f"https://www.encar.com/search/list.do?catCd=kor&searchKey={qu}"
    if year: encar_url += f"&yearMin={year}"
    if km:   encar_url += f"&mileageMax={km}"

    # Goo-net Japan
    goonet_url = f"https://www.goo-net.com/cgi-bin/fsearch/goo_used_search.cgi?category=USDN&query={qu}&phrase={qu}"
    if year: goonet_url += f"&year_min={year}"
    if km:   goonet_url += f"&mileage_max={km}"

    # Cars & Bids USA
    cab_url = f"https://carsandbids.com/search?q={qu}"
    if year: cab_url += f"&year_min={year}&year_max=2026"

    # Bring a Trailer USA
    bat_url = f"https://bringatrailer.com/search/?s={qu}"

    # Otomoto Poland
    otomoto_url = f"https://www.otomoto.pl/osobowe?search%5Bq%5D={qu}"
    if price: otomoto_url += f"&search%5Bfilter_float_price%3Ato%5D={price}"
    if km:    otomoto_url += f"&search%5Bfilter_float_mileage%3Ato%5D={km}"
    if year:  otomoto_url += f"&search%5Bfilter_float_first_registration_year%3Afrom%5D={year}"

    # Sahibinden Turkey
    sah_url = f"https://www.sahibinden.com/vasita-otomobil?query_text={qu}&pagingSize=50"
    if year: sah_url += f"&a6_min={year}"
    if km:   sah_url += f"&a5_max={km}"

    return {
        "🇩🇪 Mobile.de":    mobile_url,
        "🇪🇺 AutoScout24":  as24_url,
        "🇦🇪 Dubizzle UAE": dub_url,
        "🇰🇷 Encar Korea":  encar_url,
        "🇯🇵 Goo-net Japan": goonet_url,
        "🇺🇸 Cars & Bids":  cab_url,
        "🇺🇸 Bring a Trailer": bat_url,
        "🇵🇱 Otomoto":      otomoto_url,
        "🇹🇷 Sahibinden":   sah_url,
    }

# ── AI MARKET ANALYSIS ────────────────────────────────────────────────────
async def ai_market_analysis(query, filters):
    """Use GPT-4o-mini to provide real market intelligence"""
    if not OPENAI_KEY:
        return None

    price = filters.get("max_price")
    year  = filters.get("min_year")
    km    = filters.get("max_km")
    fuel  = filters.get("fuel", "")
    trans = filters.get("transmission", "")
    color = filters.get("color", "")
    body  = filters.get("body_type", "")

    prompt = f"""You are an expert global car dealer and importer. Analyze the market for:

Car: {query}
Filters: {'Max budget: '+fe(price) if price else 'No budget limit'} | {'From year: '+str(year) if year else ''} | {'Max km: '+f"{km:,}" if km else ''} | {('Fuel: '+fuel) if fuel else ''} | {('Transmission: '+trans) if trans else ''} | {('Color: '+color) if color else ''}

Provide a detailed market analysis with:
1. Real mid-market price range in EUR for this specific car with these filters
2. Best 3 markets to buy this car (countries) with reasons
3. Typical mileage for this year range
4. Red flags to watch for
5. Best negotiation strategy
6. Estimated import costs if buying from Asia/USA to Europe

Be specific and accurate. Use real market knowledge from 2024-2025.

Respond in JSON only:
{{
  "market_low": 45000,
  "market_high": 75000,
  "market_mid": 60000,
  "currency": "EUR",
  "best_markets": [
    {{"country": "Germany", "flag": "🇩🇪", "reason": "Large supply, competitive pricing", "price_vs_eu": "-5%"}},
    {{"country": "South Korea", "flag": "🇰🇷", "reason": "Well-maintained, low mileage", "price_vs_eu": "-15%"}},
    {{"country": "UAE", "flag": "🇦🇪", "reason": "Low-mileage, tax-free", "price_vs_eu": "-10%"}}
  ],
  "typical_km": "30,000-80,000 km",
  "red_flags": ["Watch for hidden accident damage", "Check service history carefully"],
  "negotiation_tip": "Specific tactic to use",
  "import_note": "Import cost estimate from best non-EU market",
  "demand_level": "High/Medium/Low",
  "price_trend": "Rising/Falling/Stable"
}}"""

    try:
        async with httpx.AsyncClient() as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "max_tokens": 800, "temperature": 0.2,
                      "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            text = r.json()["choices"][0]["message"]["content"]
            return json.loads(re.sub(r'```json|```', '', text).strip())
    except Exception as e:
        print(f"AI market: {e}")
        return None

async def ai_negotiate(car_info, offer, lang, seller_response=None):
    """Generate negotiation message"""
    if not OPENAI_KEY:
        return _fallback_nego(car_info, offer, lang)

    lang_names = {"en":"English","de":"German","fr":"French","jp":"Japanese",
                  "ar":"Arabic","kr":"Korean","cn":"Chinese","pl":"Polish","tr":"Turkish"}

    if seller_response:
        prompt = f"""Write a counter-negotiation in {lang_names.get(lang,'English')}.
Car: {car_info.get('query','')} | My offer: {offer} | Seller: "{seller_response}"
Firm but polite, cite market value, 50 words max."""
    else:
        prompt = f"""Write a first offer message in {lang_names.get(lang,'English')}.
Car: {car_info.get('query','')} | Offer: {offer} | Market value: {fe(car_info.get('market_mid',0))}
Polite, cite market research, genuine interest. 60 words max. No subject."""

    try:
        async with httpx.AsyncClient() as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "max_tokens": 150,
                      "messages": [{"role": "user", "content": prompt}]}, timeout=15)
            return r.json()["choices"][0]["message"]["content"]
    except:
        return _fallback_nego(car_info, offer, lang)

def _fallback_nego(car_info, offer, lang):
    q = car_info.get('query', 'vehicle')
    templates = {
        "en": f"Hello,\n\nI'm interested in your {q} and would like to offer {offer}. Based on current market research, I believe this is a fair price. I'm a serious buyer ready to proceed quickly.\n\nKind regards",
        "de": f"Guten Tag,\n\nIhr {q} interessiert mich sehr. Ich möchte {offer} anbieten – basierend auf aktuellen Marktdaten ein fairer Preis. Ich bin ein ernsthafter Käufer.\n\nMit freundlichen Grüßen",
        "ar": f"مرحباً،\n\nأنا مهتم بـ {q} وأود تقديم {offer} بناءً على أسعار السوق الحالية. أنا مشترٍ جاد.\n\nمع التحية",
        "jp": f"はじめまして。\n\n{q}に大変興味があります。市場調査に基づき、{offer}でのご検討をお願いします。\n\nよろしくお願いします",
        "kr": f"안녕하세요,\n\n{q}에 관심이 있습니다. 시장 조사를 바탕으로 {offer}을 제안드립니다.\n\n감사합니다",
        "fr": f"Bonjour,\n\nJe suis intéressé par {q} et souhaite proposer {offer} selon les prix du marché actuel.\n\nCordialement",
        "pl": f"Dzień dobry,\n\nJestem zainteresowany {q} i proponuję {offer} zgodnie z cenami rynkowymi.\n\nZ poważaniem",
        "tr": f"Merhaba,\n\n{q} için {offer} teklif etmek istiyorum. Piyasa araştırmama göre bu adil bir fiyat.\n\nSaygılarımla",
        "cn": f"您好，\n\n我对{q}很感兴趣，根据市场价格希望以{offer}购买。\n\n谢谢",
    }
    return templates.get(lang, templates["en"])

# ── FILTER WIZARD ─────────────────────────────────────────────────────────
FILTER_STEPS = [
    ("budget",       "💶 What's your maximum budget?",
     [["Under €30k","b_30000"],["€30k–50k","b_50000"],["€50k–80k","b_80000"],
      ["€80k–120k","b_120000"],["€120k–200k","b_200000"],["€200k+","b_999999"],["No limit","b_0"]]),

    ("min_year",     "📅 Minimum year?",
     [["2015+","y_2015"],["2017+","y_2017"],["2018+","y_2018"],["2019+","y_2019"],
      ["2020+","y_2020"],["2021+","y_2021"],["2022+","y_2022"],["Any year","y_0"]]),

    ("max_km",       "🛣 Maximum mileage?",
     [["Under 20k km","k_20000"],["Under 40k km","k_40000"],["Under 60k km","k_60000"],
      ["Under 80k km","k_80000"],["Under 100k km","k_100000"],["Under 150k km","k_150000"],["Any","k_0"]]),

    ("fuel",         "⛽ Fuel type?",
     [["🔥 Petrol","f_petrol"],["💧 Diesel","f_diesel"],["⚡ Electric","f_electric"],
      ["🔋 Hybrid","f_hybrid"],["Any","f_any"]]),

    ("transmission", "⚙️ Transmission?",
     [["🤖 Automatic","t_automatic"],["🖐 Manual","t_manual"],["Any","t_any"]]),

    ("color",        "🎨 Preferred color?",
     [["⚫ Black","c_black"],["⚪ White","c_white"],["🩶 Grey","c_grey"],
      ["🔵 Blue","c_blue"],["🔴 Red","c_red"],["Any color","c_any"]]),

    ("body_type",    "🚗 Body type?",
     [["Coupe","bt_coupe"],["Sedan","bt_sedan"],["SUV","bt_suv"],
      ["Convertible","bt_convertible"],["Estate/Wagon","bt_estate"],["Any","bt_any"]]),
]

FILTER_KEYS = [s[0] for s in FILTER_STEPS]

async def start_filter_wizard(cid, username, query):
    s = get_sess(cid, username)
    s["state"] = "filtering"
    s["search"] = {"query": query, "step": 0}
    await send_filter_step(cid, 0, s["search"])

async def send_filter_step(cid, step_idx, search):
    if step_idx >= len(FILTER_STEPS):
        await run_search_with_filters(cid)
        return
    key, question, options = FILTER_STEPS[step_idx]
    total = len(FILTER_STEPS)
    q = search.get("query", "")
    filters_so_far = ""
    for k in FILTER_KEYS[:step_idx]:
        v = search.get(k)
        if v and v not in ("any", "0", 0):
            if k == "max_price":    filters_so_far += f" 💶{fe(v)}"
            elif k == "min_year":   filters_so_far += f" 📅{v}+"
            elif k == "max_km":     filters_so_far += f" 🛣<{v//1000}k"
            elif k == "fuel":       filters_so_far += f" ⛽{v}"
            elif k == "transmission": filters_so_far += f" ⚙️{v}"
            elif k == "color":      filters_so_far += f" 🎨{v}"
            elif k == "body_type":  filters_so_far += f" 🚗{v}"

    header = f"🔍 <b>{q}</b>{filters_so_far}\n<i>Step {step_idx+1}/{total}</i>\n\n"
    await tg(cid, header + question, options + [["⏭ Skip","skip_filter"],["🔍 Search Now","search_now"]])

async def run_search_with_filters(cid):
    s = get_sess(cid)
    search = s.get("search", {})
    q = search.get("query", "")

    # Build clean filters
    filters = {}
    if search.get("max_price") and search["max_price"] != 0: filters["max_price"] = search["max_price"]
    if search.get("min_year")  and search["min_year"] != 0:  filters["min_year"]  = search["min_year"]
    if search.get("max_km")    and search["max_km"] != 0:    filters["max_km"]    = search["max_km"]
    if search.get("fuel")      and search["fuel"] != "any":  filters["fuel"]      = search["fuel"]
    if search.get("transmission") and search["transmission"] != "any": filters["transmission"] = search["transmission"]
    if search.get("color")     and search["color"] != "any": filters["color"]     = search["color"]
    if search.get("body_type") and search["body_type"] != "any": filters["body_type"] = search["body_type"]

    s["current_filters"] = filters
    s["state"] = "analyzing"

    # Filter summary
    fs_parts = []
    if filters.get("max_price"):    fs_parts.append(f"💶 Max {fe(filters['max_price'])}")
    if filters.get("min_year"):     fs_parts.append(f"📅 From {filters['min_year']}")
    if filters.get("max_km"):       fs_parts.append(f"🛣 Max {filters['max_km']//1000}k km")
    if filters.get("fuel"):         fs_parts.append(f"⛽ {filters['fuel'].title()}")
    if filters.get("transmission"): fs_parts.append(f"⚙️ {filters['transmission'].title()}")
    if filters.get("color"):        fs_parts.append(f"🎨 {filters['color'].title()}")
    if filters.get("body_type"):    fs_parts.append(f"🚗 {filters['body_type'].title()}")
    fs = " · ".join(fs_parts)

    await tg(cid,
        f"🤖 <b>Analyzing market for {q}</b>\n"
        + (f"<i>{fs}</i>\n" if fs else "")
        + "\n⏳ AI scanning global market data..."
    )

    # Run AI analysis
    ai = await ai_market_analysis(q, filters)
    links = build_search_links(q, filters)
    s["last_analysis"] = ai
    s["last_links"] = links
    s["last_query"] = q
    s["state"] = "results"

    await send_search_results(cid, q, filters, ai, links, fs)

async def send_search_results(cid, q, filters, ai, links, fs):
    if not ai:
        # No AI key - just send search links
        msg = f"🔍 <b>Search results for: {q}</b>\n"
        if fs: msg += f"<i>{fs}</i>\n"
        msg += "\n<b>Click to see real listings:</b>\n\n"
        for site, url in list(links.items())[:6]:
            msg += f"• <a href=\"{url}\">{site}</a>\n"
        msg += "\n<i>Add OpenAI key for AI market analysis</i>"
        await tg(cid, msg, [["🔍 New Search", "do_search"], ["⚙️ Settings", "do_settings"]])
        return

    mid = ai.get("market_mid", 0)
    low = ai.get("market_low", 0)
    high = ai.get("market_high", 0)
    best_markets = ai.get("best_markets", [])
    red_flags = ai.get("red_flags", [])
    tip = ai.get("negotiation_tip", "")
    demand = ai.get("demand_level", "")
    trend = ai.get("price_trend", "")
    import_note = ai.get("import_note", "")
    typical_km = ai.get("typical_km", "")

    # Trend emoji
    trend_icon = {"Rising": "📈", "Falling": "📉", "Stable": "➡️"}.get(trend, "")
    demand_icon = {"High": "🔥", "Medium": "🌡", "Low": "❄️"}.get(demand, "")

    offer_price = round(mid * 0.91 / 1000) * 1000  # round to nearest 1000

    msg = f"🤖 <b>AI Market Analysis: {q}</b>\n"
    if fs: msg += f"<i>{fs}</i>\n"
    msg += "\n"

    msg += f"💶 <b>Market Price Range</b>\n"
    msg += f"Low: {fe(low)} · Mid: {fe(mid)} · High: {fe(high)}\n"
    msg += f"{trend_icon} Trend: {trend}  {demand_icon} Demand: {demand}\n"
    if typical_km: msg += f"🛣 Typical mileage: {typical_km}\n"
    msg += "\n"

    msg += f"🌍 <b>Best Markets to Buy</b>\n"
    for m in best_markets[:3]:
        msg += f"{m.get('flag','')} <b>{m.get('country','')}</b> ({m.get('price_vs_eu','')}) — {m.get('reason','')}\n"
    msg += "\n"

    if red_flags:
        msg += f"⚠️ <b>Watch Out For</b>\n"
        for flag in red_flags[:2]:
            msg += f"• {flag}\n"
        msg += "\n"

    if tip:
        msg += f"💬 <b>Negotiation Tip</b>\n{tip}\n\n"

    if import_note:
        msg += f"🚢 <b>Import Note</b>\n{import_note}\n\n"

    msg += f"💡 <b>Your target offer: {fe(offer_price)}</b>\n\n"
    msg += "─────────────────\n"
    msg += "<b>Now open listings on the best markets:</b>\n\n"

    # Top 3 links based on AI best markets
    priority_sites = []
    for m in best_markets:
        country = m.get("country", "")
        if "German" in country:    priority_sites.append("🇩🇪 Mobile.de")
        if "Europe" in country or "EU" in country: priority_sites.append("🇪🇺 AutoScout24")
        if "Korea" in country:     priority_sites.append("🇰🇷 Encar Korea")
        if "Japan" in country:     priority_sites.append("🇯🇵 Goo-net Japan")
        if "UAE" in country or "Dubai" in country: priority_sites.append("🇦🇪 Dubizzle UAE")
        if "USA" in country or "US" in country:    priority_sites.append("🇺🇸 Cars & Bids")

    # Add remaining sites not in priority
    all_sites = list(links.keys())
    ordered = priority_sites[:3] + [s for s in all_sites if s not in priority_sites]

    for site in ordered[:6]:
        url = links.get(site, "")
        if url:
            msg += f"🔗 <a href=\"{url}\">{site} →</a>\n"

    # Store for negotiation
    get_sess(cid)["active_search"] = {
        "query": q, "filters": filters, "market_mid": mid,
        "market_low": low, "market_high": high, "offer": fe(offer_price),
        "links": links, "ai": ai
    }

    buttons = [
        ["✍️ Draft Negotiation", "start_nego"],
        ["🌍 All 9 Markets", "show_all_links"],
        ["🔄 Refine Filters", "refine_filters"],
        ["🔍 New Search", "do_search"],
    ]
    await tg(cid, msg, buttons)

# ── NEGOTIATION FLOW ──────────────────────────────────────────────────────
async def start_nego(cid, username, cbid=None):
    s = get_sess(cid, username)
    search = s.get("active_search", {})
    if not search:
        await tg(cid, "❌ Run a search first!", [["🔍 Search", "do_search"]])
        return
    if cbid: await tg_cb(cbid)
    q = search.get("query", "")
    offer = search.get("offer", "")
    mid = search.get("market_mid", 0)

    await tg(cid,
        f"✍️ <b>Negotiation Setup</b>\n\n"
        f"Car: <b>{q}</b>\n"
        f"Market mid: {fe(mid)}\n"
        f"Suggested offer: <b>{offer}</b>\n\n"
        f"<i>What offer price do you want to use?</i>",
        [[f"✅ Use {offer}", "nego_use_suggested"],
         ["✏️ Set my price", "nego_custom_price"],
         ["⬅️ Back", "back_results"]]
    )

async def send_nego_draft(cid, username, offer, lang="auto"):
    s = get_sess(cid, username)
    search = s.get("active_search", {})
    q = search.get("query", "")

    # Auto-detect best language based on target market
    if lang == "auto":
        # Default to English for international
        lang = "en"

    await tg(cid, "🤖 <i>Drafting your negotiation message...</i>")
    text = await ai_negotiate({"query": q, "market_mid": search.get("market_mid", 0)}, offer, lang)
    s["nego"] = {"query": q, "offer": offer, "text": text, "lang": lang}

    FLAGS = {"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷","cn":"🇨🇳","pl":"🇵🇱","tr":"🇹🇷"}
    await tg(cid,
        f"✍️ <b>Negotiation Draft {FLAGS.get(lang,'')} ({lang.upper()})</b>\n\n"
        f"Car: {q}\nOffer: <b>{offer}</b>\n\n"
        f"<code>{text}</code>\n\n"
        f"─────────────────\n<i>Approve this message?</i>",
        [["✅ Approve & Use", "nego_approved"],
         ["🌍 Change Language", "nego_lang"],
         ["💶 Change Offer", "nego_custom_price"],
         ["🔄 Regenerate", "nego_regen"],
         ["❌ Cancel", "back_results"]]
    )

# ── MAIN HANDLERS ─────────────────────────────────────────────────────────
HELP_TEXT = """🤖 <b>AutoJäger — Your Car Deal Hunter</b>

<b>How to search:</b>
Just type the car name and I'll guide you through filters step by step.

<b>Commands:</b>
/search — Start a new search with filter wizard
/search BMW M4 — Quick search (I'll ask filters)
/deals — Your last search results
/status — Active negotiations
/help — This menu

<b>What I do:</b>
1️⃣ You tell me the car
2️⃣ I ask budget, year, km, fuel, etc.
3️⃣ AI analyzes global market prices
4️⃣ I send you filtered links to 9 real sites
5️⃣ I draft negotiation in seller's language
6️⃣ We close the deal together"""

async def handle_msg(cid, username, name, text):
    s = get_sess(cid, username, name)
    tl = text.lower().strip()

    if tl.startswith("/start"):
        await tg(cid,
            f"🏎 <b>Welcome to AutoJäger, {name}!</b>\n\n"
            f"I'm your AI-powered car deal hunter. I analyze global markets across "
            f"<b>9 countries</b> and guide you from search to closed deal.\n\n"
            f"<b>Just tell me what car you're looking for:</b>",
            [["🔍 Start Searching", "do_search"],
             ["❓ How it works", "do_help"],
             ["⚙️ Settings", "do_settings"]]
        )

    elif tl.startswith("/search") or tl.startswith("/s "):
        raw = re.sub(r'^/search\s*', '', text, flags=re.I).strip()
        if raw:
            s["state"] = "got_query"
            await start_filter_wizard(cid, username, raw)
        else:
            s["state"] = "awaiting_query"
            await tg(cid,
                "🔍 <b>What car are you looking for?</b>\n\n"
                "Just type the make and model:\n\n"
                "• BMW M4\n• Porsche 911 GT3\n• Ferrari 488\n• Mercedes G63\n• Lamborghini Huracan",
                [["❌ Cancel", "do_cancel"]]
            )

    elif tl.startswith("/deals"):
        search = s.get("active_search", {})
        if not search:
            await tg(cid, "No recent search. Start a new one!", [["🔍 Search", "do_search"]])
        else:
            await send_search_results(cid, search.get("query",""), search.get("filters",{}),
                search.get("ai"), search.get("links",{}), "")

    elif tl.startswith("/status"):
        deals = s.get("deals", {})
        if not deals:
            await tg(cid, "📋 No active deals yet.\n\n/search to find your first deal!", [["🔍 Search","do_search"]])
        else:
            icons = {"sent":"📤","waiting":"⏳","closed":"✅","refused":"❌"}
            msg = f"📋 <b>Your Deals ({len(deals)})</b>\n\n"
            for k, d in deals.items():
                msg += f"{icons.get(d.get('status',''),'🔄')} <b>{d.get('query','')[:40]}</b>\n"
                msg += f"   Offer: {d.get('offer','')} · {d.get('status','').replace('_',' ').title()}\n\n"
            await tg(cid, msg, [["🔍 New Search", "do_search"]])

    elif tl in ("/help", "/menu"):
        await tg(cid, HELP_TEXT, [["🔍 Search Now", "do_search"]])

    elif s.get("state") == "awaiting_query":
        # User typed a car name after /search
        s["state"] = "got_query"
        await start_filter_wizard(cid, username, text.strip())

    elif s.get("state") == "awaiting_custom_offer":
        m = re.search(r'\d[\d,. ]*', text.replace(' ',''))
        if m:
            val = int(float(m.group(0).replace(',','').replace(' ','')))
            s["state"] = "idle"
            await send_nego_draft(cid, username, fe(val))
        else:
            await tg(cid, "❌ Please type a number, e.g. 67500")

    elif s.get("state") == "awaiting_seller_reply":
        # User typed seller's response
        s["state"] = "idle"
        nego = s.get("nego", {})
        q = nego.get("query", s.get("active_search", {}).get("query", ""))
        offer = nego.get("offer", "")
        lang = nego.get("lang", "en")
        mid = s.get("active_search", {}).get("market_mid", 0)
        await tg(cid, "🤖 <i>Crafting counter-offer...</i>")
        counter = await ai_negotiate({"query": q, "market_mid": mid}, offer, lang, seller_response=text)
        s["nego"]["text"] = counter
        FLAGS = {"en":"🇬🇧","de":"🇩🇪","fr":"🇫🇷","jp":"🇯🇵","ar":"🇦🇪","kr":"🇰🇷","cn":"🇨🇳","pl":"🇵🇱","tr":"🇹🇷"}
        await tg(cid,
            f"🔄 <b>Counter-Offer {FLAGS.get(lang,'')} ({lang.upper()})</b>\n\n"
            f"Seller said: <i>\"{text[:100]}\"</i>\n\nMy response:\n<code>{counter}</code>",
            [["✅ Use This", "nego_approved"],
             ["🔄 Regenerate", "nego_regen"],
             ["✅ Deal Done!", "deal_closed"],
             ["❌ Walk Away", "do_search"]]
        )

    else:
        # Unknown input - check if it looks like a car name
        if len(text) > 2 and not text.startswith('/') and re.match(r'^[A-Za-z0-9\s\-]+$', text):
            await tg(cid,
                f"🔍 Search for <b>{text}</b>?",
                [[f"✅ Yes, search {text}", f"quick_search_{text.replace(' ','_')}"],
                 ["🔍 Type different car", "do_search"],
                 ["❌ Cancel", "do_cancel"]]
            )
        else:
            await tg(cid,
                "👋 What would you like to do?",
                [["🔍 Search a Car", "do_search"], ["📋 My Deals", "do_status"], ["❓ Help", "do_help"]]
            )

async def handle_cb(cid, username, name, cbd, cbid):
    s = get_sess(cid, username, name)

    # ── Filter steps
    if cbd.startswith("b_"):  # budget
        val = int(cbd[2:])
        s["search"]["max_price"] = val if val > 0 else None
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "✅")
        await send_filter_step(cid, step, s["search"])

    elif cbd.startswith("y_"):  # year
        val = int(cbd[2:])
        s["search"]["min_year"] = val if val > 0 else None
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "✅")
        await send_filter_step(cid, step, s["search"])

    elif cbd.startswith("k_"):  # km
        val = int(cbd[2:])
        s["search"]["max_km"] = val if val > 0 else None
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "✅")
        await send_filter_step(cid, step, s["search"])

    elif cbd.startswith("f_"):  # fuel
        val = cbd[2:]
        s["search"]["fuel"] = val if val != "any" else None
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "✅")
        await send_filter_step(cid, step, s["search"])

    elif cbd.startswith("t_"):  # transmission
        val = cbd[2:]
        s["search"]["transmission"] = val if val != "any" else None
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "✅")
        await send_filter_step(cid, step, s["search"])

    elif cbd.startswith("c_"):  # color
        val = cbd[2:]
        s["search"]["color"] = val if val != "any" else None
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "✅")
        await send_filter_step(cid, step, s["search"])

    elif cbd.startswith("bt_"):  # body type
        val = cbd[3:]
        s["search"]["body_type"] = val if val != "any" else None
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "✅")
        await send_filter_step(cid, step, s["search"])

    elif cbd == "skip_filter":
        step = s["search"].get("step", 0) + 1
        s["search"]["step"] = step
        await tg_cb(cbid, "⏭ Skipped")
        await send_filter_step(cid, step, s["search"])

    elif cbd == "search_now":
        await tg_cb(cbid, "🔍 Searching!")
        await run_search_with_filters(cid)

    elif cbd == "refine_filters":
        await tg_cb(cbid)
        q = s.get("last_query") or s.get("active_search", {}).get("query", "")
        if q:
            s["search"] = {"query": q, "step": 0}
            await send_filter_step(cid, 0, s["search"])

    elif cbd.startswith("quick_search_"):
        await tg_cb(cbid, "🔍")
        q = cbd[13:].replace("_", " ")
        s["search"] = {"query": q, "step": 0}
        await send_filter_step(cid, 0, s["search"])

    # ── Search results
    elif cbd == "show_all_links":
        await tg_cb(cbid)
        links = s.get("active_search", {}).get("links", {})
        q = s.get("active_search", {}).get("query", "")
        if links:
            msg = f"🌍 <b>All markets for: {q}</b>\n\nClick any to see real listings:\n\n"
            for site, url in links.items():
                msg += f"🔗 <a href=\"{url}\">{site}</a>\n"
            await tg(cid, msg, [["✍️ Draft Negotiation", "start_nego"], ["⬅️ Back", "back_results"]])

    elif cbd == "back_results":
        await tg_cb(cbid)
        search = s.get("active_search", {})
        if search:
            await send_search_results(cid, search.get("query",""), search.get("filters",{}),
                search.get("ai"), search.get("links",{}), "")

    # ── Negotiation
    elif cbd == "start_nego":
        await start_nego(cid, username, cbid)

    elif cbd == "nego_use_suggested":
        await tg_cb(cbid, "✅")
        offer = s.get("active_search", {}).get("offer", "")
        await send_nego_draft(cid, username, offer)

    elif cbd == "nego_custom_price":
        await tg_cb(cbid)
        s["state"] = "awaiting_custom_offer"
        search = s.get("active_search", {})
        await tg(cid,
            f"💶 <b>Your Offer Price</b>\n\n"
            f"Car: {search.get('query','')}\n"
            f"Market: {fe(search.get('market_low',0))} – {fe(search.get('market_high',0))}\n\n"
            f"<i>Type your offer amount (just the number):\ne.g. 67500</i>"
        )

    elif cbd == "nego_regen":
        await tg_cb(cbid, "🔄")
        nego = s.get("nego", {})
        if nego:
            await send_nego_draft(cid, username, nego.get("offer",""), nego.get("lang","en"))

    elif cbd == "nego_lang":
        await tg_cb(cbid)
        await tg(cid, "🌍 <b>Choose language for the negotiation message:</b>",
            [["🇬🇧 English","nl_en"],["🇩🇪 German","nl_de"],["🇦🇪 Arabic","nl_ar"],
             ["🇯🇵 Japanese","nl_jp"],["🇰🇷 Korean","nl_kr"],["🇫🇷 French","nl_fr"],
             ["🇨🇳 Chinese","nl_cn"],["🇵🇱 Polish","nl_pl"],["🇹🇷 Turkish","nl_tr"]]
        )

    elif cbd.startswith("nl_"):
        lang = cbd[3:]
        await tg_cb(cbid, f"Language set!")
        nego = s.get("nego", {})
        await send_nego_draft(cid, username, nego.get("offer",""), lang)

    elif cbd == "nego_approved":
        await tg_cb(cbid, "✅ Approved!")
        nego = s.get("nego", {})
        search = s.get("active_search", {})
        links = search.get("links", {})
        # Send approved message with top links
        top_links = list(links.items())[:3]
        link_text = "\n".join([f"🔗 <a href=\"{url}\">{site}</a>" for site, url in top_links])
        await tg(cid,
            f"📤 <b>Message ready to send!</b>\n\n"
            f"Copy the message above and paste it to the seller.\n\n"
            f"<b>Open listings here:</b>\n{link_text}\n\n"
            f"<i>After sending to the seller — what happened?</i>",
            [["✅ Seller accepted!", "deal_closed"],
             ["💬 Seller replied", "seller_replied"],
             ["⏳ Waiting", "deal_waiting"],
             ["❌ Seller refused", "deal_refused"]]
        )
        # Track deal
        s.setdefault("deals", {})[str(int(time.time()))] = {
            "query": nego.get("query",""), "offer": nego.get("offer",""),
            "status": "sent", "ts": int(time.time())
        }

    elif cbd == "seller_replied":
        await tg_cb(cbid, "📨")
        s["state"] = "awaiting_seller_reply"
        await tg(cid,
            "📨 <b>What did the seller say?</b>\n\n"
            "Type their response and I'll craft the perfect counter-offer.\n\n"
            "<i>Example: \"Best price is €72,000\" or \"Not negotiable\"</i>"
        )

    elif cbd == "deal_closed":
        await tg_cb(cbid, "🎉")
        nego = s.get("nego", {})
        for k, d in s.get("deals", {}).items():
            if d.get("status") == "sent": d["status"] = "closed"
        await tg(cid,
            f"🎉 <b>DEAL CLOSED! Congratulations!</b>\n\n"
            f"Car: <b>{nego.get('query','')}</b>\n"
            f"Your price: <b>{nego.get('offer','')}</b>\n\n"
            f"✅ AutoJäger helped you close this deal!\n\nFind the next one? /search",
            [["🔍 Find Next Car", "do_search"]]
        )

    elif cbd == "deal_refused":
        await tg_cb(cbid)
        await tg(cid,
            "😔 <b>Seller refused</b> — no problem, there are better deals!\n\n<i>What now?</i>",
            [["💰 Make Higher Offer", "nego_custom_price"],
             ["🔍 Search Again", "do_search"],
             ["🌍 Try Different Market", "show_all_links"]]
        )

    elif cbd == "deal_waiting":
        await tg_cb(cbid, "⏳")
        await tg(cid,
            "⏳ <b>Waiting for seller response</b>\n\nI'll be here when they reply!\n\nUse /status to track your deals.",
            [["💬 They replied!", "seller_replied"], ["🔍 Search Meanwhile", "do_search"]]
        )

    # ── Navigation
    elif cbd == "do_search":
        await tg_cb(cbid)
        s["state"] = "awaiting_query"
        await tg(cid,
            "🔍 <b>What car are you looking for?</b>\n\n"
            "Type the make and model:\n\n"
            "• BMW M4 Competition\n• Porsche 911 GT3\n• Ferrari 488 GTB\n• Mercedes-AMG G63\n• Lamborghini Huracán EVO",
            [["❌ Cancel", "do_cancel"]]
        )

    elif cbd == "do_help":
        await tg_cb(cbid)
        await tg(cid, HELP_TEXT, [["🔍 Search Now", "do_search"]])

    elif cbd == "do_settings":
        await tg_cb(cbid)
        await tg(cid,
            "⚙️ <b>Settings</b>\n\n"
            f"👤 Username: @{s.get('username','')}\n"
            f"🤖 AI Analysis: {'✅ Active' if OPENAI_KEY else '❌ Add OpenAI key'}\n"
            f"🔍 Total searches: {len(s.get('deals',{}))}\n\n"
            f"<i>Contact admin to add OpenAI key for full AI analysis</i>",
            [["🔍 Start Searching", "do_search"]]
        )

    elif cbd in ("do_cancel", "do_cancel_search"):
        await tg_cb(cbid, "❌")
        s["state"] = "idle"
        await tg(cid, "Cancelled.", [["🔍 Search", "do_search"], ["❓ Help", "do_help"]])

    elif cbd == "do_status":
        await tg_cb(cbid)
        deals = s.get("deals", {})
        if not deals:
            await tg(cid, "📋 No deals yet. Start a search!", [["🔍 Search", "do_search"]])
        else:
            icons = {"sent":"📤","waiting":"⏳","closed":"✅","refused":"❌"}
            msg = f"📋 <b>Your Deals ({len(deals)})</b>\n\n"
            for k, d in deals.items():
                msg += f"{icons.get(d.get('status',''),'🔄')} <b>{d.get('query','')[:40]}</b>\n"
                msg += f"   Offer: {d.get('offer','')} · {d.get('status','').replace('_',' ').title()}\n\n"
            await tg(cid, msg, [["🔍 New Search", "do_search"]])

# ── WEBHOOK ───────────────────────────────────────────────────────────────
@app.post("/telegram")
async def webhook(req: Request, bg: BackgroundTasks):
    try: data = await req.json()
    except: return {"ok": True}

    if "callback_query" in data:
        cb = data["callback_query"]
        cid = cb["message"]["chat"]["id"]
        username = cb["from"].get("username", "")
        name = cb["from"].get("first_name", username or "there")
        cbd = cb.get("data", "")
        cbid = cb["id"]
        bg.add_task(handle_cb, cid, username, name, cbd, cbid)

    elif "message" in data or "edited_message" in data:
        msg = data.get("message") or data.get("edited_message")
        cid = msg["chat"]["id"]
        username = msg.get("from", {}).get("username", "")
        name = msg.get("from", {}).get("first_name", username or "there")
        text = (msg.get("text") or "").strip()
        if text:
            bg.add_task(handle_msg, cid, username, name, text)

    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "AutoJäger v3", "bot": "@autociker787bot", "website": "https://danterra1.github.io/autojager/"}

@app.get("/health")
async def health():
    return {"ok": True}

class SearchReq(BaseModel):
    query: str
    max_price: Optional[int] = None
    min_year: Optional[int] = None
    max_km: Optional[int] = None
    fuel: Optional[str] = None
    tg_chat_id: Optional[str] = None

@app.post("/search")
async def search_api(req: SearchReq):
    filters = {k: v for k, v in {"max_price": req.max_price, "min_year": req.min_year,
               "max_km": req.max_km, "fuel": req.fuel}.items() if v}
    links = build_search_links(req.query, filters)
    ai = await ai_market_analysis(req.query, filters)
    return {"query": req.query, "filters": filters, "links": links, "ai_analysis": ai}

@app.post("/setup-webhook")
async def setup_webhook():
    url = os.getenv("RENDER_EXTERNAL_URL", "https://autojager.onrender.com") + "/telegram"
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{TG_BASE}/setWebhook", json={"url": url, "drop_pending_updates": True})
        return r.json()

@app.get("/webhook-info")
async def webhook_info():
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{TG_BASE}/getWebhookInfo")
        return r.json()

@app.on_event("startup")
async def startup():
    await asyncio.sleep(3)
    url = os.getenv("RENDER_EXTERNAL_URL", "https://autojager.onrender.com") + "/telegram"
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(f"{TG_BASE}/setWebhook", json={"url": url, "drop_pending_updates": True}, timeout=10)
            print(f"Webhook: {r.json()}")
        except Exception as e:
            print(f"Webhook err: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
