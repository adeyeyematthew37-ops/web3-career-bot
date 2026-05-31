from dotenv import load_dotenv
load_dotenv()

import os
import logging
import json
import aiohttp
from datetime import datetime
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

BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SUBSCRIPTION_CODE = os.environ.get("SUBSCRIPTION_CODE", "Pelumi1@")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")

# Simple in-memory store (replace with DB for production)
users_db: dict = {}       # chat_id -> {verified, subscribed, preferences, applications}

# Conversation states
AWAITING_CODE = 1
AWAITING_PROJECT = 2
AWAITING_APPLY_ROLE = 3
AWAITING_APPLY_LINK = 4
AWAITING_STATUS_UPDATE = 5
AWAITING_PREF_CHOICE = 6


# ─── Helpers ────────────────────────────────────────────────────────────────

def get_user(chat_id: int) -> dict:
    if chat_id not in users_db:
        users_db[chat_id] = {
            "verified": False,
            "subscribed": False,
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


async def fetch_web3_jobs() -> list[dict]:
    """Fetch Web3 jobs from public crypto job boards."""
    jobs = []
    try:
        async with aiohttp.ClientSession() as session:
            # Crypto Jobs List RSS (free, no auth)
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
        # Fallback static sample
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


async def research_project(project: str) -> str:
    """Pull project info from CoinGecko or DefiLlama."""
    result_lines = [f"🔍 *Research: {project}*\n"]
    slug = project.lower().replace(" ", "-")

    try:
        async with aiohttp.ClientSession() as session:
            # Try CoinGecko
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
            # Try DefiLlama
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


# ─── Command Handlers ───────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    verified = is_verified(chat_id)

    keyboard = [
        [InlineKeyboardButton("🔑 Verify Access", callback_data="cmd_verify")],
        [InlineKeyboardButton("💼 Browse Jobs", callback_data="cmd_jobs"),
         InlineKeyboardButton("🔍 Research Project", callback_data="cmd_research")],
        [InlineKeyboardButton("📋 My Applications", callback_data="cmd_applications"),
         InlineKeyboardButton("⚙️ Preferences", callback_data="cmd_preferences")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    status = "✅ Verified" if verified else "🔒 Not Verified"
    msg = (
        f"👋 Welcome, *{user.first_name}*!\n\n"
        f"🤖 *Web3 Career & Fundraising Bot*\n"
        f"Status: {status}\n\n"
        f"I help you find Web3 jobs, research crypto projects, track applications, and monitor fundraising rounds.\n\n"
        f"*Commands:*\n"
        f"/verify `<code>` — Unlock full access\n"
        f"/jobs — Browse Web3 job listings\n"
        f"/research `<project>` — Deep-dive any project\n"
        f"/apply — Log a job application\n"
        f"/applications — View your applications\n"
        f"/update\\_status — Update application status\n"
        f"/preferences — Set your job preferences\n"
        f"/subscribe — Subscribe to daily alerts\n"
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
            "Use /jobs to browse live listings or /research to analyse any project.",
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
        "You'll receive daily Web3 job alerts and fundraising news once verified.",
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

    # Apply user preferences filter
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
        return

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
        return

    apps = get_user(chat_id).get("applications", [])
    if not apps:
        await update.message.reply_text("📋 No applications to update. Use /apply first.")
        return

    lines = ["🔄 *Update Application Status*\n\nReply with:\n`<application number> <new status>`\n\nStatuses: Applied, Interviewing, Offer, Rejected, Withdrawn\n\n*Your applications:*\n"]
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


# ─── Callback Query Handler ─────────────────────────────────────────────────

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "cmd_verify":
        await query.message.reply_text("Use: `/verify <your-code>`", parse_mode="Markdown")
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
            "👨‍💻 Reply to this with your preferred roles (comma-separated):\n"
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
            await update.message.reply_text("❌ Please enter a number, e.g. `80000`")

    ctx.user_data.pop("setting_pref", None)


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it to your .env file.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Apply conversation handler
    apply_conv = ConversationHandler(
        entry_points=[CommandHandler("apply", apply_command)],
        states={
            AWAITING_APPLY_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_role_received)],
            AWAITING_APPLY_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_link_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Update status conversation handler
    status_conv = ConversationHandler(
        entry_points=[CommandHandler("update_status", update_status)],
        states={
            AWAITING_STATUS_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_update_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("verify", verify_command))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("jobs", jobs))
    app.add_handler(CommandHandler("research", research))
    app.add_handler(CommandHandler("applications", applications))
    app.add_handler(CommandHandler("preferences", preferences))
    app.add_handler(apply_conv)
    app.add_handler(status_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pref_text_handler))

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
