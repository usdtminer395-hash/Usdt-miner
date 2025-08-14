import os, json, time, math, logging, io, csv
from uuid import uuid4
from datetime import datetime
import requests

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ───────────────── CONFIG ─────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEPOSIT_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()  # TRC-20 USDT address (Trust Wallet/TRON)
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()

# TRC-20 USDT contract on Tron (mainnet)
USDT_CONTRACT = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"

PLANS = [10, 50, 100]       # USDT
DAILY_RATE = 0.04           # 4% / day (tap every 24h)
REFERRAL_RATE = 0.10        # 10% on confirmed deposits
MIN_PROFIT_WITHDRAW = 10.0  # profit withdrawal threshold
LOCK_DAYS = 15              # principal lock
DATA_FILE = "data.json"

# Optional admin list (comma-separated user IDs in an env var)
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

def now_ts() -> int:
    return int(time.time())

def load_db():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "seen_tx": [], "withdraw_queue": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

DB = load_db()

# ──────────────── HELPERS ────────────────
def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS if ADMIN_IDS else False

def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"

def ensure_user(tg_user):
    uid = str(tg_user.id)
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
        [InlineKeyboardButton("💳 Invest / Deposit", callback_data="invest"),
         InlineKeyboardButton("⛏ Start Mining", callback_data="mine")],
        [InlineKeyboardButton("💼 Balance", callback_data="balance"),
         InlineKeyboardButton("👥 Referral", callback_data="ref")],
        [InlineKeyboardButton("📤 Withdraw Profit", callback_data="wd_profit"),
         InlineKeyboardButton("🏦 Withdraw Investment", callback_data="wd_inv")],
        [InlineKeyboardButton("⚙️ Payout Address", callback_data="set_addr"),
         InlineKeyboardButton("📘 Terms", callback_data="terms")]
    ])

