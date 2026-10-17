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
| `policy/` | Weights that combine answers into one probability per action, and one threshold per action: the break-even probability given expected move and costs, with a floor. |
| `risk/` | Hard vetoes before every order: max position, daily loss limit, leverage cap, range guard, fees-plus-gas, model version. `GuardedBroker` is the only way to place an order. A code-only stop-loss (`stop_loss_bps`) closes the position before the judge is asked, so it still fires when the judge is down. |
| `broker/` | `Broker` interface plus `PaperBroker`. `make_broker()` can only return a paper broker. |
| `filter/` | The funnel, cheapest check first: free cut, one-call depth check, hard facts in code, Jev, final pick. Every rejection is logged with its stage and check. |
| `book/` | One open position at a time (no scanning while one is open). A bench where the rejecting check sets how long a symbol sits out. |
| `logs/` | JSONL event log, one file per UTC day, in `logs/data/` (git-ignored). Scrubs secret-looking keys and known secret values. |
| `judge/` | The only process that holds the TypeSafe key. Desk calls it with a separate desk secret. Returns Jev's answers raw. |
| `reports/` | Calibration report (buckets, Brier, reliability SVG) and daily summary. |
| `desk/` | Config, market feeds, and the run loop. |

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
```

The desk refuses to start if `TYPESAFE_API_KEY` is in its environment. The judge refuses to start with `TYPESAFE_LOG_LEVEL=debug`, because the SDK logs full request bodies at debug.

Modes:
- `shadow`: Jev labels every decision, the policy and risk run, and the log records what would have happened. Nothing executes.
- `paper`: the policy executes against `PaperBroker`.

Tests: `python -m unittest discover -s tests -t .`

## Not built yet

- A live market data feed. The shipped feeds are synthetic and replay. Running 24/7 needs a real-time feed and a wall-clock loop in `desk/run.py`.
- Tuned numbers. Every threshold, weight and limit in `desk/config.py` and `policy/policy.py` is a starting default.
