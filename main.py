import os, json, time, threading, math, logging, csv, io
from uuid import uuid4
from datetime import datetime
import requests

# Optional: load .env locally; on Render/host, use real env vars
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext

# ---------------------- CONFIG ----------------------
PLANS = [10, 50, 100]                 # USDT (TRC20)
DAILY_RATE = 0.04                     # 4%/day (when user taps "Start Mining" every 24h)
USDT_CONTRACT = os.getenv("USDT_CONTRACT", "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj")  # TRC20 USDT
# Your deposit (receiving) TRC-20 address (env wins; otherwise your provided default)
MERCHANT_TRON_ADDRESS = os.getenv("MERCHANT_TRON_ADDRESS", "TXRu4QXGhgMtqNF8NaPLSkDU6GPFRnPyA1").strip()
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()

# Bot token (use .env locally; or set Render env var)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE").strip()

# Comma-separated Telegram user IDs who are admins, e.g. "12345678,87654321"
ADMIN_IDS = [s.strip() for s in os.getenv("ADMIN_IDS", "").split(",") if s.strip()]

# External config (social links & gating)
CONFIG_FILE = "config.json"

DATA_FILE = "data.json"
logging.basicConfig(level=logging.INFO)

DEFAULT_TERMS = (
    "Terms & Conditions\n\n"
    "1) We have taken all possible measures to make this bot secure. However, in the event of any technical fault "
    "or any other issue resulting in a financial loss, the bot administration will not be held responsible.\n\n"
    "2) Profit withdrawals are only available when your profit balance is at least $10.\n\n"
    "3) Investment withdrawals are available only after a 15-day lock period AND if you have at least one referral "
    "in the same investment plan.\n\n"
    "4) All withdrawal requests will be processed within 24 hours (manual payouts).\n\n"
    "5) Deposits must be sent to the TRC-20 USDT address shown inside the bot. Using the wrong network or address "
    "may result in loss of funds and is solely the user's responsibility.\n\n"
    "6) The mining button must be pressed every 24 hours to collect daily profit (4% of active principal).\n\n"
    "7) Referral bonus: 10% on confirmed deposits of your referrals.\n\n"
    "8) The platform may update rules at any time. The latest version will be available via /terms.\n\n"
    "By using this bot, you agree to all the above terms and conditions."
)

DEFAULT_CONFIG = {
    "REQUIRE_SOCIAL_TASK": False,
    "SOCIAL_LINKS": {
        "website": "",
        "telegram": "",
        "twitter": "",
        "facebook": "",
        "youtube": ""
    }
}

# ------------------- PERSISTENCE --------------------
def now_ts() -> int:
    return int(time.time())

def load_db():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "tx_seen": [], "withdraw_queue": [], "terms": DEFAULT_TERMS}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                # merge defaults
                merged = DEFAULT_CONFIG.copy()
                merged.update(cfg)
                if "SOCIAL_LINKS" in cfg:
                    merged_links = DEFAULT_CONFIG["SOCIAL_LINKS"].copy()
                    merged_links.update(cfg["SOCIAL_LINKS"])
                    merged["SOCIAL_LINKS"] = merged_links
                return merged
        except Exception as e:
            logging.exception(e)
    return DEFAULT_CONFIG.copy()

DB = load_db()
CONFIG = load_config()
SAVE_LOCK = threading.Lock()

def with_save(fn):
    def wrap(*a, **k):
        res = fn(*a, **k)
        with SAVE_LOCK:
            save_db()
        return res
    return wrap

# ---------------------- MODELS ----------------------
def ensure_user(tg_user, ref_code=None):
    uid = str(tg_user.id)
    u = DB["users"].get(uid)
    if not u:
        u = {
            "id": uid,
            "name": tg_user.full_name,
            "username": tg_user.username,
            "created_at": now_ts(),
            "referrer": ref_code if ref_code and ref_code != uid else None,
            "referrals": [],
            "payout_address": "",
            "balances": {"profit": 0.0, "referral": 0.0},
            "investments": [],                 # {id, plan, amount, start_ts, lock_until, active, txid}
            "last_mine_ts": 0,
            "referrals_by_plan": {},           # {"10": count, "50": count, "100": count}
            "accepted_terms": False,
            "social_done": False               # set true when user confirms completing social tasks
        }
        DB["users"][uid] = u
        # attach to referrer
        if u["referrer"] and DB["users"].get(u["referrer"]):
            DB["users"][u["referrer"]]["referrals"].append(uid)
    return u

