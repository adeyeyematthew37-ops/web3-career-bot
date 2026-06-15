import asyncio, aiohttp, json, logging, os, re, sqlite3, hashlib, time
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv
from funding_handlers import (funding_stats_handler, twitter_scan_handler,
                              status_handler, scout_communities_handler)
from daily_report import (daily_scheduler, send_daily_report,
                           project_detail_callback, init_daily_tables)

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SUB_CODE = "Pelumi1@"
DB_PATH  = os.getenv("DB_PATH","enhanced_fundraising_alerts.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS subscribers(
        chat_id INTEGER PRIMARY KEY, subscribed_at TEXT,
        subscription_verified BOOLEAN DEFAULT FALSE, verification_code TEXT,
        stages TEXT DEFAULT "[]", funding_amounts TEXT DEFAULT "[]",
        include_startups BOOLEAN DEFAULT TRUE,
        include_small_community BOOLEAN DEFAULT TRUE,
        include_newly_launched BOOLEAN DEFAULT TRUE,
        min_followers INTEGER DEFAULT 0, max_followers INTEGER DEFAULT 100000,
        telegram_communities BOOLEAN DEFAULT TRUE,
        community_size_filters TEXT DEFAULT "[]")""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_job_applications(
        id TEXT PRIMARY KEY, chat_id INTEGER, project_name TEXT,
        job_title TEXT, platform TEXT, application_date TEXT,
        status TEXT, job_url TEXT, salary_range TEXT, notes TEXT,
        follow_up_date TEXT, response_date TEXT, interview_date TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, unique_id TEXT UNIQUE,
        project_name TEXT, stage TEXT, amount TEXT, description TEXT,
        source_url TEXT, timestamp TEXT, sent BOOLEAN DEFAULT FALSE)""")
    conn.commit(); conn.close()
    init_daily_tables()

def is_verified(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT subscription_verified FROM subscribers WHERE chat_id=?",(chat_id,))
    row  = c.fetchone(); conn.close()
    return bool(row and row[0])

def verify_user(chat_id, code):
    if code.strip() != SUB_CODE: return False
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO subscribers
        (chat_id,subscribed_at,subscription_verified,verification_code)
        VALUES(?,?,TRUE,?)""",(chat_id,datetime.now().isoformat(),code))
    conn.commit(); conn.close(); return True

def get_verified_subs():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT chat_id FROM subscribers WHERE subscription_verified=TRUE")
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