def tron_headers():
    h = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        h["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return h

def fetch_tx(txid: str):
    """Check TX events on TronGrid and find a USDT Transfer to our deposit address."""
    try:
        url = f"https://api.trongrid.io/v1/transactions/{txid}/events"
        r = requests.get(url, headers=tron_headers(), timeout=20)
        if r.status_code != 200: return None
        for e in r.json().get("data", []):
            if e.get("event_name") == "Transfer" and e.get("contract") == USDT_CONTRACT:
                fr = e["result"].get("from")
                to = e["result"].get("to")
                val = float(e["result"].get("value", 0)) / 1_000_000.0
                return {"from": fr, "to": to, "amount": val}
    except Exception as ex:
        logging.exception(ex)
    return None

# ──────────────── HANDLERS ────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update.effective_user)
    # handle referral
    if context.args:
        ref = context.args[0].strip()
        if ref != user["id"] and not user.get("referrer"):
            if ref in DB["users"]:
                user["referrer"] = ref
                DB["users"][ref]["referrals"].append(user["id"])
                save_db()

    cover = (
        "💎 <b>USDT Miner</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>How it works</b>\n"
        "• Plans: <b>$10 / $50 / $100</b> (TRC-20)\n"
        "• Mining: <b>4% daily</b> — tap once every 24h\n"
        "• Referral: <b>10%</b> on each confirmed deposit\n"
        f"• Profit withdraw: min <b>${MIN_PROFIT_WITHDRAW}</b> (manual, within 24h)\n"
        f"• Investment withdraw: after <b>{LOCK_DAYS} days</b> + <b>1 referral</b> in same plan\n"
        "━━━━━━━━━━━━━━━━"
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
        await q.edit_message_text("Loading terms…")
        await q.edit_message_markdown(TERMS_TEXT, reply_markup=main_menu())
        return

    if q.data == "balance":
        txt = (
            "💼 <b>Your Balances</b>\n"
            f"• Active Principal: <b>{fmt_usd(active_principal(user))}</b>\n"
            f"• Profit: <b>{fmt_usd(user['balances']['profit'])}</b>\n"
            f"• Referral: <b>{fmt_usd(user['balances']['referral'])}</b>\n"
            f"• Payout Address: <code>{user['payout'] or 'Not set'}</code>"
        )
        await q.edit_message_html(txt, reply_markup=main_menu())
        return

    if q.data == "set_addr":
        context.user_data["await_addr"] = True
        await q.edit_message_html(
            "⚙️ <b>Set Payout Address</b>\n\nSend your <b>TRC-20 USDT</b> address now."
        )
        return

    if q.data == "invest":
        plans = "\n".join([f"• <b>${p}</b> → 4% daily" for p in PLANS])
        txt = (
            "💳 <b>Invest / Deposit</b>\n\n"
            f"Send USDT to this address:\n<code>{DEPOSIT_ADDRESS}</code>\n"
            "Network: <b>TRC-20 (TRON)</b>\n\n"
            f"{plans}\n\n"
            "After sending, tap a plan and paste your <b>TXID</b> for auto-verification."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"${p}", callback_data=f"plan_{p}") for p in PLANS],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
        await q.edit_message_html(txt, reply_markup=kb)
        return

    if q.data.startswith("plan_"):
        plan = int(q.data.split("_")[1])
        context.user_data["await_tx_for_plan"] = plan
        await q.edit_message_html(
            f"✅ Selected plan: <b>${plan}</b>\n\n"
            f"Send to: <code>{DEPOSIT_ADDRESS}</code> (TRC-20)\n"
            "Now paste your <b>TXID</b> (hash) in the chat to auto-activate."
        )
        return

    if q.data == "mine":
        # 24h mining button
        last = user.get("last_mine", 0)
        if now_ts() - last < 24*3600:
            remain = 24*3600 - (now_ts() - last)
            hrs = math.ceil(remain / 3600)
            await q.edit_message_text(f"⏳ You've already mined today. Try again in ~{hrs}h.",
                                      reply_markup=main_menu())
            return
        principal = active_principal(user)
        if principal <= 0:
            await q.edit_message_text("No active investment yet. Please deposit first.",
                                      reply_markup=main_menu())
            return
        gain = round(principal * DAILY_RATE, 2)
        user["balances"]["profit"] = round(user["balances"]["profit"] + gain, 2)
        user["last_mine"] = now_ts()
        save_db()
        await q.edit_message_text(
            f"⛏ Mining complete!\n+{fmt_usd(gain)} added to profit.\n"
            f"Total Profit: {fmt_usd(user['balances']['profit'])}",
            reply_markup=main_menu()
        )
        return

    if q.data == "ref":
        link = f"https://t.me/{context.bot.username}?start={user['id']}"
        txt = (
            "👥 <b>Referral</b>\n"
            f"• Bonus: <b>10%</b> of each confirmed deposit\n"
            f"• Your referrals: <b>{len(user['referrals'])}</b>\n\n"
            f"🔗 Your link:\n<code>{link}</code>"
        )
        await q.edit_message_html(txt, reply_markup=main_menu())
        return

    if q.data == "wd_profit":
        amt = user["balances"]["profit"]
        if amt < MIN_PROFIT_WITHDRAW:
            await q.edit_message_text(
                f"Minimum profit withdrawal is {fmt_usd(MIN_PROFIT_WITHDRAW)}.",
                reply_markup=main_menu()
            )
            return
        if not user["payout"]:
            await q.edit_message_text("Set your payout address first (⚙️ Payout Address).",
                                      reply_markup=main_menu())
            return
        req_id = str(uuid4())[:8]
        DB["withdraw_queue"].append({
            "id": req_id, "user_id": user["id"], "type": "profit",
            "amount": round(amt, 2), "to": user["payout"],
            "status": "pending", "created_ts": now_ts()
        })
        user["balances"]["profit"] = 0.0
        save_db()
        await q.edit_message_text(
            f"📤 Profit withdrawal request queued.\n"
            f"Request ID: <code>{req_id}</code>\n"
            "Payout will be processed within 24 hours.",
            reply_markup=main_menu()
        )
        return

    if q.data == "wd_inv":
        # show eligibility per plan
        lines = ["🏦 <b>Investment Withdrawal Eligibility</b>"]
        ok_any = False
        for p in PLANS:
            principal_plan = active_principal_by_plan(user, p)
            if principal_plan <= 0:
                lines.append(f"• ${p}: no active investment")
                continue
            ok_lock = any(inv.get("active", True) and int(inv["plan"]) == p and now_ts() >= inv["lock_until"]
                          for inv in user["investments"])
            refs_plan = user["referrals_by_plan"].get(str(p), 0)
            ok_ref = refs_plan >= 1
            status = "✅" if (ok_lock and ok_ref) else "❌"
            lines.append(f"• ${p}: {status} (Lock: {'OK' if ok_lock else 'Not yet'} | Referral: {refs_plan}/1)")
            if ok_lock and ok_ref:
                ok_any = True
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back")]]
        if ok_any:
            kb.insert(0, [InlineKeyboardButton("Request Withdraw", callback_data="req_wd_inv")])
        await q.edit_message_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
        return

    if q.data == "req_wd_inv":
        context.user_data["await_inv_withdraw_plan"] = True
        await q.edit_message_text("Which plan to withdraw? Send: 10 / 50 / 100")
        return

    if q.data == "back":
        await q.edit_message_text("Main menu:", reply_markup=main_menu())
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = ensure_user(update.effective_user)
    txt = update.message.text.strip()

    # set payout address
    if context.user_data.get("await_addr"):
        user["payout"] = txt
        context.user_data.pop("await_addr", None)
        save_db()
        await update.message.reply_html(
            f"✅ Payout address saved:\n<code>{txt}</code>",
            reply_markup=main_menu()
        )
        return

    # receive TXID after choosing plan
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

        # referral bonus
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

    # investment withdraw which plan?
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

        # deactivate principals in this plan
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

    # fallback
    await update.message.reply_text("Use the menu below.", reply_markup=main_menu())

# ──────────────── ADMIN (optional) ────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Queue", callback_data="ad_queue"),
         InlineKeyboardButton("📤 Export CSV", callback_data="ad_export")]
    ])
    await update.message.reply_text("Admin Panel:", reply_markup=kb)

async def on_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Admin only", show_alert=True); return
    await q.answer()

    if q.data == "ad_queue":
        rows = [r for r in DB.get("withdraw_queue", []) if r["status"] == "pending"]
        if not rows:
            await q.edit_message_text("Queue is empty."); return
        lines = ["Pending Requests:"]
        for r in rows[:50]:
            created = datetime.utcfromtimestamp(r['created_ts']).strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f"{r['id']} | {r['type']} | {fmt_usd(r['amount'])} → {r['to']} "
                         f"(user {r['user_id']}) | {created} UTC")
        await q.edit_message_text("\n".join(lines)); return

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
        await q.edit_message_text("CSV sent."); return

# ──────────────── BOOTSTRAP ────────────────
def main():
    if not BOT_TOKEN or not DEPOSIT_ADDRESS:
        raise RuntimeError("Missing BOT_TOKEN or WALLET_ADDRESS in environment (see .env).")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("terms", cmd_terms))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_admin_buttons, pattern="^ad_"))

    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()

if __name__ == "__main__":
    main()