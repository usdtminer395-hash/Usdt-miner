import os, json, time, math, logging, io, csv
import threading
from uuid import uuid4
from datetime import datetime
import requests
import base58  # For TRON address validation

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, JobQueue
)

# ───────────────── CONFIG ─────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEPOSIT_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()  # TRC-20 USDT address
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()
USDT_CONTRACT = os.getenv("USDT_CONTRACT", "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj").strip()

PLANS = [10, 50, 100]       # USDT
DAILY_RATE = 0.04           # 4% / day
REFERRAL_RATE = 0.10        # 10% on confirmed deposits
MIN_PROFIT_WITHDRAW = 10.0  # profit withdrawal threshold
LOCK_DAYS = 15              # principal lock
DATA_FILE = "data.json"

ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

TERMS_TEXT = (
    "📜 *Terms & Conditions*\n\n"
    "1) We have taken all possible measures to keep this bot secure. However, in case of any technical fault or any "
    "other issue that results in financial loss, the bot administration will *not* be held responsible.\n\n"
    "2) Profit withdrawals: minimum $10 (manual). Requests are processed within 24 hours.\n\n"
    "3) Investment withdrawal: available *only after 15 days* of lock *and* requires at least *one referral in the same plan*.\n\n"
    "4) Mining: tap the ⛏ button once every 24 hours to collect *4%* of your *active principal*.\n\n"
    "5) Deposits must be sent to the TRC-20 USDT address shown inside the bot. Using the wrong network/address may result "
    "in loss of funds (user’s responsibility).\n\n"
    "6) Rules may be updated at any time via /terms.\n\n"
    "By using this bot, you agree to these terms."
)

# ──────────────── STORAGE ────────────────
logging.basicConfig(level=logging.INFO)

DB_LOCK = threading.Lock()
DB = None

def now_ts() -> int:
    return int(time.time())

def load_db():
    with DB_LOCK:
        if not os.path.exists(DATA_FILE):
            return {"users": {}, "seen_tx": [], "withdraw_queue": []}
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as ex:
            logging.error(f"Corrupted data.json: {ex}")
            return {"users": {}, "seen_tx": [], "withdraw_queue": []}

def save_db():
    with DB_LOCK:
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(DB, f, ensure_ascii=False, indent=2)
        except Exception as ex:
            logging.error(f"Failed to save DB: {ex}")

DB = load_db()

# ──────────────── HELPERS ────────────────
def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS if ADMIN_IDS else False

def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"

def ensure_user(tg_user):
    uid = str(tg_user.id)
    with DB_LOCK:
        u = DB["users"].get(uid)
        if u: return u
        u = {
            "id": uid,
            "name": tg_user.full_name,
            "username": tg_user.username,
            "created": now_ts(),
            "referrer": None,
            "referrals": [],
            "referrals_by_plan": {},    # {"10": count, ...}
            "payout": "",
            "balances": {"profit": 0.0, "referral": 0.0},
            "investments": [],          # {id, plan, amount, start_ts, lock_until, active, txid}
            "last_mine": 0,
            "accepted_terms": False
        }
        DB["users"][uid] = u
        save_db()
        return u

def active_principal(u) -> float:
    return sum(inv["amount"] for inv in u["investments"] if inv.get("active", True))

def active_principal_by_plan(u, plan) -> float:
    return sum(inv["amount"] for inv in u["investments"]
               if inv.get("active", True) and int(inv["plan"]) == int(plan))

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 Invest", callback_data="invest"),
            InlineKeyboardButton("⛏ Start Mining", callback_data="mine")
        ],
        [
            InlineKeyboardButton("💼 Balance", callback_data="balance"),
            InlineKeyboardButton("👥 Referral", callback_data="ref")
        ],
        [
            InlineKeyboardButton("📤 Withdraw Profit", callback_data="wd_profit"),
            InlineKeyboardButton("🏦 Withdraw Investment", callback_data="wd_inv")
        ],
        [
            InlineKeyboardButton("⚙️ Payout Address", callback_data="set_addr"),
            InlineKeyboardButton("📜 Terms", callback_data="terms")
        ]
    ])

