from ib_insync import *
from datetime import datetime
import math, time

def connect_ib(host="127.0.0.1", port=7497, client_id=7):
    ib = IB()
    ib.connect(host, port, clientId=client_id)
    ib.reqMarketDataType(4)  # 4 = delayed (works without live sub)
    return ib

def _spot_price(ib, symbol):
    stk = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stk)
    t = ib.reqMktData(stk, '', False, False)
    ib.sleep(1.0)
    for x in (t.last, t.close, t.bid, t.ask, t.marketPrice()):
        if x and x > 0:
            return float(x)
    return None

def pick_put_by_delta(ib, symbol, target_delta=0.25, dte_range=(30,45), max_wait=6.0):
    spot = _spot_price(ib, symbol)
    if not spot:
        print(f"[{symbol}] No spot/quote available.")
        return None

    stk = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stk)
    params = ib.reqSecDefOptParams(symbol, '', 'STK', stk.conId)
    chain = next(p for p in params if p.exchange == 'SMART')

    today = datetime.utcnow().date()
    exps_all = sorted(chain.expirations)
    exps = [e for e in exps_all if dte_range[0] <= (datetime.strptime(e,"%Y%m%d").date()-today).days <= dte_range[1]]
    if not exps:
        print(f"[{symbol}] No expirations in DTE window. Found {len(exps_all)} total.")
        return None

    strikes = [s for s in chain.strikes if 0.7*spot <= s <= 1.3*spot]
    if not strikes:
        print(f"[{symbol}] No strikes in range around spot {spot}.")
        return None
    strikes = sorted(strikes, key=lambda k: abs(k-spot))[:80]

    best = None
    for exp in exps[:6]:
        dte = (datetime.strptime(exp, "%Y%m%d").date() - today).days
        for K in strikes:
            opt = Option(symbol, exp, K, 'P', 'SMART')
            ib.qualifyContracts(opt)
            tk = ib.reqMktData(opt, '106', False, False)  # 106 = model greeks
            # wait up to max_wait seconds for greeks to populate
            waited = 0.0
            while waited < max_wait and not (tk.modelGreeks and tk.modelGreeks.delta is not None):
                ib.sleep(0.2)
                waited += 0.2
            if not (tk.modelGreeks and tk.modelGreeks.delta is not None):
                continue
            delta = abs(tk.modelGreeks.delta)
            diff = abs(delta - target_delta)
            if best is None or diff < best[0]:
                bid = tk.bid or 0.0
                ask = tk.ask or 0.0
                best = (diff, {
                    "symbol": symbol, "strike": K, "exp": exp, "dte": dte,
                    "delta": round(delta, 3), "bid": bid, "ask": ask,
                    "localSymbol": opt.localSymbol, "contract": opt
                })
    if best is None:
        print(f"[{symbol}] Couldn’t get model greeks (delayed ok?) — try again or during RTH.")
    return best[1] if best else None
