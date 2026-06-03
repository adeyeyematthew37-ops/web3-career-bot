"""
funding_handlers.py
───────────────────
Handlers for:
  /funding_stats    — live funding news from DeFiLlama + RSS
  /twitter_scan     — Twitter/X Web3 signal headlines via Nitter RSS
  /scout_communities — Telegram community scouting tips + curated list
  /status           — subscriber's account status and preferences
  /weekly_report    — weekly digest summary

All handlers are async and accept (update, context) per python-telegram-bot v20.
"""

import os
import logging
import sqlite3
import aiohttp
import feedparser
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "enhanced_fundraising_alerts.db")


# ── DB helper ─────────────────────────────────────────────────────────────────

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


# ── Data fetchers ─────────────────────────────────────────────────────────────

FUNDING_RSS = [
    {"name": "The Block",      "url": "https://www.theblock.co/rss.xml",                    "tag": "⬛"},
    {"name": "CoinDesk",       "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",    "tag": "🟡"},
    {"name": "Cointelegraph",  "url": "https://cointelegraph.com/rss",                      "tag": "🟠"},
    {"name": "Decrypt",        "url": "https://decrypt.co/feed",                             "tag": "🔵"},
    {"name": "Messari",        "url": "https://messari.io/rss",                              "tag": "🟣"},
]

FUNDING_KEYWORDS = [
    "raises", "funding", "round", "seed", "series", "investment",
    "venture", "backed", "million", "billion", "vc", "capital",
    "grant", "launchpad", "ido", "ico", "token sale",
]

TWITTER_NITTER_RSS = [
    # Public Nitter instances — fallback if one is down
    "https://nitter.net/search/rss?q=web3+funding+round&f=tweets",
    "https://nitter.poast.org/search/rss?q=crypto+raises+million&f=tweets",
    "https://nitter.net/search/rss?q=blockchain+startup+raises&f=tweets",
]

SIGNAL_ACCOUNTS_RSS = [
    # Key Web3 VCs & funds with public RSS / Nitter
    {"name": "a16z crypto",      "url": "https://nitter.net/a16zcrypto/rss",      "tag": "🏦"},
    {"name": "Paradigm",         "url": "https://nitter.net/paradigm/rss",         "tag": "🏦"},
    {"name": "Multicoin",        "url": "https://nitter.net/multicoincap/rss",     "tag": "🏦"},
    {"name": "Binance Labs",     "url": "https://nitter.net/BinanceLabs/rss",      "tag": "🏦"},
    {"name": "Coinbase Ventures","url": "https://nitter.net/CoinbaseVentures/rss", "tag": "🏦"},
    {"name": "Electric Capital", "url": "https://nitter.net/ElectricCapital/rss",  "tag": "🏦"},
    {"name": "Delphi Digital",   "url": "https://nitter.net/Delphi_Digital/rss",   "tag": "🔬"},
    {"name": "Messari",          "url": "https://nitter.net/MessariCrypto/rss",    "tag": "📊"},
]


async def fetch_funding_news(limit: int = 15) -> List[Dict]:
    """Pull funding-related headlines from crypto RSS feeds."""
    articles: List[Dict] = []
    loop = asyncio.get_event_loop()

    for feed in FUNDING_RSS:
        try:
            parsed = await loop.run_in_executor(None, feedparser.parse, feed["url"])
            for entry in parsed.entries[:8]:
                title   = entry.get("title", "")
                summary = (entry.get("summary") or "")[:200]
                combined = (title + " " + summary).lower()
                # Only include articles that mention fundraising keywords
                if any(kw in combined for kw in FUNDING_KEYWORDS):
                    published = ""
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6]).strftime("%b %d")
                    articles.append({
                        "title":   title,
                        "url":     entry.get("link", ""),
                        "source":  feed["name"],
                        "tag":     feed["tag"],
                        "date":    published,
                        "summary": summary,
                    })
        except Exception as e:
            logger.warning(f"RSS fetch failed for {feed['name']}: {e}")

    return articles[:limit]


