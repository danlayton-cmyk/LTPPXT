# LTPPXT

Skeleton of a 24/7 AI trading desk. **Paper trading only.** There is no live broker, no
live mode, and no exchange key anywhere in this repo.

Jev (TypeSafe's typed-judgment model) answers questions about market state. Code decides
what to do with the answers, and code-only risk checks sit in front of every order.

## Layout

| dir | what it does |
|---|---|
| `state/` | Deterministic snapshot from raw ticks: mid, spread, imbalance, realized vol, returns over 1m/5m/15m, inventory, drawdown. Reads only ticks stamped before the decision. Rejects any snapshot estimated at 400 tokens or more. |
| `questions/` | One battery per bot (`gambler`, `house`, `spy`). One judgment per question. Every Choice gets an `other` option automatically. Resolvers turn later market data into outcomes for calibration. |
| `policy/` | Weights that combine answers into one probability per action, and one threshold per action: the break-even probability given expected move and costs, with a floor. Each bot has its own holding period (`DEFAULT_HORIZON_S_BY_BOT` in `desk/config.py`): gambler and spy 30 minutes, because they pay taker fees; house 5 minutes, because it pays maker fees. |
| `risk/` | Hard vetoes before every order: max position, daily loss limit, leverage cap, range guard, fees-plus-gas, model version. `GuardedBroker` is the only way to place an order. A code-only stop-loss (`stop_loss_bps`) closes the position before the judge is asked, so it still fires when the judge is down. |
| `broker/` | `Broker` interface plus `PaperBroker`. `make_broker()` can only return a paper broker. |
| `filter/` | The funnel, cheapest check first: free cut, one-call depth check, hard facts in code, Jev, final pick. Every rejection is logged with its stage and check. |
| `book/` | One open position at a time (no scanning while one is open). A bench where the rejecting check sets how long a symbol sits out. |
| `logs/` | JSONL event log, one file per UTC day, in `logs/data/` (git-ignored). Scrubs secret-looking keys and known secret values. |
| `judge/` | The only process that holds the TypeSafe key. Desk calls it with a separate desk secret. Returns Jev's answers raw. |
| `reports/` | Calibration report, one section per model and battery version (buckets, Brier, per-label rates, reliability SVG), and daily summary. |
| `desk/` | Config, market feeds (synthetic, replay, live Coinbase), and the run loop. |

## Jev

- The judge pins `jev-1.13.0` (`desk/config.py`). `jev-latest` and `jev-preview` are refused at startup. As of 2026-10-17 TypeSafe's models page lists `jev-latest` -> `jev-1.13.0` and `jev-preview` -> `jev-1.14.0`.
- Every answer is logged with the exact `model` string Jev returned. The risk layer vetoes any order whose answer came from a different version.
- Each decision sends the bot's whole battery in one call.
- The judge deadline is the start of the next block. A decision that misses it holds.
- Rate limits back off inside the deadline, using Jev's `retry-after-ms` when present, and put the desk in a short cooldown. A malformed-question error is never retried, and the symbol is benched for a day.
- SDK: `typesafe-sdk==0.7.5`, used only in `judge/`. I read TypeSafe's official agent skill (`typesafe-ai/skills`) and the SDK source to write this. I did not install the skill as a plugin.

## Running

Requires Python 3.11+ (written and tested on 3.13). The desk itself uses only the standard library.

```bash
# offline smoke test: synthetic data, fake judge, nothing leaves the machine
python -m desk.run --mode shadow --bot gambler --feed synthetic --judge fake --blocks 360
python -m reports.daily --day 2025-10-09
python -m reports.calibration --include-fake   # fake answers are excluded by default

# with Jev (two processes, two environments)
pip install -e '.[judge]'
DESK_JUDGE_SECRET=<32+ random chars> TYPESAFE_API_KEY=<your key> python -m judge.server
DESK_JUDGE_SECRET=<same value> python -m desk.run --mode shadow --bot spy --feed replay --replay-file ticks.jsonl

# live market data: Coinbase Exchange public level-1 book, no key. Runs until Ctrl-C.
DESK_JUDGE_SECRET=<same value> python -m desk.run --mode shadow --bot spy --feed coinbase
```

The desk refuses to start if `TYPESAFE_API_KEY` is in its environment. The judge refuses to start with `TYPESAFE_LOG_LEVEL=debug`, because the SDK logs full request bodies at debug.

The live feed polls `GET https://api.exchange.coinbase.com/products/{symbol}/book?level=1` once a second per symbol, stamps each tick with the exchange's `time` field, and keeps about 17 minutes in memory. On start it backfills that history from Coinbase's public 1-minute candles (one request per symbol), so trading can begin as soon as fresh book data arrives. Backfilled ticks are candle closes with no spread or size; they feed returns and realized vol only. If backfill fails, or with `--no-backfill`, the first 15 minutes are warm-up and every symbol is rejected as `insufficient_history`. Coinbase caches this endpoint for up to 2 s, so ticks arrive every 1-2 s. Data age is measured against your machine's clock, so keep it NTP-synced.

Modes:
- `shadow`: Jev labels every decision, the policy and risk run, and the log records what would have happened. Nothing executes.
  A hypothetical ledger (`desk/hypothetical.py`) gives each would-be entry that passed risk a simulated fill on its own `PaperBroker`, one trade at a time, and closes it at the stop-loss or the bot's holding period without asking Jev. `ltppxt daily` reports the result under "Hypothetical trades". It is simulated P&L, never real, and it does not change what shadow mode scans, asks or logs otherwise.
- `paper`: the policy executes against `PaperBroker`. While a position is open it stops scanning other symbols and asks Jev the exit question every block.

Tests: `python -m unittest discover -s tests -t .`

## Running 24/7 on a VM

`deploy/setup.sh` sets up an always-on Debian 12+ or Ubuntu 24.04+ machine. It was tested on Debian 12 under systemd. A Google Cloud e2-micro is enough; third-party guides say one is free in us-west1, us-central1 and us-east1, but check Google's pricing and set a budget alert.

1. Create the VM: Compute Engine → Create instance → e2-micro, us-central1, Debian 12, 10 GB standard disk.
2. Tap **SSH** next to it, then run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/danlayton-cmyk/LTPPXT/main/deploy/setup.sh | sudo bash
   ```
3. Paste the TypeSafe key when asked. It doesn't echo, and it's stored in `/etc/ltppxt/judge.env`, readable only by root.

The judge and the desk run as two separate users and restart after crashes and reboots. The desk can't read the key file or the judge's environment. It starts in shadow mode with the house bot; to change that, put the settings before the command, e.g. `curl ... | sudo LTPPXT_BOT=spy bash`.

Check on it with `ltppxt status`, `ltppxt answers`, `ltppxt rejections`, `ltppxt daily`, `ltppxt calibration` and `ltppxt logs`. `ltppxt update` pulls the latest `main` and restarts. To change the key: `curl ... | sudo LTPPXT_ROTATE_KEY=1 bash`.

If `ltppxt answers` stays empty, check `ltppxt logs`: a `401 Unauthorized` from api.typesafe.ai means the key is wrong. Rejections such as `thin_book` mean no candidate reached Jev yet.

## Not built yet

- Tuned numbers. Every threshold, weight and limit in `desk/config.py` and `policy/policy.py` is a starting default.
