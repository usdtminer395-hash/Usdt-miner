# Bot configuration file

import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Sensitive data from .env
BOT_TOKEN = os.getenv("BOT_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

# Mining Plans: price and daily profit percentage
PLANS = {
    10: 0.04,   # 4% daily profit
    50: 0.04,
    100: 0.04
}

# Withdrawal rules
WITHDRAWAL_MIN_PROFIT = 10.0  # Minimum profit withdrawal
INVESTMENT_LOCK_DAYS = 15     # Investment lock period
WITHDRAW_REFERRAL_REQUIRED = True  # Require at least 1 referral for investment withdrawal