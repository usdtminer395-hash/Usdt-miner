import telebot
import datetime
from config import BOT_TOKEN, WALLET_ADDRESS, PLANS, WITHDRAWAL_MIN_PROFIT, INVESTMENT_LOCK_DAYS, WITHDRAW_REFERRAL_REQUIRED

bot = telebot.TeleBot(BOT_TOKEN)

# In-memory data (in production, use a database)
users = {}
investments = {}
referrals = {}

def get_user(user_id):
    if user_id not in users:
        users[user_id] = {"profit": 0.0, "investment": None, "start_date": None, "referrals": []}
    return users[user_id]

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id, 
        "💰 Welcome to the USDT Miner Bot!\n\n"
        "Plans:\n"
        "1️⃣ $10 - 4% daily\n"
        "2️⃣ $50 - 4% daily\n"
        "3️⃣ $100 - 4% daily\n\n"
        f"Deposit to: `{WALLET_ADDRESS}` (TRC20)\n\n"
        "Use /mine daily to activate mining.\n"
        "Use /withdraw to request withdrawals.\n"
        "Use /refer to get your referral link."
    )

@bot.message_handler(commands=['mine'])
def mine_profit(message):
    user = get_user(message.chat.id)
    if not user["investment"]:
        bot.send_message(message.chat.id, "❌ You have no active investment. Please deposit first.")
        return
    # Calculate daily profit
    invested_amount = user["investment"]
    daily_profit = invested_amount * PLANS[invested_amount]
    user["profit"] += daily_profit
    bot.send_message(message.chat.id, f"✅ Mining activated! You earned ${daily_profit:.2f} today.")

@bot.message_handler(commands=['withdraw'])
def withdraw_request(message):
    user = get_user(message.chat.id)
    if user["profit"] < WITHDRAWAL_MIN_PROFIT:
        bot.send_message(message.chat.id, f"❌ Minimum profit withdrawal is ${WITHDRAWAL_MIN_PROFIT}.")
        return
    if user["investment"]:
        # Check lock period
        days_invested = (datetime.datetime.now() - user["start_date"]).days
        if days_invested < INVESTMENT_LOCK_DAYS:
            bot.send_message(message.chat.id, f"⏳ Investment withdrawal is locked for {INVESTMENT_LOCK_DAYS} days.")
            return
        if WITHDRAW_REFERRAL_REQUIRED and len(user["referrals"]) < 1:
            bot.send_message(message.chat.id, "❌ You must have at least 1 referral to withdraw your investment.")
            return
    bot.send_message(message.chat.id, "✅ Withdrawal request sent. You will receive it within 24 hours.")

@bot.message_handler(commands=['refer'])
def refer_link(message):
    bot.send_message(message.chat.id, 
        f"📢 Share this link and earn 10% commission:\n"
        f"https://t.me/{bot.get_me().username}?start={message.chat.id}"
    )

bot.polling()