async def fetch_defi_llama_raises(limit: int = 8) -> List[Dict]:
    """Fetch recent raises from DeFiLlama public API."""
    rounds: List[Dict] = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.llama.fi/raises",
                timeout=aiohttp.ClientTimeout(total=12)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("raises", data) if isinstance(data, dict) else data
                    items = sorted(
                        [r for r in items if isinstance(r, dict)],
                        key=lambda x: x.get("date", 0),
                        reverse=True
                    )[:limit]
                    for r in items:
                        amount = r.get("amount")
                        amount_str = f"${amount:,.1f}M" if amount else "Undisclosed"
                        date_ts = r.get("date")
                        date_str = (
                            datetime.utcfromtimestamp(date_ts).strftime("%b %d, %Y")
                            if date_ts else "Recent"
                        )
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
        logger.warning(f"DeFiLlama raises fetch failed: {e}")
    return rounds


async def fetch_twitter_signals(limit: int = 10) -> List[Dict]:
    """Pull Web3 signals from VC Twitter accounts via Nitter RSS (no API key)."""
    signals: List[Dict] = []
    loop = asyncio.get_event_loop()

    for account in SIGNAL_ACCOUNTS_RSS:
        try:
            parsed = await loop.run_in_executor(None, feedparser.parse, account["url"])
            for entry in parsed.entries[:3]:
                title   = entry.get("title", "")
                summary = (entry.get("summary") or entry.get("description") or "")
                # Strip HTML tags simply
                import re
                clean = re.sub(r"<[^>]+>", " ", summary).strip()[:200]
                combined = (title + " " + clean).lower()
                # Only surface posts mentioning investment/project signals
                if any(kw in combined for kw in [
                    "invest", "fund", "raise", "back", "launch", "partner",
                    "excited", "portfolio", "grant", "announce", "seed", "series"
                ]):
                    signals.append({
                        "account": account["name"],
                        "tag":     account["tag"],
                        "text":    clean[:160] or title[:160],
                        "url":     entry.get("link", ""),
                    })
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"Nitter RSS failed for {account['name']}: {e}")

    # Fallback: keyword search RSS if account feeds all fail
    if not signals:
        for url in TWITTER_NITTER_RSS[:2]:
            try:
                parsed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in parsed.entries[:5]:
                    import re
                    clean = re.sub(r"<[^>]+>", " ",
                                   entry.get("summary") or entry.get("title") or "").strip()[:200]
                    signals.append({
                        "account": "Web3 Twitter",
                        "tag":     "🐦",
                        "text":    clean,
                        "url":     entry.get("link", ""),
                    })
            except Exception:
                pass

    return signals[:limit]


# ── /funding_stats ────────────────────────────────────────────────────────────

