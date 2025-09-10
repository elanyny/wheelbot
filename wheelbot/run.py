import yaml
from broker_ib import connect_ib, pick_put_by_delta

DEFAULTS = {"tickers": ["SPY"], "target_delta": 0.25, "put_dte": [30, 45]}
try:
    with open("config.yaml","r") as f:
        cfg = {**DEFAULTS, **(yaml.safe_load(f) or {})}
except FileNotFoundError:
    cfg = DEFAULTS

ib = connect_ib(port=7497, client_id=11)
print("Connected:", ib.isConnected())

for sym in cfg["tickers"]:
    res = pick_put_by_delta(
        ib, sym,
        target_delta=cfg["target_delta"],
        dte_range=tuple(cfg["put_dte"]),
        max_wait=8.0
    )
    if res:
        print(f"[{sym}] Put {res['localSymbol']}  strike={res['strike']}  "
              f"exp={res['exp']}  DTE={res['dte']}  Δ≈{res['delta']}  "
              f"bid={res['bid']}  ask={res['ask']}")
    else:
        print(f"[{sym}] No suitable put found.")

ib.disconnect()
