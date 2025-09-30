# wheel_wheel.py
# A paper-tradeable Wheel bot for IBKR using ib_insync.
# CSP -> assignment -> covered call -> called away -> repeat.

from ib_insync import *
from math import log, sqrt, exp
from statistics import stdev
from datetime import datetime, timedelta, timezone
import argparse, time
from ib_insync import Option


# ------------------- Config-----------------------
TARGET_PUT_DELTA   = 0.25
TARGET_CALL_DELTA  = 0.20
PUT_DTE_RANGE      = (28, 45)   # min/max days to expiration
CALL_DTE_RANGE     = (28, 45)
MARKUP_OVER_THEO   = 0.10       # place order above theo by 10%
TAKE_PROFIT        = 0.50       # close at 50% of collected premium
ROLL_DTE_THRESHOLD = 5          # if fewer than 5 DTE and not at target -> roll
CHECK_EVERY_SEC    = 60 * 5     # main loop cadence (5 minutes)
TAG                = "WHEELBOT" # order tag to identify our orders
# ------------------------------------------------

SILENT_CODES = {10091, 10167, 2103, 2104, 2106, 2107, 2108}  # noisy farm/sub msg

def utc_date():
    return datetime.now(timezone.utc).date()

def bs_put_price(S, K, T, r, vol):
    if T<=0 or vol<=0 or S<=0 or K<=0: return max(0.0, K-S)
    d1 = (log(S/K) + (r + 0.5*vol*vol)*T)/(vol*sqrt(T))
    d2 = d1 - vol*sqrt(T)
    # N(x) via error function
    import math
    N = lambda x: 0.5*(1.0 + math.erf(x/math.sqrt(2.0)))
    return K*exp(-r*T)*(1.0 - N(d2)) - S*(1.0 - N(d1))

def bs_put_delta(S, K, T, r, vol):
    if T<=0 or vol<=0 or S<=0 or K<=0: return 0.0
    import math
    N = lambda x: 0.5*(1.0 + math.erf(x/math.sqrt(2.0)))
    d1 = (log(S/K) + (r + 0.5*vol*vol)*T)/(vol*sqrt(T))
    return abs(N(d1) - 1.0)

def realized_vol_annualized(ib, stock, lookback=21):
    bars = ib.reqHistoricalData(
        stock, endDateTime='', durationStr=f'{lookback+5} D',
        barSizeSetting='1 day', whatToShow='TRADES', useRTH=True, formatDate=1
    )
    closes = [b.close for b in bars]
    if len(closes) < lookback + 1: return 0.20
    rets = [log(closes[i]/closes[i-1]) for i in range(1, len(closes))]
    return stdev(rets[-lookback:]) * sqrt(252.0)

def robust_spot(ib, stock):
    # try snapshot
    t = ib.reqMktData(stock, '', True, False)
    ib.sleep(1.5)
    for v in (t.last, t.close, t.bid, t.ask, t.marketPrice()):
        if v and v > 0:
            return float(v)
    # fallback to 1D close
    bars = ib.reqHistoricalData(stock, endDateTime='', durationStr='3 D',
                                barSizeSetting='1 day', whatToShow='TRADES',
                                useRTH=True, formatDate=1)
    if bars:
        return float(bars[-1].close)
    return None

def qualify_stock(ib, symbol):
    stk = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stk)
    return stk

def get_chain(ib, symbol, conId):
    params = ib.reqSecDefOptParams(symbol, '', 'STK', conId)
    # prefer SMART if present
    for p in params:
        if p.exchange == 'SMART':
            return p
    return params[0] if params else None

def dte_of(exp_str):
    return (datetime.strptime(exp_str, "%Y%m%d").date() - utc_date()).days

def choose_strike_by_delta(S, strikes, exp, target_delta, r, iv, put=True):
    T = max(dte_of(exp), 0) / 365.0
    if T <= 0: return None
    best = None
    for K in strikes:
        d = bs_put_delta(S, K, T, r, iv)
        diff = abs(d - target_delta)
        if best is None or diff < best[0]:
            best = (diff, K)
    return best[1] if best else None

def make_opt(symbol: str, expiry: str, strike: float, right: str) -> Option:
    """
    Return a fully-specified IB option contract so IB never asks for an exchange.
    """
    return Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,  # 'YYYYMMDD'
        strike=float(strike),
        right=right,                           # 'P' or 'C'
        exchange="SMART",                      # <— IMPORTANT
        currency="USD",
        tradingClass=symbol                    # for SPY this is 'SPY'
    )

def theo_option_price(S, K, exp, r, iv, put=True):
    T = max(dte_of(exp), 0)/365.0
    if put: return bs_put_price(S, K, T, r, iv)
    # covered call price (theo to sell) ~ put-call parity; here we just reuse put theo distance for markup symmetry
    # for pricing CC order we’ll use bid/ask snapshot when available; if not, use a simple parity-ish fallback:
    # C ≈ P + S - K*e^{-rT}; but using put theo is okay for limit anchor on delayed data.
    P = bs_put_price(S, K, T, r, iv)
    return max(0.01, P + S - K*exp(-r*T))

def mark_price_for_order(theo, markup=MARKUP_OVER_THEO):
    return round(max(0.05, theo*(1.0+markup)), 2)

def fetch_positions_and_orders(ib, symbol):
    positions = ib.positions()
    open_trades = [t for t in ib.trades() if t.isActive()]

    # identify our orders by tag
    my_trades = [t for t in open_trades
                 if (t.order.orderRef or "") == TAG
                 or (t.order.orderRef or "").find(TAG) >= 0]

    shares = 0
    short_puts, short_calls = [], []

    for p in positions:
        c = p.contract
        if isinstance(c, Stock) and c.localSymbol.upper() == symbol:
            shares += int(p.position)
        elif isinstance(c, Option) and c.localSymbol and c.localSymbol.startswith(symbol):
            # REPAIR/normalize option contracts coming from positions
            c = normalize_option(ib, c, symbol)
            if c.right == 'P' and p.position < 0:
                short_puts.append((c, int(p.position)))
            if c.right == 'C' and p.position < 0:
                short_calls.append((c, int(p.position)))

    return shares, short_puts, short_calls, my_trades


def place_limit(ib, contract, action, qty, limitPrice, dry):
    o = LimitOrder(action, qty, limitPrice)
    o.tif = 'GTC'
    o.orderRef = TAG
    print(f"[ORDER] {action} {qty} {contract.localSymbol or contract.symbol} @ {limitPrice} GTC")
    if dry:
        print("[DRY RUN] Not placing.")
        return None
    return ib.placeOrder(contract, o)

def best_put_to_sell(ib, symbol, stock, target_delta, dte_range, r, iv):
    chain = get_chain(ib, symbol, stock.conId)
    if not chain: return None
    exps = sorted([e for e in chain.expirations if dte_range[0] <= dte_of(e) <= dte_range[1]],
                  key=lambda e: dte_of(e))
    if not exps: return None
    strikes = [k for k in chain.strikes if 0.7*1000 <= k <= 1.3*1000]  # wide filter; we’ll narrow via delta below
    # Better filter around S
    S = robust_spot(ib, stock)
    if not S: return None
    strikes = [k for k in chain.strikes if 0.7*S <= k <= 1.3*S] or sorted(chain.strikes)[:80]
    exp = exps[0]
    K = choose_strike_by_delta(S, strikes, exp, target_delta, r, iv, put=True)
    if not K: return None
    opt = make_opt(symbol, exp, K, 'P')
    ib.qualifyContracts(opt)

    theo = theo_option_price(S, K, exp, r, iv, put=True)
    return dict(contract=opt, theo=theo, S=S, K=K, exp=exp)

