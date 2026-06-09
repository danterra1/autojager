# AutoJager — AI Car Deal Hunter

## Live Product
- Website: https://danterra1.github.io/autojager/
- Backend: https://autojager.onrender.com
- Bot: @autociker787bot
- Render: https://dashboard.render.com/web/srv-d8jup6rtqb8s73co1q7g

## Bot Flow
1. /search BMW M4 -> 7 filter questions (budget/year/km/fuel/transmission/color/body)
2. AI market analysis (price range, best countries, red flags)
3. Real filtered search links to 9 markets
4. User sees real listings, picks one
5. Bot drafts negotiation in seller language
6. Handles counter-offers -> deal closed

## 9 Markets
DE Mobile.de, EU AutoScout24, AE Dubizzle, KR Encar, JP Goo-net,
US Cars&Bids, US BringATrailer, PL Otomoto, TR Sahibinden

## Stack
- Frontend: docs/index.html on GitHub Pages
- Backend: main.py FastAPI on Render free tier  
- Bot: Telegram webhook -> /telegram endpoint
- AI: GPT-4o-mini (needs OPENAI_API_KEY on Render)

## Env Vars Needed on Render
- TG_BOT_TOKEN: SET
- OPENAI_API_KEY: ADD THIS (for AI analysis)
- STRIPE_SECRET_KEY: ADD THIS (for payments)

## TODO
1. Add OPENAI_API_KEY to Render dashboard -> Environment
2. Set up Stripe (3 products: 29/69/99 EUR/month)
3. Test @autociker787bot on Telegram

## Admin Access
- admin@autojager.com / autojager2024
- Owner TG: 8402310255

## New Chat Handoff
Tell Claude: Read README.md from github.com/danterra1/autojager
then read main.py to see the current code and continue.