def active_principal(u) -> float:
    return sum(float(inv["amount"]) for inv in u["investments"] if inv.get("active", True))

def active_principal_by_plan(u, plan) -> float:
    return sum(float(inv["amount"]) for inv in u["investments"]
               if inv.get("active", True) and int(inv["plan"]) == int(plan))

# ------------------- TRON HELPERS -------------------
def _headers():
    h = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        h["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return h

def get_usdt_transfer_by_tx(txid: str):
    """
    Look up a transaction's events on TronGrid, find a USDT Transfer to our address.
    Return dict {from,to,amount,confirmed} if found; otherwise None.
    """
    try:
        url = f"https://api.trongrid.io/v1/transactions/{txid}/events"
        r = requests.get(url, headers=_headers(), timeout=20)
        if r.status_code != 200:
            return None
        events = r.json().get("data", [])
        for e in events:
            if e.get("event_name") == "Transfer" and e.get("contract") == USDT_CONTRACT:
                fr = e["result"].get("from")
                to = e["result"].get("to")
                amount = float(e["result"].get("value", 0)) / 1_000_000.0
                confirmed = bool(e.get("block_timestamp"))
                return {"from": fr, "to": to, "amount": amount, "confirmed": confirmed}
        return None
    except Exception as ex:
        logging.exception(ex)
        return None

# ---------------------- UI --------------------------
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Invest / Deposit", callback_data="invest"),
         InlineKeyboardButton("⛏ Start Mining", callback_data="mine")],
        [InlineKeyboardButton("💰 Balance", callback_data="balance"),
         InlineKeyboardButton("👥 Referral", callback_data="ref")],
        [InlineKeyboardButton("📤 Withdraw Profit", callback_data="wd_profit"),
         InlineKeyboardButton("🏦 Withdraw Investment", callback_data="wd_invest")],
        [InlineKeyboardButton("⚙️ Set Payout Address", callback_data="set_addr"),
         InlineKeyboardButton("📘 Rules", callback_data="rules")]
    ])

def fmt(x): return f"${x:,.2f}"

def rules_text():
    return (
        "📘 Rules\n"
        f"• Plans: $10 / $50 / $100 (USDT-TRC20)\n"
        f"• Mining: 4% daily (tap ⛏ every 24 hours)\n"
        f"• Referral: 10% on confirmed deposits\n"
        f"• Profit Withdrawal: minimum $10 — manual queue (processed within 24h)\n"
        f"• Investment Withdrawal: manual queue\n"
        f"  1) 15-day lock\n"
        f"  2) After 15 days, at least 1 referral in the same plan required\n"
        f"• Deposit Address: `{MERCHANT_TRON_ADDRESS}` (TRC-20)\n"
        f"• Full Terms: /terms\n"
        f"• Social: /social"
    )

def is_admin(uid: str) -> bool:
    return str(uid) in ADMIN_IDS

