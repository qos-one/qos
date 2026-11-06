"""Market groups for Market Overview and the heatmap (weights are approximate, used to size the boxes)"""

# (symbol, name, weight)
SP500 = [
    ("NVDA", "NVIDIA", 7.5), ("AAPL", "Apple", 6.5), ("MSFT", "Microsoft", 6.2), ("AMZN", "Amazon", 3.8), ("META", "Meta Platforms", 2.9),
    ("GOOGL", "Alphabet A", 2.4), ("GOOG", "Alphabet C", 2.0), ("AVGO", "Broadcom", 2.3), ("TSLA", "Tesla", 1.9), ("BRK B", "Berkshire Hathaway", 1.6),
    ("JPM", "JPMorgan", 1.5), ("LLY", "Eli Lilly", 1.4), ("V", "Visa", 1.1), ("XOM", "Exxon Mobil", 1.0), ("UNH", "UnitedHealth", 0.9),
    ("MA", "Mastercard", 0.9), ("COST", "Costco", 0.9), ("NFLX", "Netflix", 0.8), ("WMT", "Walmart", 0.8), ("PG", "Procter & Gamble", 0.8),
    ("JNJ", "Johnson & Johnson", 0.8), ("HD", "Home Depot", 0.7), ("ABBV", "AbbVie", 0.7), ("BAC", "Bank of America", 0.6), ("CRM", "Salesforce", 0.6),
    ("ORCL", "Oracle", 0.6), ("CVX", "Chevron", 0.6), ("KO", "Coca-Cola", 0.5), ("AMD", "AMD", 0.5), ("MRK", "Merck", 0.5),
    ("PEP", "PepsiCo", 0.5), ("CSCO", "Cisco", 0.5), ("TMO", "Thermo Fisher", 0.5), ("ACN", "Accenture", 0.4), ("LIN", "Linde", 0.4),
    ("MCD", "McDonald's", 0.4), ("ADBE", "Adobe", 0.4), ("ABT", "Abbott", 0.4), ("GE", "GE Aerospace", 0.4), ("PM", "Philip Morris", 0.4),
    ("WFC", "Wells Fargo", 0.4), ("INTU", "Intuit", 0.4), ("CAT", "Caterpillar", 0.4), ("IBM", "IBM", 0.4), ("TXN", "Texas Instruments", 0.4),
    ("QCOM", "Qualcomm", 0.4), ("GS", "Goldman Sachs", 0.4), ("DIS", "Disney", 0.3), ("AMGN", "Amgen", 0.3), ("ISRG", "Intuitive Surgical", 0.3),
    ("NOW", "ServiceNow", 0.3), ("MS", "Morgan Stanley", 0.3), ("BKNG", "Booking", 0.3), ("AXP", "American Express", 0.3), ("NEE", "NextEra Energy", 0.3),
    ("SPGI", "S&P Global", 0.3), ("RTX", "RTX", 0.3), ("UBER", "Uber", 0.3), ("PFE", "Pfizer", 0.3), ("HON", "Honeywell", 0.3),
    ("T", "AT&T", 0.3), ("LOW", "Lowe's", 0.3), ("BLK", "BlackRock", 0.3), ("UNP", "Union Pacific", 0.3), ("PGR", "Progressive", 0.3),
    ("SCHW", "Charles Schwab", 0.3), ("BA", "Boeing", 0.3), ("COP", "ConocoPhillips", 0.3), ("ETN", "Eaton", 0.3), ("LMT", "Lockheed Martin", 0.2),
    ("DE", "Deere", 0.2), ("PLTR", "Palantir", 0.4), ("MU", "Micron", 0.3), ("ANET", "Arista", 0.3), ("PANW", "Palo Alto", 0.2),
    ("SBUX", "Starbucks", 0.2), ("NKE", "Nike", 0.2), ("GM", "General Motors", 0.1), ("F", "Ford", 0.1), ("INTC", "Intel", 0.2),
]

