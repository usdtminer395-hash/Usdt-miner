import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ─────────────────── Config ───────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "TPutYourTronUSDTAddress").strip()
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "usdtminer395@gmail.com").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8459345615").strip())

PLANS = [10, 50, 100]
DAILY_RATE = 0.04
LOCK_DAYS = 15
MIN_PROFIT_WITHDRAW = 10.0

# ───────── Terms (English; translated from your Urdu text) ─────────
TERMS_TEXT = (
    "📜 *Terms & Conditions*\n\n"
    "*Investment Plans*\n"
    "You can potentially double your investment in ~20 days and also earn via referrals. "
    "We currently offer 3 plans: $10, $50 and $100. You can earn up to 4% daily *if you tap* "
    "the Start Mining button every 24 hours.\n\n"
    "*Earning Duration*\n"
    "You keep earning as long as you want and as long as this bot continues to operate. "
    "Based on our model, we expect long-term operation, but there are no guarantees.\n\n"
    "*Referral System*\n"
    "You can earn more than your own investment via referrals. For example, if you refer 1 user who "
    "invests $100, you get $10 commission immediately (paid by the bot; the investor’s principal remains intact).\n\n"
    "*Withdraw Rules*\n"
    f"• Profit or referral commission can be withdrawn any time (minimum ${MIN_PROFIT_WITHDRAW:.0f}).\n"
    f"• Principal can be withdrawn after *{LOCK_DAYS} days* provided you have *at least one referral in the same plan*.\n\n"
    "*Note*\n"
    "We are not responsible for any technical errors, faults, data loss, or any financial loss. "
    "If for any reason the bot stops or data becomes unavailable, we do not accept liability."
)

TERMS_ACCEPT_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("✅ Accept Terms", callback_data="accept_terms")]]
)

# ────────────────── Simple in-memory “DB” ──────────────────
# This is a lightweight placeholder store (per user). Replace with real DB later.
def ensure_user(context: ContextTypes.DEFAULT_TYPE) -> dict:
    u = context.user_data
    if "profile" not in u:
        u["profile"] = {
            "terms_accepted": False,
            "principal": 0.0,              # set real after verifying deposit
            "plan": None,                  # 10 / 50 / 100
            "deposit_time": None,          # epoch seconds
            "referral_count": 0,
            "referral_same_plan": False,   # at least one same-plan referral
            "mining_profit": 0.0,          # demo counter
            "referral_profit": 0.0,        # demo counter
            "withdrawals": [],             # list of dicts: {id, type, amount, address, status, reason, ts}
        }
    return u["profile"]

def now_ts() -> int:
    return int(time.time())

def days_since(ts: int | None) -> int:
    if not ts:
        return 0
    return max(0, int((now_ts() - ts) // 86400))

# ────────────────── UI Helpers ────────────────
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Invest / Deposit", callback_data="invest"),
         InlineKeyboardButton("⛏ Start Mining", callback_data="mine")],
        [InlineKeyboardButton("💼 Balance", callback_data="balance"),
         InlineKeyboardButton("👥 Referral", callback_data="ref")],
        [InlineKeyboardButton("💌 Withdraw Profit", callback_data="wd_profit"),
         InlineKeyboardButton("🏦 Withdraw Investment", callback_data="wd_inv")],
        [InlineKeyboardButton("📥 Withdraw Status", callback_data="wd_status"),
         InlineKeyboardButton("⚙️ Payout Address", callback_data="payout")],
        [InlineKeyboardButton("📜 Terms", callback_data="terms"),
         InlineKeyboardButton("🛟 Support", callback_data="support")],
    ])

WELCOME = (
    "👋 <b>Welcome to USDT Miner Bot!</b>\n"
    "Please choose an option below:"
)

# ────────────────── START / TERMS ──────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = ensure_user(context)
    if not profile["terms_accepted"]:
        # Force terms first
        await update.message.reply_markdown(TERMS_TEXT, reply_markup=TERMS_ACCEPT_KB)
        return

    cover = (
        "• Plans: <b>$10 / $50 / $100</b> (TRC-20)\n"
        "• Mining: <b>4% daily</b> — tap once every 24h\n"
        "• Referral: <b>10%</b> per confirmed deposit\n"
        f"• Profit withdraw: min <b>${MIN_PROFIT_WITHDRAW:.0f}</b> (manual, within 24h)\n"
        f"• Investment withdraw: after <b>{LOCK_DAYS} days</b> + 1 referral in same plan\n"
        "────────────────────────"
    )
    await update.message.reply_html(cover, reply_markup=main_menu())
    await update.message.reply_html(WELCOME, reply_markup=main_menu())

