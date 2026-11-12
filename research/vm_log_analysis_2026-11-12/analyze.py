"""Pre-specified before any score was looked at (see chat, 2026-11-12):
rows = house-v2 funnel answers (not exit questions) with a resolved outcome, answers as logged at the time;
per-symbol greedy thinning to non-overlapping answer->outcome windows; 60/20/20 chronological split with
(300 + 900) s purged; bootstrap over whole UTC days; Holm over every test-fold comparison.
Targets: toxic_flow, quote_lean bid / ask / neither. 'other' scored separately.
Forecasters: train base rate; logistic on realized vol; logistic on all snapshot fields; Jev as logged;
Jev recalibrated (logistic on Jev's own probability, fit on train); all fields + Jev's probability."""
import json, glob, math, sys, collections, datetime
import numpy as np
sys.path.insert(0, "/home/user/LTPPXT")
from decisionlab.evaluate import Logistic, auc, brier, log_loss, brier_difference, block_bootstrap_mean, holm, purged_chronological_split

root = sys.argv[1]
Q, A, O = {}, {}, {}
usage = collections.Counter()
for p in sorted(glob.glob(root + "/*.jsonl")):
    for line in open(p):
        e = json.loads(line); t = e["type"]
        if t == "question": Q[e["decision_id"]] = e
        elif t == "answer":
            A[e["decision_id"]] = e
            for k, v in (e.get("usage") or {}).items():
                if isinstance(v, (int, float)): usage[k] += v
        elif t == "outcome": O[e["decision_id"]] = e
print("answers", len(A), "outcomes", len(O), "answers without outcome", len(set(A) - set(O)))
print("usage totals:", dict(usage), "| calls:", len(A))

SYMS = ["BTC-USD", "ETH-USD", "SOL-USD"]
rows = []
other_p = []
for did, a in A.items():
    if a.get("battery_version") != "house-v2" or "-exit-" in did or did not in O or did not in Q: continue
    o = O[did]; lean, toxic = o["outcomes"].get("quote_lean"), o["outcomes"].get("toxic_flow")
    if lean is None or toxic is None: continue
    st = Q[did]["state"]; r = st.get("return_bps") or {}
    if any(r.get(k) is None for k in ("1m", "5m", "15m")): continue
    ql = a["answers"]["quote_lean"]["probabilities"]
    rows.append(dict(ts=a["ts"], ots=o["ts"], sym=st["symbol"], move=(o["mid_now"] / o["mid_then"] - 1) * 1e4,
                     x=[st["spread_bps"], st["book_imbalance"], math.log1p(st["realized_vol_bps"]), r["1m"], r["5m"], r["15m"], abs(r["1m"]), abs(r["5m"]), abs(r["15m"])],
                     y=dict(toxic=bool(toxic), bid=lean == "bid", ask=lean == "ask", neither=lean == "neither"),
                     jev=dict(toxic=float(a["answers"]["toxic_flow"]["noul"]), bid=float(ql.get("bid", 0)), ask=float(ql.get("ask", 0)), neither=float(ql.get("neither", 0))),
                     other=float(ql.get("other", 0))))
print("house-v2 funnel rows with outcomes:", len(rows), "| elapsed s (median, p95):", np.median([r["ots"] - r["ts"] for r in rows]), np.quantile([r["ots"] - r["ts"] for r in rows], .95))

# reproduce the review's way: all rows, hindsight base rate, 4-label mean incl. other
def review_style(rs, label):
    n = len(rs)
    tox = np.array([r["y"]["toxic"] for r in rs], float); jt = np.array([r["jev"]["toxic"] for r in rs])
    ql_j = np.mean([(r["jev"][k] - r["y"][k]) ** 2 for r in rs for k in ("bid", "ask", "neither")] + [r["other"] ** 2 for r in rs])
    rates = {k: np.mean([r["y"][k] for r in rs]) for k in ("bid", "ask", "neither")}
    ql_b = sum(v * (1 - v) for v in rates.values()) / 4
    print(f"{label}: n={n} quote_lean Jev {ql_j:.4f} vs hindsight base {ql_b:.4f} | toxic Jev {brier(jt, tox):.4f} vs base {tox.mean() * (1 - tox.mean()):.4f} | rates bid {rates['bid']:.3f} ask {rates['ask']:.3f} neither {rates['neither']:.3f} toxic {tox.mean():.3f} | Jev mean bid {np.mean([r['jev']['bid'] for r in rs]):.3f} ask {np.mean([r['jev']['ask'] for r in rs]):.3f} neither {np.mean([r['jev']['neither'] for r in rs]):.3f} toxic {jt.mean():.3f} other {np.mean([r['other'] for r in rs]):.3f}")