async def funding_stats_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show live funding news: DeFiLlama raises + RSS headlines."""
    if not await guard(update):
        return

    msg = await update.message.reply_text(
        "📡 Scanning live funding data...\n"
        "🔗 DeFiLlama · The Block · CoinDesk · Messari\n"
        "⏳ 15–30 seconds..."
    )

    raises, articles = await asyncio.gather(
        fetch_defi_llama_raises(limit=6),
        fetch_funding_news(limit=10),
    )

    lines = [f"📊 LIVE FUNDING STATS\n{'━'*30}\n"]

    # DeFiLlama recent raises
    if raises:
        lines.append(f"💰 RECENT VC ROUNDS ({len(raises)} found)\n")
        for r in raises:
            chain_str = f" | ⛓️ {r['chains']}" if r["chains"] else ""
            src_str   = f"\n   🔗 [Source]({r['source']})" if r.get("source") else ""
            lines.append(
                f"🚀 *{r['name']}* — {r['amount']}\n"
                f"   📋 {r['round']} | 🗓️ {r['date']}{chain_str}\n"
                f"   🏦 Lead: {r['leads']}{src_str}\n"
            )
    else:
        lines.append("⚠️ DeFiLlama data unavailable right now.\n")

    # RSS funding headlines
    if articles:
        lines.append(f"\n{'━'*30}\n📰 FUNDING HEADLINES ({len(articles)} found)\n")
        for a in articles[:8]:
            date_str = f" · {a['date']}" if a.get("date") else ""
            lines.append(
                f"{a['tag']} *{a['title']}*\n"
                f"   _{a['source']}{date_str}_ | 🔗 [Read]({a['url']})\n"
            )
    else:
        lines.append("\n⚠️ No funding headlines found right now.\n")

    lines.append(f"{'━'*30}\n_Use /daily\\_report for the full 20-source scan._")

    try:
        await ctx.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"funding_stats edit failed: {e}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
        )


# ── /twitter_scan ─────────────────────────────────────────────────────────────

async def twitter_scan_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pull Web3 investment signals from top VC Twitter accounts via Nitter RSS."""
    if not await guard(update):
        return

    msg = await update.message.reply_text(
        "🐦 Scanning Twitter/X for Web3 signals...\n"
        "Accounts: a16z · Paradigm · Binance Labs · Multicoin\n"
        "Coinbase Ventures · Electric Capital · Delphi · Messari\n"
        "⏳ 20–40 seconds..."
    )

    signals = await fetch_twitter_signals(limit=10)

    if not signals:
        await ctx.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                "⚠️ Twitter/X signal scan came back empty.\n\n"
                "Nitter public instances may be rate-limited right now.\n"
                "Try again in a few minutes, or use /funding_stats for RSS-based signals."
            ),
        )
        return

    lines = [f"🐦 TWITTER/X WEB3 SIGNALS\n{'━'*30}\n{len(signals)} signals detected\n"]
    for i, s in enumerate(signals, 1):
        url_str = f"\n   🔗 [View tweet]({s['url']})" if s.get("url") else ""
        lines.append(
            f"{i}. {s['tag']} *{s['account']}*\n"
            f"   {s['text']}{url_str}\n"
        )

    lines.append(
        f"{'━'*30}\n"
        "_Signals sourced from Nitter RSS. Run /funding\\_stats for confirmed raises._"
    )

    try:
        await ctx.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"twitter_scan edit failed: {e}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
        )


# ── /scout_communities ────────────────────────────────────────────────────────

