"""Tradable assets via IBKR by market (starter set; more can be searched via IBKR)"""

MARKETS = [
    {"id": "FX",     "name": "Forex",      "exchange": "IDEALPRO", "tradable": True,  "unit": "units",  "decimals": 5},
    {"id": "STK",    "name": "US Stocks",  "exchange": "SMART",    "tradable": True,  "unit": "shares", "decimals": 2},
    {"id": "CRYPTO", "name": "Crypto",     "exchange": "PAXOS",    "tradable": True,  "unit": "coins",  "decimals": 2},
    {"id": "IDX",    "name": "Indices",    "exchange": "CBOE",     "tradable": False, "unit": "",       "decimals": 2},
]

FX = [
    ("EURUSD", "Euro / US Dollar"), ("GBPUSD", "British Pound / US Dollar"), ("USDJPY", "US Dollar / Japanese Yen"),
    ("AUDUSD", "Australian Dollar / US Dollar"), ("USDCHF", "US Dollar / Swiss Franc"), ("USDCAD", "US Dollar / Canadian Dollar"),
    ("NZDUSD", "New Zealand Dollar / US Dollar"), ("EURGBP", "Euro / British Pound"), ("EURJPY", "Euro / Japanese Yen"),
    ("GBPJPY", "British Pound / Japanese Yen"), ("AUDJPY", "Australian Dollar / Yen"), ("EURAUD", "Euro / Australian Dollar"),
    ("EURCHF", "Euro / Swiss Franc"), ("EURCAD", "Euro / Canadian Dollar"), ("GBPAUD", "Pound / Australian Dollar"),
    ("GBPCAD", "Pound / Canadian Dollar"), ("GBPCHF", "Pound / Swiss Franc"), ("AUDNZD", "Australian / New Zealand Dollar"),
    ("AUDCAD", "Australian / Canadian Dollar"), ("CADJPY", "Canadian Dollar / Yen"), ("CHFJPY", "Swiss Franc / Yen"),
    ("NZDJPY", "New Zealand Dollar / Yen"), ("USDSGD", "US Dollar / Singapore Dollar"), ("USDHKD", "US Dollar / Hong Kong Dollar"),
    ("USDMXN", "US Dollar / Mexican Peso"), ("USDZAR", "US Dollar / South African Rand"), ("USDSEK", "US Dollar / Swedish Krona"),
    ("USDNOK", "US Dollar / Norwegian Krone"), ("USDCNH", "US Dollar / Offshore Yuan"), ("EURNZD", "Euro / New Zealand Dollar"),
]