NDX = [
    ("NVDA", "NVIDIA", 9.0), ("AAPL", "Apple", 8.0), ("MSFT", "Microsoft", 7.8), ("AMZN", "Amazon", 5.5), ("AVGO", "Broadcom", 4.5),
    ("META", "Meta Platforms", 3.8), ("TSLA", "Tesla", 3.0), ("GOOGL", "Alphabet A", 2.7), ("GOOG", "Alphabet C", 2.6), ("COST", "Costco", 2.6),
    ("NFLX", "Netflix", 2.4), ("PLTR", "Palantir", 1.6), ("TMUS", "T-Mobile", 1.5), ("CSCO", "Cisco", 1.5), ("AMD", "AMD", 1.4),
    ("LIN", "Linde", 1.3), ("PEP", "PepsiCo", 1.2), ("ISRG", "Intuitive Surgical", 1.1), ("INTU", "Intuit", 1.1), ("ADBE", "Adobe", 1.0),
    ("AMGN", "Amgen", 1.0), ("QCOM", "Qualcomm", 1.0), ("TXN", "Texas Instruments", 1.0), ("BKNG", "Booking", 0.9), ("HON", "Honeywell", 0.8),
    ("CMCSA", "Comcast", 0.8), ("AMAT", "Applied Materials", 0.8), ("PANW", "Palo Alto", 0.7), ("MU", "Micron", 0.7), ("ADP", "ADP", 0.7),
    ("GILD", "Gilead", 0.7), ("VRTX", "Vertex", 0.6), ("SBUX", "Starbucks", 0.6), ("LRCX", "Lam Research", 0.6), ("MELI", "MercadoLibre", 0.6),
    ("ADI", "Analog Devices", 0.6), ("INTC", "Intel", 0.6), ("CRWD", "CrowdStrike", 0.6), ("KLAC", "KLA", 0.5), ("CTAS", "Cintas", 0.5),
    ("MDLZ", "Mondelez", 0.5), ("REGN", "Regeneron", 0.5), ("MAR", "Marriott", 0.4), ("ORLY", "O'Reilly", 0.4), ("CEG", "Constellation Energy", 0.4),
    ("ARM", "Arm Holdings", 0.4), ("SNPS", "Synopsys", 0.4), ("CDNS", "Cadence", 0.4), ("ABNB", "Airbnb", 0.3), ("PYPL", "PayPal", 0.3),
]

DOW = [(s, n, 1.0) for s, n in [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"), ("AMZN", "Amazon"), ("JPM", "JPMorgan"), ("V", "Visa"), ("UNH", "UnitedHealth"),
    ("HD", "Home Depot"), ("PG", "Procter & Gamble"), ("JNJ", "Johnson & Johnson"), ("CAT", "Caterpillar"), ("GS", "Goldman Sachs"), ("MCD", "McDonald's"),
    ("CRM", "Salesforce"), ("AXP", "American Express"), ("IBM", "IBM"), ("HON", "Honeywell"), ("TRV", "Travelers"), ("AMGN", "Amgen"), ("CVX", "Chevron"),
    ("SHW", "Sherwin-Williams"), ("MRK", "Merck"), ("CSCO", "Cisco"), ("DIS", "Disney"), ("KO", "Coca-Cola"), ("WMT", "Walmart"), ("MMM", "3M"),
    ("BA", "Boeing"), ("NKE", "Nike"), ("VZ", "Verizon"),
]]

RUSSELL = [(s, n, 1.0) for s, n in [
    ("IWM", "Russell 2000 ETF"), ("IWN", "Russell 2000 Value"), ("IWO", "Russell 2000 Growth"), ("VTWO", "Vanguard Russell 2000"),
    ("XBI", "Biotech ETF"), ("KRE", "Regional Banks ETF"), ("IJR", "S&P SmallCap 600"), ("VB", "Vanguard Small-Cap"),
]]

GOLD = [
    ("GLD", "SPDR Gold", 6.0), ("IAU", "iShares Gold", 3.0), ("GDX", "Gold Miners ETF", 3.0), ("NEM", "Newmont", 2.5), ("GOLD", "Barrick", 2.0),
    ("AEM", "Agnico Eagle", 2.0), ("FNV", "Franco-Nevada", 1.5), ("WPM", "Wheaton Precious", 1.5), ("KGC", "Kinross", 1.0), ("AU", "AngloGold", 1.0),
    ("SLV", "iShares Silver", 2.0), ("PAAS", "Pan American Silver", 0.8), ("HL", "Hecla", 0.5), ("RGLD", "Royal Gold", 0.8),
]

ENERGY = [
    ("XLE", "Energy Select SPDR", 4.0), ("XOM", "Exxon Mobil", 4.0), ("CVX", "Chevron", 3.0), ("COP", "ConocoPhillips", 2.0), ("EOG", "EOG Resources", 1.2),
    ("SLB", "Schlumberger", 1.2), ("MPC", "Marathon Petroleum", 1.0), ("PSX", "Phillips 66", 1.0), ("VLO", "Valero", 1.0), ("OXY", "Occidental", 1.0),
    ("WMB", "Williams", 0.9), ("KMI", "Kinder Morgan", 0.9), ("HAL", "Halliburton", 0.7), ("DVN", "Devon", 0.6), ("USO", "US Oil Fund", 1.5), ("UNG", "US Nat Gas Fund", 0.8),
]