async def scout_communities_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Curated list of high-signal Web3 Telegram communities to scout."""
    if not await guard(update):
        return

    communities = [
        ("🏦 DeFi",         "@DeFiMillionaire",       "DeFi alpha, new projects"),
        ("🏦 DeFi",         "@defi_news_channel",     "DeFi funding & launches"),
        ("🚀 Launchpads",   "@polkastarter",           "IDO announcements"),
        ("🚀 Launchpads",   "@seedifyfund",            "Seedify project launches"),
        ("💰 Funding",      "@CryptoFundingAlerts",   "VC rounds & funding news"),
        ("💰 Funding",      "@Web3Funding",            "Startup funding alerts"),
        ("💼 Jobs",         "@Web3Jobs",               "Web3 job board"),
        ("💼 Jobs",         "@cryptojobs",             "Crypto career listings"),
        ("📊 Research",     "@MessariCrypto",          "On-chain research & data"),
        ("📊 Research",     "@TheBlockResearch",       "Market intelligence"),
        ("🎮 Gaming/NFT",   "@nft_community",          "NFT project drops"),
        ("⛓️ L1/L2",        "@ethereum",               "Ethereum ecosystem news"),
        ("⛓️ L1/L2",        "@solana",                 "Solana ecosystem updates"),
        ("🔐 Security",     "@blockchain_security",    "Audit alerts & hacks"),
        ("🌐 DAO",          "@gitcoin",                "Grants & DAO governance"),
    ]

    lines = [f"🔭 WEB3 TELEGRAM COMMUNITIES TO SCOUT\n{'━'*34}\n"]
    current_cat = ""
    for cat, handle, desc in communities:
        if cat != current_cat:
            current_cat = cat
            lines.append(f"\n*{cat}*")
        lines.append(f"  {handle} — _{desc}_")

    lines.append(
        f"\n\n{'━'*34}\n"
        "💡 *Scouting tips:*\n"
        "• Watch for pinned 'New Project' announcements\n"
        "• Track admin posts about partnerships\n"
        "• Monitor job posts — active hiring = funded project\n"
        "• Check /daily\\_report for auto-scouted projects every morning"
    )

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def status_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show user's subscription status and current settings."""
    chat_id  = update.effective_chat.id
    verified = is_verified(chat_id)

    if not verified:
        await update.message.reply_text(
            "📊 YOUR STATUS\n\n"
            "🔐 Subscription: Not verified\n\n"
            "Use /verify [code] to unlock full access."
        )
        return

    # Pull preferences from DB if they exist
    try:
        conn = _db()
        row  = conn.execute(
            "SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
        conn.close()
        prefs = dict(row) if row else {}
    except Exception:
        prefs = {}

    subscribed_at = prefs.get("subscribed_at", "Unknown")
    if subscribed_at and subscribed_at != "Unknown":
        try:
            subscribed_at = subscribed_at[:10]  # just the date
        except Exception:
            pass

    # Application count
    try:
        conn = _db()
        app_count = conn.execute(
            "SELECT COUNT(*) FROM user_job_applications WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
        conn.close()
    except Exception:
        app_count = 0

    # Today's report count
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        conn = _db()
        report_count = conn.execute(
            "SELECT projects_found FROM daily_report_log WHERE report_date=?", (today,)
        ).fetchone()
        conn.close()
        todays_projects = report_count[0] if report_count else 0
    except Exception:
        todays_projects = 0

    report_hour = int(os.getenv("DAILY_REPORT_HOUR", "8"))

    lines = [
        f"📊 YOUR ACCOUNT STATUS\n{'━'*30}",
        f"\n✅ Subscription: *ACTIVE*",
        f"📅 Member since: {subscribed_at}",
        f"🔔 Daily report: {report_hour:02d}:00 UTC",
        f"\n{'━'*30}",
        f"📈 TODAY'S INTELLIGENCE",
        f"  Projects found: {todays_projects}",
        f"  Run /daily\\_report to refresh",
        f"\n{'━'*30}",
        f"💼 JOB TRACKER",
        f"  Applications tracked: {app_count}",
        f"  /applications to view all",
        f"\n{'━'*30}",
        f"🛠 COMMANDS",
        f"  /daily\\_report — full morning scan",
        f"  /funding\\_stats — live funding news",
        f"  /twitter\\_scan  — VC Twitter signals",
        f"  /scout\\_communities — Telegram scouting",
        f"  /research [name] — deep project dive",
        f"  /jobs — live job listings",
        f"  /set\\_report\\_time [hour] — change report time",
    ]

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


# ── /weekly_report ────────────────────────────────────────────────────────────

async def weekly_report_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show a summary of projects found this week."""
    if not await guard(update):
        return

    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        conn = _db()
        rows = conn.execute(
            """SELECT source, COUNT(*) as cnt, AVG(legitimacy_score) as avg_score
               FROM daily_projects
               WHERE report_date >= ?
               GROUP BY source
               ORDER BY cnt DESC""",
            (week_ago,)
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) FROM daily_projects WHERE report_date >= ?",
            (week_ago,)
        ).fetchone()[0]

        top_projects = conn.execute(
            """SELECT project_name, stage, amount_raised, legitimacy_score, source
               FROM daily_projects
               WHERE report_date >= ?
               ORDER BY legitimacy_score DESC LIMIT 5""",
            (week_ago,)
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"weekly_report DB error: {e}")
        await update.message.reply_text(
            "⚠️ Weekly report data not available yet.\n"
            "Run /daily_report each day to build up your weekly history."
        )
        return

    lines = [
        f"📅 WEEKLY INTELLIGENCE REPORT\n{'━'*32}\n",
        f"🗓️ Last 7 days  |  {total} projects tracked\n",
    ]

    if rows:
        lines.append("📡 BY SOURCE\n")
        for row in rows[:8]:
            lines.append(
                f"  • {row['source']}: {row['cnt']} projects "
                f"(avg score: {row['avg_score']:.0f}/100)"
            )

    if top_projects:
        lines.append(f"\n{'━'*32}\n⭐ TOP PROJECTS THIS WEEK (by legitimacy score)\n")
        for i, p in enumerate(top_projects, 1):
            lines.append(
                f"{i}. *{p['project_name']}* — {p['stage']}\n"
                f"   💰 {p['amount_raised']} | Score: {p['legitimacy_score']:.0f}/100\n"
                f"   Source: {p['source']}"
            )

    lines.append(
        f"\n{'━'*32}\n"
        "_Run /daily\\_report every morning to keep your data fresh._"
    )

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )
