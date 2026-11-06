"""
Runs a NautilusTrader TradingNode as a separate process (started by the API via nautilus_bridge)
Usage: uv run nautilus_runner.py --config '<json>'
config: {"strategy":"ema_cross","instrument":"EUR/USD.IDEALPRO","tf_minutes":1,"fast":10,"slow":20,"trade_size":"20000",
         "sl_pips":0,"tp_pips":0,"account_id":"DU…","host":"127.0.0.1","port":4004,"client_id":100}
       Strategy comes from qx_strategies (same as scripts/backtest.py) · live bars = INTERNAL (aggregated from IBKR quotes)
       (host/port/account_id/client_id are filled in by api/nautilus_bridge.start from the selected gateway)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from nautilus_trader.adapters.interactive_brokers.common import IB
from nautilus_trader.adapters.interactive_brokers.config import IBMarketDataTypeEnum
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersDataClientConfig
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersExecClientConfig
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersInstrumentProviderConfig
from nautilus_trader.adapters.interactive_brokers.factories import InteractiveBrokersLiveDataClientFactory
from nautilus_trader.adapters.interactive_brokers.factories import InteractiveBrokersLiveExecClientFactory
from nautilus_trader.config import LiveDataEngineConfig, LiveExecEngineConfig, LoggingConfig, RoutingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode

from qx_strategies import build_strategy
from qx_strategies.package_limits import LIMITS

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
args = ap.parse_args()
cfg = json.loads(args.config)

instrument = cfg["instrument"]
tf = int(cfg.get("tf_minutes", 1))
provider = InteractiveBrokersInstrumentProviderConfig(load_ids=frozenset([instrument]))
host, port = cfg.get("host", "127.0.0.1"), int(cfg["port"])          # from the selected gateway profile (nautilus_bridge)
data_client_id = int(cfg["client_id"])                                  # allocated by nautilus_bridge: data = base, exec = base + 1
exec_client_id = data_client_id + 1

from api.nautilus_bridge import node_tags  # noqa: E402  (unique trader_id / order_id_tag per node id)
_trader_id, _order_tag = node_tags(str(cfg.get("agent_id", "EMA")))
node_cfg = TradingNodeConfig(
    trader_id=_trader_id,
    logging=LoggingConfig(log_level="INFO", log_directory=cfg.get("log_dir"), log_file_name=cfg.get("log_name"), log_file_format=None),
    data_clients={IB: InteractiveBrokersDataClientConfig(
        ibg_host=host, ibg_port=port, ibg_client_id=data_client_id,
        market_data_type=IBMarketDataTypeEnum.DELAYED, instrument_provider=provider)},
    exec_clients={IB: InteractiveBrokersExecClientConfig(
        ibg_host=host, ibg_port=port, ibg_client_id=exec_client_id,
        account_id=cfg["account_id"], instrument_provider=provider, routing=RoutingConfig(default=True))},
    data_engine=LiveDataEngineConfig(time_bars_timestamp_on_close=False, validate_data_sequence=True),
    # the node manages only its own orders: it ignores account-level positions from IBKR (IBKR FX positions are virtual and may not match real cash balances,
    # and several nodes may trade the same pair) and ignores other nodes' / manual orders — account-level correctness is checked by the API (/reconcile)
    exec_engine=LiveExecEngineConfig(reconciliation=True, filter_position_reports=True, filter_unclaimed_external_orders=True),
    timeout_connection=90.0, timeout_reconciliation=10.0, timeout_portfolio=10.0, timeout_disconnection=10.0, timeout_post_stop=2.0,
)
node = TradingNode(config=node_cfg)
# Step 22: strategy package limits (instruments / notional / orders / flips per hour) — sent by node_manager at start
if cfg.get("package_limits"):
    LIMITS.configure(cfg["package_limits"])
from qos import paths  # noqa: E402
_state_dir = Path(cfg["log_dir"]).parent / "strategy_state" if cfg.get("log_dir") else paths.state_dir()
strategy = build_strategy({"slow": 20, **cfg, "tf_minutes": tf}, source="INTERNAL",
                          order_id_tag=_order_tag if cfg.get("agent_id") else "001",
                          state_file=str(_state_dir / f"{cfg.get('agent_id', 'node')}.json"))
node.trader.add_strategy(strategy)
# send events (order/fill/position/signal/heartbeat) to Redis → API → Terminal · disable with QX_EVENTS=0
if os.getenv("QX_EVENTS", "1") != "0":
    from qx_strategies.events import QXEventBridge, QXEventBridgeConfig
    node.trader.add_actor(QXEventBridge(QXEventBridgeConfig(
        node_id=str(cfg.get("agent_id", "node")), redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))))
# same risk limits as the API + kill switch via Redis (qx:risk / qx:control)
from qx_strategies.risk_guard import QXRiskGuard, QXRiskGuardConfig
guard = QXRiskGuard(QXRiskGuardConfig(node_id=str(cfg.get("agent_id", "node")),
                                      redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")), strategies=[strategy])
node.trader.add_actor(guard)
node.add_data_client_factory(IB, InteractiveBrokersLiveDataClientFactory)
node.add_exec_client_factory(IB, InteractiveBrokersLiveExecClientFactory)
node.build()
guard.risk_engine = node.kernel.risk_engine
# Step 22: the whole process may write only to the package folder + data/strategy_state (enforced again on top of the SDK adapter's scope)
if cfg.get("package_limits"):
    from qos.sdk import sandbox
    sys.dont_write_bytecode = True
    sandbox.install_global(sandbox.default_allowed(cfg.get("package_dir"), _state_dir / "x.json"))
print(json.dumps({"event": "nautilus_started", "instrument": instrument, "tf": tf, "port": port, "client_ids": [data_client_id, exec_client_id]}), flush=True)
try:
    node.run()
finally:
    node.dispose()
    print(json.dumps({"event": "nautilus_stopped"}), flush=True)
