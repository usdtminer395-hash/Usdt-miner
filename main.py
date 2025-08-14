import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

PLANS = {
    10: 0.04,   # 4% daily profit
    50: 0.04,
    100: 0.04
}

WITHDRAWAL_MIN_PROFIT = 10.0
INVESTMENT_LOCK_DAYS = 15
WITHDRAW_REFERRAL_REQUIRED = True

users = {}
investments = {}
referrals = {}

# ---------------- LOGGING ----------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# ---------------- START COMMAND ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users[user_id] = {"balance": 0, "profit": 0, "referrals": 0}
    
    keyboard = [
        [InlineKeyboardButton("💳 Invest", callback_data="invest"),
         InlineKeyboardButton("📤 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("👥 Referrals", callback_data="referrals"),
         InlineKeyboardButton("ℹ️ Terms", callback_data="terms")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "💎 *USDT Mining Bot* 💎\n"
        "━━━━━━━━━━━━━━━\n"
        "📈 *Plans:*\n"
        "💰 10 USDT → 4% daily\n"
        "💰 50 USDT → 4% daily\n"
        "💰 100 USDT → 4% daily\n\n"
        "⚠️ *Withdraw Rules:*\n"
        f"• Min Profit: {WITHDRAWAL_MIN_PROFIT} USDT\n"
        f"• Lock Period: {INVESTMENT_LOCK_DAYS} days\n"
        "• 1 Referral required\n"
        "━━━━━━━━━━━━━━━"
    )

    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

# ---------------- BUTTON HANDLERS ----------------
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "invest":
        plans_text = "💳 *Choose your investment plan:*\n\n"
        for amount, profit in PLANS.items():
            plans_text += f"💰 {amount} USDT → {profit*100}% daily\n"
        plans_text += f"\nSend your USDT (TRC20) to:\n`{WALLET_ADDRESS}`"

        await query.edit_message_text(plans_text, parse_mode=ParseMode.MARKDOWN)

    elif query.data == "withdraw":
        await query.edit_message_text(
            "📤 *Withdraw Request*\n\n"
            f"Minimum profit to withdraw: {WITHDRAWAL_MIN_PROFIT} USDT\n"
            "Your request will be processed within 24 hours.\n\n"
            "Please send your wallet address and amount to admin.",
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == "referrals":
        user_id = query.from_user.id
        ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
        await query.edit_message_text(
            f"👥 *Your Referrals:*\n"
            f"Total: {users[user_id]['referrals']}\n\n"
            f"🔗 Your link:\n{ref_link}",
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == "terms":
        terms_text = (
            "📜 *Terms & Conditions:*\n\n"
            "We have taken every possible measure to keep this bot secure.\n"
            "However, in case of any technical fault or problem, if you face any financial loss, "
            "the bot administration will not be held responsible.\n\n"
            "Withdrawals will be processed within 24 hours of request."
        )
        await query.edit_message_text(terms_text, parse_mode=ParseMode.MARKDOWN)

# ---------------- MAIN FUNCTION ----------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_click))
    application.run_polling()

if __name__ == "__main__":
    main()