def add_application(chat_id,app_id,project,title,platform,url,salary="TBD"):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    try:
        c.execute("""INSERT INTO user_job_applications
            (id,chat_id,project_name,job_title,platform,application_date,status,job_url,salary_range)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (app_id,chat_id,project,title,platform,datetime.now().isoformat(),"pending",url,salary))
        conn.commit(); return True
    except sqlite3.IntegrityError: return False
    finally: conn.close()

def get_applications(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT * FROM user_job_applications WHERE chat_id=? ORDER BY application_date DESC LIMIT 20",(chat_id,))
    rows = c.fetchall(); conn.close(); return rows

def update_app_status(chat_id,app_id,status,notes=""):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("UPDATE user_job_applications SET status=?,notes=? WHERE id=? AND chat_id=?",
              (status,notes,app_id,chat_id))
    ok = c.rowcount>0; conn.commit(); conn.close(); return ok

async def guard(update, context):
    if not is_verified(update.effective_chat.id):
        await update.message.reply_text(
            "🔐 Access restricted.\n\nUse /verify [code] to unlock.\n"
            "Contact admin for your subscription code.")
        return False
    return True

# ── /start ────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    verified = is_verified(chat_id)
    if not verified:
        await update.message.reply_text(
            "🚀 Web3 Career & Intelligence Bot\n\n"
            "🔐 Premium access required.\n\n"
            "Step 1: Get your code from the admin\n"
            "Step 2: /verify [your_code]\n"
            "Step 3: Full access unlocked!\n\n"
            "📞 @PelumiAdmin for access")
    else:
        await update.message.reply_text(
            "🚀 Web3 Career Bot — FULL ACCESS ✅\n\n"
            "📅 DAILY REPORTS (auto every morning)\n"
            "  /daily_report — run report right now\n"
            "  /set_report_time — change report time\n\n"
            "📊 LIVE SCANS\n"
            "  /funding_stats — live funding news\n"
            "  /twitter_scan  — Twitter signals\n"
            "  /scout_communities — Telegram scout\n\n"
            "🔍 RESEARCH\n"
            "  /research [project] — deep analysis\n\n"
            "💼 JOBS & APPLICATIONS\n"
            "  /jobs — latest listings\n"
            "  /apply /applications /update_status\n\n"
            "⚙️ SETTINGS\n"
            "  /preferences /status /weekly_report")

# ── /verify ───────────────────────────────────────────────────────
async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /verify [code]"); return
    if verify_user(update.effective_chat.id, " ".join(context.args)):
        await update.message.reply_text(
            "✅ ACCESS GRANTED! Welcome 🚀\n\n"
            "Your daily morning intelligence report starts tomorrow.\n"
            "Or run /daily_report right now for today's data!")
    else:
        await update.message.reply_text("❌ Invalid code. Contact @PelumiAdmin")

# ── /daily_report — manual trigger ───────────────────────────────
async def daily_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    msg = await update.message.reply_text(
        "🌅 Starting full intelligence scan...\n\n"
        "📡 Scanning all 20 sources:\n"
        "CoinGecko · DexScreener · DexTools\n"
        "ICO Drops · CryptoRank · Seedify\n"
        "Polkastarter · DAO Maker · PinkSale\n"
        "CoinList · Binance Launchpad · Messari\n"
        "RSS Feeds · Twitter/X · CoinCarp\n"
        "Dune · Nomics · Chain Broker\n\n"
        "⏳ This takes 60-90 seconds. Stand by...")
    try:
        await send_daily_report(context.bot)
        # Delete loading message since report was sent
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id)
    except Exception as e:
        logger.error(f"daily_report_cmd: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Report failed: {e}\nTry /funding_stats for a quick scan instead.")

# ── /set_report_time ──────────────────────────────────────────────
async def set_report_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⏰ Set your daily report hour (UTC)\n\n"
            "Usage: /set_report_time [hour]\n"
            "Example: /set_report_time 8\n  (sends at 8:00 AM UTC)\n\n"
            f"Current setting: {os.getenv('DAILY_REPORT_HOUR','8')}:00 UTC\n\n"
            "To change globally, update DAILY_REPORT_HOUR in Railway → Variables")
        return
    hour = int(context.args[0])
    if not 0 <= hour <= 23:
        await update.message.reply_text("❌ Hour must be 0-23"); return
    await update.message.reply_text(
        f"✅ Report time noted: {hour:02d}:00 UTC\n\n"
        f"⚠️ To make it permanent, set DAILY_REPORT_HOUR={hour} in Railway Variables tab.")

# ── /subscribe ────────────────────────────────────────────────────
async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    await update.message.reply_text(
        "🔔 You are subscribed to daily reports!\n\n"
        "Every morning you will receive:\n"
        "• Full scan of all 20 sources\n"
        "• Every new project found\n"
        "• Risk analysis for each\n"
        "• Job opportunities per project\n"
        "• Tap-for-detail buttons\n\n"
        "Run /daily_report now to see today's results!")

# ── /preferences ─────────────────────────────────────────────────
async def preferences_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    keyboard = [
        [InlineKeyboardButton("🎯 Project Stages",     callback_data="pref_stages")],
        [InlineKeyboardButton("💰 Funding Amounts",    callback_data="pref_amounts")],
        [InlineKeyboardButton("👥 Community Settings", callback_data="pref_community")],
        [InlineKeyboardButton("📊 View Current",       callback_data="pref_view")],
    ]
    await update.message.reply_text(
        "⚙️ PREFERENCES\nChoose what to configure:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data.startswith("proj:"):
        await project_detail_callback(update, context); return
    await q.answer()
    if q.data == "pref_view":
        await q.edit_message_text("📊 Use /status for full settings overview.")
    else:
        await q.edit_message_text(f"⚙️ Use /preferences to reconfigure settings.\n{q.data}")

# ── /research ─────────────────────────────────────────────────────
async def research_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /research [project name]"); return
    project = " ".join(context.args)
    msg     = await update.message.reply_text(
        f"🔍 Researching {project}...\n⏳ 30-60 seconds...")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent":"Mozilla/5.0"}
        ) as s:
            async with s.get(
                f"https://api.coingecko.com/api/v3/search?query={project}") as r:
                if r.status != 200: raise Exception("CoinGecko unavailable")
                data  = await r.json()
                coins = data.get("coins",[])
            if not coins:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id, message_id=msg.message_id,
                    text=(f"🔍 Research: {project}\n\n"
                          "Not found on CoinGecko — may be very early stage.\n\n"
                          "Try /daily_report to see all newly discovered projects."))
                return
            cid = coins[0]["id"]
            async with s.get(
                f"https://api.coingecko.com/api/v3/coins/{cid}",
                params={"localization":"false","tickers":"false",
                        "community_data":"true","developer_data":"true"}) as dr:
                d = await dr.json()
            links  = d.get("links",{})
            mkt    = d.get("market_data",{})
            name   = d.get("name","Unknown")
            desc   = (d.get("description",{}).get("en","") or "")[:500]
            web    = (links.get("homepage",["N/A"])[0] or "N/A").rstrip("/")
            tw     = f"@{links.get('twitter_screen_name','N/A')}"
            tg     = links.get("telegram_channel_identifier","N/A")
            gh     = (links.get("repos_url",{}).get("github",["N/A"])[0] or "N/A")
            mcap   = mkt.get("market_cap",{}).get("usd",0) or 0
            price  = mkt.get("current_price",{}).get("usd",0) or 0
            change = mkt.get("price_change_percentage_24h",0) or 0
            # Delete the loading message, then send each section clean
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=msg.message_id)

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                parse_mode="Markdown",
                text=f"📊 *RESEARCH: {name.upper()}*\n\n{desc[:500]}")
            await asyncio.sleep(0.3)

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                disable_web_page_preview=True,
                parse_mode="Markdown",
                text=(f"📱 *WHERE TO FIND THEM*\n\n"
                      f"🌐 Website: {web}\n"
                      f"🐦 Twitter: {tw}\n"
                      f"✈️ Telegram: {tg}\n"
                      f"💻 GitHub: {gh}"))
            await asyncio.sleep(0.3)

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                parse_mode="Markdown",
                text=(f"📊 *MARKET DATA*\n\n"
                      f"💵 Price: ${price:,.6f}\n"
                      f"🏦 Market Cap: ${mcap:,.0f}\n"
                      f"📈 24h Change: {change:+.1f}%\n\n"
                      f"💼 Use /jobs for roles in this space."))
    except Exception as e:
        logger.error(f"research: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=msg.message_id,
            text=f"❌ Research failed: {e}\nTry again shortly.")

# ── /jobs ─────────────────────────────────────────────────────────
async def jobs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    msg = await update.message.reply_text(
        "💼 Fetching live Web3 jobs...\n"
        "🏆 Superteam Earn · ₿ CryptoJobsList · 🌐 Web3.career\n⏳ 30s...")
    fallback = [
        {"p":"🏆 Superteam","t":"Community Manager","c":"Web3 Project","s":"$500–3000","u":"https://earn.superteam.fun"},
        {"p":"₿ CryptoJobsList","t":"Social Media Manager","c":"DeFi Protocol","s":"$1000–4000","u":"https://cryptojobslist.com"},
        {"p":"🌐 Web3.career","t":"Business Development","c":"NFT Platform","s":"$2000–8000","u":"https://web3.career"},
    ]
    try:
        real = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                                          headers={"User-Agent":"Mozilla/5.0"}) as s:
            try:
                async with s.get("https://earn.superteam.fun/api/listings?limit=5") as r:
                    if r.status == 200:
                        data = await r.json()
                        for item in (data.get("listings") or data if isinstance(data,list) else [])[:5]:
                            real.append({
                                "p":"🏆 Superteam Earn",
                                "t":item.get("title","Unknown"),
                                "c":item.get("sponsor",{}).get("name","Unknown") if isinstance(item.get("sponsor"),dict) else "Unknown",
                                "s":f"${item.get('rewardAmount',0):,}" if item.get("rewardAmount") else "See listing",
                                "u":f"https://earn.superteam.fun/listings/{item.get('slug','')}"})
            except: pass
        jobs = real if real else fallback
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=msg.message_id,
            text=f"💼 LIVE WEB3 JOBS\n{'─'*28}\n{len(jobs)} opportunities found\n\nTrack: /apply [url] [company] [title]")
        for i,j in enumerate(jobs,1):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                disable_web_page_preview=True,
                text=(f"#{i} {j['p']}\n📌 {j['t']}\n🏢 {j['c']}\n💰 {j['s']}\n🔗 {j['u']}\n"
                      f"Track: /apply {j['u']} {j['c'].replace(' ','_')} {j['t'].replace(' ','_')}"))
            await asyncio.sleep(0.4)
    except Exception as e:
        logger.error(f"jobs: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=msg.message_id,
            text=f"❌ Jobs fetch failed: {e}")

# ── /apply ────────────────────────────────────────────────────────
async def apply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    if len(context.args) < 3:
        await update.message.reply_text(
            "📝 /apply [url] [company] [title]\n\nExample:\n"
            "/apply https://earn.superteam.fun Chainlink Community_Manager"); return
    chat_id  = update.effective_chat.id
    url      = context.args[0]
    company  = context.args[1].replace("_"," ")
    title    = " ".join(context.args[2:]).replace("_"," ")
    platform = ("superteam_earn" if "superteam" in url else
                "crypto_jobs"   if "cryptojobs" in url else "direct")
    app_id   = hashlib.md5(f"{chat_id}{url}{int(time.time())}".encode()).hexdigest()[:8]
    if add_application(chat_id,app_id,company,title,platform,url):
        await update.message.reply_text(
            f"✅ Application Tracked!\n📌 {title}\n🏢 {company}\n"
            f"🆔 {app_id}\n📊 Status: Pending\n\n"
            f"Update: /update_status {app_id} applied\nView all: /applications")
    else:
        await update.message.reply_text("❌ Error. Try again.")

# ── /applications ─────────────────────────────────────────────────
async def applications_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    chat_id = update.effective_chat.id
    rows    = get_applications(chat_id)
    if not rows:
        await update.message.reply_text("📋 No applications yet.\nBrowse /jobs then /apply"); return
    total = len(rows)
    stats = {}
    for r in rows: stats[r[6]] = stats.get(r[6],0)+1
    resp = stats.get("response_received",0)
    ints = stats.get("interview_scheduled",0)
    await update.message.reply_text(
        f"📊 JOB PIPELINE\n{'━'*24}\n"
        f"Total: {total}  Applied: {stats.get('applied',0)}\n"
        f"Responses: {resp} ({f'{(resp/total)*100:.0f}%' if total else 'N/A'})\n"
        f"Interviews: {ints}  Accepted: {stats.get('accepted',0)}\n\n"
        f"{'━'*24}\nRECENT APPLICATIONS")
    icons = {"pending":"⏳","applied":"📤","response_received":"📬",
             "interview_scheduled":"📅","rejected":"❌","accepted":"🎉"}
    for row in rows[:8]:
        icon = icons.get(row[6],"❓")
        days = (datetime.now()-datetime.fromisoformat(row[5])).days
        await context.bot.send_message(chat_id=chat_id, disable_web_page_preview=True,
            text=(f"{icon} {row[3]}\n🏢 {row[2]}\n"
                  f"📅 {days}d ago · 🆔 {row[0]}\n"
                  f"📊 {row[6].replace('_',' ').title()}\n🔗 {row[7]}\n"
                  f"Update: /update_status {row[0]} [status]"))
        await asyncio.sleep(0.3)

# ── /update_status ────────────────────────────────────────────────
async def update_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /update_status [app_id] [status]\n\n"
            "Statuses: pending applied response interview rejected accepted withdrawn"); return
    chat_id = update.effective_chat.id
    app_id  = context.args[0]
    status  = {"response":"response_received","interview":"interview_scheduled"}.get(
               context.args[1].lower(), context.args[1].lower())
    notes   = " ".join(context.args[2:])
    if update_app_status(chat_id, app_id, status, notes):
        extra = {"accepted":"🎊 CONGRATULATIONS!","interview_scheduled":"💪 Good luck!",
                 "rejected":"💪 Keep going!"}.get(status,"")
        await update.message.reply_text(
            f"✅ Updated\n🆔 {app_id}\n📊 {status.replace('_',' ').title()}\n{extra}\n\n/applications")
    else:
        await update.message.reply_text("❌ ID not found. Check /applications")

# ── /weekly_report ────────────────────────────────────────────────
async def weekly_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context): return
    chat_id  = update.effective_chat.id
    rows     = get_applications(chat_id)
    total    = len(rows)
    week_ago = datetime.now() - timedelta(days=7)
    new_week = sum(1 for r in rows if datetime.fromisoformat(r[5]) > week_ago)
    # Get last report stats
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    log_row = conn.execute(
        "SELECT * FROM daily_report_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    log_txt = ""
    if log_row:
        log_txt = (f"\n📡 Last Report: {log_row['report_date']}\n"
                   f"  Sources: {log_row['sources_checked']}  "
                   f"Projects: {log_row['projects_found']}")
    await update.message.reply_text(
        f"📅 WEEKLY REPORT — {datetime.now().strftime('%B %d, %Y')}\n{'━'*30}\n\n"
        f"💼 New applications this week: {new_week}\n"
        f"📊 Total pipeline: {total}\n"
        f"{log_txt}\n\n"
        f"🔍 /daily_report — full today's scan\n"
        f"💼 /jobs — new opportunities\n"
        f"📊 /applications — manage pipeline")

# ── /scout_communities ────────────────────────────────────────────

# ── MAIN ──────────────────────────────────────────────────────────
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set"); return
    init_db()
    app = Application.builder().token(token).build()

    for cmd, handler in [
        ("start",             start_cmd),
        ("verify",            verify_cmd),
        ("subscribe",         subscribe_cmd),
        ("unsubscribe",       subscribe_cmd),
        ("preferences",       preferences_cmd),
        ("status",            status_handler),
        ("research",          research_cmd),
        ("funding_stats",     funding_stats_handler),
        ("twitter_scan",      twitter_scan_handler),
        ("daily_report",      daily_report_cmd),
        ("set_report_time",   set_report_time_cmd),
        ("scout_communities", scout_communities_handler),
        ("scout",             scout_communities_handler),
        ("jobs",              jobs_cmd),
        ("superteam",         jobs_cmd),
        ("apply",             apply_cmd),
        ("applications",      applications_cmd),
        ("pipeline",          applications_cmd),
        ("update_status",     update_status_cmd),
        ("weekly_report",     weekly_report_cmd),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(handle_callback))

    async def post_init(application):
        asyncio.create_task(daily_scheduler(application.bot))
        logger.info("Daily scheduler started")

    app.post_init = post_init
    logger.info("Bot starting with daily intelligence engine...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
