from ib_insync import *
from datetime import datetime

def log(msg): print(">>", msg)

ib = IB()

# 1) Connect
try:
    ib.connect('127.0.0.1', 7497, clientId=55, timeout=6)
    log(f"Connected={ib.isConnected()}")
except Exception as e:
    log(f"CONNECT FAILED: {e}")
    raise SystemExit

# Show TWS time (proves TWS is connected to IB servers)
try:
    t = ib.reqCurrentTime()
    log(f"TWS server time: {t}")
except Exception as e:
    log(f"reqCurrentTime FAILED: {e}")

# Log any API errors from TWS
def on_error(reqId, code, msg, misc):
    print(f"[API ERROR] reqId={reqId} code={code} msg={msg}")
ib.errorEvent += on_error

# 2) Force delayed market data (works even without live subs)
ib.reqMarketDataType(4)  # 4=delayed, 3=delayed-frozen
log("Set market data type to 4 (delayed)")

# 3) Simple stock quote sanity check
stk = Stock('SPY', 'SMART', 'USD')
ib.qualifyContracts(stk)
tkr = ib.reqMktData(stk, '', False, False)
ib.sleep(2.0)
log(f"SPY quote -> last={tkr.last} close={tkr.close} bid={tkr.bid} ask={tkr.ask}")

# 4) Option chain meta (doesn't require data subscription)
try:
    params = ib.reqSecDefOptParams(stk.symbol, '', 'STK', stk.conId)
    chains = [p for p in params if p.exchange == 'SMART']
    if not chains:
        log("No SMART option chain returned.")
    else:
        c = chains[0]
        log(f"Found chain: expirations={len(c.expirations)} strikes={len(c.strikes)}")
        exp = sorted(c.expirations)[0]
        K = sorted(c.strikes)[len(c.strikes)//2]  # pick a mid strike
        log(f"Testing first expiration {exp}, strike {K}")

        # 5) One option quote + greeks
        opt = Option('SPY', exp, K, 'P', 'SMART')
        ib.qualifyContracts(opt)
        otkr = ib.reqMktData(opt, '106', False, False)  # 106=model greeks
        waited = 0.0
        while waited < 8.0 and not (otkr.modelGreeks and otkr.modelGreeks.delta is not None):
            ib.sleep(0.4); waited += 0.4
        g = otkr.modelGreeks
        log(f"OPT quote -> bid={otkr.bid} ask={otkr.ask} delta={(g.delta if g else None)} after {waited:.1f}s")
except Exception as e:
    log(f"Option chain/quote FAILED: {e}")

ib.disconnect()
log("Done.")