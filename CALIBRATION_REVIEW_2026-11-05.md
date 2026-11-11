# LTPPXT calibration review, 2026-11-05

## Bottom line
The house bot's questions were fixed on 2026-10-23 (v2), and Jev's probabilities still don't beat a constant guess of each outcome's average rate. The house bot has no usable signal at a 5-minute horizon and shouldn't move toward real money.

## Setup
- VM: Google Cloud `ltppxt`, us-central1-a, e2-micro.
- Mode: paper since 2026-10-24 00:05 UTC. Shadow mode before that, from 2026-10-21. House bot, Jev `jev-1.13.0`.
- Simulated money only. No exchange key, no real orders.
- Questions: `house-v2` (PR #4). Moves are measured against a 10 bps band over 5 minutes, instead of v1's spread-based band.

## History
1. **v1 (Oct 21–23):** the results were meaningless. BTC-USD's 1-cent spread is about 0.001 bps, so nearly every move counted as "more than two spreads". `quote_lean` came out `neither` 90% of the time, and `toxic_flow` came out true 89%. Fixed in PR #4.
2. **v2 (Oct 24 onward):** outcomes are balanced, so this is a fair test.

## v2 results (calibration report, 2026-11-04)
Brier score: lower is better. "Constant guess" means always predicting the average rate actually observed.

| question | decisions | Jev | constant guess | Jev vs. guess |
|---|---|---|---|---|
| quote_lean | 12,878 | 0.183 | 0.167 | 10% worse |
| toxic_flow | 12,878 | 0.219 | 0.221 | 1% better, effectively a tie |
| inventory_adverse | 157 | 0.216 | 0.170 | 27% worse |

- **Observed outcome rates:** `ask` 33.7%, `bid` 32.2%, `neither` 34.1%. `toxic_flow` true 33.1%.
- **Buckets are flat.** When Jev predicted 0.6–0.9, the event happened 38–48% of the time. When it predicted 0.2–0.5, it happened 31–37% of the time. Its confidence doesn't separate likely outcomes from unlikely ones.
- **Overconfident where it matters.** Jev predicts `ask` at 41% and `toxic_flow` at 41%, against 34% and 33% observed. Entries trigger at about 0.6+, and those calls come true less than 40% of the time.
- **Caveat.** Answers within one decision are correlated, so the effective sample is smaller than the counts. All three questions and the bucket table point the same way.

## Open questions
- **No position held since about Oct 29.** `inventory_adverse` has stayed at 157 pairs since then, and that question only resolves while a position is open. The cause is unknown: entries below threshold, resting orders that never fill, or risk vetoes. The daily summaries on the VM will show which.
- **Paper PnL hasn't been reviewed.** The trade logs live only on the VM, in `/var/lib/ltppxt/logs`. The Cloud Logging exports in Drive contain only Google's agent messages.
- **Jev credit.** It was still answering through about Nov 3, at roughly 1,000 decisions a day. Check the balance on the TypeSafe dashboard.
- **Fees.** Paper PnL assumes 0.01% maker and 0.05% taker (VIP 3, from the read-only Coinbase connector, which also shows a $0 balance). This isn't confirmed by an order preview.
- **Google Cloud budget alert.** Not set up yet.

## Command to get the trade results
Run in SSH-in-browser on the VM. It only reads.
```
ltppxt status | head -12; for d in $(sudo ls /var/lib/ltppxt/logs | sed 's/.jsonl//' | tail -12); do echo "== $d"; ltppxt daily $d | grep -E 'Mode|Decisions|Orders|round trips|Fees|PnL|Judge errors|veto'; done
```

## Next steps (pick one after seeing the trade results)
1. **Stop the house bot** to stop spending Jev credit: `ltppxt stop`.
2. **Test the gambler bot** at a 30-minute horizon, with no code changes: `LTPPXT_BOT=gambler LTPPXT_MODE=paper ltppxt update`. Review after 3 to 5 days, the same way as this one.
3. **Build a rules-only baseline:** a coded rule on the same snapshot, scored the same way. If it can't beat the average either, these inputs don't predict 5-minute moves, and no judge model will change that.

Don't tune thresholds or weights to rescue the house bot. With no signal in the inputs, tuning only fits noise.