def require_terms(u, query):
    if u.get("accepted_terms"):
        return False
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I Agree", callback_data="agree_terms")],
        [InlineKeyboardButton("📘 Read Terms", callback_data="show_terms")]
    ])
    query.edit_message_text(
        "Before continuing, please accept the Terms & Conditions.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    return True

def require_social(u, query):
    if not CONFIG.get("REQUIRE_SOCIAL_TASK", False):
        return False
    if u.get("social_done", False):
        return False
    # Show social panel
    buttons = []
    links = CONFIG.get("SOCIAL_LINKS", {})
    if links.get("website"):  buttons.append([InlineKeyboardButton("🌐 Open Website", url=links["website"])])
    if links.get("telegram"): buttons.append([InlineKeyboardButton("📣 Join Telegram", url=links["telegram"])])
    if links.get("twitter"):  buttons.append([InlineKeyboardButton("🐦 Follow Twitter/X", url=links["twitter"])])
    if links.get("facebook"): buttons.append([InlineKeyboardButton("👍 Like Facebook", url=links["facebook"])])
    if links.get("youtube"):  buttons.append([InlineKeyboardButton("▶️ Subscribe YouTube", url=links["youtube"])])
    buttons.append([InlineKeyboardButton("✅ I have completed the task", callback_data="social_done")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
    query.edit_message_text(
        "To continue, please complete the social task(s) below, then confirm:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return True

# -------------------- HANDLERS ----------------------
@with_save
def start(update: Update, context: CallbackContext):
    ref = context.args[0].strip() if context.args else None
    u = ensure_user(update.effective_user, ref)
    link = f"https://t.me/{context.bot.username}?start={u['id']}"
    update.message.reply_text(
        f"Welcome *{u['name']}*!\n"
        f"Your referral link:\n`{link}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=menu()
    )

def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(rules_text(), parse_mode=ParseMode.MARKDOWN)

def terms_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(DB.get("terms", DEFAULT_TERMS), parse_mode=ParseMode.MARKDOWN)

def social_cmd(update: Update, context: CallbackContext):
    u = ensure_user(update.effective_user)
    links = CONFIG.get("SOCIAL_LINKS", {})
    parts = ["Social Links"]
    for k, label in [("website","Website"),("telegram","Telegram"),("twitter","Twitter/X"),
                     ("facebook","Facebook"),("youtube","YouTube")]:
        if links.get(k):
            parts.append(f"• {label}: {links[k]}")
    txt = "\n".join(parts) if len(parts) > 1 else "No social links configured yet."
    # Provide quick action buttons too
    buttons = []
    if links.get("website"):  buttons.append([InlineKeyboardButton("🌐 Open Website", url=links["website"])])
    if links.get("telegram"): buttons.append([InlineKeyboardButton("📣 Join Telegram", url=links["telegram"])])
    if links.get("twitter"):  buttons.append([InlineKeyboardButton("🐦 Follow Twitter/X", url=links["twitter"])])
    if links.get("facebook"): buttons.append([InlineKeyboardButton("👍 Like Facebook", url=links["facebook"])])
    if links.get("youtube"):  buttons.append([InlineKeyboardButton("▶️ Subscribe YouTube", url=links["youtube"])])
    if CONFIG.get("REQUIRE_SOCIAL_TASK", False) and not u.get("social_done", False):
        buttons.append([InlineKeyboardButton("✅ I have completed the task", callback_data="social_done")])
    update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

def btn(update: Update, context: CallbackContext):
    q = update.callback_query
    q.answer()
    uid = str(q.from_user.id)
    u = ensure_user(q.from_user)

    if q.data == "rules":
        q.edit_message_text(rules_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=menu()); return

    if q.data == "show_terms":
        q.edit_message_text(DB.get("terms", DEFAULT_TERMS), parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ I Agree", callback_data="agree_terms")],
                                                               [InlineKeyboardButton("🔙 Back", callback_data="back")]])); return

    if q.data == "agree_terms":
        u["accepted_terms"] = True
        with SAVE_LOCK: save_db()
        q.edit_message_text("✅ You accepted the Terms & Conditions.", reply_markup=menu()); return

    if q.data == "social_done":
        u["social_done"] = True
        with SAVE_LOCK: save_db()
        q.edit_message_text("✅ Social task confirmed. You can continue.", reply_markup=menu()); return

    if q.data == "balance":
        txt = (f"💼 *Balances*\n"
               f"• Active Principal: {fmt(active_principal(u))}\n"
               f"• Profit: {fmt(u['balances']['profit'])}\n"
               f"• Referral: {fmt(u['balances']['referral'])}\n"
               f"• Payout Address: `{u['payout_address'] or 'Not set'}`\n")
        q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=menu()); return

    if q.data == "set_addr":
        context.user_data["await_addr"] = True
        q.edit_message_text("Send your TRC-20 USDT payout address:", parse_mode=ParseMode.MARKDOWN); return

    if q.data == "invest":
        if require_terms(u, q): return
        if require_social(u, q): return
        k = InlineKeyboardMarkup([[InlineKeyboardButton(f"${p}", callback_data=f"plan_{p}") for p in PLANS],
                                  [InlineKeyboardButton("🔙 Back", callback_data="back")]])
        q.edit_message_text(
            f"Choose a plan and then send the *TXID*.\nDeposit Address:\n`{MERCHANT_TRON_ADDRESS}`\n*Network: TRC-20*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=k); return

    if q.data.startswith("plan_"):
        if require_terms(u, q): return
        if require_social(u, q): return
        plan = int(q.data.split("_")[1])
        context.user_data["await_tx_for_plan"] = plan
        q.edit_message_text(
            f"Selected *${plan}* plan.\nFirst send USDT to:\n`{MERCHANT_TRON_ADDRESS}`\n"
            "Then paste your *TXID/Hash* here so the bot can auto-verify.",
            parse_mode=ParseMode.MARKDOWN); return

    if q.data == "mine":
        if require_terms(u, q): return
        last = u.get("last_mine_ts", 0)
        if now_ts() - last < 24*3600:
            remain = 24*3600 - (now_ts() - last)
            hrs = math.ceil(remain/3600)
            q.edit_message_text(f"⏳ You've already mined today. Try again in ~{hrs} hour(s).", reply_markup=menu()); return
        principal = active_principal(u)
        if principal <= 0:
            q.edit_message_text("No active investment. Please deposit first.", reply_markup=menu()); return
        gain = round(principal * DAILY_RATE, 2)
        u["balances"]["profit"] += gain
        u["last_mine_ts"] = now_ts()
        with SAVE_LOCK: save_db()
        q.edit_message_text(f"✅ Mining complete! Today's profit: {fmt(gain)}\nTotal Profit: {fmt(u['balances']['profit'])}",
                            reply_markup=menu()); return

    if q.data == "ref":
        link = f"https://t.me/{context.bot.username}?start={u['id']}"
        q.edit_message_text(f"👥 *Referral*\nLink:\n`{link}`\n"
                            f"Total referrals: {len(u['referrals'])}\n"
                            "You earn 10% on each confirmed deposit.",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=menu()); return

    if q.data == "wd_profit":
        if require_terms(u, q): return
        if require_social(u, q): return
        amt = u["balances"]["profit"]
        if amt < 10:
            q.edit_message_text("Minimum profit withdrawal is $10.", reply_markup=menu()); return
        if not u["payout_address"]:
            q.edit_message_text("Set your payout address first (⚙️ Set Payout Address).", reply_markup=menu()); return
        req_id = str(uuid4())[:8]
        DB["withdraw_queue"].append({
            "id": req_id, "user_id": u["id"], "type": "profit",
            "amount": round(amt, 2), "to": u["payout_address"],
            "status": "pending", "created_ts": now_ts()
        })
        u["balances"]["profit"] = 0.0
        with SAVE_LOCK: save_db()
        q.edit_message_text(f"📤 Profit withdrawal request queued.\nRequest ID: `{req_id}`\n"
                            "Payout will be processed within 24 hours.",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=menu()); return

    if q.data == "wd_invest":
        if require_terms(u, q): return
        if require_social(u, q): return
        lines = ["🏦 *Investment Withdrawal Eligibility*"]
        ok_any = False
        for plan in PLANS:
            principal_plan = active_principal_by_plan(u, plan)
            if principal_plan <= 0:
                lines.append(f"• ${plan}: no active investment"); continue
            ok_lock = any((inv.get("active", True) and int(inv["plan"]) == plan and now_ts() >= inv["lock_until"])
                          for inv in u["investments"])
            refs_plan = u["referrals_by_plan"].get(str(plan), 0)
            ok_ref = refs_plan >= 1
            status = "✅" if (ok_lock and ok_ref) else "❌"
            lines.append(f"• ${plan}: {status} (Lock: {'OK' if ok_lock else 'Not yet'} | Referral: {refs_plan}/1)")
            if ok_lock and ok_ref: ok_any = True
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back")]]
        if ok_any:
            kb.insert(0, [InlineKeyboardButton("Request Withdraw", callback_data="req_wd_inv")])
        q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)); return

    if q.data == "req_wd_inv":
        context.user_data["await_inv_withdraw_plan"] = True
        q.edit_message_text("Which plan to withdraw? Send: 10 / 50 / 100"); return

    if q.data == "back":
        q.edit_message_text("Main menu:", reply_markup=menu()); return