cut1 = datetime.datetime(2026, 10, 24, tzinfo=datetime.UTC).timestamp(); cut2 = datetime.datetime(2026, 11, 5, tzinfo=datetime.UTC).timestamp()
review_style([r for r in rows if r["ts"] < cut1], "house-v2 to Oct 23 (plan sample)")
review_style([r for r in rows if r["ts"] < cut2], "house-v2 to Nov 4 (review sample)")
review_style(rows, "house-v2, all")
print("mean Jev 'neither' vs 'toxic':", np.mean([r['jev']['neither'] for r in rows]), np.mean([r['jev']['toxic'] for r in rows]), "| share of rows where Jev prices neither < toxic:", np.mean([r['jev']['neither'] < r['jev']['toxic'] for r in rows]))

# thin per symbol
kept = []
for s in SYMS:
    free = -math.inf
    for r in sorted((r for r in rows if r["sym"] == s), key=lambda r: r["ots"]):
        if r["ts"] >= free: kept.append(r); free = r["ots"]
kept.sort(key=lambda r: r["ts"])
print("non-overlapping per symbol:", len(kept), dict(collections.Counter(r["sym"] for r in kept)))
t = np.array([r["ts"] for r in kept]); day = (t // 86400).astype(int)
X = np.array([r["x"] + [1.0 * (r["sym"] == "ETH-USD"), 1.0 * (r["sym"] == "SOL-USD")] for r in kept])
tr, va, te = purged_chronological_split(t, (0.6, 0.2, 0.2), purge=1200)
iso = lambda x: datetime.datetime.fromtimestamp(float(x), datetime.UTC).strftime("%m-%d %H:%M")
for n, f in (("train", tr), ("val", va), ("test", te)): print(f"  {n}: {len(f)} rows, {iso(t[f].min())} to {iso(t[f].max())}, {len(np.unique(day[f]))} days")

cells, fam = [], {}
for tg in ("toxic", "bid", "ask", "neither"):
    y = np.array([r["y"][tg] for r in kept], float); j = np.array([r["jev"][tg] for r in kept])
    jl = np.log(np.clip(j, 1e-4, 1 - 1e-4) / (1 - np.clip(j, 1e-4, 1 - 1e-4)))[:, None]
    P = {"base_rate": np.full(len(y), y[tr].mean()),
         "vol_logit": Logistic().fit(X[tr][:, [2]], y[tr]).predict(X[:, [2]]),
         "full_logit": Logistic().fit(X[tr], y[tr]).predict(X),
         "jev_logged": j,
         "jev_recalibrated": Logistic().fit(jl[tr], y[tr]).predict(jl),
         "full_plus_jev": Logistic().fit(np.hstack([X, jl])[tr], y[tr]).predict(np.hstack([X, jl]))}
    for fold, f in (("val", va), ("test", te)):
        for m, p in P.items():
            c = dict(target=tg, fold=fold, model=m, n=len(f), rate=y[f].mean(), brier=brier(p[f], y[f]), ll=log_loss(p[f], y[f]), auc=auc(p[f], y[f]))
            for ref in ("base_rate", "full_logit"):
                if m != ref and not (ref == "full_logit" and m in ("base_rate", "vol_logit")):
                    d = brier_difference(p[f], P[ref][f], y[f], day[f], seed=20261112)
                    c[f"vs_{ref}"] = (d["mean"], d["lo"], d["hi"], d["p_ge_zero"])
                    if fold == "test": fam[f"{tg}|{m}|{ref}"] = d["p_ge_zero"]
            cells.append(c)
adj = holm(fam)
print(f"\nHolm family: {len(fam)} test comparisons")
print("target fold model n rate brier logloss auc | Brier - base (95% CI) p holm | Brier - full_logit (95% CI) p holm")
for c in cells:
    def fmt(ref):
        k = f"vs_{ref}"
        if k not in c: return "-"
        m, lo, hi, p = c[k]; h = adj.get(f"{c['target']}|{c['model']}|{ref}")
        return f"{m:+.5f} ({lo:+.5f},{hi:+.5f}) p={p:.3f}" + (f" holm={h:.3f}" if h is not None else "")
    print(f"{c['target']:7s} {c['fold']:4s} {c['model']:16s} {c['n']} {c['rate']:.3f} {c['brier']:.5f} {c['ll']:.4f} {c['auc']:.3f} | {fmt('base_rate')} | {fmt('full_logit')}")

# decision-relevant: what happened when Jev was confident (entry needs lean >= ~0.6)
print("\nJev P(bid)/P(ask) top buckets on ALL rows (not thinned): threshold, n, observed rate of that label, mean move bps in the leaned direction")
for tg, sgn in (("bid", 1), ("ask", -1)):
    for th in (0.5, 0.6, 0.7):
        sel = [r for r in rows if r["jev"][tg] >= th]
        if sel: print(f"  {tg}>={th}: n={len(sel)} hit={np.mean([r['y'][tg] for r in sel]):.3f} mean signed move={np.mean([sgn * r['move'] for r in sel]):+.2f} bps")
