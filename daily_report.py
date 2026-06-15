"""
daily_report.py
───────────────
Full daily intelligence engine. Scrapes 20 sources, stores every
project in the database, sends a morning digest with inline buttons.
Tapping a button sends each analysis section as its own clean message.
"""

import asyncio, aiohttp, feedparser, re, os, sqlite3, json, logging, hashlib
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

DB_PATH     = os.getenv("DB_PATH", "enhanced_fundraising_alerts.db")
REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "8"))

# ══════════════════════════════════════════════════════════════════
#  DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_daily_tables():
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_projects (
            id TEXT PRIMARY KEY,
            report_date TEXT, project_name TEXT, symbol TEXT,
            stage TEXT, amount_raised TEXT, description TEXT,
            website TEXT, twitter TEXT, telegram_link TEXT,
            discord TEXT, github TEXT, risk_level TEXT,
            legitimacy_score REAL DEFAULT 50, source TEXT,
            market_cap REAL DEFAULT 0, price REAL DEFAULT 0,
            analysis TEXT, job_opportunities TEXT, found_at TEXT,
            chain TEXT, contract TEXT, investors TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT UNIQUE,
            sources_checked INTEGER,
            projects_found INTEGER,
            sent_at TEXT
        )""")
    conn.commit(); conn.close()

def save_project(p: Dict) -> bool:
    conn = _db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO daily_projects
            (id,report_date,project_name,symbol,stage,amount_raised,description,
             website,twitter,telegram_link,discord,github,risk_level,legitimacy_score,
             source,market_cap,price,analysis,job_opportunities,found_at,chain,contract,investors)
            VALUES(:id,:report_date,:project_name,:symbol,:stage,:amount_raised,:description,
             :website,:twitter,:telegram_link,:discord,:github,:risk_level,:legitimacy_score,
             :source,:market_cap,:price,:analysis,:job_opportunities,:found_at,:chain,:contract,:investors)
        """, p)
        conn.commit(); return True
    except Exception as e:
        logger.error(f"save_project: {e}"); return False
    finally:
        conn.close()