def text_handler(update: Update, context: CallbackContext):
    u = ensure_user(update.effective_user)

    # Set payout address
    if context.user_data.get("await_addr"):
        addr = update.message.text.strip()
        u["payout_address"] = addr
        context.user_data.pop("await_addr", None)
        with SAVE_LOCK: save_db()
        update.message.reply_text(f"✅ Payout address saved:\n`{addr}`", parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
        return

    # TXID after selecting a plan
    if "await_tx_for_plan" in context.user_data:
        plan = int(context.user_data["await_tx_for_plan"])
        txid = update.message.text.strip()
        update.message.reply_text("🔎 Verifying transaction…")
        info = get_usdt_transfer_by_tx(txid)
        if not info:
            update.message.reply_text("⚠️ USDT transfer not found. Please provide a valid TXID.", reply_markup=menu()); return
        if info["to"] != MERCHANT_TRON_ADDRESS:
            update.message.reply_text("⚠️ The funds were not sent to our deposit address.", reply_markup=menu()); return
        if info["amount"] + 0.000001 < plan:
            update.message.reply_text(f"⚠️ Amount is insufficient. For this plan you must send at least ${plan}.", reply_markup=menu()); return
        if txid in DB.get("tx_seen", []):
            update.message.reply_text("⚠️ This TXID has already been used.", reply_markup=menu()); return

        DB.setdefault("tx_seen", []).append(txid)
        inv = {
            "id": str(uuid4()),
            "plan": plan,
            "amount": float(plan),
            "start_ts": now_ts(),
            "lock_until": now_ts() + 15*24*3600,
            "active": True,
            "txid": txid
        }
        u["investments"].append(inv)

        # Referral 10%
        ref = u.get("referrer")
        if ref and DB["users"].get(ref):
            DB["users"][ref]["balances"]["referral"] = round(DB["users"][ref]["balances"]["referral"] + plan * 0.10, 2)
            DB["users"][ref]["referrals_by_plan"][str(plan)] = DB["users"][ref]["referrals_by_plan"].get(str(plan), 0) + 1

        with SAVE_LOCK: save_db()
        context.user_data.pop("await_tx_for_plan", None)
        update.message.reply_text(
            f"✅ Investment activated: ${plan}\nLock: 15 days | TXID: `{txid}`\n"
            "Remember to tap ⛏ Start Mining every 24 hours.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=menu()
        )
        return

    # Investment withdraw flow: user sends plan number
    if context.user_data.get("await_inv_withdraw_plan"):
        msg = update.message.text.strip()
        if msg not in ["10", "50", "100"]:
            update.message.reply_text("Please send 10 or 50 or 100."); return
        plan = int(msg)
        principal_plan = active_principal_by_plan(u, plan)
        if principal_plan <= 0:
            update.message.reply_text("No active investment in this plan.", reply_markup=menu())
            context.user_data.pop("await_inv_withdraw_plan", None)
            return
        ok_lock = any((inv.get("active", True) and int(inv["plan"]) == plan and now_ts() >= inv["lock_until"])
                      for inv in u["investments"])
        refs_plan = u["referrals_by_plan"].get(str(plan), 0)
        ok_ref = refs_plan >= 1
        if not ok_lock or not ok_ref:
            update.message.reply_text("Eligibility not met: 15-day lock + 1 referral in the same plan required.", reply_markup=menu())
            context.user_data.pop("await_inv_withdraw_plan", None)
            return
        if not u["payout_address"]:
            update.message.reply_text("Set your payout address first.", reply_markup=menu())
            context.user_data.pop("await_inv_withdraw_plan", None)
            return

        # deactivate all active investments in this plan
        for inv in u["investments"]:
            if inv.get("active", True) and int(inv["plan"]) == plan:
                inv["active"] = False
        with SAVE_LOCK: save_db()

        # queue the principal for manual payout
        req_id = str(uuid4())[:8]
        DB["withdraw_queue"].append({
            "id": req_id, "user_id": u["id"], "type": "principal",
            "amount": round(principal_plan, 2), "to": u["payout_address"],
            "status": "pending", "created_ts": now_ts(), "plan": plan
        })
        with SAVE_LOCK: save_db()
        update.message.reply_text(
            f"📤 Investment withdrawal request queued: {fmt(principal_plan)}\n"
            f"Request ID: `{req_id}`\nPayout will be processed within 24 hours.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=menu()
        )
        context.user_data.pop("await_inv_withdraw_plan", None)
        return

    # Fallback
    update.message.reply_text("Main menu:", reply_markup=menu())

# -------------------- ADMIN -------------------------
def admin_only(fn):
    def wrap(update: Update, context: CallbackContext, *a, **k):
        uid = str(update.effective_user.id)
        if not is_admin(uid):
            update.message.reply_text("⛔ Admin only.")
            return
        return fn(update, context, *a, **k)
    return wrap

@admin_only
def admin_cmd(update: Update, context: CallbackContext):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Queue", callback_data="ad_queue"),
         InlineKeyboardButton("📤 Export CSV", callback_data="ad_export")],
        [InlineKeyboardButton("📝 Set Terms", callback_data="ad_set_terms")],
        [InlineKeyboardButton("🔄 Reload Config", callback_data="ad_reload_cfg")]
    ])
    update.message.reply_text("Admin Panel:", reply_markup=kb)