BONDS = [
    ("TLT", "20+ Yr Treasury", 3.0), ("IEF", "7-10 Yr Treasury", 2.5), ("SHY", "1-3 Yr Treasury", 2.0), ("BND", "Total Bond Market", 3.0),
    ("AGG", "US Aggregate Bond", 3.0), ("LQD", "Inv Grade Corporate", 2.0), ("HYG", "High Yield", 2.0), ("TIP", "TIPS", 1.5), ("MBB", "Mortgage-Backed", 1.0),
]

CRYPTO = [("BTC", "Bitcoin", 6.0), ("ETH", "Ethereum", 3.0), ("SOL", "Solana", 1.2), ("LTC", "Litecoin", 0.6), ("BCH", "Bitcoin Cash", 0.5),
          ("AVAX", "Avalanche", 0.5), ("LINK", "Chainlink", 0.6), ("UNI", "Uniswap", 0.4), ("DOGE", "Dogecoin", 0.8)]

FOREX = [("EURUSD", "EUR/USD", 3.0), ("USDJPY", "USD/JPY", 2.5), ("GBPUSD", "GBP/USD", 2.0), ("AUDUSD", "AUD/USD", 1.5), ("USDCAD", "USD/CAD", 1.5),
         ("USDCHF", "USD/CHF", 1.2), ("NZDUSD", "NZD/USD", 1.0), ("EURGBP", "EUR/GBP", 1.0), ("EURJPY", "EUR/JPY", 1.0), ("GBPJPY", "GBP/JPY", 0.8),
         ("USDCNH", "USD/CNH", 0.8), ("USDMXN", "USD/MXN", 0.6), ("USDSGD", "USD/SGD", 0.5), ("USDSEK", "USD/SEK", 0.5), ("USDNOK", "USD/NOK", 0.5)]

VOL = [("VIX", "VIX Index", 2.0), ("VXX", "VIX Short-Term ETN", 1.0), ("UVXY", "Ultra VIX", 0.8), ("SVXY", "Short VIX", 0.8)]

# id, name, subtitle, proxy key, members' market, members
GROUPS = [
    {"id": "SP500",   "name": "S&P 500",        "subtitle": "US large cap · top 80 by weight", "proxy": "IDX:SPX", "alt_proxy": "STK:SPY", "market": "STK",    "members": SP500},
    {"id": "NDX",     "name": "Nasdaq 100",     "subtitle": "US tech & growth · top 50",      "proxy": "STK:QQQ", "alt_proxy": "IDX:NDX", "market": "STK",    "members": NDX},
    {"id": "DOW",     "name": "Dow 30",         "subtitle": "US blue chips",                  "proxy": "STK:DIA", "alt_proxy": "IDX:DJI", "market": "STK",    "members": DOW},
    {"id": "RUSSELL", "name": "Russell 2000",   "subtitle": "US small cap ETFs",              "proxy": "STK:IWM", "alt_proxy": None,      "market": "STK",    "members": RUSSELL},
    {"id": "GOLD",    "name": "Gold & Metals",  "subtitle": "bullion ETFs and miners",        "proxy": "STK:GLD", "alt_proxy": None,      "market": "STK",    "members": GOLD},
    {"id": "ENERGY",  "name": "Energy",         "subtitle": "oil, gas and producers",         "proxy": "STK:XLE", "alt_proxy": None,      "market": "STK",    "members": ENERGY},
    {"id": "BONDS",   "name": "Bonds",          "subtitle": "US treasuries and credit",       "proxy": "STK:TLT", "alt_proxy": None,      "market": "STK",    "members": BONDS},
    {"id": "CRYPTO",  "name": "Crypto",         "subtitle": "PAXOS spot",                     "proxy": "CRYPTO:BTC", "alt_proxy": None,   "market": "CRYPTO", "members": CRYPTO},
    {"id": "FOREX",   "name": "Forex",          "subtitle": "major and minor pairs",          "proxy": "FX:EURUSD", "alt_proxy": None,    "market": "FX",     "members": FOREX},
    {"id": "VOL",     "name": "Volatility",     "subtitle": "VIX and volatility products",    "proxy": "IDX:VIX", "alt_proxy": None,      "market": "STK",    "members": VOL},
]
GROUP_BY_ID = {g["id"]: g for g in GROUPS}