STK = [
    # ETF
    ("SPY", "SPDR S&P 500 ETF"), ("QQQ", "Invesco QQQ Trust"), ("IWM", "iShares Russell 2000 ETF"), ("DIA", "SPDR Dow Jones ETF"),
    ("VTI", "Vanguard Total Market ETF"), ("VOO", "Vanguard S&P 500 ETF"), ("TLT", "iShares 20+ Year Treasury ETF"),
    ("GLD", "SPDR Gold Shares"), ("SLV", "iShares Silver Trust"), ("USO", "United States Oil Fund"), ("XLF", "Financial Select SPDR"),
    ("XLE", "Energy Select SPDR"), ("XLK", "Technology Select SPDR"), ("ARKK", "ARK Innovation ETF"), ("EEM", "iShares MSCI Emerging"),
    ("EFA", "iShares MSCI EAFE"), ("HYG", "iShares High Yield Bond"), ("VXX", "iPath S&P 500 VIX Short-Term"), ("SOXX", "iShares Semiconductor ETF"),
    ("SMH", "VanEck Semiconductor ETF"), ("IBIT", "iShares Bitcoin Trust"),
    # Mega / large cap tech
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"), ("AMZN", "Amazon"), ("GOOGL", "Alphabet Class A"), ("GOOG", "Alphabet Class C"),
    ("META", "Meta Platforms"), ("TSLA", "Tesla"), ("AVGO", "Broadcom"), ("ORCL", "Oracle"), ("ADBE", "Adobe"), ("CRM", "Salesforce"),
    ("AMD", "Advanced Micro Devices"), ("INTC", "Intel"), ("QCOM", "Qualcomm"), ("TXN", "Texas Instruments"), ("MU", "Micron Technology"),
    ("TSM", "Taiwan Semiconductor ADR"), ("ASML", "ASML Holding ADR"), ("ARM", "Arm Holdings ADR"), ("PLTR", "Palantir"), ("SNOW", "Snowflake"),
    ("NFLX", "Netflix"), ("UBER", "Uber"), ("SHOP", "Shopify"), ("SQ", "Block"), ("PYPL", "PayPal"), ("COIN", "Coinbase"), ("MSTR", "MicroStrategy"),
    ("IBM", "IBM"), ("CSCO", "Cisco"), ("NOW", "ServiceNow"), ("INTU", "Intuit"), ("PANW", "Palo Alto Networks"), ("CRWD", "CrowdStrike"),
    # Finance
    ("JPM", "JPMorgan Chase"), ("BAC", "Bank of America"), ("WFC", "Wells Fargo"), ("GS", "Goldman Sachs"), ("MS", "Morgan Stanley"),
    ("C", "Citigroup"), ("BRK B", "Berkshire Hathaway B"), ("V", "Visa"), ("MA", "Mastercard"), ("AXP", "American Express"), ("BLK", "BlackRock"),
    ("SCHW", "Charles Schwab"), ("IBKR", "Interactive Brokers"),
    # Health
    ("JNJ", "Johnson & Johnson"), ("UNH", "UnitedHealth"), ("LLY", "Eli Lilly"), ("PFE", "Pfizer"), ("MRK", "Merck"), ("ABBV", "AbbVie"),
    ("NVO", "Novo Nordisk ADR"), ("TMO", "Thermo Fisher"), ("ABT", "Abbott"), ("AMGN", "Amgen"), ("ISRG", "Intuitive Surgical"),
    # Consumer / industrial / energy
    ("WMT", "Walmart"), ("COST", "Costco"), ("HD", "Home Depot"), ("MCD", "McDonald's"), ("NKE", "Nike"), ("SBUX", "Starbucks"),
    ("KO", "Coca-Cola"), ("PEP", "PepsiCo"), ("PG", "Procter & Gamble"), ("DIS", "Walt Disney"), ("BA", "Boeing"), ("CAT", "Caterpillar"),
    ("GE", "GE Aerospace"), ("HON", "Honeywell"), ("UPS", "UPS"), ("F", "Ford"), ("GM", "General Motors"), ("XOM", "Exxon Mobil"),
    ("CVX", "Chevron"), ("COP", "ConocoPhillips"), ("T", "AT&T"), ("VZ", "Verizon"), ("LMT", "Lockheed Martin"), ("RTX", "RTX"),
]

CRYPTO = [
    ("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("LTC", "Litecoin"), ("BCH", "Bitcoin Cash"),
    ("SOL", "Solana"), ("LINK", "Chainlink"), ("UNI", "Uniswap"),          # AVAX, DOGE: not on IBKR PAXOS
]

IDX = [
    ("SPX", "S&P 500 Index"), ("NDX", "Nasdaq-100 Index"), ("VIX", "CBOE Volatility Index"), ("DJI", "Dow Jones Industrial"),
    ("RUT", "Russell 2000"), ("DXY", "US Dollar Index"),
]

UNIVERSE = {"FX": FX, "STK": STK, "CRYPTO": CRYPTO, "IDX": IDX}


def default_watchlist():
    return [{"key": f"FX:{s}"} for s, _ in FX[:7]] + [{"key": "STK:SPY"}, {"key": "STK:AAPL"}, {"key": "STK:NVDA"}, {"key": "CRYPTO:BTC"}]