def admin_btn(update: Update, context: CallbackContext):
    q = update.callback_query
    q.answer()
    uid = str(q.from_user.id)
    if not is_admin(uid):
        q.answer("Admin only", show_alert=True); return

    if q.data == "ad_queue":
        rows = [r for r in DB.get("withdraw_queue", []) if r["status"] == "pending"]
        if not rows:
            q.edit_message_text("Queue is empty."); return
        lines = ["Pending Requests:"]
        for r in rows[:50]:
            created = datetime.utcfromtimestamp(r['created_ts']).strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f"{r['id']} | {r['type']} | {fmt(r['amount'])} → `{r['to']}` "
                         f"(user {r['user_id']}) | {created} UTC")
        q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN); return

    if q.data == "ad_export":
        rows = DB.get("withdraw_queue", [])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id","user_id","type","amount","to","status","created_ts","plan"])
        for r in rows:
            w.writerow([r.get("id"), r.get("user_id"), r.get("type"), r.get("amount"),
                        r.get("to"), r.get("status"), r.get("created_ts"), r.get("plan")])
        data = buf.getvalue().encode("utf-8")
        q.message.reply_document(document=data, filename="withdraw_queue.csv", caption="Withdraw Queue CSV")
        q.edit_message_text("CSV sent."); return

    if q.data == "ad_set_terms":
        context.user_data["await_terms"] = True
        q.edit_message_text("Reply with the new *Terms & Conditions* (Markdown allowed).",
                            parse_mode=ParseMode.MARKDOWN); return

    if q.data == "ad_reload_cfg":
        global CONFIG
        CONFIG = load_config()
        q.edit_message_text("✅ Config reloaded from config.json."); return