def best_call_to_sell(ib, symbol, stock, target_delta, dte_range, r, iv):
    chain = get_chain(ib, symbol, stock.conId)
    if not chain: return None
    exps = sorted([e for e in chain.expirations if dte_range[0] <= dte_of(e) <= dte_range[1]],
                  key=lambda e: dte_of(e))
    if not exps: return None
    S = robust_spot(ib, stock)
    if not S: return None
    strikes = [k for k in chain.strikes if 0.7*S <= k <= 1.3*S] or sorted(chain.strikes)[:80]
    exp = exps[0]
    K = choose_strike_by_delta(S, strikes, exp, target_delta, r, iv, put=False)
    if not K: return None
    opt = make_opt(symbol, exp, K, 'C')
    ib.qualifyContracts(opt)
    theo = theo_option_price(S, K, exp, r, iv, put=False)
    return dict(contract=opt, theo=theo, S=S, K=K, exp=exp)

def dte_from_contract(c):
    try:
        return dte_of(c.lastTradeDateOrContractMonth)
    except Exception:
        return 999

def ensure_profit_take(ib, pos_tuple, credit_hint, dry):
    """
    If option mid <= (1-TAKE_PROFIT)*credit, buy-to-close.
    credit_hint: our best guess at original credit (can be theo or last mid).
    """
    c, qty = pos_tuple  # qty negative
    md = ib.reqMktData(c, '', True, False); ib.sleep(1.0)
    mid = None
    fields = dict(last=md.last, bid=md.bid, ask=md.ask)
    if md.bid and md.ask and md.bid > 0 and md.ask > 0:
        mid = (md.bid + md.ask) / 2.0
    elif md.last and md.last > 0:
        mid = md.last

    if mid is None:
        print(f"[TP] No mid for {c.localSymbol}; quotes={fields}. Skipping.")
        return False

    threshold = max(0.01, credit_hint * (1.0 - TAKE_PROFIT))
    print(f"[TP] {c.localSymbol} mid≈{mid:.2f}  threshold≈{threshold:.2f}  (credit≈{credit_hint:.2f})")

    if mid <= threshold:
        qty_to_close = -qty
        px = round(mid, 2)
        place_limit(ib, c, 'BUY', qty_to_close, px, dry)
        return True
    return False


def maybe_roll(ib, pos_tuple, dry):
    """If near expiry and not profitable yet, roll out by buying to close and selling next cycle."""
    c, qty = pos_tuple
    if dte_from_contract(c) > ROLL_DTE_THRESHOLD:
        return False
    # BTC at marketable (use snapshot)
    md = ib.reqMktData(c, '', True, False); ib.sleep(1.0)
    px = None
    if md.ask and md.ask > 0:
        px = md.ask
    elif md.last and md.last > 0:
        px = md.last
    if px is None: return False
    place_limit(ib, c, 'BUY', -qty, round(px, 2), dry)
    return True