async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = ensure_user(context)
    kb = TERMS_ACCEPT_KB if not profile["terms_accepted"] else main_menu()
    await update.message.reply_markdown(TERMS_TEXT, reply_markup=kb)

async def on_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    profile = ensure_user(context)

    # Accept Terms
    if q.data == "accept_terms":
        profile["terms_accepted"] = True
        await q.message.reply_text("✅ You have accepted the Terms & Conditions. You can use the bot now.",
                                   reply_markup=main_menu())
        return

    # Gate everything behind terms
    if not profile["terms_accepted"]:
        await q.message.reply_text("❌ Please accept Terms & Conditions first.", reply_markup=TERMS_ACCEPT_KB)
        return

    # Existing buttons (kept same reply_* style)
    if q.data == "invest":
        plans = "\n".join([f"• ${p} → 4% daily" for p in PLANS])
        txt = (
            "💸 <b>Invest / Deposit</b>\n\n"
            f"Send USDT (TRC-20) to this address:\n<code>{WALLET_ADDRESS}</code>\n\n"
            f"{plans}\n\n"
            "After sending, return here (verification is manual in this demo)."
        )
        await q.message.reply_html(txt, reply_markup=main_menu())
        return

    if q.data == "mine":
        # demo: bump mining profit a little to simulate daily tap
        profile["mining_profit"] = round(profile["mining_profit"] + 1.00, 2)
        await q.message.reply_text(
            "⛏ Mining started for today. Come back in 24 hours to tap again (demo).",
            reply_markup=main_menu()
        )
        return

    if q.data == "balance":
        dep_days = days_since(profile["deposit_time"])
        bal_txt = (
            "💼 <b>Your Balance</b>\n"
            f"• Active Principal: ${profile['principal']:.2f}\n"
            f"• Mining Profit: ${profile['mining_profit']:.2f}\n"
            f"• Referral Profit: ${profile['referral_profit']:.2f}\n"
            f"• Plan: {profile['plan'] or 'N/A'}\n"
            f"• Days Since Deposit: {dep_days}\n"
        )
        await q.message.reply_html(bal_txt, reply_markup=main_menu())
        return

    if q.data == "ref":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=yourid"
        await q.message.reply_html(
            "👥 <b>Referral</b>\n• Bonus: 10%\n"
            f"• Your link:\n<code>{link}</code>",
            reply_markup=main_menu()
        )
        return

    if q.data == "payout":
        await q.message.reply_html(
            "⚙️ <b>Payout Address</b>\nSend your TRC-20 USDT address in chat (will be requested during withdrawal).",
            reply_markup=main_menu()
        )
        return

    if q.data == "terms":
        kb = TERMS_ACCEPT_KB if not profile["terms_accepted"] else main_menu()
        await q.message.reply_markdown(TERMS_TEXT, reply_markup=kb)
        return

    if q.data == "support":
        await q.message.reply_html(
            f"🛟 <b>Support</b>\nEmail: <code>{SUPPORT_EMAIL}</code>\n"
            f"<a href='mailto:{SUPPORT_EMAIL}'>Open mail app</a>",
            reply_markup=main_menu()
        )
        return

    if q.data == "website":
        await q.message.reply_text("🌐 Website: coming soon.", reply_markup=main_menu())
        return

    if q.data == "wd_status":
        await show_withdraw_status(q, context, profile)
        return

# ────────────────── Withdraw Flow ──────────────────
# Conversation states
ASK_AMOUNT, ASK_ADDRESS = range(2)

