---

# WheelBot — Automated Wheel Strategy on Interactive Brokers

A production-style Python bot that automates the **Wheel** options strategy (cash-secured puts → assignment → covered calls → called away → repeat) using `ib_insync` and Interactive Brokers (IBKR).

* Works with **delayed** data (paper accounts) and **live** data (with market-data subscriptions).
* Delta-targeted strike selection via **Black–Scholes**.
* **Take-profit** and **rolling** logic for open options.
* **Dry-run** mode for safe testing (no orders sent).
* CLI flags, error handling, contract normalization, and loop mode for continuous operation.

---

## Features

* **Wheel logic**

  * Sells **cash-secured puts (CSPs)** when flat.
  * If assigned (≥100 shares), sells **covered calls (CCs)**.
  * **Take-profit**: buy-to-close when premium decays to a configurable threshold (default: 50%).
  * **Roll**: if near expiry (default: ≤5 DTE) and not at target, buy-to-close and re-sell next cycle.

* **Strike selection**

  * Delta targeting (defaults: PUT Δ≈0.25, CALL Δ≈0.20).
  * Black–Scholes theoretical pricing (for order anchoring) and realized-vol fallback.

* **Execution**

  * GTC **limit** orders with configurable markup over theoretical price (default: +10%).
  * Orders tagged with `orderRef="WHEELBOT"` for easy tracking.

* **Control & safety**

  * `--dry` (no orders), `--loop N` (run every N seconds), `--live` (use live feed if you have subs).
  * Contract **normalization** to avoid “Please enter exchange” (SMART/USD/tradingClass set).
  * Filters noisy market-data messages and prints concise diagnostics.

---

## Quick Start

```bash
git clone <your-repo-url>
cd wheelbot
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

**Enable IBKR API** in TWS (or IB Gateway):

```
TWS/IBG → Global Configuration → API → Settings
  [x] Enable ActiveX and Socket Clients
  Trusted IPs: 127.0.0.1
  Socket port: 7497 (paper) / 7496 (live)
```

Sanity test (dry run):

```bash
python run.py --symbol SPY --dry
```

---

## Usage

### Full Wheel loop (`wheel_wheel.py`)

Run the complete strategy cycle (CSP → CC → manage/roll/take-profit):

```bash
python wheel_wheel.py --help
# usage: wheel_wheel.py [--symbol SYMBOL] [--qty QTY] [--live] [--dry] [--loop SECONDS]

# One pass (diagnostic):
python wheel_wheel.py --symbol SPY --qty 1 --dry

# Continuous loop every 5 minutes (paper / delayed):
python wheel_wheel.py --symbol SPY --qty 1 --loop 300 --dry

# Live data (requires market-data subs + API acknowledgments):
python wheel_wheel.py --symbol SPY --qty 1 --loop 300 --live
```

What each pass does:

1. Reads positions & normalizes option contracts (prevents exchange errors).
2. For **existing short puts/calls**: attempts **take-profit** or **roll** if near expiry.
3. If **no shares** and **no short puts** → sells a CSP (delta-targeted).
4. If **≥100 shares** and **no short calls** → sells a CC (delta-targeted).
5. Otherwise logs `[IDLE] Nothing to do this pass.`

### One-shot CSP picker (`run.py`)

Find (and optionally place) a single CSP via delta targeting:

```bash
python run.py --symbol SPY --delta 0.25 --dte-min 30 --dte-max 45 --qty 1 --dry
# add --live to use live data type if you have subscriptions
```

---

## Configuration

Edit the constants at the top of `wheel_wheel.py`:

```python
TARGET_PUT_DELTA   = 0.25      # CSP delta target
TARGET_CALL_DELTA  = 0.20      # Covered call delta target
PUT_DTE_RANGE      = (28, 45)  # DTE bounds for puts
CALL_DTE_RANGE     = (28, 45)  # DTE bounds for calls
MARKUP_OVER_THEO   = 0.10      # +10% over theoretical price for limit orders
TAKE_PROFIT        = 0.50      # close at 50% of collected premium (e.g., 0.30 = close at 70% profit)
ROLL_DTE_THRESHOLD = 5         # roll if DTE ≤ 5 and not at target
CHECK_EVERY_SEC    = 60 * 5    # loop cadence (5 minutes)
TAG                = "WHEELBOT"
```

> To **adjust profit-take**, set `TAKE_PROFIT` (e.g., `0.30` = take profit faster).

---

## IBKR Requirements & Notes

* **Paper trading** works with **delayed/frozen** data; the bot falls back to snapshots/historical bars as needed.
* **Live streaming** requires market-data subscriptions (US equities + US options). Also acknowledge **Market Data for API** in Account Management.
* If you see errors like `10089/10091/210x`, they’re market-data/farm notices; the bot still functions on delayed data.

---

## Troubleshooting

* **“Please enter exchange (321)”**
  Use the latest code: options are constructed with `exchange="SMART"`, `currency="USD"`, and `tradingClass` set, and positions are normalized on load.

* **“Nothing to do this pass.”**
  You already have an open short put/call and neither take-profit nor roll conditions are met.
  Options:

  * Let it run with `--loop` and it will act when thresholds hit.
  * Loosen thresholds (e.g., `TAKE_PROFIT=0.30`, `ROLL_DTE_THRESHOLD=10`).
  * Manually close the position in TWS to let it place a new CSP.

* **Permission to run scripts (Windows PowerShell)**
  Run as admin:

  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```

---

## Safety & Disclaimers

* Educational project — **not** financial advice.
* Paper trade first. Real trading involves risk; options can lose more than the premium received.
* If you run live, ensure you understand **assignment**, **margin**, and **position sizing**.

---

## Roadmap

* CSV/SQLite **trade logging** and basic P&L reporting.
* Portfolio **risk controls** (cash checks, max open puts, halt conditions).
* Execution enhancements (re-quote if not filled, cancel/replace toward mid).
* **Optional AI tuner** to adapt Δ/DTE/markup from realized outcomes (paper-first, guard-railed).

---

## Quick Commands

```bash
# Activate venv
# Windows:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

# One-shot CSP preview
python run.py --symbol SPY --delta 0.25 --dte-min 30 --dte-max 45 --qty 1 --dry

# Start Wheel loop (paper, delayed)
python wheel_wheel.py --symbol SPY --qty 1 --loop 300 --dry

# Start Wheel with live data (requires subs)
python wheel_wheel.py --symbol SPY --qty 1 --loop 300 --live
```

---

**License:** MIT

---
