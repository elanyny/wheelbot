from ib_insync import *
ib = IB()
ib.connect('127.0.0.1', 7497, clientId=42)
ib.reqMarketDataType(4)  # 4 = delayed, 3 = delayed-frozen (try 3 if 4 shows nothing)

stk = Stock('SPY', 'SMART', 'USD')
ib.qualifyContracts(stk)
t = ib.reqMktData(stk, '', False, False)
ib.sleep(2.0)  # wait a moment for data to arrive

print("last:", t.last, "close:", t.close, "bid:", t.bid, "ask:", t.ask)
ib.disconnect()