def is_valid_tron_address(address: str) -> bool:
    try:
        if len(address) != 34 or not address.startswith("T"):
            return False
        base58.b58decode_check(address)
        return True
    except Exception:
        return False

def tron_headers():
    h = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        h["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return h

def fetch_tx(txid: str):
    try:
        url = f"https://api.trongrid.io/v1/transactions/{txid}/events"
        r = requests.get(url, headers=tron_headers(), timeout=20)
        if r.status_code != 200: return None
        data = r.json().get("data", [])
        for e in data:
            if e.get("event_name") == "Transfer" and e.get("contract_address") == USDT_CONTRACT:
                fr = e["result"].get("from")
                to = e["result"].get("to")
                val = float(e["result"].get("value", 0)) / 1_000_000.0
                # Check confirmation
                tx_info_url = f"https://api.trongrid.io/v1/transactions/{txid}"
                tx_r = requests.get(tx_info_url, headers=tron_headers(), timeout=20)
                if tx_r.status_code == 200 and tx_r.json().get("confirmed", False):
                    return {"from": fr, "to": to, "amount": val}
    except Exception as ex:
        logging.exception(ex)
    return None

# ──────────────── HANDLERS ────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update.effective_user)
    # Handle referral
    if context.args and not user.get("referrer"):
        ref = context.args[0].strip()
        if ref != user["id"] and ref in DB["users"]:
            user["referrer"] = ref
            DB["users"][ref]["referrals"].append(user["id"])
            save_db()

    cover = (
        "🌟 <b>Welcome! USDT Miner Bot</b> 🌟\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💸 <b>How it works?</b>\n"
        "• 📈 <b>Plans</b>: $10, $50, $100 (TRC-20 USDT)\n"
        "• ⛏ <b>Mining</b>: 4% daily profit every 24h\n"
        "• 👥 <b>Referral Bonus</b>: 10% per deposit\n"
        "• 💰 <b>Profit Withdrawal</b>: Min $10 (within 24h)\n"
        "• 🔒 <b>Investment Withdrawal</b>: After 15 days + 1 referral\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 Start growing your investment now!"
    )
    await update.message.reply_html(cover, reply_markup=main_menu())

async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(TERMS_TEXT)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use the buttons below.", reply_markup=main_menu())

async def on_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = ensure_user(q.from_user)

    if q.data == "terms":
        await q.edit_message_markdown(TERMS_TEXT, reply_markup=main_menu())
        return

    if q.data == "balance":
        txt = (
            "💼 <b>Your Balance</b> 💼\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Active Principal</b>: {fmt_usd(active_principal(user))}\n"
            f"💰 <b>Profit</b>: {fmt_usd(user['balances']['profit'])}\n"
            f"👥 <b>Referral Bonus</b>: {fmt_usd(user['balances']['referral'])}\n"
            f"🏦 <b>Payout Address</b>: <code>{user['payout'] or 'Not set'}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        await q.edit_message_html(txt, reply_markup=main_menu())
        return

    if q.data == "set_addr":
        context.user_data["await_addr"] = True
        txt = "⚙️ <b>Set Payout Address</b>\n\nSend your <b>TRC-20 USDT</b> address now."
        await q.edit_message_html(txt)
        return

    if q.data == "invest":
        plans = "\n".join([f"• 💵 <b>${p}</b>: 4% daily profit" for p in PLANS])
        txt = (
            "💸 <b>Invest / Deposit</b> 💸\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 <b>Send USDT to this address</b>:\n<code>{DEPOSIT_ADDRESS}</code>\n"
            "🌐 <b>Network</b>: TRC-20 (TRON)\n\n"
            f"<b>Plans</b>:\n{plans}\n\n"
            "<b>How to?</b>\n"
            "1. Send USDT.\n"
            "2. Select a plan below.\n"
            "3. Paste your TXID."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💵 ${p}", callback_data=f"plan_{p}") for p in PLANS],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
        await q.edit_message_html(txt, reply_markup=kb)
        return

    if q.data.startswith("plan_"):
        plan = int(q.data.split("_")[1])
        context.user_data["await_tx_for_plan"] = plan
        txt = (
            f"✅ Selected plan: <b>${plan}</b>\n\n"
            f"Send to: <code>{DEPOSIT_ADDRESS}</code> (TRC-20)\n"
            "Now paste your <b>TXID</b> in the chat."
        )
        await q.edit_message_html(txt)
        return

    if q.data == "mine":
        last = user.get("last_mine", 0)
        if now_ts() - last < 24*3600:
            remain = 24*3600 - (now_ts() - last)
            hrs = math.ceil(remain / 3600)
            txt = f"⏳ You've already mined today. Try again in ~{hrs}h. 😊"
            await q.edit_message_text(txt, reply_markup=main_menu())
            return
        principal = active_principal(user)
        if principal <= 0:
            txt = "⚠️ No active investment yet. Please invest first. 💸"
            await q.edit_message_text(txt, reply_markup=main_menu())
            return
        gain = round(principal * DAILY_RATE, 2)
        user["balances"]["profit"] = round(user["balances"]["profit"] + gain, 2)
        user["last_mine"] = now_ts()
        save_db()
        txt = (
            f"⛏ <b>Mining Successful!</b> 🎉\n"
            f"💰 Added {fmt_usd(gain)} to your profit!\n"
            f"📊 Total Profit: {fmt_usd(user['balances']['profit'])}"
        )
        await q.edit_message_text(txt, reply_markup=main_menu())
        return

    if q.data == "ref":
        link = f"https://t.me/{context.bot.username}?start={user['id']}"
        ref_count = len(user["referrals"])
        badge = "🥇" if ref_count >= 10 else "🥈" if ref_count >= 5 else "🥉" if ref_count >= 1 else ""
        txt = (
            "👥 <b>Referral Program</b> 👥\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• 💎 <b>Bonus</b>: 10% on each deposit\n"
            f"• 👨‍👩‍👧‍👦 <b>Your Referrals</b>: {ref_count} {badge}\n"
            f"• 🔗 <b>Your Link</b>:\n<code>{link}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Invite friends and earn bonuses! 🚀"
        )
        await q.edit_message_html(txt, reply_markup=main_menu())
        return

    if q.data == "wd_profit":
        amt = user["balances"]["profit"]
        if amt < MIN_PROFIT_WITHDRAW:
            txt = f"⚠️ Minimum profit withdrawal is {fmt_usd(MIN_PROFIT_WITHDRAW)}."
            await q.edit_message_text(txt, reply_markup=main_menu())
            return
        if not user["payout"]:
            txt = "⚠️ Set your payout address first (⚙️ Payout Address)."
            await q.edit_message_text(txt, reply_markup=main_menu())
            return
        req_id = str(uuid4())[:8]
        DB["withdraw_queue"].append({
            "id": req_id, "user_id": user["id"], "type": "profit",
            "amount": round(amt, 2), "to": user["payout"],
            "status": "pending", "created_ts": now_ts()
        })
        user["balances"]["profit"] = 0.0
        save_db()
        txt = (
            f"📤 Profit withdrawal request queued.\n"
            f"Request ID: <code>{req_id}</code>\n"
            "Payout will be processed within 24 hours."
        )
        await q.edit_message_text(txt, reply_markup=main_menu())
        return

    if q.data == "wd_inv":
        lines = ["🏦 <b>Investment Withdrawal Eligibility</b>"]
        ok_any = False
        for p in PLANS:
            principal_plan = active_principal_by_plan(user, p)
            if principal_plan <= 0:
                lines.append(f"• ${p}: No active investment")
                continue
            ok_lock = any(inv.get("active", True) and int(inv["plan"]) == p and now_ts() >= inv["lock_until"]
                          for inv in user["investments"])
            refs_plan = user["referrals_by_plan"].get(str(p), 0)
            ok_ref = refs_plan >= 1
            status = "✅" if (ok_lock and ok_ref) else "❌"
            lock_status = "OK" if ok_lock else "Not yet"
            ref_status = f"Referral: {refs_plan}/1"
            lines.append(f"• ${p}: {status} (Lock: {lock_status} | {ref_status})")
            if ok_lock and ok_ref:
                ok_any = True
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back")]]
        if ok_any:
            kb.insert(0, [InlineKeyboardButton("📤 Request Withdrawal", callback_data="req_wd_inv")])
        await q.edit_message_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
        return

    if q.data == "req_wd_inv":
        context.user_data["await_inv_withdraw_plan"] = True
        txt = "Which plan to withdraw? Send: 10 / 50 / 100"
        await q.edit_message_text(txt)
        return

    if q.data == "back":
        txt = "Main menu:"
        await q.edit_message_text(txt, reply_markup=main_menu())
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update.effective_user)
    txt = update.message.text.strip()

    # Set payout address
    if context.user_data.get("await_addr"):
        if not is_valid_tron_address(txt):
            await update.message.reply_text("⚠️ Invalid TRC-20 address. Please send a valid address.")
            return
        user["payout"] = txt
        context.user_data.pop("await_addr", None)
        save_db()
        await update.message.reply_html(
            f"✅ Payout address saved:\n<code>{txt}</code>",
            reply_markup=main_menu()
        )
        return

    # Receive TXID after choosing plan
    if "await_tx_for_plan" in context.user_data:
        plan = int(context.user_data["await_tx_for_plan"])
        txid = txt
        await update.message.reply_text("🔎 Verifying transaction…")
        info = fetch_tx(txid)
        if not info:
            await update.message.reply_text("⚠️ Could not find a valid USDT transfer for this TXID.")
            return
        if info["to"] != DEPOSIT_ADDRESS:
            await update.message.reply_text("⚠️ Funds were not sent to the bot’s deposit address.")
            return
        if info["amount"] + 1e-6 < plan:
            await update.message.reply_text(f"⚠️ Amount is insufficient for the ${plan} plan.")
            return
        if txid in DB["seen_tx"]:
            await update.message.reply_text("⚠️ This TXID was already used.")
            return

        DB["seen_tx"].append(txid)
        inv = {
            "id": str(uuid4()),
            "plan": plan,
            "amount": float(plan),
            "start_ts": now_ts(),
            "lock_until": now_ts() + LOCK_DAYS*24*3600,
            "active": True,
            "txid": txid
        }
        user["investments"].append(inv)

        # Referral bonus
        ref = user.get("referrer")
        if ref and ref in DB["users"]:
            ref_u = DB["users"][ref]
            ref_u["balances"]["referral"] = round(ref_u["balances"]["referral"] + plan * REFERRAL_RATE, 2)
            ref_u["referrals_by_plan"][str(plan)] = ref_u["referrals_by_plan"].get(str(plan), 0) + 1

        save_db()
        context.user_data.pop("await_tx_for_plan", None)
        await update.message.reply_html(
            f"✅ Investment activated: <b>${plan}</b>\n"
            f"Lock: <b>{LOCK_DAYS} days</b>\n"
            f"TXID: <code>{txid}</code>\n\n"
            "Remember to tap ⛏ <b>Start Mining</b> every 24 hours.",
            reply_markup=main_menu()
        )
        return

    # Investment withdraw plan
    if context.user_data.get("await_inv_withdraw_plan"):
        if txt not in {"10", "50", "100"}:
            await update.message.reply_text("Please send 10 or 50 or 100.")
            return
        plan = int(txt)
        principal_plan = active_principal_by_plan(user, plan)
        if principal_plan <= 0:
            await update.message.reply_text("No active investment in this plan.", reply_markup=main_menu())
            context.user_data.pop("await_inv_withdraw_plan", None)
            return
        ok_lock = any(inv.get("active", True) and int(inv["plan"]) == plan and now_ts() >= inv["lock_until"]
                      for inv in user["investments"])
        refs_plan = user["referrals_by_plan"].get(str(plan), 0)
        ok_ref = refs_plan >= 1
        if not ok_lock or not ok_ref:
            await update.message.reply_text("Eligibility not met: 15-day lock + 1 same-plan referral required.",
                                            reply_markup=main_menu())
            context.user_data.pop("await_inv_withdraw_plan", None)
            return
        if not user["payout"]:
            await update.message.reply_text("Set your payout address first (⚙️ Payout Address).",
                                            reply_markup=main_menu())
            context.user_data.pop("await_inv_withdraw_plan", None)
            return

        # Deactivate investments in this plan
        for inv in user["investments"]:
            if inv.get("active", True) and int(inv["plan"]) == plan:
                inv["active"] = False

        req_id = str(uuid4())[:8]
        DB["withdraw_queue"].append({
            "id": req_id, "user_id": user["id"], "type": "principal",
            "amount": round(principal_plan, 2), "to": user["payout"],
            "status": "pending", "created_ts": now_ts(), "plan": plan
        })
        save_db()
        context.user_data.pop("await_inv_withdraw_plan", None)
        await update.message.reply_html(
            f"📤 Principal withdrawal queued: <b>{fmt_usd(principal_plan)}</b>\n"
            f"Request ID: <code>{req_id}</code>\nProcessed within 24h.",
            reply_markup=main_menu()
        )
        return

    # Fallback
    await update.message.reply_text("Use the menu below.", reply_markup=main_menu())

# ──────────────── ADMIN ────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Queue", callback_data="ad_queue"),
         InlineKeyboardButton("📤 Export CSV", callback_data="ad_export")]
    ])
    await update.message.reply_text("Admin Panel:", reply_markup=kb)