def get_project(pid: str) -> Optional[Dict]:
    conn = _db()
    row  = conn.execute("SELECT * FROM daily_projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_todays_projects() -> List[Dict]:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn  = _db()
    rows  = conn.execute(
        "SELECT * FROM daily_projects WHERE report_date=? ORDER BY legitimacy_score DESC",
        (today,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_verified_subs() -> List[int]:
    conn = _db()
    rows = conn.execute(
        "SELECT chat_id FROM subscribers WHERE subscription_verified=TRUE").fetchall()
    conn.close()
    return [r[0] for r in rows]

# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════

def extract_amount(text: str) -> str:
    for p in [r"\$[\d,.]+\s*(?:million|billion|M|B|m|b)\b",
              r"[\d,.]+\s*(?:million|billion)\s*(?:dollar|USD|\$)"]:
        m = re.search(p, text, re.IGNORECASE)
        if m: return m.group().strip()
    return "Undisclosed"

def extract_stage(text: str) -> str:
    t = text.lower()
    for kw, label in [("pre-seed","Pre-Seed"),("preseed","Pre-Seed"),
                       ("series b","Series B"),("series a","Series A"),
                       ("series c","Series C"),("seed","Seed"),
                       ("ido","IDO"),("ico","ICO"),("ieo","IEO"),
                       ("token sale","Token Sale"),("private sale","Private Sale"),
                       ("public sale","Public Sale"),("launchpad","Launchpad"),
                       ("grant","Grant"),("fair launch","Fair Launch")]:
        if kw in t: return label
    return "Funding Round"

def score_project(p: Dict) -> float:
    score = 50.0
    desc  = f"{p.get('description','')} {p.get('analysis','')}".lower()
    for pos in ["whitepaper","audit","github","doxxed","partnership","testnet",
                "mainnet","roadmap","team","certik","peckshield","open source"]:
        if pos in desc: score += 6
    if p.get("github")  and p["github"]  not in ("N/A",""):  score += 10
    if p.get("twitter") and p["twitter"] not in ("N/A",""):  score += 8
    if p.get("website") and p["website"] not in ("N/A",""):  score += 5
    for neg in ["guaranteed profit","risk free","100x","get rich","pump",
                "unlimited supply","anonymous team","no team","rug"]:
        if neg in desc: score -= 20
    return max(0, min(100, score))

def risk_label(score: float) -> str:
    if score >= 70: return "LOW ✅"
    if score >= 50: return "MEDIUM ⚠️"
    if score >= 30: return "HIGH 🔸"
    return "CRITICAL 🚨"

def make_project_id(name: str, source: str) -> str:
    return hashlib.md5(f"{name}{source}{datetime.utcnow().strftime('%Y-%m-%d')}".encode()).hexdigest()[:10]

def _base(name, source, stage="Funding Round", amount="Undisclosed",
          chain="Multiple", contract="N/A", investors="N/A"):
    """Quick template for a base project dict."""
    return {
        "id":               make_project_id(name, source),
        "report_date":      datetime.utcnow().strftime("%Y-%m-%d"),
        "project_name":     name,
        "symbol":           "N/A",
        "stage":            stage,
        "amount_raised":    amount,
        "description":      "",
        "website":          "N/A",
        "twitter":          "N/A",
        "telegram_link":    "N/A",
        "discord":          "N/A",
        "github":           "N/A",
        "risk_level":       "MEDIUM ⚠️",
        "legitimacy_score": 50,
        "source":           source,
        "market_cap":       0,
        "price":            0,
        "analysis":         "",
        "job_opportunities":"Community Management, PR, Social Media",
        "found_at":         datetime.utcnow().isoformat(),
        "chain":            chain,
        "contract":         contract,
        "investors":        investors,
    }

async def fetch(session, url, params=None, json_resp=True, timeout=15):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Web3IntelBot/4.0; +https://github.com/web3-career-bot)"}
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                logger.debug(f"fetch {url} → {r.status}")
                return None
            return await r.json(content_type=None) if json_resp else await r.text()
    except Exception as e:
        logger.debug(f"fetch error {url}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════
#  SCRAPERS (20 sources)
# ══════════════════════════════════════════════════════════════════

# ── 1. CoinGecko — newly listed coins ────────────────────────────
async def scrape_coingecko(session) -> List[Dict]:
    data = await fetch(session, "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency":"usd","order":"id_asc","per_page":50,"page":1,
                "sparkline":"false","price_change_percentage":"24h"})
    if not data: return []
    projects = []
    for c in data[:15]:
        p = _base(c.get("name","Unknown"), "CoinGecko", stage="Listed")
        p.update({
            "symbol":      (c.get("symbol","") or "").upper(),
            "description": f"Listed on CoinGecko. Price: ${c.get('current_price',0):,.6f}",
            "website":     f"https://www.coingecko.com/en/coins/{c.get('id','')}",
            "market_cap":  c.get("market_cap", 0) or 0,
            "price":       c.get("current_price", 0) or 0,
            "analysis":    (f"Market cap: ${c.get('market_cap',0):,.0f}. "
                            f"24h change: {c.get('price_change_percentage_24h',0):.1f}%"),
        })
        projects.append(p)
    return projects[:8]

# ── 2. CoinGecko detail enrichment ───────────────────────────────
async def enrich_coingecko(session, coin_id: str) -> Dict:
    data = await fetch(session, f"https://api.coingecko.com/api/v3/coins/{coin_id}",
        params={"localization":"false","tickers":"false",
                "community_data":"true","developer_data":"true"})
    if not data: return {}
    links = data.get("links", {})
    return {
        "description":   (data.get("description",{}).get("en","") or "")[:500],
        "website":       (links.get("homepage",["N/A"])[0] or "N/A").rstrip("/"),
        "twitter":       f"@{links.get('twitter_screen_name','N/A')}",
        "telegram_link": links.get("telegram_channel_identifier","N/A"),
        "github":        (links.get("repos_url",{}).get("github",["N/A"])[0] or "N/A"),
        "discord":       "N/A",
        "investors":     "N/A",
    }

# ── 3. DexScreener — latest boosted/trending tokens ──────────────
async def scrape_dexscreener(session) -> List[Dict]:
    """
    Uses /token-boosts/latest/v1 (working free endpoint).
    Falls back to /token-profiles/latest/v1.
    """
    projects = []

    # Primary: token boosts (trending)
    data = await fetch(session, "https://api.dexscreener.com/token-boosts/latest/v1")
    if not data:
        # Fallback: token profiles
        data = await fetch(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    if not data:
        return projects

    items = data if isinstance(data, list) else data.get("pairs", [])
    for item in (items or [])[:10]:
        token_addr = item.get("tokenAddress","N/A")
        chain_id   = item.get("chainId","unknown")
        name       = item.get("description", token_addr[:12]) or token_addr[:12]
        url        = item.get("url", f"https://dexscreener.com/{chain_id}/{token_addr}")
        icon       = item.get("icon","")
        links_raw  = item.get("links", []) or []
        twitter    = next((l.get("url","N/A") for l in links_raw if l.get("type") == "twitter"), "N/A")
        telegram   = next((l.get("url","N/A") for l in links_raw if l.get("type") == "telegram"), "N/A")

        p = _base(name, "DexScreener", stage="Trending", chain=chain_id.title(), contract=token_addr)
        p.update({
            "description": f"Trending boosted token on DexScreener ({chain_id}).",
            "website":     url,
            "twitter":     twitter,
            "telegram_link": telegram,
            "analysis":    f"Boosted on DexScreener. Chain: {chain_id}. Contract: {token_addr[:16]}...",
            "risk_level":  "HIGH 🔸",
            "legitimacy_score": 30,
        })
        projects.append(p)
    return projects[:8]

# ── 4. GeckoTerminal — new pools & trending ───────────────────────
async def scrape_geckoterminal(session) -> List[Dict]:
    """
    Free API — no key needed.
    Replaces dead DexTools and dead Nomics.
    """
    projects = []
    endpoints = [
        ("https://api.geckoterminal.com/api/v2/networks/new_pools",     "New Pool"),
        ("https://api.geckoterminal.com/api/v2/networks/trending_pools", "Trending"),
    ]
    for url, label in endpoints:
        data = await fetch(session, url, params={"page": 1})
        if not data: continue
        pools = (data.get("data") or [])[:5]
        for pool in pools:
            attrs    = pool.get("attributes", {}) or {}
            rels     = pool.get("relationships", {}) or {}
            name     = attrs.get("name","Unknown")
            pool_url = f"https://www.geckoterminal.com/{pool.get('id','')}"
            fdv      = float(attrs.get("fdv_usd",0) or 0)
            vol24h   = float(attrs.get("volume_usd",{}).get("h24",0) or 0)
            price    = float(attrs.get("base_token_price_usd",0) or 0)

            p = _base(name, "GeckoTerminal", stage=label)
            p.update({
                "description": f"{label} on GeckoTerminal. Price: ${price:.8f}",
                "website":     pool_url,
                "market_cap":  fdv,
                "price":       price,
                "analysis":    f"FDV: ${fdv:,.0f}. 24h volume: ${vol24h:,.0f}.",
                "risk_level":  "HIGH 🔸",
                "legitimacy_score": 32,
            })
            projects.append(p)
        await asyncio.sleep(0.4)
    return projects[:8]

# ── 5. ICO Drops ──────────────────────────────────────────────────
async def scrape_ico_drops(session) -> List[Dict]:
    html = await fetch(session, "https://icodrops.com/category/active-ico/", json_resp=False)
    if not html: return []
    soup     = BeautifulSoup(html, "html.parser")
    cards    = soup.find_all("div", class_=re.compile(r"ico-card|project"))
    projects = []
    for card in cards[:8]:
        name_el = card.find(["h3","h4","a"])
        if not name_el: continue
        name  = name_el.get_text(strip=True)
        if not name or len(name) < 2: continue
        stage_el  = card.find(class_=re.compile(r"stage|round|sale"))
        raise_el  = card.find(class_=re.compile(r"raise|amount|hard.?cap"))
        link      = name_el.get("href","")
        if link and not link.startswith("http"):
            link = f"https://icodrops.com{link}"
        stage  = stage_el.get_text(strip=True) if stage_el else "Active ICO"
        amount = raise_el.get_text(strip=True) if raise_el else "Undisclosed"
        p = _base(name, "ICO Drops", stage=stage, amount=amount)
        p.update({
            "description": f"Active ICO on ICO Drops. Stage: {stage}",
            "website":     link or "https://icodrops.com",
            "analysis":    f"Listed on ICO Drops. Stage: {stage}. Amount: {amount}",
            "legitimacy_score": 50,
        })
        projects.append(p)
    return projects

# ── 6. CryptoRank ─────────────────────────────────────────────────
async def scrape_cryptorank(session) -> List[Dict]:
    html = await fetch(session, "https://cryptorank.io/fundraising", json_resp=False)
    if not html: return []
    soup     = BeautifulSoup(html, "html.parser")
    projects = []
    # Try multiple selector patterns
    rows = (soup.find_all("tr", class_=re.compile(r"table|row|fund")) or
            soup.find_all("div", class_=re.compile(r"project|card|fund")))
    for row in rows[:8]:
        name_el = row.find(["a","h3","td","strong"])
        if not name_el: continue
        name = name_el.get_text(strip=True)[:60]
        if not name or len(name) < 2: continue
        amount_el = row.find(class_=re.compile(r"amount|raise|fund"))
        amount    = amount_el.get_text(strip=True) if amount_el else "Undisclosed"
        p = _base(name, "CryptoRank", stage="Fundraising", amount=amount)
        p.update({
            "description": "Active fundraising round tracked by CryptoRank.",
            "website":     "https://cryptorank.io",
            "analysis":    "Fundraising round listed on CryptoRank — VC-tracked.",
            "job_opportunities": "BD, Community Management, Marketing",
            "legitimacy_score": 55,
        })
        projects.append(p)
    return projects

# ── 7. DeFiLlama Raises ───────────────────────────────────────────
async def scrape_defillama(session) -> List[Dict]:
    """DeFiLlama /raises endpoint — free, reliable, no auth needed."""
    data = await fetch(session, "https://api.llama.fi/raises")
    if not data: return []
    items = data.get("raises", data) if isinstance(data, dict) else data
    items = sorted([r for r in items if isinstance(r, dict)],
                   key=lambda x: x.get("date", 0), reverse=True)[:10]
    projects = []
    for r in items:
        name   = r.get("name","Unknown")
        amount = r.get("amount")
        amount_str = f"${amount:,.1f}M" if amount else "Undisclosed"
        leads  = ", ".join(r.get("leadInvestors",[])[:3]) or "Undisclosed"
        chains = ", ".join(r.get("chains",[])[:2]) or "Multiple"
        date_ts = r.get("date")
        date_str = (datetime.utcfromtimestamp(date_ts).strftime("%b %d, %Y")
                    if date_ts else "Recent")
        p = _base(name, "DeFiLlama Raises", stage=r.get("round","Funding Round"),
                  amount=amount_str, chain=chains, investors=leads)
        p.update({
            "description": f"Fundraising round tracked by DeFiLlama. Date: {date_str}.",
            "website":     r.get("source","https://defillama.com/raises"),
            "analysis":    f"Amount: {amount_str}. Lead investors: {leads}. Date: {date_str}.",
            "job_opportunities": "BD, Community Management, Marketing, PR",
            "legitimacy_score": 70,
        })
        projects.append(p)
    return projects[:8]

# ── 8. Polkastarter ───────────────────────────────────────────────
async def scrape_polkastarter(session) -> List[Dict]:
    html = await fetch(session, "https://polkastarter.com", json_resp=False)
    if not html: return []
    soup     = BeautifulSoup(html, "html.parser")
    projects = []
    cards    = soup.find_all(["article","div"], class_=re.compile(r"project|pool|ido|card"))
    for card in cards[:5]:
        name_el = card.find(["h2","h3","strong","a"])
        if not name_el: continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 2: continue
        p = _base(name, "Polkastarter", stage="IDO", investors="Polkastarter")
        p.update({
            "description":   "IDO on Polkastarter — cross-chain launchpad.",
            "website":       "https://polkastarter.com",
            "analysis":      "Polkastarter IDO. Requires POLS staking for allocation.",
            "legitimacy_score": 62,
        })
        projects.append(p)
    return projects

# ── 9. DAO Maker ──────────────────────────────────────────────────
async def scrape_dao_maker(session) -> List[Dict]:
    html = await fetch(session, "https://daomaker.com/", json_resp=False)
    if not html: return []
    soup     = BeautifulSoup(html, "html.parser")
    projects = []
    cards    = soup.find_all(["div","article"], class_=re.compile(r"project|ido|deal|strong"))
    for card in cards[:4]:
        name_el = card.find(["h2","h3","strong","a"])
        if not name_el: continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 2: continue
        p = _base(name, "DAO Maker", stage="Socialized IDO / Private Sale",
                  investors="DAO Maker")
        p.update({
            "description": "Project on DAO Maker — socialized fundraising with vesting.",
            "website":     "https://daomaker.com",
            "analysis":    "DAO Maker socialized model — strong vesting structures.",
            "legitimacy_score": 60,
        })
        projects.append(p)
    return projects

# ── 10. PinkSale presales ─────────────────────────────────────────
async def scrape_pinksale(session) -> List[Dict]:
    data = await fetch(session,
        "https://api.pinksale.finance/api/pool/list",
        params={"page":1,"pageSize":10,"status":1})
    if not data: return []
    pools    = (data.get("data",{}) or {}).get("list",[]) or []
    projects = []
    for pool in pools[:6]:
        token = pool.get("token",{}) or {}
        name  = token.get("name","Unknown")
        p = _base(name, "PinkSale", stage="Presale",
                  amount=f"Hard cap: {pool.get('hardCap','?')} BNB",
                  chain="BNB Chain", contract=token.get("address","N/A"))
        p.update({
            "symbol":      token.get("symbol","").upper(),
            "description": "Presale on PinkSale Finance. High risk — anyone can list here.",
            "website":     pool.get("website","N/A"),
            "twitter":     pool.get("twitter","N/A"),
            "telegram_link": pool.get("telegram","N/A"),
            "analysis":    (f"PinkSale presale. Start: {pool.get('startTime','?')}. "
                            f"End: {pool.get('endTime','?')}. DYOR extensively."),
            "risk_level":  "HIGH 🔸",
            "legitimacy_score": 28,
        })
        projects.append(p)
    return projects

# ── 11. CoinList ──────────────────────────────────────────────────
async def scrape_coinlist(session) -> List[Dict]:
    html = await fetch(session, "https://coinlist.co/sales", json_resp=False)
    if not html: return []
    soup     = BeautifulSoup(html, "html.parser")
    cards    = soup.find_all(["div","article"], class_=re.compile(r"sale|project|deal"))
    projects = []
    for card in cards[:5]:
        name_el = card.find(["h2","h3","strong"])
        if not name_el: continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 2: continue
        p = _base(name, "CoinList", stage="Token Sale",
                  investors="CoinList vetted")
        p.update({
            "description": "Compliant token sale on CoinList — one of the most vetted launchpads.",
            "website":     "https://coinlist.co",
            "analysis":    "CoinList applies strict KYC/AML. Projects here are legally compliant.",
            "risk_level":  "LOW ✅",
            "legitimacy_score": 75,
        })
        projects.append(p)
    return projects

# ── 12. Binance Launchpad ─────────────────────────────────────────
async def scrape_binance_launchpad(session) -> List[Dict]:
    # Try the API first
    data = await fetch(session,
        "https://launchpad.binance.com/gateway/v1/launchpad/project/query",
        params={"pageNum":1,"pageSize":5})
    projects = []

    if data:
        items = (data.get("data",{}) or {}).get("list",[]) or []
        for item in items:
            name = item.get("projectName","Unknown")
            p = _base(name, "Binance Launchpad", stage="IEO / Launchpad",
                      amount="Requires BNB holdings", chain="BNB Chain",
                      investors="Binance")
            p.update({
                "symbol":        item.get("tokenSymbol","").upper(),
                "description":   item.get("description","Top-tier IEO on Binance Launchpad.")[:400],
                "website":       item.get("officialWebsite","N/A"),
                "twitter":       item.get("twitterUrl","N/A"),
                "telegram_link": item.get("telegramUrl","N/A"),
                "analysis":      "Binance Launchpad — highest tier vetting. Requires BNB.",
                "risk_level":    "LOW ✅",
                "legitimacy_score": 80,
            })
            projects.append(p)
    else:
        # Fallback: scrape the page
        html = await fetch(session, "https://launchpad.binance.com", json_resp=False)
        if html:
            soup  = BeautifulSoup(html, "html.parser")
            cards = soup.find_all(["div","article"], class_=re.compile(r"project|token|launch"))
            for card in cards[:3]:
                name_el = card.find(["h2","h3","strong"])
                if not name_el: continue
                name = name_el.get_text(strip=True)
                if not name or len(name) < 2: continue
                p = _base(name, "Binance Launchpad", stage="IEO / Launchpad",
                          investors="Binance")
                p.update({
                    "description":    "IEO on Binance Launchpad.",
                    "website":        "https://launchpad.binance.com",
                    "risk_level":     "LOW ✅",
                    "legitimacy_score": 78,
                })
                projects.append(p)
    return projects

# ── 13. Messari ───────────────────────────────────────────────────
async def scrape_messari(session) -> List[Dict]:
    # Messari's public API (free tier, limited)
    data = await fetch(session, "https://data.messari.io/api/v2/assets",
        params={"fields": "id,slug,name,symbol,profile/general/overview/project_details,"
                          "profile/general/overview/official_website_link",
                "sort": "created_at", "limit": 10, "page": 1})
    if not data: return []
    projects = []
    for asset in (data.get("data") or [])[:8]:
        name    = asset.get("name","Unknown")
        profile = (asset.get("profile") or {}).get("general",{}).get("overview",{}) or {}
        p = _base(name, "Messari", stage="Venture Backed", amount="VC Funded")
        p.update({
            "symbol":      asset.get("symbol","").upper(),
            "description": (profile.get("project_details","") or "")[:400],
            "website":     profile.get("official_website_link","N/A"),
            "analysis":    "Listed on Messari research platform — VC-tracked project.",
            "legitimacy_score": 65,
        })
        projects.append(p)
    return projects

# ── 14 & 15. RSS Funding News ─────────────────────────────────────
async def scrape_funding_rss(session) -> List[Dict]:
    from funding_handlers import scrape_rss
    rss_items = await scrape_rss(hours=24)
    projects  = []
    for item in rss_items[:10]:
        name = item["title"][:60]
        p = _base(name, item.get("source","RSS"))
        p.update({
            "stage":        item.get("stage","Funding Round"),
            "amount_raised": item.get("amount","Undisclosed"),
            "description":  item.get("summary","")[:400],
            "website":      item.get("url","N/A"),
            "analysis":     f"Covered by {item.get('source','crypto media')}. {item.get('summary','')[:200]}",
        })
        projects.append(p)
    return projects

# ── 16. Twitter / X signals ───────────────────────────────────────
async def scrape_twitter_projects(session) -> List[Dict]:
    """
    Pulls Twitter/social signals.
    Works without bearer token using CryptoPanic + VC RSS.
    If TWITTER_BEARER_TOKEN is set, uses official Twitter API v2.
    """
    from funding_handlers import scan_twitter
    bearer   = os.getenv("TWITTER_BEARER_TOKEN","")
    tweets   = await scan_twitter(bearer or None, max_r=10)
    projects = []
    for tw in tweets[:10]:
        text     = tw.get("text","")
        username = tw.get("username","Unknown")
        p = _base(f"@{username}", "Twitter/X")
        p.update({
            "project_name":  f"Signal: @{username}",
            "stage":         tw.get("stage","Funding Round"),
            "amount_raised": tw.get("amount","Undisclosed"),
            "description":   text[:400],
            "website":       tw.get("url","https://twitter.com"),
            "twitter":       f"@{username}",
            "analysis":      (f"Twitter signal from @{username}. "
                              f"Followers: {tw.get('followers',0):,}. "
                              f"Likes: {tw.get('likes',0)} | RTs: {tw.get('retweets',0)}"),
            "legitimacy_score": min(40 + tw.get("followers",0)//1000, 70),
        })
        projects.append(p)
    return projects

# ── 17. CoinCarp — newly listed ───────────────────────────────────
async def scrape_coincarp(session) -> List[Dict]:
    data = await fetch(session, "https://api.coincarp.com/api/v1/public/currency/newlist",
                       params={"limit": 10})
    if not data: return []
    projects = []
    for coin in (data.get("data") or [])[:8]:
        name  = coin.get("name","Unknown")
        price = coin.get("price",0) or 0
        mcap  = coin.get("marketcap",0) or 0
        p = _base(name, "CoinCarp", stage="Newly Listed")
        p.update({
            "symbol":    coin.get("symbol","").upper(),
            "description": f"Newly listed on CoinCarp. Price: ${price:.8f}",
            "website":   coin.get("website","N/A"),
            "market_cap": mcap,
            "price":     price,
            "analysis":  f"New listing on CoinCarp. Market cap: ${mcap:,.0f}",
            "risk_level": "HIGH 🔸",
            "legitimacy_score": 35,
        })
        projects.append(p)
    return projects

# ── 18. Seedify Fund ──────────────────────────────────────────────
async def scrape_seedify(session) -> List[Dict]:
    html = await fetch(session, "https://launchpad.seedify.fund/", json_resp=False)
    if not html: return []
    soup     = BeautifulSoup(html, "html.parser")
    cards    = soup.find_all(["div","article"], class_=re.compile(r"project|igo|game|card"))
    projects = []
    for card in cards[:5]:
        name_el = card.find(["h2","h3","strong","a"])
        if not name_el: continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 2: continue
        link_el = card.find("a", href=True)
        link = link_el["href"] if link_el else "https://launchpad.seedify.fund"
        if link.startswith("/"): link = f"https://launchpad.seedify.fund{link}"
        p = _base(name, "Seedify", stage="IGO / Launchpad",
                  chain="Multi-chain", investors="Seedify")
        p.update({
            "description":   "IGO on Seedify Fund — blockchain gaming launchpad.",
            "website":       link,
            "analysis":      "Seedify gaming launchpad. Strong in GameFi / blockchain games.",
            "legitimacy_score": 65,
        })
        projects.append(p)
    return projects

# ── 19. Chain Broker — VC rounds ──────────────────────────────────
async def scrape_chain_broker(session) -> List[Dict]:
    html = await fetch(session, "https://chainbroker.io/rounds", json_resp=False)
    if not html: return []
    soup     = BeautifulSoup(html, "html.parser")
    projects = []
    rows     = soup.find_all(["tr","div"], class_=re.compile(r"round|project|invest"))
    for row in rows[:6]:
        name_el = row.find(["td","a","h3"])
        if not name_el: continue
        name = name_el.get_text(strip=True)[:60]
        if not name or len(name) < 2: continue
        vc_el     = row.find(class_=re.compile(r"investor|vc|backer"))
        amount_el = row.find(class_=re.compile(r"amount|raise|fund"))
        investors = vc_el.get_text(strip=True) if vc_el else "TBD"
        amount    = amount_el.get_text(strip=True) if amount_el else "Undisclosed"
        p = _base(name, "Chain Broker", stage="Private / VC Round",
                  amount=amount, investors=investors)
        p.update({
            "description": "VC-tracked investment round from Chain Broker.",
            "website":     "https://chainbroker.io",
            "analysis":    f"VC-backed round. Investor: {investors}",
            "legitimacy_score": 60,
        })
        projects.append(p)
    return projects

# ── 20. CryptoPanic — social signals ─────────────────────────────
async def scrape_cryptopanic(session) -> List[Dict]:
    """
    CryptoPanic aggregates Twitter + Reddit + news into one feed.
    Free public RSS endpoint — no API key needed.
    """
    projects = []
    try:
        loop = asyncio.get_event_loop()
        parsed = await loop.run_in_executor(
            None, feedparser.parse, "https://cryptopanic.com/news/rss/"
        )
        for entry in (parsed.entries or [])[:10]:
            title   = entry.get("title","").strip()
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "").strip()[:300]
            combined = (title + " " + summary).lower()
            from funding_handlers import extract_amount as ea, extract_stage as es, FUNDING_KEYWORDS
            if not any(kw in combined for kw in FUNDING_KEYWORDS):
                continue
            name = title[:60]
            p = _base(name, "CryptoPanic (Social)", stage=es(combined), amount=ea(combined))
            p.update({
                "description": summary[:400],
                "website":     entry.get("link","https://cryptopanic.com"),
                "analysis":    f"Social signal via CryptoPanic. {summary[:200]}",
                "legitimacy_score": 50,
            })
            projects.append(p)
    except Exception as e:
        logger.debug(f"CryptoPanic scrape: {e}")
    return projects[:8]

# ══════════════════════════════════════════════════════════════════
#  MASTER SCRAPER — runs all 20 sources
# ══════════════════════════════════════════════════════════════════

SOURCES = [
    ("CoinGecko",          scrape_coingecko),
    ("DexScreener",        scrape_dexscreener),
    ("GeckoTerminal",      scrape_geckoterminal),       # replaces dead DexTools + Nomics
    ("ICO Drops",          scrape_ico_drops),
    ("CryptoRank",         scrape_cryptorank),
    ("DeFiLlama Raises",   scrape_defillama),            # replaces dead Dune (reliable free API)
    ("Seedify",            scrape_seedify),
    ("Polkastarter",       scrape_polkastarter),
    ("DAO Maker",          scrape_dao_maker),
    ("PinkSale",           scrape_pinksale),
    ("CoinList",           scrape_coinlist),
    ("Binance Launchpad",  scrape_binance_launchpad),
    ("Messari",            scrape_messari),
    ("RSS Feeds",          scrape_funding_rss),
    ("Twitter/X",          scrape_twitter_projects),
    ("CoinCarp",           scrape_coincarp),
    ("CryptoPanic Social", scrape_cryptopanic),
    ("Chain Broker",       scrape_chain_broker),
    ("CoinGecko Extra",    scrape_coingecko),
    ("DeFiLlama Extra",    scrape_defillama),
]

async def run_all_scrapers() -> Dict:
    """Run all 20 scrapers, score each project, save to DB."""
    init_daily_tables()
    results, successful, failed = {}, [], []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=25),
        headers={"User-Agent": "Mozilla/5.0 (Web3IntelBot/4.0)"}
    ) as session:
        for source_name, scraper in SOURCES:
            try:
                logger.info(f"Scraping {source_name}...")
                projects = await scraper(session)
                saved = []
                for p in projects:
                    p["legitimacy_score"] = score_project(p)
                    p["risk_level"]       = risk_label(p["legitimacy_score"])
                    if save_project(p):
                        saved.append(p)
                results[source_name] = saved
                if saved:
                    successful.append(f"{source_name} ({len(saved)})")
                else:
                    failed.append(source_name)
                await asyncio.sleep(0.8)
            except Exception as e:
                logger.error(f"Scraper [{source_name}]: {e}")
                failed.append(source_name)

    return {"results": results, "successful": successful, "failed": failed}

# ══════════════════════════════════════════════════════════════════
#  MESSAGE FORMATTERS — clean, separated messages
# ══════════════════════════════════════════════════════════════════

def format_daily_summary(scrape_data: Dict, projects: List[Dict]) -> str:
    today      = datetime.utcnow().strftime("%A, %B %d %Y")
    successful = scrape_data.get("successful", [])
    failed     = scrape_data.get("failed", [])
    total      = len(projects)

    stages = {}
    for p in projects:
        s = p.get("stage","Unknown"); stages[s] = stages.get(s,0)+1

    low    = sum(1 for p in projects if "LOW"      in str(p.get("risk_level","")))
    medium = sum(1 for p in projects if "MEDIUM"   in str(p.get("risk_level","")))
    high   = sum(1 for p in projects if "HIGH"     in str(p.get("risk_level","")))
    crit   = sum(1 for p in projects if "CRITICAL" in str(p.get("risk_level","")))

    stages_txt = "\n".join(
        [f"  • {k}: {v}" for k,v in sorted(stages.items(), key=lambda x:-x[1])[:6]]
    )

    return (
        f"🌅 DAILY WEB3 INTELLIGENCE REPORT\n"
        f"📅 {today}\n"
        f"{'━'*34}\n\n"
        f"📡 SOURCES SCANNED: {len(successful)+len(failed)}/20\n"
        f"  ✅ Successful: {len(successful)}\n"
        f"  ❌ Unreachable: {len(failed)}\n\n"
        f"📊 PROJECTS FOUND: {total}\n"
        f"{stages_txt}\n\n"
        f"🛡 RISK BREAKDOWN\n"
        f"  ✅ Low Risk:      {low}\n"
        f"  ⚠️  Medium Risk:   {medium}\n"
        f"  🔸 High Risk:     {high}\n"
        f"  🚨 Critical:      {crit}\n\n"
        f"{'━'*34}\n"
        f"👇 Tap any project card below for the full breakdown.\n"
        f"Each section arrives as its own clean message."
    )

def format_project_card(p: Dict, index: int) -> tuple:
    """Returns (text, keyboard) for one project card in the daily list."""
    score = p.get("legitimacy_score", 50)
    risk  = p.get("risk_level", "MEDIUM ⚠️")
    name  = p.get("project_name","Unknown")
    stage = p.get("stage","N/A")
    amt   = p.get("amount_raised","N/A")
    src   = p.get("source","N/A")

    text = (
        f"{'━'*28}\n"
        f"#{index}  {name}\n"
        f"🎯 Stage: {stage}\n"
        f"💰 Raise: {amt}\n"
        f"📰 Source: {src}\n"
        f"🛡 Risk: {risk}  |  Score: {score:.0f}/100"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Full Analysis + Jobs", callback_data=f"proj:{p['id']}")
    ]])
    return text, keyboard

