"""
Legacy agents (before the move to NautilusTrader) — the API no longer runs these agents (step 13: cutover)

Kept only as a reference implementation for:
- qx_strategies.parity  compares the new Strategy with the old code on the same bars
- api.soak              shadow parity during the paper soak

All automated trading runs as Nautilus nodes (api.node_manager) from configs/strategies/*.json
"""