async def on_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Admin only", show_alert=True)
        return
    await q.answer()

    if q.data == "ad_queue":
        rows = [r for r in DB.get("withdraw_queue", []) if r["status"] == "pending"]
        if not rows:
            await q.edit_message_text("Queue is empty.")
            return
        lines = ["Pending Requests:"]
        for r in rows[:50]:
            created = datetime.utcfromtimestamp(r['created_ts']).strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f"{r['id']} | {r['type']} | {fmt_usd(r['amount'])} → {r['to']} "
                         f"(user {r['user_id']}) | {created} UTC")
        await q.edit_message_text("\n".join(lines))
        return

    if q.data == "ad_export":
        rows = DB.get("withdraw_queue", [])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id","user_id","type","amount","to","status","created_ts","plan"])
        for r in rows:
            w.writerow([r.get("id"), r.get("user_id"), r.get("type"), r.get("amount"),
                        r.get("to"), r.get("status"), r.get("created_ts"), r.get("plan")])
        data = buf.getvalue().encode("utf-8")
        await q.message.reply_document(document=data, filename="withdraw_queue.csv",
                                       caption="Withdraw Queue CSV")
        await q.edit_message_text("CSV sent.")
        return

# ──────────────── REMINDER JOB ────────────────
async def mine_reminder(context: ContextTypes.DEFAULT_TYPE):
    with DB_LOCK:
        for uid, user in DB["users"].items():
            if active_principal(user) > 0 and now_ts() - user.get("last_mine", 0) >= 24*3600:
                txt = "⛏ It's time to mine! Tap ⛏ Start Mining now! 😊"
                await context.bot.send_message(
                    chat_id=uid,
                    text=txt,
                    reply_markup=main_menu()
                )

# ──────────────── BOOTSTRAP ────────────────
def main():
    if not BOT_TOKEN or not DEPOSIT_ADDRESS or not USDT_CONTRACT:
        raise RuntimeError("Missing required env vars: BOT_TOKEN, WALLET_ADDRESS, USDT_CONTRACT")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("terms", cmd_terms))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_admin_buttons, pattern="^ad_"))

    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Job queue for reminders (every hour)
    app.job_queue.run_repeating(mine_reminder, interval=3600, first=10)

    app.run_polling()

if __name__ == "__main__":
    main()