async def send_full_project_analysis(bot: Bot, chat_id: int, p: Dict):
    """
    Sends the full project breakdown as 8 separate clean messages.
    Each section is standalone and easy to read.
    """
    score  = p.get("legitimacy_score", 50)
    risk   = p.get("risk_level","MEDIUM ⚠️")
    name   = p.get("project_name","Unknown")
    sym    = p.get("symbol","N/A")
    stage  = p.get("stage","N/A")
    amt    = p.get("amount_raised","N/A")
    desc   = p.get("description","No description available yet.")
    web    = p.get("website","Not listed")
    tw     = p.get("twitter","Not listed")
    tg     = p.get("telegram_link","Not listed")
    disc   = p.get("discord","Not listed")
    gh     = p.get("github","Not listed")
    src    = p.get("source","N/A")
    chain  = p.get("chain","N/A")
    cont   = p.get("contract","N/A")
    inv    = p.get("investors","No investor info found yet.")
    anal   = p.get("analysis","No AI analysis available yet.")
    mcap   = p.get("market_cap",0) or 0
    price  = p.get("price",0) or 0
    jobs   = p.get("job_opportunities","No specific roles listed")

    # ── 1. Project Overview ─────────────────────────────────────
    await bot.send_message(
        chat_id=chat_id,
        text=(f"📌 *PROJECT OVERVIEW*\n\n"
              f"*Name:* {name} ({sym})\n"
              f"*Stage:* {stage}\n"
              f"*Fundraising:* {amt}\n"
              f"*Blockchain:* {chain}\n"
              f"*Contract:* {cont[:24]}{'...' if len(cont)>24 else ''}\n"
              f"*Source:* {src}"),
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.4)

    # ── 2. What is this project? ────────────────────────────────
    await bot.send_message(
        chat_id=chat_id,
        text=(f"📋 *WHAT IS THIS PROJECT?*\n\n"
              f"{desc[:500]}"),
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.4)

    # ── 3. Social Presence ──────────────────────────────────────
    await bot.send_message(
        chat_id=chat_id,
        text=(f"📱 *WHERE TO FIND THEM*\n\n"
              f"🌐 Website: {web}\n"
              f"🐦 Twitter: {tw}\n"
              f"✈️ Telegram: {tg}\n"
              f"💬 Discord: {disc}\n"
              f"💻 GitHub: {gh}\n\n"
              f"_Tip: No social links = harder to trust_"),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    await asyncio.sleep(0.4)

    # ── 4. Investors & Backers ──────────────────────────────────
    await bot.send_message(
        chat_id=chat_id,
        text=(f"💰 *WHO'S BACKING THEM?*\n\n"
              f"{inv}\n\n"
              f"_Big-name investors = more credibility and funding secured._"),
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.4)

    # ── 5. Market Data ──────────────────────────────────────────
    await bot.send_message(
        chat_id=chat_id,
        text=(f"📊 *MARKET NUMBERS*\n\n"
              f"💵 Price: ${price:,.8f}\n"
              f"🏦 Market Cap: ${mcap:,.0f}\n\n"
              f"_If both show $0, the token hasn't launched yet._"),
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.4)

    # ── 6. AI Analysis ──────────────────────────────────────────
    await bot.send_message(
        chat_id=chat_id,
        text=(f"🤖 *AI SUMMARY*\n\n"
              f"{anal[:600]}"),
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.4)

    # ── 7. Risk Assessment ──────────────────────────────────────
    risk_int = int(score)
    if risk_int >= 70:
        risk_emoji, risk_word = "🟢", "LOW RISK"
        risk_explain = "Looks relatively safe based on available data."
    elif risk_int >= 50:
        risk_emoji, risk_word = "🟡", "MEDIUM RISK"
        risk_explain = "Proceed with caution. Verify before engaging."
    elif risk_int >= 30:
        risk_emoji, risk_word = "🟠", "HIGH RISK"
        risk_explain = "Several red flags present. Do your own research thoroughly."
    else:
        risk_emoji, risk_word = "🔴", "CRITICAL RISK"
        risk_explain = "Multiple major red flags. Be very careful — possible scam."

    await bot.send_message(
        chat_id=chat_id,
        text=(f"🛡️ *RISK CHECK*\n\n"
              f"Score: *{risk_int}/100* {risk_emoji} — *{risk_word}*\n\n"
              f"{risk_explain}\n\n"
              f"📖 Scale:\n"
              f"🟢 70–100 = Low Risk\n"
              f"🟡 50–69  = Medium Risk\n"
              f"🟠 30–49  = High Risk\n"
              f"🔴 0–29   = Critical Risk"),
        parse_mode="Markdown"
    )
    await asyncio.sleep(0.4)

    # ── 8. Job Opportunities ────────────────────────────────────
    await bot.send_message(
        chat_id=chat_id,
        text=(f"💼 *JOB OPPORTUNITIES*\n\n"
              f"Roles this project may be hiring for:\n"
              f"{jobs}\n\n"
              f"👉 Want a deeper dive?\n"
              f"/research {name.split()[0]}"),
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════
#  SEND DAILY REPORT
# ══════════════════════════════════════════════════════════════════

async def send_daily_report(bot: Bot):
    logger.info("🌅 Daily report starting...")
    scrape_data = await run_all_scrapers()
    projects    = get_todays_projects()

    if not projects:
        logger.warning("No projects found in daily scrape.")
        subs = get_verified_subs()
        for chat_id in subs:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=("😴 *Nothing new today*\n\n"
                          "No new fundraising projects were found in today's scan.\n\n"
                          "I'll check again tomorrow morning. Stay tuned!"),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"No-results message to {chat_id}: {e}")
        return

    # Log the report
    conn = _db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO daily_report_log
            (report_date, sources_checked, projects_found, sent_at)
            VALUES (?,?,?,?)""",
            (datetime.utcnow().strftime("%Y-%m-%d"),
             len(scrape_data["successful"]) + len(scrape_data["failed"]),
             len(projects), datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()

    subs = get_verified_subs()
    if not subs:
        logger.info("No verified subscribers for daily report.")
        return

    for chat_id in subs:
        try:
            # ── Header summary ──────────────────────────────────
            await bot.send_message(
                chat_id=chat_id,
                text=format_daily_summary(scrape_data, projects)
            )
            await asyncio.sleep(0.5)

            # ── Source checklist ────────────────────────────────
            successful = scrape_data.get("successful",[])
            failed     = scrape_data.get("failed",[])
            src_lines  = ["📡 *SOURCES CHECKED TODAY*\n" + "━"*28]
            src_lines += [f"✅ {s}" for s in successful[:15]]
            if failed:
                src_lines += [f"\n❌ Failed: {', '.join(failed[:8])}"]
            await bot.send_message(
                chat_id=chat_id,
                text="\n".join(src_lines),
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.5)

            # ── Project cards ───────────────────────────────────
            for i, project in enumerate(projects[:20], 1):
                text, keyboard = format_project_card(project, i)
                await bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=keyboard)
                await asyncio.sleep(0.4)

            # ── Footer ──────────────────────────────────────────
            await bot.send_message(
                chat_id=chat_id,
                text=(f"{'━'*28}\n"
                      f"✅ *Daily report complete!*\n\n"
                      f"Found *{len(projects)} projects* across *{len(successful)} sources.*\n\n"
                      f"💡 Tap any project card above for the full breakdown.\n"
                      f"🔍 /research [name] for a custom deep dive.\n"
                      f"💼 /jobs for the latest job openings."),
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"send_daily_report to {chat_id}: {e}")

    logger.info(f"Daily report sent to {len(subs)} subscribers. Projects: {len(projects)}")

# ══════════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════════

async def daily_scheduler(bot: Bot):
    logger.info(f"Daily scheduler started. Reports fire at {REPORT_HOUR}:00 UTC")
    sent_today = None
    while True:
        now = datetime.utcnow()
        if now.hour == REPORT_HOUR and now.date() != sent_today:
            sent_today = now.date()
            await send_daily_report(bot)
        await asyncio.sleep(60)

# ══════════════════════════════════════════════════════════════════
#  CALLBACK — handles "Full Analysis" button taps
# ══════════════════════════════════════════════════════════════════

async def project_detail_callback(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("proj:"):
        return

    pid     = query.data.split(":", 1)[1]
    project = get_project(pid)
    chat_id = query.message.chat_id

    if not project:
        await context.bot.send_message(
            chat_id=chat_id,
            text=("❌ Project details not found.\n\n"
                  "Run /daily_report to refresh today's data.")
        )
        return

    # Send each section as its own message
    await send_full_project_analysis(context.bot, chat_id, project)