def normalize_option(ib: IB, c: Option, symbol: str) -> Option:
    """
    Ensure option contract has the essentials so IB won't ask for 'exchange'.
    Repairs options coming from positions/trades that lack exchange.
    """
    if not isinstance(c, Option):
        return c
    # Fill missing fields
    if not getattr(c, 'exchange', None):
        c.exchange = "SMART"
    if not getattr(c, 'currency', None):
        c.currency = "USD"
    if not getattr(c, 'tradingClass', None):
        c.tradingClass = symbol  # e.g., 'SPY'
    try:
        ib.qualifyContracts(c)
    except Exception:
        # Qualification can fail with delayed data; keep best effort
        pass
    return c

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--live", action="store_true", help="use live data type (requires subs)")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="seconds between cycles; 0 = one-shot")
    args = ap.parse_args()

    # Connect
    ib = IB()
    client_id = 900 + int(time.time()) % 4000
    ib.connect("127.0.0.1", 7497, clientId=client_id)
    ib.reqMarketDataType(1 if args.live else 4)

    def on_error(reqId, code, msg, contract):
        if code in SILENT_CODES: return
        print(f"[IB ERROR] id={reqId} code={code} msg={msg}")
    ib.errorEvent += on_error

    symbol = args.symbol.upper()
    stock = qualify_stock(ib, symbol)
    r = 0.03  # simple constant rate

    def one_cycle():
        # state summary
        shares, short_puts, short_calls, my_trades = fetch_positions_and_orders(ib, symbol)
        iv = realized_vol_annualized(ib, stock)

        print(f"== {symbol} state: shares={shares} short_puts={len(short_puts)} short_calls={len(short_calls)} IV≈{iv:.2%}")

        # 1) Manage existing short puts: take profit or roll if needed
        for c, q in short_puts:
            # estimate original credit via last trade or mid as approximation
            # inside loop for existing short puts/calls
            md = ib.reqMktData(c, '', True, False); ib.sleep(1.0)
            credit = None
            if md.last and md.last > 0: credit = md.last
            elif md.bid and md.ask and md.bid > 0 and md.ask > 0: credit = (md.bid + md.ask)/2.0
            if credit is None:
                # crude fallback: use theoretical price so we still get a threshold
                T = max(dte_from_contract(c), 0)/365.0
                # S from robust_spot + a guessed IV (or realized_vol_annualized)
                S = robust_spot(ib, qualify_stock(ib, c.symbol))
                iv = 0.20
                if S:
                    from math import exp
                    if c.right == 'P':
                        credit = bs_put_price(S, c.strike, T, 0.03, iv)
                    else:
                        credit = max(0.01, bs_put_price(S, c.strike, T, 0.03, iv) + S - c.strike*exp(-0.03*T))
                else:
                    credit = 1.00  # last-ditch placeholder

            if ensure_profit_take(ib, (c, q), credit, args.dry):
                return


        # 2) Manage existing short calls: take profit or roll
        for c, q in short_calls:
           # inside loop for existing short puts/calls
            md = ib.reqMktData(c, '', True, False); ib.sleep(1.0)
            credit = None
            if md.last and md.last > 0: credit = md.last
            elif md.bid and md.ask and md.bid > 0 and md.ask > 0: credit = (md.bid + md.ask)/2.0
            if credit is None:
                # crude fallback: use theoretical price so we still get a threshold
                T = max(dte_from_contract(c), 0)/365.0
                # S from robust_spot + a guessed IV (or realized_vol_annualized)
                S = robust_spot(ib, qualify_stock(ib, c.symbol))
                iv = 0.20
                if S:
                    from math import exp
                    if c.right == 'P':
                        credit = bs_put_price(S, c.strike, T, 0.03, iv)
                    else:
                        credit = max(0.01, bs_put_price(S, c.strike, T, 0.03, iv) + S - c.strike*exp(-0.03*T))
                else:
                    credit = 1.00  # last-ditch placeholder

            if ensure_profit_take(ib, (c, q), credit, args.dry):
                return

        # 3) Decide next action
        if shares >= 100 and not short_calls:
            # Sell covered call
            sel = best_call_to_sell(ib, symbol, stock, TARGET_CALL_DELTA, CALL_DTE_RANGE, r, iv)
            if sel:
                cc_px = mark_price_for_order(sel["theo"], MARKUP_OVER_THEO)
                place_limit(ib, sel["contract"], 'SELL', min(args.qty, shares//100), cc_px, args.dry)
                return
            print("[INFO] No CC candidate found.")
            return

        if shares < 100 and not short_puts:
            # Sell cash-secured put
            sel = best_put_to_sell(ib, symbol, stock, TARGET_PUT_DELTA, PUT_DTE_RANGE, r, iv)
            if sel:
                csp_px = mark_price_for_order(sel["theo"], MARKUP_OVER_THEO)
                place_limit(ib, sel["contract"], 'SELL', args.qty, csp_px, args.dry)
                return
            print("[INFO] No CSP candidate found.")
            return

        print("[IDLE] Nothing to do this pass.")

    # Run once or forever
    if args.loop and args.loop > 0:
        print(f"[LOOP] Running every {args.loop} sec. Ctrl+C to stop.")
        try:
            while True:
                one_cycle()
                ib.sleep(args.loop)
        finally:
            ib.disconnect()
    else:
        one_cycle()
        ib.disconnect()

if __name__ == "__main__":
    main()
