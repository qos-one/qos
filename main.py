import os
from decimal import Decimal

from nautilus_trader.adapters.interactive_brokers.common import IB
from nautilus_trader.adapters.interactive_brokers.config import IBMarketDataTypeEnum
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersDataClientConfig
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersExecClientConfig
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersInstrumentProviderConfig
from nautilus_trader.adapters.interactive_brokers.factories import InteractiveBrokersLiveDataClientFactory
from nautilus_trader.adapters.interactive_brokers.factories import InteractiveBrokersLiveExecClientFactory
from nautilus_trader.config import LiveDataEngineConfig, LoggingConfig, RoutingConfig, TradingNodeConfig
from nautilus_trader.examples.strategies.ema_cross import EMACross, EMACrossConfig
from nautilus_trader.examples.strategies.subscribe import SubscribeStrategy, SubscribeStrategyConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

# ---------- Defaults ----------
IB_HOST = "127.0.0.1"
IB_PORT = 4004                      # paper/demo via the docker gateway
ACCOUNT_ID = os.getenv("IB_ACCOUNT", "")                 # set in this machine's .env (no default in code)
ENABLE_EXEC = os.getenv("ENABLE_EXEC", "0") == "1"
INSTRUMENT = InstrumentId.from_str("EUR/USD.IDEALPRO")

instrument_provider = InteractiveBrokersInstrumentProviderConfig(
    load_ids=frozenset(["EUR/USD.IDEALPRO"]),
)

data_clients = {
    IB: InteractiveBrokersDataClientConfig(
        ibg_host=IB_HOST,
        ibg_port=IB_PORT,
        ibg_client_id=11,
        market_data_type=IBMarketDataTypeEnum.DELAYED,   # demo has no market data subscription
        instrument_provider=instrument_provider,
    ),
}

exec_clients = {}
if ENABLE_EXEC:
    exec_clients = {
        IB: InteractiveBrokersExecClientConfig(
            ibg_host=IB_HOST,
            ibg_port=IB_PORT,
            ibg_client_id=12,
            account_id=ACCOUNT_ID,
            instrument_provider=instrument_provider,
            routing=RoutingConfig(default=True),
        ),
    }

config = TradingNodeConfig(
    trader_id="DEMO-001",
    logging=LoggingConfig(log_level="INFO"),
    data_clients=data_clients,
    exec_clients=exec_clients,
    data_engine=LiveDataEngineConfig(
        time_bars_timestamp_on_close=False,
        validate_data_sequence=True,
    ),
    timeout_connection=90.0,
    timeout_reconciliation=10.0,
    timeout_portfolio=10.0,
    timeout_disconnection=10.0,
    timeout_post_stop=2.0,
)

node = TradingNode(config=config)

# ---------- Pick the strategy for the mode ----------
if ENABLE_EXEC:
    # build 1-minute bars from quote ticks inside Nautilus (INTERNAL), then run EMA cross
    strategy = EMACross(
        config=EMACrossConfig(
            instrument_id=INSTRUMENT,
            bar_type=BarType.from_str("EUR/USD.IDEALPRO-1-MINUTE-MID-INTERNAL"),
            trade_size=Decimal("20000"),
            fast_ema_period=5,
            slow_ema_period=10,
        ),
    )
else:
    strategy = SubscribeStrategy(
        config=SubscribeStrategyConfig(
            instrument_id=INSTRUMENT,
            quote_ticks=True,
            trade_ticks=False,
        ),
    )

node.trader.add_strategy(strategy)
node.add_data_client_factory(IB, InteractiveBrokersLiveDataClientFactory)
if ENABLE_EXEC:
    node.add_exec_client_factory(IB, InteractiveBrokersLiveExecClientFactory)
node.build()

if __name__ == "__main__":
    try:
        node.run()          # stop with Ctrl+C
    finally:
        node.dispose()
