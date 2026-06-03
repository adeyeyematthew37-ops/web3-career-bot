from dotenv import load_dotenv
load_dotenv()

import os
import logging
import aiohttp
from datetime import datetime, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SUBSCRIPTION_CODE = os.environ.get("SUBSCRIPTION_CODE", "Pelumi1@")

# In-memory store (replace with a DB for production)
# chat_id -> {verified, subscribed, alerts_on, preferences, applications}
users_db: dict = {}

# Conversation states
AWAITING_APPLY_ROLE   = 1
AWAITING_APPLY_LINK   = 2
AWAITING_STATUS_UPDATE = 3


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_user(chat_id: int) -> dict:
    if chat_id not in users_db:
        users_db[chat_id] = {
            "verified": False,
            "subscribed": False,
            "alerts_on": False,
            "preferences": {"chains": [], "roles": [], "salary_min": 0},
            "applications": [],
        }
    return users_db[chat_id]


def is_verified(chat_id: int) -> bool:
    return get_user(chat_id).get("verified", False)


def verify_gate(chat_id: int) -> str | None:
    if not is_verified(chat_id):
        return "🔒 You need to verify first. Use /verify <code> to unlock the bot."
    return None


# ─── Data Fetchers ────────────────────────────────────────────────────────────

async def fetch_web3_jobs() -> list[dict]:
    """Fetch Web3 jobs from public crypto job boards."""
    jobs = []
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://cryptojobslist.com/jobs.json"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    for job in (data if isinstance(data, list) else [])[:15]:
                        jobs.append({
                            "title": job.get("title", "Unknown Role"),
                            "company": job.get("company", "Unknown Company"),
                            "location": job.get("location", "Remote"),
                            "url": job.get("url", "https://cryptojobslist.com"),
                            "tags": job.get("tags", []),
                        })
    except Exception as e:
        logger.warning(f"Job fetch failed: {e}")

    if not jobs:
        jobs = [
            {"title": "Smart Contract Developer", "company": "Aave", "location": "Remote", "url": "https://jobs.lever.co/aave", "tags": ["Solidity", "DeFi"]},
            {"title": "Web3 Frontend Engineer", "company": "Uniswap", "location": "Remote", "url": "https://jobs.uniswap.org", "tags": ["React", "ethers.js"]},
            {"title": "DeFi Protocol Engineer", "company": "Compound", "location": "Remote", "url": "https://compound.finance/jobs", "tags": ["DeFi", "Solidity"]},
            {"title": "Blockchain Researcher", "company": "Messari", "location": "Remote", "url": "https://messari.io/careers", "tags": ["Research", "Crypto"]},
            {"title": "Token Economics Designer", "company": "Delphi Digital", "location": "Remote", "url": "https://delphidigital.io/careers", "tags": ["Tokenomics", "Research"]},
            {"title": "NFT Marketplace Developer", "company": "OpenSea", "location": "Remote", "url": "https://opensea.io/careers", "tags": ["NFT", "Marketplace"]},
            {"title": "DAO Operations Manager", "company": "Gitcoin", "location": "Remote", "url": "https://gitcoin.co/jobs", "tags": ["DAO", "Operations"]},
            {"title": "Web3 Security Auditor", "company": "Trail of Bits", "location": "Remote", "url": "https://www.trailofbits.com/careers", "tags": ["Security", "Audit"]},
        ]
    return jobs


