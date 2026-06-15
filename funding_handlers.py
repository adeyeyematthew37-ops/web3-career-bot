"""
funding_handlers.py
───────────────────
Handlers for:
  /funding_stats      — live funding news from DeFiLlama + RSS
  /twitter_scan       — Twitter/X Web3 signals (CryptoPanic + VC RSS + Twitter API)
  /scout_communities  — Telegram community scouting tips + curated list
  /status             — subscriber's account status and preferences

All handlers are async and accept (update, context) per python-telegram-bot v20.
"""

import os, re, logging, sqlite3, asyncio, aiohttp, feedparser
from datetime import datetime, timedelta
from typing import List, Dict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)
DB_PATH = os.getenv("DB_PATH", "enhanced_fundraising_alerts.db")

# ── DB helper ──────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def is_verified(chat_id: int) -> bool:
    try:
        conn = _db()
        row  = conn.execute(
            "SELECT subscription_verified FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False

async def guard(update: Update) -> bool:
    if not is_verified(update.effective_chat.id):
        await update.message.reply_text(
            "🔐 Access restricted.\n\n"
            "Use /verify [code] to unlock.\n"
            "Contact admin for your subscription code."
        )
        return False
    return True

# ── Text helpers (shared with daily_report) ───────────────────────

def extract_amount(text: str) -> str:
    """Pull a funding amount from a text string."""
    for pattern in [
        r"\$[\d,.]+\s*(?:million|billion|M|B|m|b)\b",
        r"[\d,.]+\s*(?:million|billion)\s*(?:dollar|USD|\$)"
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group().strip()
    return "Undisclosed"

def extract_stage(text: str) -> str:
    """Identify the funding stage from text."""
    t = text.lower()
    for kw, label in [
        ("pre-seed","Pre-Seed"), ("preseed","Pre-Seed"),
        ("series b","Series B"), ("series a","Series A"), ("series c","Series C"),
        ("seed","Seed"), ("ido","IDO"), ("ico","ICO"), ("ieo","IEO"),
        ("token sale","Token Sale"), ("private sale","Private Sale"),
        ("public sale","Public Sale"), ("launchpad","Launchpad"),
        ("grant","Grant"), ("fair launch","Fair Launch")
    ]:
        if kw in t:
            return label
    return "Funding Round"

# ── RSS sources ───────────────────────────────────────────────────

FUNDING_RSS = [
    {"name": "The Block",     "url": "https://www.theblock.co/rss.xml",                 "tag": "⬛"},
    {"name": "CoinDesk",      "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "tag": "🟡"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss",                   "tag": "🟠"},
    {"name": "Decrypt",       "url": "https://decrypt.co/feed",                          "tag": "🔵"},
    {"name": "CryptoPanic",   "url": "https://cryptopanic.com/news/rss/",               "tag": "🟣"},
    {"name": "DLNews",        "url": "https://www.dlnews.com/rss/",                     "tag": "🟤"},
]

FUNDING_KEYWORDS = [
    "raises", "funding", "round", "seed", "series", "investment",
    "venture", "backed", "million", "billion", "vc", "capital",
    "grant", "launchpad", "ido", "ico", "token sale",
]

# Web3 VC and signal accounts — using their public blog/medium RSS feeds
# (Nitter is dead, using direct RSS or Medium instead)
VC_RSS_FEEDS = [
    {"name": "a16z crypto",       "url": "https://a16zcrypto.com/feed.xml",                           "tag": "🏦"},
    {"name": "Paradigm",          "url": "https://www.paradigm.xyz/feed.xml",                          "tag": "🏦"},
    {"name": "CoinGecko Blog",    "url": "https://blog.coingecko.com/rss/",                            "tag": "📊"},
    {"name": "Binance Blog",      "url": "https://www.binance.com/en/blog/rss",                        "tag": "🟡"},
    {"name": "Messari",           "url": "https://messari.io/rss",                                     "tag": "📊"},
    {"name": "DeFiLlama Blog",    "url": "https://defillama.com/blog/rss.xml",                         "tag": "🔵"},
]

# ── scrape_rss: used by daily_report.py ──────────────────────────

async def scrape_rss(hours: int = 24) -> List[Dict]:
    """
    Pull funding-related news from all crypto RSS sources.
    Returns list of dicts with keys: title, url, source, stage, amount, summary.
    Called by daily_report.scrape_funding_rss.
    """
    articles: List[Dict] = []
    loop = asyncio.get_event_loop()
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    for feed in FUNDING_RSS:
        try:
            parsed = await loop.run_in_executor(None, feedparser.parse, feed["url"])
            for entry in (parsed.entries or [])[:10]:
                title   = entry.get("title", "").strip()
                summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "").strip()[:300]
                combined = (title + " " + summary).lower()

                # Only articles mentioning fundraising
                if not any(kw in combined for kw in FUNDING_KEYWORDS):
                    continue

                # Try to respect hours cutoff
                try:
                    pub = entry.get("published_parsed")
                    if pub:
                        pub_dt = datetime(*pub[:6])
                        if pub_dt < cutoff:
                            continue
                    pub_str = datetime(*pub[:6]).strftime("%b %d") if pub else "Recent"
                except Exception:
                    pub_str = "Recent"

                articles.append({
                    "title":   title,
                    "url":     entry.get("link", ""),
                    "source":  feed["name"],
                    "tag":     feed["tag"],
                    "date":    pub_str,
                    "summary": summary,
                    "stage":   extract_stage(combined),
                    "amount":  extract_amount(combined),
                })
        except Exception as e:
            logger.debug(f"RSS fetch failed [{feed['name']}]: {e}")

    return articles[:20]

# ── scan_twitter: used by daily_report.py ────────────────────────

async def scan_twitter(bearer: str = None, max_r: int = 10) -> List[Dict]:
    """
    Pull Web3 funding signals from Twitter/X.

    Priority order:
    1. Official Twitter API v2 (if TWITTER_BEARER_TOKEN is set)
    2. CryptoPanic free public RSS (aggregates Twitter + social)
    3. VC blog RSS feeds as fallback

    Returns list of dicts with keys: text, username, stage, amount, url, followers, likes, retweets
    """
    results = []

    # ── Option 1: Official Twitter API v2 ─────────────────────────
    if bearer:
        try:
            import tweepy
            client = tweepy.AsyncClient(bearer_token=bearer, wait_on_rate_limit=False)
            query  = "(web3 OR crypto OR blockchain) (raises OR funding OR seed OR IDO OR ICO) -is:retweet lang:en"
            tweets = await client.search_recent_tweets(
                query=query, max_results=min(max_r, 10),
                tweet_fields=["author_id","public_metrics","created_at"],
                user_fields=["username","public_metrics"],
                expansions=["author_id"]
            )
            if tweets and tweets.data:
                users = {u.id: u for u in (tweets.includes.get("users") or [])}
                for tw in tweets.data:
                    user      = users.get(tw.author_id)
                    username  = user.username if user else "unknown"
                    followers = user.public_metrics.get("followers_count", 0) if user else 0
                    metrics   = tw.public_metrics or {}
                    text      = tw.text or ""
                    results.append({
                        "text":      text[:400],
                        "username":  username,
                        "stage":     extract_stage(text),
                        "amount":    extract_amount(text),
                        "url":       f"https://twitter.com/{username}/status/{tw.id}",
                        "followers": followers,
                        "likes":     metrics.get("like_count", 0),
                        "retweets":  metrics.get("retweet_count", 0),
                    })
            if results:
                logger.info(f"Twitter API v2: {len(results)} tweets")
                return results[:max_r]
        except Exception as e:
            logger.warning(f"Twitter API v2 failed: {e}")

    # ── Option 2: CryptoPanic public RSS ──────────────────────────
    try:
        cp_urls = [
            "https://cryptopanic.com/news/rss/",
            "https://cryptopanic.com/news/funding/rss/",
        ]
        loop = asyncio.get_event_loop()
        for url in cp_urls:
            try:
                parsed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in (parsed.entries or [])[:8]:
                    title   = entry.get("title", "").strip()
                    summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "").strip()[:300]
                    combined = (title + " " + summary).lower()
                    if not any(kw in combined for kw in FUNDING_KEYWORDS):
                        continue
                    results.append({
                        "text":      f"{title}. {summary}"[:400],
                        "username":  "CryptoPanic",
                        "stage":     extract_stage(combined),
                        "amount":    extract_amount(combined),
                        "url":       entry.get("link", "https://cryptopanic.com"),
                        "followers": 50000,
                        "likes":     0,
                        "retweets":  0,
                    })
            except Exception as e:
                logger.debug(f"CryptoPanic RSS [{url}]: {e}")

        if results:
            logger.info(f"CryptoPanic RSS: {len(results)} signals")
            return results[:max_r]
    except Exception as e:
        logger.warning(f"CryptoPanic fetch failed: {e}")

    # ── Option 3: VC blog RSS fallback ────────────────────────────
    try:
        loop = asyncio.get_event_loop()
        for account in VC_RSS_FEEDS:
            try:
                parsed = await loop.run_in_executor(None, feedparser.parse, account["url"])
                for entry in (parsed.entries or [])[:3]:
                    title   = entry.get("title","").strip()
                    summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "").strip()[:200]
                    combined = (title + " " + summary).lower()
                    if not any(kw in combined for kw in ["invest","fund","raise","launch","portfolio","announce","seed","series"]):
                        continue
                    results.append({
                        "text":      f"{title}. {summary}"[:400],
                        "username":  account["name"],
                        "stage":     extract_stage(combined),
                        "amount":    extract_amount(combined),
                        "url":       entry.get("link", ""),
                        "followers": 100000,
                        "likes":     0,
                        "retweets":  0,
                    })
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"VC RSS [{account['name']}]: {e}")
    except Exception as e:
        logger.warning(f"VC RSS fallback failed: {e}")

    logger.info(f"scan_twitter total signals: {len(results)}")
    return results[:max_r]

