# broker_ib.py
from ib_insync import *
from math import log, sqrt, exp
from statistics import stdev
from datetime import datetime, timedelta, timezone
import time

# Quiet the common/noisy farm + delayed messages
_SILENT = {10091, 10167, 2103, 2104, 2106, 2107, 2108}

def connect_ib(host="127.0.0.1", port=7497, client_id=None, mktdata_type=4) -> IB:
    """
    Connect to TWS/IBG and set market data mode.
    mktdata_type: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
    """
    ib = IB()
    if client_id is None:
        client_id = 5000 + int(time.time()) % 4000
    ib.connect(host, port, clientId=client_id)
    ib.reqMarketDataType(mktdata_type)

    def _on_err(reqId, code, msg, contract):
        if code in _SILENT:
            return
        print(f"[IB ERROR] id={reqId} code={code} msg={msg}")

    ib.errorEvent += _on_err

    try:
        print(">> TWS server time:", ib.reqCurrentTime())
    except Exception:
        pass
    print(f">> Connected. mktdata_type={mktdata_type}, clientId={client_id}")
    return ib

# ---------- Market data helpers ----------

def _first_price(t):
    for x in (t.last, t.close, t.bid, t.ask, t.marketPrice()):
        if x and x > 0:
            return float(x)
    return None

def robust_spot(ib: IB, symbol: str) -> float | None:
    """
    Try streaming → snapshot → 1D close; tolerant of delayed data.
    """
    stk = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stk)

    # streaming (up to ~5s)
    t = ib.reqMktData(stk, '', False, False)
    for _ in range(20):
        ib.sleep(0.25)
        v = _first_price(t)
        if v:
            return v

    # snapshot (2s)
    s = ib.reqMktData(stk, '', True, False)
    ib.sleep(2.0)
    v = _first_price(s)
    if v:
        return v

    # fallback: last 1D close
    bars = ib.reqHistoricalData(
        stk, endDateTime='', durationStr='3 D', barSizeSetting='1 day',
        whatToShow='TRADES', useRTH=True, formatDate=1
    )
    if bars:
        return float(bars[-1].close)
    return None

def realized_vol_annualized(ib: IB, symbol: str, lookback_days=21) -> float:
    """
    Simple historical vol as a safe default.
    """
    stk = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stk)
    bars = ib.reqHistoricalData(
        stk, endDateTime='', durationStr=f'{lookback_days+5} D', barSizeSetting='1 day',
        whatToShow='TRADES', useRTH=True, formatDate=1
    )
    closes = [b.close for b in bars]
    if len(closes) < lookback_days + 1:
        return 0.20
    rets = [log(closes[i]/closes[i-1]) for i in range(1, len(closes))]
    return stdev(rets[-lookback_days:]) * sqrt(252.0)

# ---------- Black–Scholes (quick & sturdy) ----------

def _phi(x: float) -> float:
    import math
    return 0.5 * (1.0 + math.erf(x / sqrt(2.0)))

def bs_put_delta(S, K, T, r, vol) -> float:
    if T <= 0 or vol <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (log(S/K) + (r + 0.5*vol*vol)*T) / (vol*sqrt(T))
    return abs(_phi(d1) - 1.0)

def bs_put_price(S, K, T, r, vol) -> float:
    if T <= 0 or vol <= 0 or S <= 0 or K <= 0:
        return max(0.0, K - S)
    d1 = (log(S/K) + (r + 0.5*vol*vol)*T) / (vol*sqrt(T))
    d2 = d1 - vol*sqrt(T)
    return K*exp(-r*T)*(1.0 - _phi(d2)) - S*(1.0 - _phi(d1))

# ---------- Option chain picking ----------

def req_chain(ib: IB, symbol: str):
    stk = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stk)
    params = ib.reqSecDefOptParams(symbol, '', 'STK', stk.conId)
    # Prefer SMART
    for p in params:
        if p.exchange == 'SMART':
            return p
    # Otherwise first
    return params[0] if params else None

def pick_put_by_model_delta(ib: IB, symbol: str,
                            target_delta=0.25,
                            dte_range=(30, 45),
                            r=0.03) -> dict | None:
    """
    Choose a put by BS delta using robust spot + hist vol.
    Returns dict with fields: exp, dte, strike, delta, spot, iv, r
    """
    S = robust_spot(ib, symbol)
    if not S:
        print(f"[{symbol}] No spot/quote available.")
        return None
    iv = realized_vol_annualized(ib, symbol)
    chain = req_chain(ib, symbol)
    if not chain:
        print(f"[{symbol}] No chain available.")
        return None

    today = datetime.now(timezone.utc).date()
    # expirations are like 'YYYYMMDD'
    exp_list = sorted(chain.expirations)
    # filter by DTE
    cands_exp = []
    for e in exp_list:
        dte = (datetime.strptime(e, "%Y%m%d").date() - today).days
        if dte_range[0] <= dte <= dte_range[1]:
            cands_exp.append((e, dte))
    if not cands_exp:
        print(f"[{symbol}] No expirations in DTE window.")
        return None

    strikes = sorted(k for k in chain.strikes if 0.7*S <= k <= 1.3*S) or sorted(chain.strikes)[:80]
    best = None
    for exp, dte in cands_exp[:10]:
        T = max(1e-6, dte/365.0)
        for K in strikes:
            d = bs_put_delta(S, K, T, r, iv)
            diff = abs(d - target_delta)
            if best is None or diff < best[0]:
                best = (diff, dict(symbol=symbol, exp=exp, dte=dte, strike=float(K),
                                   delta=round(d, 3), spot=S, iv=iv, r=r))
    return best[1] if best else None

# ---------- Orders ----------

def place_limit(ib: IB, contract: Contract, action: str, qty: int, price: float, dry: bool):
    """
    Thin wrapper to place/preview a limit order.
    """
    price = round(max(0.01, price), 2)
    print(f"[ORDER] {action} {qty} {contract.localSymbol or contract.symbol} @ {price:.2f}")
    if dry:
        print("[DRY RUN] Not placing order.")
        return None
    order = LimitOrder(action, qty, price)
    trade = ib.placeOrder(contract, order)
    return trade