@admin_only
def mark_paid_cmd(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /mark_paid <RequestID>")
        return
    reqid = context.args[0].strip()
    for r in DB.get("withdraw_queue", []):
        if r["id"] == reqid:
            r["status"] = "paid"
            with SAVE_LOCK: save_db()
            update.message.reply_text(f"✅ Marked PAID: {reqid}")
            return
    update.message.reply_text("Request ID not found.")

@admin_only
def set_terms_cmd(update: Update, context: CallbackContext):
    context.user_data["await_terms"] = True
    update.message.reply_text("Reply with the new *Terms & Conditions* (Markdown allowed).",
                              parse_mode=ParseMode.MARKDOWN)

def admin_router(update: Update, context: CallbackContext):
    # Capture new Terms text when admin replies after /set_terms or admin panel button
    uid = str(update.effective_user.id)
    if is_admin(uid) and context.user_data.get("await_terms"):
        DB["terms"] = update.message.text
        context.user_data.pop("await_terms", None)
        with SAVE_LOCK: save_db()
        update.message.reply_text("✅ Terms updated.")
        return
    # Otherwise, hand over to main text handler
    text_handler(update, context)

# -------------------- BOOTSTRAP ---------------------
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN missing or placeholder. Put your real token in .env or env vars.")
    if not MERCHANT_TRON_ADDRESS:
        raise RuntimeError("MERCHANT_TRON_ADDRESS missing (env or default)")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Public commands
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("terms", terms_cmd))
    dp.add_handler(CommandHandler("social", social_cmd))

    # Admin commands
    dp.add_handler(CommandHandler("admin", admin_cmd))
    dp.add_handler(CommandHandler("mark_paid", mark_paid_cmd))
    dp.add_handler(CommandHandler("set_terms", set_terms_cmd))

    # Buttons
    dp.add_handler(CallbackQueryHandler(admin_btn, pattern="^ad_"))
    dp.add_handler(CallbackQueryHandler(btn))

    # Messages (admin router first so it intercepts replies to set_terms)
    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), admin_router))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

import config
import database
import utils