# ── fetch_funding_news: used by /funding_stats handler ───────────

async def fetch_funding_news(limit: int = 15) -> List[Dict]:
    """Pull funding headlines from crypto RSS. Returns articles list."""
    return (await scrape_rss(hours=48))[:limit]

# ── fetch_defi_llama_raises ───────────────────────────────────────

async def fetch_defi_llama_raises(limit: int = 8) -> List[Dict]:
    """Fetch recent raises from DeFiLlama public API — no key needed."""
    rounds: List[Dict] = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.llama.fi/raises",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data  = await resp.json(content_type=None)
                    items = data.get("raises", data) if isinstance(data, dict) else data
                    items = sorted(
                        [r for r in items if isinstance(r, dict)],
                        key=lambda x: x.get("date", 0), reverse=True
                    )[:limit]
                    for r in items:
                        amount    = r.get("amount")
                        amount_str = f"${amount:,.1f}M" if amount else "Undisclosed"
                        date_ts   = r.get("date")
                        date_str  = (datetime.utcfromtimestamp(date_ts).strftime("%b %d, %Y")
                                     if date_ts else "Recent")
                        leads = ", ".join(r.get("leadInvestors", [])[:2]) or "Undisclosed"
                        rounds.append({
                            "name":   r.get("name", "Unknown"),
                            "amount": amount_str,
                            "round":  r.get("round", "Funding"),
                            "date":   date_str,
                            "leads":  leads,
                            "chains": ", ".join(r.get("chains", [])[:2]),
                            "source": r.get("source", ""),
                        })
    except Exception as e:
        logger.warning(f"DeFiLlama raises failed: {e}")
    return rounds