async def withdraw_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry via callback: wd_profit or wd_inv"""
    q = update.callback_query
    await q.answer()
    profile = ensure_user(context)

    if not profile["terms_accepted"]:
        await q.message.reply_text("❌ Please accept Terms & Conditions first.", reply_markup=TERMS_ACCEPT_KB)
        return ConversationHandler.END

    wtype = "profit" if q.data == "wd_profit" else "principal"
    context.user_data["wd_flow"] = {"type": wtype}

    title = "💌 Withdraw Profit" if wtype == "profit" else "🏦 Withdraw Investment"
    await q.message.reply_text(f"{title}\n\nPlease enter the *amount in USDT* you want to withdraw:",
                               reply_markup=main_menu(), parse_mode="Markdown")
    return ASK_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = ensure_user(context)
    text = (update.message.text or "").strip()
    try:
        amt = float(text)
    except Exception:
        await update.message.reply_text("❗ Please send a valid number (e.g., 12.5).")
        return ASK_AMOUNT

    if amt <= 0:
        await update.message.reply_text("❗ Amount must be greater than 0.")
        return ASK_AMOUNT

    context.user_data["wd_flow"]["amount"] = round(amt, 2)
    await update.message.reply_text("Great. Now send your *TRC-20 USDT address*:",
                                    parse_mode="Markdown")
    return ASK_ADDRESS

async def withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = ensure_user(context)
    addr = (update.message.text or "").strip()

    if len(addr) < 30:  # very loose check for TRON address length
        await update.message.reply_text("❗ That doesn't look like a valid TRC-20 address. Please send again.")
        return ASK_ADDRESS

    flow = context.user_data.get("wd_flow", {})
    wtype = flow.get("type")
    amount = flow.get("amount")

    # Validate request
    status = "UNDER_PROCESS"
    reason = ""

    if wtype == "profit":
        # Must meet minimum
        if amount < MIN_PROFIT_WITHDRAW:
            status = "REJECTED"
            reason = f"Minimum profit withdrawal is ${MIN_PROFIT_WITHDRAW:.0f}."
        # Must not exceed available profit (demo: mining + referral)
        available = round(profile["mining_profit"] + profile["referral_profit"], 2)
        if status == "UNDER_PROCESS" and amount > available:
            status = "REJECTED"
            reason = f"Requested ${amount:.2f} exceeds available profit ${available:.2f}."

    elif wtype == "principal":
        # Must have principal
        if profile["principal"] <= 0:
            status = "REJECTED"
            reason = "No active principal found."
        # Must be >= 15 days + at least one referral in same plan
        dep_days = days_since(profile["deposit_time"])
        if status == "UNDER_PROCESS" and dep_days < LOCK_DAYS:
            status = "REJECTED"
            reason = f"Principal locked for {LOCK_DAYS} days. Only {dep_days} days passed."
        if status == "UNDER_PROCESS" and not profile["referral_same_plan"]:
            status = "REJECTED"
            reason = "At least one referral in the same plan is required."

        # Amount must not exceed principal
        if status == "UNDER_PROCESS" and amount > profile["principal"]:
            status = "REJECTED"
            reason = f"Requested ${amount:.2f} exceeds principal ${profile['principal']:.2f}."

    # Create request record
    req_id = f"WD{int(time.time()*1000)}"
    record = {
        "id": req_id,
        "type": wtype,
        "amount": round(amount, 2),
        "address": addr,
        "status": status,           # UNDER_PROCESS / APPROVED / REJECTED
        "reason": reason,
        "ts": now_ts(),
    }
    profile["withdrawals"].append(record)

    # Notify user
    if status == "REJECTED":
        await update.message.reply_text(
            f"❌ Withdrawal request *rejected*.\nReason: {reason}\n\nID: {req_id}",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(
            f"✅ Withdrawal request *submitted*.\nStatus: UNDER_PROCESS\n\nID: {req_id}",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )

    # Notify Admin with full details
    await notify_admin_withdraw(update, context, profile, record)

    # Cleanup flow
    context.user_data.pop("wd_flow", None)
    return ConversationHandler.END

async def notify_admin_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict, record: dict):
    u = update.effective_user
    dep_days = days_since(profile["deposit_time"])
    available_profit = round(profile["mining_profit"] + profile["referral_profit"], 2)
    plan_txt = profile["plan"] or "N/A"

    msg = (
        "📥 *Withdraw Request*\n"
        f"*ID:* `{record['id']}`\n"
        f"*Type:* {record['type'].upper()}\n"
        f"*Amount:* ${record['amount']:.2f}\n"
        f"*Address:* `{record['address']}`\n"
        f"*Status:* {record['status']}\n"
        f"*Reason:* {record['reason'] or '-'}\n"
        "────────────────────\n"
        "*User Info*\n"
        f"• ID: `{u.id}`\n"
        f"• Name: {u.full_name}\n"
        f"• Username: @{u.username if u.username else 'N/A'}\n"
        "────────────────────\n"
        "*Account Snapshot*\n"
        f"• Principal: ${profile['principal']:.2f}\n"
        f"• Plan: {plan_txt}\n"
        f"• Days Since Deposit: {dep_days}\n"
        f"• Mining Profit: ${profile['mining_profit']:.2f}\n"
        f"• Referral Profit: ${profile['referral_profit']:.2f}\n"
        f"• Available Profit: ${available_profit:.2f}\n"
        f"• Referral Count: {profile['referral_count']}\n"
        f"• Same-plan Referral: {profile['referral_same_plan']}\n"
    )
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="Markdown")
    except Exception:
        # swallow admin DM errors
        pass

async def show_withdraw_status(q, context: ContextTypes.DEFAULT_TYPE, profile: dict):
    if not profile["withdrawals"]:
        await q.message.reply_text("📥 You have no withdrawal requests yet.", reply_markup=main_menu())
        return
    lines = []
    for r in sorted(profile["withdrawals"], key=lambda x: x["ts"], reverse=True)[:10]:
        dt = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"• {r['id']} — {r['type'].upper()} ${r['amount']:.2f}\n"
            f"  Address: {r['address']}\n"
            f"  Status: {r['status']}{' — ' + r['reason'] if r['reason'] else ''}\n"
            f"  Time: {dt}"
        )
    await q.message.reply_text("📥 *Your Withdrawal Requests:*\n\n" + "\n\n".join(lines),
                               parse_mode="Markdown", reply_markup=main_menu())

# ────────────────── Simple admin actions (optional) ──────────────────
# For demo convenience, you can approve/reject last UNDER_PROCESS from chat (admin only).
async def cmd_admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    # Approve last UNDER_PROCESS for that user id passed as /approve <user_id> <req_id>
    try:
        _, user_id_str, req_id = (update.message.text or "").split(maxsplit=2)
        user_id = int(user_id_str)
    except Exception:
        await update.message.reply_text("Usage: /approve <user_id> <request_id>")
        return
    ud = context.application.user_data.get(user_id, {})
    profile = ud.get("profile")
    if not profile:
        await update.message.reply_text("User/profile not found.")
        return
    for r in profile["withdrawals"]:
        if r["id"] == req_id:
            r["status"] = "APPROVED"
            r["reason"] = ""
            await update.message.reply_text(f"Approved {req_id}.")
            try:
                await context.bot.send_message(chat_id=user_id, text=f"✅ Your withdraw {req_id} has been *APPROVED*.",
                                               parse_mode="Markdown")
            except Exception:
                pass
            return
    await update.message.reply_text("Request not found.")

async def cmd_admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        _, user_id_str, req_id, *reason_parts = (update.message.text or "").split()
        user_id = int(user_id_str)
        reason = " ".join(reason_parts) if reason_parts else "Rejected by admin."
    except Exception:
        await update.message.reply_text("Usage: /reject <user_id> <request_id> [reason]")
        return
    ud = context.application.user_data.get(user_id, {})
    profile = ud.get("profile")
    if not profile:
        await update.message.reply_text("User/profile not found.")
        return
    for r in profile["withdrawals"]:
        if r["id"] == req_id:
            r["status"] = "REJECTED"
            r["reason"] = reason
            await update.message.reply_text(f"Rejected {req_id}.")
            try:
                await context.bot.send_message(chat_id=user_id,
                                               text=f"❌ Your withdraw {req_id} has been *REJECTED*.\nReason: {reason}",
                                               parse_mode="Markdown")
            except Exception:
                pass
            return
    await update.message.reply_text("Request not found.")

# ────────────────── Bootstrap ────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing. Set it in .env")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("terms", cmd_terms))
    app.add_handler(CommandHandler("approve", cmd_admin_approve))
    app.add_handler(CommandHandler("reject", cmd_admin_reject))

    # Withdraw conversation (for both profit & principal)
    wd_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(withdraw_entry, pattern="^(wd_profit|wd_inv)$"),
        ],
        states={
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            ASK_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_address)],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        per_message=True,  # so callback works reliably
    )
    app.add_handler(wd_conv)

    # Other buttons
    app.add_handler(CallbackQueryHandler(on_buttons))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()