# run.py
from broker_ib import (
    connect_ib, robust_spot, realized_vol_annualized,
    bs_put_price, pick_put_by_model_delta, place_limit
)
from ib_insync import *
from datetime import datetime, timezone
import argparse, json, os, time

STATE_FILE = "wheel_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"positions": {}}  # keyed by symbol

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)

def find_positions(ib: IB, symbol: str):
    """
    Look at account positions to detect:
    - stock shares
    - short puts
    - short calls
    """
    shares = 0
    short_puts = []
    short_calls = []
    for p in ib.positions():
        con = p.contract
        if con.symbol != symbol:
            continue
        if con.secType == 'STK':
            shares += int(p.position)
        elif con.secType == 'OPT':
            # negative position => we're short the option
            if p.position < 0:
                if con.right == 'P':
                    short_puts.append((con, int(p.position)))
                elif con.right == 'C':
                    short_calls.append((con, int(p.position)))
    return shares, short_puts, short_calls

def target_put(ib: IB, symbol: str, delta=0.25, dte_min=30, dte_max=45):
    sel = pick_put_by_model_delta(ib, symbol, target_delta=delta, dte_range=(dte_min, dte_max))
    if not sel:
        return None, None
    T = sel["dte"]/365.0
    theo = bs_put_price(sel["spot"], sel["strike"], T, 0.03, sel["iv"])
    put_con = Option(symbol, sel["exp"], sel["strike"], 'P', 'SMART', currency='USD', tradingClass=symbol)
    ib.qualifyContracts(put_con)
    return sel, theo

def sell_csp(ib: IB, symbol: str, qty: int, markup: float, dry: bool, delta=0.25, dte_min=30, dte_max=45):
    sel, theo = target_put(ib, symbol, delta, dte_min, dte_max)
    if not sel:
        print(f"[{symbol}] No suitable put found.")
        return
    px = max(0.05, round(theo*(1.0+markup), 2))
    print(f"[{symbol}] spot={sel['spot']:.2f} IV={sel['iv']:.2%}")
    print(f"[{symbol}] chosen put: exp={sel['exp']} dte={sel['dte']} strike={sel['strike']:.2f} Δ≈{sel['delta']}")
    con = Option(symbol, sel["exp"], sel["strike"], 'P', 'SMART', currency='USD', tradingClass=symbol)
    ib.qualifyContracts(con)
    place_limit(ib, con, 'SELL', qty, px, dry)

def sell_covered_call(ib: IB, symbol: str, shares: int, call_delta=0.20, dte_min=30, dte_max=45, markup=0.10, dry=False):
    """
    Quick CC selector using the same model but mirrored for calls:
    approximate by flipping the sign (use put-delta on K > S as a proxy).
    """
    sel = pick_put_by_model_delta(ib, symbol, target_delta=call_delta, dte_range=(dte_min, dte_max))
    if not sel:
        print(f"[{symbol}] No suitable call found (proxy).")
        return
    # For a call, push strike above spot (simple proxy)
    if sel["strike"] < sel["spot"]:
        # bump strike to near ATM+ ~ 5% step
        sel["strike"] = round(max(sel["strike"], sel["spot"] * 1.03), 2)

    T = sel["dte"]/365.0
    # quick & dirty call price from put-call parity (approx) or reuse put price proxy
    # (for delayed mode this is fine for demonstrating the flow)
    theo_call = max(0.05, sel["spot"] - sel["strike"])  # lower bound
    px = round(theo_call * (1.0+markup), 2)

    print(f"[{symbol}] covered call candidate: exp={sel['exp']} dte={sel['dte']} strike={sel['strike']:.2f}")
    con = Option(symbol, sel["exp"], sel["strike"], 'C', 'SMART', currency='USD', tradingClass=symbol)
    ib.qualifyContracts(con)
    qty = max(1, shares // 100)
    place_limit(ib, con, 'SELL', qty, px, dry)

def main():
    import argparse, time
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--qty", type=int, default=1, help="Contracts to sell (puts or calls)")
    ap.add_argument("--delta", type=float, default=0.25)
    ap.add_argument("--dte-min", type=int, default=30)
    ap.add_argument("--dte-max", type=int, default=45)
    ap.add_argument("--markup", type=float, default=0.10)
    ap.add_argument("--dry", action="store_true", help="Preview orders without sending")
    ap.add_argument("--loop", action="store_true", help="Run repeatedly until stopped")
    ap.add_argument("--sleep", type=int, default=900, help="Seconds between iterations")
    ap.add_argument("--mkt", type=int, default=4, help="1=live, 3=delayed, 4=delayed-frozen")
    args = ap.parse_args()

    # connect with the mktdata type selected
    ib = connect_ib(port=7497, client_id=None, mktdata_type=args.mkt)
    print("Connected:", ib.isConnected())

    sym = args.symbol
    while True:
        # these helpers are already in your file
        shares, short_puts, short_calls = find_positions(ib, sym)
        print(f"[STATE] shares={shares} short_puts={len(short_puts)} short_calls={len(short_calls)}")

        if shares <= 0 and not short_puts:
            sell_csp(
                ib, sym,
                qty=args.qty, markup=args.markup, dry=args.dry,
                delta=args.delta, dte_min=args.dte_min, dte_max=args.dte_max
            )

        elif shares > 0 and not short_calls:
            sell_covered_call(
                ib, sym,
                shares=shares, call_delta=0.20,
                dte_min=args.dte_min, dte_max=args.dte_max,
                markup=args.markup, dry=args.dry
            )

        else:
            print("[INFO] Wheel step already in place (waiting for fills/expiry/assignment).")

        if not args.loop:
            break
        time.sleep(args.sleep)

    ib.disconnect()
    print("Done.")


if __name__ == "__main__":
    main()