# ── fetch_twitter_signals: used by /twitter_scan handler ─────────

async def fetch_twitter_signals(limit: int = 10) -> List[Dict]:
    """
    Pull Web3 VC and funding signals from Twitter/social.
    Uses scan_twitter internally.
    """
    bearer  = os.getenv("TWITTER_BEARER_TOKEN", "")
    signals = await scan_twitter(bearer or None, max_r=limit)
    # Convert to display format
    display = []
    for s in signals:
        display.append({
            "account": s.get("username", "Unknown"),
            "tag":     "🐦",
            "text":    s.get("text","")[:160],
            "url":     s.get("url", ""),
        })
    return display

# ── /funding_stats handler ────────────────────────────────────────

async def funding_stats_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    chat_id = update.effective_chat.id
    msg     = await update.message.reply_text(
        "📡 Scanning live funding data...\n"
        "Checking DeFiLlama, The Block, CoinDesk, Cointelegraph...\n"
        "⏳ 20–30 seconds"
    )

    try:
        llama_task = fetch_defi_llama_raises(limit=6)
        news_task  = fetch_funding_news(limit=10)
        rounds, articles = await asyncio.gather(llama_task, news_task)
    except Exception as e:
        logger.error(f"funding_stats gather failed: {e}")
        rounds, articles = [], []

    await ctx.bot.edit_message_text(
        chat_id=chat_id, message_id=msg.message_id,
        text=(f"📊 LIVE FUNDING INTELLIGENCE\n{'━'*30}\n"
              f"🏦 DeFiLlama Raises: {len(rounds)} recent\n"
              f"📰 News Articles: {len(articles)} found\n"
              f"{'━'*30}")
    )
    await asyncio.sleep(0.3)

    # DeFiLlama raises
    if rounds:
        await ctx.bot.send_message(chat_id=chat_id,
            text=f"🏦 RECENT RAISES (DeFiLlama)\n{'━'*28}")
        for r in rounds:
            await ctx.bot.send_message(
                chat_id=chat_id,
                disable_web_page_preview=True,
                text=(f"💰 *{r['name']}*\n"
                      f"Amount: {r['amount']}\n"
                      f"Round: {r['round']}\n"
                      f"Lead investors: {r['leads']}\n"
                      f"Chain: {r.get('chains','N/A')}\n"
                      f"Date: {r['date']}"),
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.3)

    # News articles
    if articles:
        await ctx.bot.send_message(chat_id=chat_id,
            text=f"\n📰 FUNDING NEWS\n{'━'*28}")
        for a in articles[:8]:
            await ctx.bot.send_message(
                chat_id=chat_id,
                disable_web_page_preview=True,
                text=(f"{a.get('tag','📰')} *{a.get('source','News')}*  {a.get('date','')}\n"
                      f"{a['title']}\n"
                      f"Stage: {a.get('stage','N/A')}  |  Amount: {a.get('amount','N/A')}\n"
                      f"🔗 {a['url']}"),
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.3)

    if not rounds and not articles:
        await ctx.bot.send_message(chat_id=chat_id,
            text="⚠️ No funding data found right now. RSS sources may be slow — try again in a few minutes.")