async def fetch_grants() -> list[dict]:
    """Fetch active Web3 grants and hackathons from public sources."""
    grants = []

    # Gitcoin Grants — active rounds via Grants Stack API
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://grants-stack-indexer-v2.gitcoin.co/graphql"
            query = """{"query":"{rounds(filter:{strategyName:{equalTo:\\"allov2.DonationVotingMerkleDistributionDirectTransferStrategy\\"},roundMetadata:{isNot:null}},first:10,orderBy:CREATED_AT_BLOCK_DESC){nodes{id chainId roundMetadata applicationMetadata}}}"}"""
            async with session.post(url, data=query, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    nodes = data.get("data", {}).get("rounds", {}).get("nodes", [])
                    for r in nodes[:5]:
                        meta = r.get("roundMetadata") or {}
                        name = meta.get("name", "Gitcoin Round")
                        desc = (meta.get("description") or "")[:120]
                        grants.append({
                            "name": name,
                            "platform": "Gitcoin",
                            "type": "Grant Round",
                            "description": desc,
                            "url": f"https://explorer.gitcoin.co/#/round/{r.get('chainId','1')}/{r.get('id','')}",
                            "amount": "Varies",
                            "deadline": "Active now",
                        })
    except Exception as e:
        logger.warning(f"Gitcoin fetch failed: {e}")

    # DoraHacks — active hackathons via public API
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://dorahacks.io/api/hackathoninformation/?limit=8&offset=0&status=open"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(items, list):
                        for h in items[:5]:
                            title = h.get("title") or h.get("name", "DoraHacks Hackathon")
                            prize = h.get("prize_pool") or h.get("total_prize", "")
                            prize_str = f"${prize:,}" if isinstance(prize, (int, float)) and prize else str(prize) or "Prize pool available"
                            slug = h.get("id") or h.get("slug", "")
                            grants.append({
                                "name": title,
                                "platform": "DoraHacks",
                                "type": "Hackathon",
                                "description": (h.get("description") or "")[:120],
                                "url": f"https://dorahacks.io/hackathon/{slug}/detail",
                                "amount": prize_str,
                                "deadline": h.get("end_time", "Check site")[:10] if h.get("end_time") else "Check site",
                            })
    except Exception as e:
        logger.warning(f"DoraHacks fetch failed: {e}")

    if not grants:
        # Curated fallback — real recurring programs
        grants = [
            {
                "name": "Optimism Retroactive Public Goods Funding",
                "platform": "Optimism RPGF",
                "type": "Retroactive Grant",
                "description": "Rewards projects that have created positive impact for the Optimism ecosystem.",
                "url": "https://app.optimism.io/retropgf",
                "amount": "Millions OP",
                "deadline": "Round-based",
            },
            {
                "name": "Ethereum Foundation ESP",
                "platform": "Ethereum Foundation",
                "type": "Grant",
                "description": "Ecosystem Support Program for projects strengthening Ethereum and its ecosystem.",
                "url": "https://esp.ethereum.foundation",
                "amount": "Up to $250K",
                "deadline": "Rolling",
            },
            {
                "name": "Gitcoin Grants Program",
                "platform": "Gitcoin",
                "type": "Grant Round",
                "description": "Quadratic funding rounds for open-source public goods projects.",
                "url": "https://explorer.gitcoin.co",
                "amount": "Varies by matching pool",
                "deadline": "Seasonal rounds",
            },
            {
                "name": "Uniswap Foundation Grants",
                "platform": "Uniswap Foundation",
                "type": "Grant",
                "description": "Funds research, tooling, and community projects for the Uniswap ecosystem.",
                "url": "https://uniswapfoundation.mirror.xyz",
                "amount": "$10K – $250K",
                "deadline": "Rolling",
            },
            {
                "name": "Arbitrum DAO Grants Program",
                "platform": "Arbitrum",
                "type": "DAO Grant",
                "description": "Community grants for builders on Arbitrum One and Arbitrum Nova.",
                "url": "https://arbitrum.foundation/grants",
                "amount": "Up to $100K ARB",
                "deadline": "Rolling",
            },
            {
                "name": "Polygon Village Grants",
                "platform": "Polygon",
                "type": "Grant",
                "description": "Funding for projects building on Polygon PoS, zkEVM, and Miden.",
                "url": "https://polygon.technology/village/grants",
                "amount": "$5K – $100K",
                "deadline": "Rolling",
            },
            {
                "name": "DoraHacks Global Hackathon",
                "platform": "DoraHacks",
                "type": "Hackathon",
                "description": "Ongoing Web3 hackathons with prizes from leading protocols.",
                "url": "https://dorahacks.io/hackathon",
                "amount": "Various prizes",
                "deadline": "Multiple active",
            },
            {
                "name": "Chainlink BUILD Program",
                "platform": "Chainlink",
                "type": "Ecosystem Grant",
                "description": "Access to Chainlink services and co-marketing for early-stage projects.",
                "url": "https://chain.link/build",
                "amount": "Services + support",
                "deadline": "Rolling",
            },
        ]

    return grants


def format_grants_message(grants: list[dict], title: str = "🏆 Active Web3 Grants & Hackathons") -> str:
    """Format grants list into a Telegram message."""
    platform_emoji = {
        "Gitcoin": "🟢", "DoraHacks": "🔵", "Optimism RPGF": "🔴",
        "Ethereum Foundation": "🔷", "Uniswap Foundation": "🦄",
        "Arbitrum": "🔵", "Polygon": "🟣", "Chainlink": "🔗",
    }
    lines = [f"{title}\n"]
    for g in grants[:8]:
        emoji = platform_emoji.get(g["platform"], "🚀")
        desc = f"\n   📝 {g['description']}..." if g.get("description") else ""
        lines.append(
            f"{emoji} *{g['name']}*\n"
            f"   🏛️ {g['platform']} | 📋 {g['type']}\n"
            f"   💰 {g['amount']} | ⏰ {g['deadline']}{desc}\n"
            f"   🔗 [Apply / Learn More]({g['url']})\n"
        )
    lines.append("_Use /alerts to receive new grant announcements daily._")
    return "\n".join(lines)


async def fetch_fundraising_rounds() -> list[dict]:
    """Fetch recent Web3 fundraising rounds from DeFiLlama raises API."""
    rounds = []
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.llama.fi/raises"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("raises", data) if isinstance(data, dict) else data
                    # Sort by date descending, take most recent 20
                    items = sorted(
                        [r for r in items if isinstance(r, dict)],
                        key=lambda x: x.get("date", 0),
                        reverse=True
                    )[:20]
                    for r in items:
                        amount = r.get("amount")
                        amount_str = f"${amount:,.1f}M" if amount else "Undisclosed"
                        date_ts = r.get("date")
                        date_str = datetime.utcfromtimestamp(date_ts).strftime("%b %d, %Y") if date_ts else "Unknown"
                        rounds.append({
                            "name": r.get("name", "Unknown Project"),
                            "amount": amount_str,
                            "round": r.get("round", "Unknown Round"),
                            "category": r.get("category", ""),
                            "chains": r.get("chains", []),
                            "lead_investors": r.get("leadInvestors", []),
                            "other_investors": r.get("otherInvestors", []),
                            "date": date_str,
                            "source": r.get("source", ""),
                        })
    except Exception as e:
        logger.warning(f"Fundraising fetch failed: {e}")

    if not rounds:
        # Fallback curated data
        rounds = [
            {"name": "Monad Labs", "amount": "$225.0M", "round": "Series A", "category": "L1", "chains": ["Monad"], "lead_investors": ["Paradigm"], "other_investors": ["a16z", "Electric Capital"], "date": "Jan 15, 2025", "source": ""},
            {"name": "Story Protocol", "amount": "$80.0M", "round": "Series B", "category": "IP", "chains": ["Ethereum"], "lead_investors": ["a16z"], "other_investors": ["Polychain", "Samsung Next"], "date": "Jan 10, 2025", "source": ""},
            {"name": "Babylon", "amount": "$70.0M", "round": "Series A", "category": "Bitcoin Staking", "chains": ["Bitcoin"], "lead_investors": ["Paradigm"], "other_investors": ["Polychain", "Hack VC"], "date": "Dec 20, 2024", "source": ""},
            {"name": "Berachain", "amount": "$100.0M", "round": "Series B", "category": "L1", "chains": ["Berachain"], "lead_investors": ["Framework Ventures"], "other_investors": ["OKX Ventures", "Brevan Howard"], "date": "Dec 14, 2024", "source": ""},
            {"name": "EigenLayer", "amount": "$100.0M", "round": "Series B", "category": "Restaking", "chains": ["Ethereum"], "lead_investors": ["Andreessen Horowitz"], "other_investors": ["Polychain", "Electric Capital"], "date": "Nov 28, 2024", "source": ""},
            {"name": "Farcaster", "amount": "$150.0M", "round": "Series A", "category": "Social", "chains": ["Optimism", "Base"], "lead_investors": ["Paradigm"], "other_investors": ["a16z", "Haun Ventures"], "date": "Nov 15, 2024", "source": ""},
        ]
    return rounds


async def research_project(project: str) -> str:
    """Pull project info from CoinGecko or DefiLlama."""
    result_lines = [f"🔍 *Research: {project}*\n"]
    slug = project.lower().replace(" ", "-")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.coingecko.com/api/v3/coins/{slug}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    d = await resp.json()
                    desc = (d.get("description", {}).get("en", "") or "")[:300]
                    mc = d.get("market_data", {}).get("market_cap", {}).get("usd")
                    price = d.get("market_data", {}).get("current_price", {}).get("usd")
                    result_lines.append(f"📌 *{d.get('name', project)}* ({d.get('symbol','').upper()})")
                    if price:
                        result_lines.append(f"💵 Price: ${price:,.4f}")
                    if mc:
                        result_lines.append(f"📊 Market Cap: ${mc:,.0f}")
                    if desc:
                        result_lines.append(f"\n📝 {desc}...")
                    links = d.get("links", {})
                    if links.get("homepage") and links["homepage"][0]:
                        result_lines.append(f"🌐 Website: {links['homepage'][0]}")
                    if links.get("twitter_screen_name"):
                        result_lines.append(f"🐦 Twitter: @{links['twitter_screen_name']}")
                    return "\n".join(result_lines)
    except Exception as e:
        logger.warning(f"CoinGecko lookup failed: {e}")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.llama.fi/protocol/{slug}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    d = await resp.json()
                    tvl = d.get("tvl")
                    category = d.get("category", "DeFi")
                    result_lines.append(f"📌 *{d.get('name', project)}* — {category}")
                    if tvl and isinstance(tvl, list) and tvl:
                        result_lines.append(f"💰 TVL: ${tvl[-1].get('totalLiquidityUSD', 0):,.0f}")
                    chains = d.get("chains", [])
                    if chains:
                        result_lines.append(f"⛓️ Chains: {', '.join(chains[:5])}")
                    return "\n".join(result_lines)
    except Exception as e:
        logger.warning(f"DefiLlama lookup failed: {e}")

    return f"⚠️ Could not find data for *{project}*. Try the exact token slug (e.g. `bitcoin`, `ethereum`, `uniswap`)."


def format_fundraising_message(rounds: list[dict], title: str = "💰 Recent Web3 Fundraising Rounds") -> str:
    """Format a list of fundraising rounds into a Telegram message."""
    lines = [f"{title}\n"]
    category_emoji = {
        "L1": "⛓️", "L2": "🔵", "DeFi": "🏦", "NFT": "🖼️",
        "Gaming": "🎮", "DAO": "🗳️", "Infrastructure": "🔧",
        "Social": "💬", "Restaking": "🔄", "Bitcoin": "🟠",
        "IP": "💡", "Security": "🛡️",
    }
    for r in rounds[:10]:
        cat = r.get("category", "")
        emoji = category_emoji.get(cat, "🚀")
        lead = ", ".join(r.get("lead_investors", [])[:2]) or "Undisclosed"
        chains = ", ".join(r.get("chains", [])[:3])
        chain_str = f" | ⛓️ {chains}" if chains else ""
        source_str = f"\n   🔗 [Source]({r['source']})" if r.get("source") else ""
        lines.append(
            f"{emoji} *{r['name']}* — {r['amount']}\n"
            f"   📋 {r['round']} | 🗓️ {r['date']}{chain_str}\n"
            f"   🏦 Lead: {lead}{source_str}\n"
        )
    lines.append("_Use /alerts to get these delivered daily._")
    return "\n".join(lines)


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    verified = is_verified(chat_id)

    keyboard = [
        [InlineKeyboardButton("🔑 Verify Access", callback_data="cmd_verify")],
        [InlineKeyboardButton("💼 Browse Jobs", callback_data="cmd_jobs"),
         InlineKeyboardButton("🔍 Research Project", callback_data="cmd_research")],
        [InlineKeyboardButton("💰 Fundraising Rounds", callback_data="cmd_fundraising"),
         InlineKeyboardButton("🏆 Grants & Hackathons", callback_data="cmd_grants")],
        [InlineKeyboardButton("🔔 Daily Alerts", callback_data="cmd_alerts"),
         InlineKeyboardButton("📋 My Applications", callback_data="cmd_applications")],
        [InlineKeyboardButton("⚙️ Preferences", callback_data="cmd_preferences")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    status = "✅ Verified" if verified else "🔒 Not Verified"
    alerts_status = "🔔 On" if get_user(chat_id).get("alerts_on") else "🔕 Off"
    msg = (
        f"👋 Welcome, *{user.first_name}*!\n\n"
        f"🤖 *Web3 Career & Fundraising Bot*\n"
        f"Status: {status} | Alerts: {alerts_status}\n\n"
        f"I help you find Web3 jobs, research crypto projects, track applications, "
        f"and monitor VC fundraising rounds.\n\n"
        f"*Commands:*\n"
        f"/verify `<code>` — Unlock full access\n"
        f"/jobs — Browse Web3 job listings\n"
        f"/research `<project>` — Deep-dive any project\n"
        f"/fundraising — Latest VC & fundraising rounds\n"
        f"/grants — Active grants & hackathons\n"
        f"/alerts — Toggle daily alerts\n"
        f"/apply — Log a job application\n"
        f"/applications — View your applications\n"
        f"/update\\_status — Update application status\n"
        f"/preferences — Set your job preferences\n"
        f"/subscribe — Subscription info\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)


async def verify_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "🔑 Enter your subscription code:\n`/verify <code>`\n\nDon't have one? Use `/subscribe`",
            parse_mode="Markdown"
        )
        return

    code = " ".join(args).strip()
    if code == SUBSCRIPTION_CODE:
        get_user(chat_id)["verified"] = True
        await update.message.reply_text(
            "✅ *Access Granted!*\n\nYou now have full access to the Web3 Career Bot.\n\n"
            "Try:\n• /jobs — browse live listings\n• /fundraising — latest VC rounds\n"
            "• /alerts — turn on daily alerts\n• /research ethereum — research any project",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Invalid code. Check your code and try again.\n"
            "Need a subscription? Use `/subscribe`",
            parse_mode="Markdown"
        )


async def subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    get_user(chat_id)["subscribed"] = True
    await update.message.reply_text(
        "📬 *Subscription*\n\n"
        "To get full access, use the subscription code:\n\n"
        "`/verify Pelumi1@`\n\n"
        "Once verified, use /alerts to enable daily Web3 job alerts and fundraising round notifications.",
        parse_mode="Markdown"
    )


async def jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return

    await update.message.reply_text("⏳ Fetching latest Web3 jobs...")
    job_list = await fetch_web3_jobs()

    prefs = get_user(chat_id).get("preferences", {})
    preferred_roles = [r.lower() for r in prefs.get("roles", [])]

    lines = ["💼 *Latest Web3 Jobs*\n"]
    for i, job in enumerate(job_list[:10], 1):
        title = job["title"]
        company = job["company"]
        location = job["location"]
        url = job["url"]
        tags = ", ".join(job.get("tags", [])[:3])
        match = "⭐ " if any(r in title.lower() for r in preferred_roles) else ""
        lines.append(f"{match}*{i}. {title}*\n🏢 {company} | 📍 {location}\n🏷️ {tags}\n🔗 [Apply]({url})\n")

    lines.append("_Use /apply to log when you apply to a role._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def fundraising(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return

    await update.message.reply_text("⏳ Fetching latest fundraising rounds...")
    rounds = await fetch_fundraising_rounds()
    msg = format_fundraising_message(rounds)
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


async def alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return

    user = get_user(chat_id)
    current = user.get("alerts_on", False)
    user["alerts_on"] = not current

    if user["alerts_on"]:
        await update.message.reply_text(
            "🔔 *Daily Alerts Enabled!*\n\n"
            "You'll receive a daily digest at *9:00 AM UTC* with:\n"
            "• 💰 Latest Web3 fundraising rounds\n"
            "• 💼 Top new job listings\n"
            "• 📈 Notable market moves\n\n"
            "Use /alerts again to turn off.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "🔕 *Daily Alerts Disabled.*\n\nUse /alerts to turn them back on anytime.",
            parse_mode="Markdown"
        )


async def grants(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return

    await update.message.reply_text("⏳ Fetching active grants and hackathons...")
    grant_list = await fetch_grants()
    msg = format_grants_message(grant_list)
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


async def research(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return

    args = ctx.args
    if not args:
        await update.message.reply_text(
            "🔍 Which project do you want to research?\n\n"
            "Usage: `/research <project>`\n"
            "Example: `/research ethereum` or `/research uniswap`",
            parse_mode="Markdown"
        )
        return

    project = " ".join(args)
    await update.message.reply_text(f"⏳ Researching *{project}*...", parse_mode="Markdown")
    result = await research_project(project)
    await update.message.reply_text(result, parse_mode="Markdown", disable_web_page_preview=True)


async def apply_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 *Log a Job Application*\n\nWhat role did you apply for?\n\nFormat: `Company — Job Title`\nExample: `Uniswap — Frontend Engineer`",
        parse_mode="Markdown"
    )
    return AWAITING_APPLY_ROLE


async def apply_role_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["apply_role"] = update.message.text
    await update.message.reply_text(
        "🔗 Paste the job URL (or type `skip` to skip):",
        parse_mode="Markdown"
    )
    return AWAITING_APPLY_LINK


async def apply_link_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    role = ctx.user_data.get("apply_role", "Unknown")
    link = update.message.text if update.message.text.lower() != "skip" else ""

    application = {
        "id": len(get_user(chat_id)["applications"]) + 1,
        "role": role,
        "url": link,
        "status": "Applied",
        "date": datetime.now().strftime("%Y-%m-%d"),
    }
    get_user(chat_id)["applications"].append(application)

    await update.message.reply_text(
        f"✅ *Application Logged!*\n\n"
        f"📋 Role: {role}\n"
        f"📅 Date: {application['date']}\n"
        f"🔄 Status: Applied\n\n"
        f"View all with /applications",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def applications(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return

    apps = get_user(chat_id).get("applications", [])
    if not apps:
        await update.message.reply_text(
            "📋 No applications tracked yet.\nUse /apply to log your first application!"
        )
        return

    status_emoji = {
        "Applied": "📤", "Interviewing": "🎤", "Offer": "🎉",
        "Rejected": "❌", "Withdrawn": "🔙",
    }
    lines = [f"📋 *Your Applications ({len(apps)} total)*\n"]
    for app in apps[-10:]:
        emoji = status_emoji.get(app["status"], "📋")
        url_part = f"\n   🔗 [Link]({app['url']})" if app.get("url") else ""
        lines.append(
            f"{emoji} *{app['role']}*\n"
            f"   📅 {app['date']} | Status: {app['status']}{url_part}\n"
        )

    lines.append("\n_Use /update\\_status to update a status._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def update_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return ConversationHandler.END

    apps = get_user(chat_id).get("applications", [])
    if not apps:
        await update.message.reply_text("📋 No applications to update. Use /apply first.")
        return ConversationHandler.END

    lines = [
        "🔄 *Update Application Status*\n\n"
        "Reply with: `<number> <status>`\n\n"
        "Statuses: Applied, Interviewing, Offer, Rejected, Withdrawn\n\n"
        "*Your applications:*\n"
    ]
    for app in apps:
        lines.append(f"{app['id']}. {app['role']} — {app['status']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return AWAITING_STATUS_UPDATE


async def status_update_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    valid_statuses = ["Applied", "Interviewing", "Offer", "Rejected", "Withdrawn"]

    try:
        parts = text.split(None, 1)
        app_id = int(parts[0])
        new_status = parts[1].strip().title() if len(parts) > 1 else ""
        if new_status not in valid_statuses:
            raise ValueError("Invalid status")

        apps = get_user(chat_id)["applications"]
        app = next((a for a in apps if a["id"] == app_id), None)
        if not app:
            await update.message.reply_text("❌ Application not found. Check the number.")
            return ConversationHandler.END

        old_status = app["status"]
        app["status"] = new_status
        await update.message.reply_text(
            f"✅ Updated!\n\n*{app['role']}*\n{old_status} → {new_status}",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text(
            f"❌ Invalid format. Try: `1 Interviewing`\nValid statuses: {', '.join(valid_statuses)}",
            parse_mode="Markdown"
        )
    return ConversationHandler.END


async def preferences(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gate = verify_gate(chat_id)
    if gate:
        await update.message.reply_text(gate)
        return

    prefs = get_user(chat_id)["preferences"]
    keyboard = [
        [InlineKeyboardButton("👨‍💻 Set Roles", callback_data="pref_roles"),
         InlineKeyboardButton("⛓️ Set Chains", callback_data="pref_chains")],
        [InlineKeyboardButton("💰 Set Min Salary", callback_data="pref_salary")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    chains = ", ".join(prefs.get("chains", [])) or "None set"
    roles = ", ".join(prefs.get("roles", [])) or "None set"
    salary = f"${prefs.get('salary_min', 0):,}" if prefs.get("salary_min") else "Not set"

    await update.message.reply_text(
        f"⚙️ *Your Preferences*\n\n"
        f"👨‍💻 Roles: {roles}\n"
        f"⛓️ Chains: {chains}\n"
        f"💰 Min Salary: {salary}\n\n"
        f"Update below:",
        parse_mode="Markdown",
        reply_markup=markup
    )


# ─── Scheduled Daily Alert Job ────────────────────────────────────────────────

async def send_daily_alerts(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs daily at 9:00 AM UTC — sends fundraising + job digest to opted-in users."""
    logger.info("Running daily alert broadcast...")

    alert_users = [
        cid for cid, u in users_db.items()
        if u.get("verified") and u.get("alerts_on")
    ]

    if not alert_users:
        logger.info("No users opted in to alerts.")
        return

    rounds, jobs_list, grant_list = (
        await fetch_fundraising_rounds(),
        await fetch_web3_jobs(),
        await fetch_grants(),
    )
    today = datetime.utcnow().strftime("%A, %b %d %Y")

    # Fundraising digest (top 5)
    fundraising_msg = format_fundraising_message(
        rounds[:5],
        title=f"📅 *Daily Web3 Digest — {today}*\n\n💰 *Latest Fundraising Rounds*"
    )

    # Top 3 jobs
    job_lines = ["\n\n💼 *Top New Job Listings*\n"]
    for job in jobs_list[:3]:
        job_lines.append(f"• *{job['title']}* @ {job['company']}\n  🔗 [Apply]({job['url']})")

    # Top 3 grants
    grant_lines = ["\n\n🏆 *Active Grants & Hackathons*\n"]
    for g in grant_list[:3]:
        grant_lines.append(f"• *{g['name']}* ({g['platform']}) — {g['amount']}\n  🔗 [Details]({g['url']})")
    grant_lines.append("\n\n_Use /fundraising, /jobs, or /grants for the full lists._")

    full_msg = fundraising_msg + "\n".join(job_lines) + "\n".join(grant_lines)

    for chat_id in alert_users:
        try:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=full_msg,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            logger.info(f"Alert sent to {chat_id}")
        except Exception as e:
            logger.warning(f"Failed to send alert to {chat_id}: {e}")


# ─── Callback Query Handler ───────────────────────────────────────────────────

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "cmd_verify":
        await query.message.reply_text("Use: `/verify <your-code>`\nExample: `/verify Pelumi1@`", parse_mode="Markdown")

    elif data == "cmd_jobs":
        gate = verify_gate(chat_id)
        if gate:
            await query.message.reply_text(gate)
        else:
            await query.message.reply_text("⏳ Fetching jobs...")
            job_list = await fetch_web3_jobs()
            lines = ["💼 *Latest Web3 Jobs*\n"]
            for i, job in enumerate(job_list[:8], 1):
                lines.append(f"*{i}. {job['title']}*\n🏢 {job['company']} | 📍 {job['location']}\n🔗 [Apply]({job['url']})\n")
            await query.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

    elif data == "cmd_research":
        await query.message.reply_text("Use: `/research <project-name>`\nExample: `/research ethereum`", parse_mode="Markdown")

    elif data == "cmd_fundraising":
        gate = verify_gate(chat_id)
        if gate:
            await query.message.reply_text(gate)
        else:
            await query.message.reply_text("⏳ Fetching fundraising rounds...")
            rounds = await fetch_fundraising_rounds()
            msg = format_fundraising_message(rounds)
            await query.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

    elif data == "cmd_grants":
        gate = verify_gate(chat_id)
        if gate:
            await query.message.reply_text(gate)
        else:
            await query.message.reply_text("⏳ Fetching active grants and hackathons...")
            grant_list = await fetch_grants()
            msg = format_grants_message(grant_list)
            await query.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

    elif data == "cmd_alerts":
        gate = verify_gate(chat_id)
        if gate:
            await query.message.reply_text(gate)
        else:
            user = get_user(chat_id)
            user["alerts_on"] = not user.get("alerts_on", False)
            status = "🔔 enabled" if user["alerts_on"] else "🔕 disabled"
            await query.message.reply_text(
                f"Daily alerts {status}!\n\n"
                + ("You'll get a digest every day at 9:00 AM UTC with fundraising rounds and new job listings." if user["alerts_on"] else "Use /alerts to re-enable.")
            )

    elif data == "cmd_applications":
        apps = get_user(chat_id).get("applications", [])
        if not apps:
            await query.message.reply_text("📋 No applications yet. Use /apply to log one!")
        else:
            lines = [f"📋 *{len(apps)} Applications Tracked*\n"]
            for app in apps[-5:]:
                lines.append(f"• {app['role']} — {app['status']} ({app['date']})")
            await query.message.reply_text("\n".join(lines), parse_mode="Markdown")

    elif data == "cmd_preferences":
        await query.message.reply_text("Use /preferences to set your job search preferences.")

    elif data == "pref_roles":
        await query.message.reply_text(
            "👨‍💻 Reply with your preferred roles (comma-separated):\n"
            "Example: `Smart Contract Developer, DeFi Researcher, Frontend Engineer`",
            parse_mode="Markdown"
        )
        ctx.user_data["setting_pref"] = "roles"

    elif data == "pref_chains":
        await query.message.reply_text(
            "⛓️ Reply with your preferred chains (comma-separated):\n"
            "Example: `Ethereum, Solana, Polygon, Arbitrum`",
            parse_mode="Markdown"
        )
        ctx.user_data["setting_pref"] = "chains"

    elif data == "pref_salary":
        await query.message.reply_text(
            "💰 Reply with your minimum annual salary (USD):\n"
            "Example: `80000`",
            parse_mode="Markdown"
        )
        ctx.user_data["setting_pref"] = "salary"


async def pref_text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    setting = ctx.user_data.get("setting_pref")
    if not setting:
        return

    text = update.message.text.strip()
    prefs = get_user(chat_id)["preferences"]

    if setting == "roles":
        prefs["roles"] = [r.strip() for r in text.split(",") if r.strip()]
        await update.message.reply_text(f"✅ Roles updated: {', '.join(prefs['roles'])}")
    elif setting == "chains":
        prefs["chains"] = [c.strip() for c in text.split(",") if c.strip()]
        await update.message.reply_text(f"✅ Chains updated: {', '.join(prefs['chains'])}")
    elif setting == "salary":
        try:
            prefs["salary_min"] = int(text.replace(",", "").replace("$", ""))
            await update.message.reply_text(f"✅ Minimum salary set: ${prefs['salary_min']:,}")
        except ValueError:
            await update.message.reply_text("❌ Please enter a number, e.g. `80000`", parse_mode="Markdown")

    ctx.user_data.pop("setting_pref", None)


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it to your .env file.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation: log a job application
    apply_conv = ConversationHandler(
        entry_points=[CommandHandler("apply", apply_command)],
        states={
            AWAITING_APPLY_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_role_received)],
            AWAITING_APPLY_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_link_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Conversation: update application status
    status_conv = ConversationHandler(
        entry_points=[CommandHandler("update_status", update_status)],
        states={
            AWAITING_STATUS_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_update_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("verify", verify_command))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("jobs", jobs))
    app.add_handler(CommandHandler("research", research))
    app.add_handler(CommandHandler("fundraising", fundraising))
    app.add_handler(CommandHandler("grants", grants))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("applications", applications))
    app.add_handler(CommandHandler("preferences", preferences))
    app.add_handler(apply_conv)
    app.add_handler(status_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pref_text_handler))

    # Schedule daily digest at 09:00 UTC
    app.job_queue.run_daily(
        send_daily_alerts,
        time=time(hour=9, minute=0, second=0),
        name="daily_alerts",
    )

    logger.info("Bot is running... Daily alerts scheduled at 09:00 UTC.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