# ── /twitter_scan handler ─────────────────────────────────────────

async def twitter_scan_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    chat_id = update.effective_chat.id
    bearer  = os.getenv("TWITTER_BEARER_TOKEN","")
    source_note = "Twitter API v2" if bearer else "CryptoPanic + VC blogs (no Twitter key set)"

    msg = await update.message.reply_text(
        f"🐦 Scanning Web3 Twitter signals...\n"
        f"Source: {source_note}\n"
        f"⏳ 20–30 seconds"
    )

    try:
        signals = await fetch_twitter_signals(limit=12)
    except Exception as e:
        logger.error(f"twitter_scan failed: {e}")
        signals = []

    if not signals:
        await ctx.bot.edit_message_text(
            chat_id=chat_id, message_id=msg.message_id,
            text=("⚠️ No Twitter signals found right now.\n\n"
                  "To unlock real-time Twitter data:\n"
                  "Add TWITTER_BEARER_TOKEN to your Railway Variables.\n"
                  "Get a free token at developer.twitter.com")
        )
        return

    await ctx.bot.edit_message_text(
        chat_id=chat_id, message_id=msg.message_id,
        text=(f"🐦 TWITTER / SOCIAL SIGNALS\n{'━'*30}\n"
              f"Found {len(signals)} funding signals\n"
              f"Source: {source_note}")
    )
    await asyncio.sleep(0.3)

    for i, s in enumerate(signals[:10], 1):
        await ctx.bot.send_message(
            chat_id=chat_id,
            disable_web_page_preview=True,
            text=(f"#{i} {s['tag']} *{s['account']}*\n\n"
                  f"{s['text']}\n\n"
                  f"🔗 {s['url']}"),
            parse_mode="Markdown"
        )
        await asyncio.sleep(0.35)

    if not bearer:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=("💡 *Tip:* Add TWITTER_BEARER_TOKEN in Railway Variables\n"
                  "to unlock real-time tweet scanning from Web3 VCs.\n"
                  "Free token: developer.twitter.com"),
            parse_mode="Markdown"
        )

# ── /scout_communities handler ────────────────────────────────────

async def scout_communities_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    communities = [
        ("🏦 DeFi",       "@DeFiMillionaire",       "DeFi alpha and new projects"),
        ("🏦 DeFi",       "@defi_news_channel",     "DeFi funding and launches"),
        ("🚀 Launchpads", "@polkastarter",           "IDO announcements"),
        ("🚀 Launchpads", "@seedifyfund",            "Seedify project launches"),
        ("💰 Funding",    "@CryptoFundingAlerts",   "VC rounds and funding news"),
        ("💰 Funding",    "@Web3Funding",            "Startup funding alerts"),
        ("💼 Jobs",       "@Web3Jobs",               "Web3 job board"),
        ("💼 Jobs",       "@cryptojobs",             "Crypto career listings"),
        ("📊 Research",   "@MessariCrypto",          "On-chain research and data"),
        ("📊 Research",   "@TheBlockResearch",       "Market intelligence"),
        ("🎮 Gaming/NFT", "@nft_community",          "NFT project drops"),
        ("⛓️ L1/L2",      "@ethereum",               "Ethereum ecosystem news"),
        ("⛓️ L1/L2",      "@solana",                 "Solana ecosystem updates"),
        ("🔐 Security",   "@blockchain_security",    "Audit alerts and hacks"),
        ("🌐 DAO",        "@gitcoin",                "Grants and DAO governance"),
    ]

    await update.message.reply_text(
        f"🔭 WEB3 TELEGRAM COMMUNITIES\n{'━'*32}\n\n"
        "Here are the best channels to scout for new projects, jobs, and funding signals:"
    )
    await asyncio.sleep(0.3)

    current_cat = ""
    batch = []
    for cat, handle, desc in communities:
        if cat != current_cat:
            if batch:
                await update.message.reply_text("\n".join(batch), parse_mode="Markdown",
                                                disable_web_page_preview=True)
                await asyncio.sleep(0.3)
                batch = []
            current_cat = cat
            batch.append(f"*{cat}*")
        batch.append(f"  {handle} — _{desc}_")

    if batch:
        await update.message.reply_text("\n".join(batch), parse_mode="Markdown",
                                        disable_web_page_preview=True)

    await update.message.reply_text(
        "💡 *Scouting tips*\n\n"
        "• Watch pinned posts for new project announcements\n"
        "• Track admin posts about partnerships\n"
        "• Active hiring = funded project — check job posts\n"
        "• Run /daily\\_report every morning for auto-scouted projects",
        parse_mode="Markdown"
    )

# ── /status handler ───────────────────────────────────────────────

async def status_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    verified = is_verified(chat_id)

    if not verified:
        await update.message.reply_text(
            "📊 YOUR STATUS\n\n"
            "🔐 Subscription: Not verified\n\n"
            "Use /verify [code] to unlock full access."
        )
        return

    try:
        conn  = _db()
        row   = conn.execute("SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
        prefs = dict(row) if row else {}
        conn.close()
    except Exception:
        prefs = {}

    subscribed_at = prefs.get("subscribed_at","Unknown")
    if subscribed_at and subscribed_at != "Unknown":
        subscribed_at = subscribed_at[:10]

    try:
        conn      = _db()
        app_count = conn.execute(
            "SELECT COUNT(*) FROM user_job_applications WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
        conn.close()
    except Exception:
        app_count = 0

    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        conn  = _db()
        rc    = conn.execute(
            "SELECT projects_found FROM daily_report_log WHERE report_date=?", (today,)
        ).fetchone()
        conn.close()
        todays_projects = rc[0] if rc else 0
    except Exception:
        todays_projects = 0

    report_hour  = int(os.getenv("DAILY_REPORT_HOUR","8"))
    twitter_live = "✅ Connected" if os.getenv("TWITTER_BEARER_TOKEN") else "⚠️ Not set (add in Railway)"

    await update.message.reply_text(
        f"📊 YOUR ACCOUNT STATUS\n{'━'*30}\n\n"
        f"✅ Subscription: ACTIVE\n"
        f"📅 Member since: {subscribed_at}\n"
        f"🔔 Daily report: {report_hour:02d}:00 UTC\n"
        f"🐦 Twitter API: {twitter_live}",
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.2)

    await update.message.reply_text(
        f"📈 TODAY'S INTELLIGENCE\n{'━'*28}\n\n"
        f"Projects found today: {todays_projects}\n"
        f"Run /daily\\_report to refresh",
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.2)

    await update.message.reply_text(
        f"💼 JOB TRACKER\n{'━'*28}\n\n"
        f"Applications tracked: {app_count}\n"
        f"Use /applications to view all\n\n"
        f"{'━'*28}\n"
        f"🛠 QUICK COMMANDS\n\n"
        f"/daily\\_report — full morning scan\n"
        f"/funding\\_stats — live funding news\n"
        f"/twitter\\_scan — VC signals\n"
        f"/research [name] — deep project dive\n"
        f"/jobs — live job listings",
        parse_mode="Markdown"
    )
