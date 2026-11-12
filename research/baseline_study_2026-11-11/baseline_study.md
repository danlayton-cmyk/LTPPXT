# Baseline study on public candles

Data: Coinbase Exchange public 1-minute candles, 2026-08-13T00:00:00Z to 2026-11-11T00:00:00Z. BTC-USD: 129597 of 129600 minutes, sha256 af8cd35e81b9; ETH-USD: 129596 of 129600 minutes, sha256 0113e96e46f1; SOL-USD: 129593 of 129600 minutes, sha256 7c3d224fa4a6.
Bootstrap: whole UTC days, seed 20261111. Holm family: 24 test-fold comparisons.

## Folds

| horizon | fold | rows | from (UTC) | to (UTC) | days |
|---|---|---|---|---|---|
| 5m | train | 46565 | 2026-08-13 04:01 | 2026-10-06 01:26 | 55 |
| 5m | validation | 15483 | 2026-10-06 02:36 | 2026-10-24 00:36 | 19 |
| 5m | test | 15481 | 2026-10-24 01:46 | 2026-11-10 23:51 | 18 |
| 30m | train | 7761 | 2026-08-13 04:01 | 2026-10-06 01:01 | 55 |
| 30m | validation | 2577 | 2026-10-06 03:01 | 2026-10-24 00:01 | 19 |
| 30m | test | 2577 | 2026-10-24 02:01 | 2026-11-10 23:01 | 18 |
| 240m | train | 969 | 2026-08-13 04:01 | 2026-10-05 20:01 | 54 |
| 240m | validation | 315 | 2026-10-06 08:01 | 2026-10-23 16:01 | 18 |
| 240m | test | 318 | 2026-10-24 04:01 | 2026-11-10 16:01 | 18 |

## Forecast skill against the training base rate

Brier difference below zero favors the model. The interval is a 95% day-block bootstrap. `p` is the one-sided bootstrap share of resamples with no improvement; `p Holm` adjusts it across every test-fold comparison in this report.

| horizon | target | fold | model | n | event rate | Brier | Brier - base (95% interval) | skill | AUC | p | p Holm |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 5m | big_move | validation | base_rate | 15483 | 0.319 | 0.21724 | - | - | 0.500 | - | - |
| 5m | big_move | validation | rolling_rate | 15483 | 0.319 | 0.20416 | -0.01307 (-0.01977 to -0.00760) | +0.0602 | 0.652 | 0.000 | - |
| 5m | big_move | validation | vol_logit | 15483 | 0.319 | 0.18755 | -0.02969 (-0.03711 to -0.02220) | +0.1367 | 0.729 | 0.000 | - |
| 5m | big_move | validation | momentum_logit | 15483 | 0.319 | 0.21765 | +0.00041 (+0.00007 to +0.00076) | -0.0019 | 0.484 | 0.990 | - |
| 5m | big_move | validation | full_logit | 15483 | 0.319 | 0.18734 | -0.02990 (-0.03720 to -0.02243) | +0.1376 | 0.730 | 0.000 | - |
| 5m | big_move | test | base_rate | 15481 | 0.345 | 0.22615 | - | - | 0.500 | - | - |
| 5m | big_move | test | rolling_rate | 15481 | 0.345 | 0.21165 | -0.01450 (-0.02180 to -0.00803) | +0.0641 | 0.654 | 0.000 | 0.000 |
| 5m | big_move | test | vol_logit | 15481 | 0.345 | 0.19013 | -0.03602 (-0.04352 to -0.02814) | +0.1593 | 0.743 | 0.000 | 0.000 |
| 5m | big_move | test | momentum_logit | 15481 | 0.345 | 0.22672 | +0.00057 (-0.00001 to +0.00117) | -0.0025 | 0.477 | 0.973 | 1.000 |
| 5m | big_move | test | full_logit | 15481 | 0.345 | 0.19017 | -0.03597 (-0.04355 to -0.02822) | +0.1591 | 0.743 | 0.000 | 0.000 |
| 5m | up | validation | base_rate | 15483 | 0.488 | 0.24989 | - | - | 0.500 | - | - |
| 5m | up | validation | rolling_rate | 15483 | 0.488 | 0.25079 | +0.00090 (+0.00056 to +0.00127) | -0.0036 | 0.496 | 1.000 | - |
| 5m | up | validation | vol_logit | 15483 | 0.488 | 0.24995 | +0.00006 (-0.00001 to +0.00013) | -0.0002 | 0.488 | 0.952 | - |
| 5m | up | validation | momentum_logit | 15483 | 0.488 | 0.24986 | -0.00003 (-0.00038 to +0.00035) | +0.0001 | 0.516 | 0.441 | - |
| 5m | up | validation | full_logit | 15483 | 0.488 | 0.25002 | +0.00013 (-0.00030 to +0.00060) | -0.0005 | 0.510 | 0.714 | - |
| 5m | up | test | base_rate | 15481 | 0.484 | 0.24984 | - | - | 0.500 | - | - |
| 5m | up | test | rolling_rate | 15481 | 0.484 | 0.25049 | +0.00065 (+0.00023 to +0.00107) | -0.0026 | 0.506 | 0.999 | 1.000 |
| 5m | up | test | vol_logit | 15481 | 0.484 | 0.24982 | -0.00002 (-0.00008 to +0.00003) | +0.0001 | 0.506 | 0.209 | 1.000 |
| 5m | up | test | momentum_logit | 15481 | 0.484 | 0.24941 | -0.00044 (-0.00090 to +0.00011) | +0.0017 | 0.534 | 0.053 | 0.856 |
| 5m | up | test | full_logit | 15481 | 0.484 | 0.24945 | -0.00040 (-0.00101 to +0.00033) | +0.0016 | 0.529 | 0.124 | 1.000 |
| 30m | big_move | validation | base_rate | 2577 | 0.311 | 0.21449 | - | - | 0.500 | - | - |
| 30m | big_move | validation | rolling_rate | 2577 | 0.311 | 0.21154 | -0.00295 (-0.00977 to +0.00463) | +0.0138 | 0.579 | 0.223 | - |
| 30m | big_move | validation | vol_logit | 2577 | 0.311 | 0.18858 | -0.02591 (-0.03565 to -0.01659) | +0.1208 | 0.716 | 0.000 | - |
| 30m | big_move | validation | momentum_logit | 2577 | 0.311 | 0.21487 | +0.00038 (-0.00027 to +0.00110) | -0.0018 | 0.497 | 0.864 | - |
| 30m | big_move | validation | full_logit | 2577 | 0.311 | 0.18865 | -0.02584 (-0.03547 to -0.01681) | +0.1205 | 0.715 | 0.000 | - |
| 30m | big_move | test | base_rate | 2577 | 0.331 | 0.22145 | - | - | 0.500 | - | - |
| 30m | big_move | test | rolling_rate | 2577 | 0.331 | 0.21770 | -0.00375 (-0.00801 to +0.00064) | +0.0169 | 0.580 | 0.046 | 0.810 |
| 30m | big_move | test | vol_logit | 2577 | 0.331 | 0.19024 | -0.03121 (-0.04407 to -0.01841) | +0.1409 | 0.726 | 0.000 | 0.000 |
| 30m | big_move | test | momentum_logit | 2577 | 0.331 | 0.22242 | +0.00096 (-0.00044 to +0.00227) | -0.0044 | 0.487 | 0.911 | 1.000 |
| 30m | big_move | test | full_logit | 2577 | 0.331 | 0.18963 | -0.03183 (-0.04426 to -0.01951) | +0.1437 | 0.728 | 0.000 | 0.000 |
| 30m | up | validation | base_rate | 2577 | 0.500 | 0.25005 | - | - | 0.500 | - | - |
| 30m | up | validation | rolling_rate | 2577 | 0.500 | 0.25107 | +0.00102 (+0.00028 to +0.00173) | -0.0041 | 0.469 | 0.997 | - |
| 30m | up | validation | vol_logit | 2577 | 0.500 | 0.25007 | +0.00001 (-0.00021 to +0.00024) | -0.0001 | 0.498 | 0.545 | - |
| 30m | up | validation | momentum_logit | 2577 | 0.500 | 0.25156 | +0.00151 (-0.00084 to +0.00448) | -0.0060 | 0.493 | 0.863 | - |
| 30m | up | validation | full_logit | 2577 | 0.500 | 0.25220 | +0.00214 (-0.00080 to +0.00567) | -0.0086 | 0.495 | 0.914 | - |
| 30m | up | test | base_rate | 2577 | 0.492 | 0.25018 | - | - | 0.500 | - | - |
| 30m | up | test | rolling_rate | 2577 | 0.492 | 0.25093 | +0.00076 (-0.00014 to +0.00152) | -0.0030 | 0.482 | 0.960 | 1.000 |
| 30m | up | test | vol_logit | 2577 | 0.492 | 0.25014 | -0.00004 (-0.00019 to +0.00013) | +0.0001 | 0.509 | 0.312 | 1.000 |
| 30m | up | test | momentum_logit | 2577 | 0.492 | 0.25261 | +0.00244 (+0.00073 to +0.00425) | -0.0097 | 0.487 | 0.996 | 1.000 |
| 30m | up | test | full_logit | 2577 | 0.492 | 0.25262 | +0.00245 (+0.00012 to +0.00484) | -0.0098 | 0.500 | 0.980 | 1.000 |
| 240m | big_move | validation | base_rate | 315 | 0.343 | 0.22588 | - | - | 0.500 | - | - |
| 240m | big_move | validation | rolling_rate | 315 | 0.343 | 0.21968 | -0.00620 (-0.01450 to +0.00195) | +0.0275 | 0.576 | 0.070 | - |
| 240m | big_move | validation | vol_logit | 315 | 0.343 | 0.21623 | -0.00965 (-0.02400 to +0.00404) | +0.0427 | 0.638 | 0.083 | - |
| 240m | big_move | validation | momentum_logit | 315 | 0.343 | 0.22662 | +0.00074 (-0.00137 to +0.00276) | -0.0033 | 0.457 | 0.780 | - |
| 240m | big_move | validation | full_logit | 315 | 0.343 | 0.21273 | -0.01315 (-0.02965 to +0.00195) | +0.0582 | 0.650 | 0.042 | - |
| 240m | big_move | test | base_rate | 318 | 0.327 | 0.22015 | - | - | 0.500 | - | - |
| 240m | big_move | test | rolling_rate | 318 | 0.327 | 0.21576 | -0.00440 (-0.01090 to +0.00181) | +0.0200 | 0.560 | 0.102 | 1.000 |
| 240m | big_move | test | vol_logit | 318 | 0.327 | 0.20619 | -0.01396 (-0.03597 to +0.00800) | +0.0634 | 0.669 | 0.103 | 1.000 |
| 240m | big_move | test | momentum_logit | 318 | 0.327 | 0.21952 | -0.00063 (-0.00308 to +0.00186) | +0.0029 | 0.552 | 0.283 | 1.000 |
| 240m | big_move | test | full_logit | 318 | 0.327 | 0.20445 | -0.01570 (-0.03352 to +0.00207) | +0.0713 | 0.675 | 0.045 | 0.810 |
| 240m | up | validation | base_rate | 315 | 0.463 | 0.25480 | - | - | 0.500 | - | - |
| 240m | up | validation | rolling_rate | 315 | 0.463 | 0.25277 | -0.00203 (-0.00463 to +0.00021) | +0.0080 | 0.483 | 0.040 | - |
| 240m | up | validation | vol_logit | 315 | 0.463 | 0.25514 | +0.00034 (-0.00119 to +0.00179) | -0.0014 | 0.491 | 0.679 | - |
| 240m | up | validation | momentum_logit | 315 | 0.463 | 0.25619 | +0.00139 (-0.00175 to +0.00460) | -0.0054 | 0.501 | 0.815 | - |
| 240m | up | validation | full_logit | 315 | 0.463 | 0.26636 | +0.01156 (+0.00234 to +0.01977) | -0.0454 | 0.438 | 0.995 | - |
| 240m | up | test | base_rate | 318 | 0.506 | 0.25122 | - | - | 0.500 | - | - |
| 240m | up | test | rolling_rate | 318 | 0.506 | 0.25130 | +0.00008 (-0.00371 to +0.00355) | -0.0003 | 0.439 | 0.508 | 1.000 |
| 240m | up | test | vol_logit | 318 | 0.506 | 0.25056 | -0.00066 (-0.00216 to +0.00071) | +0.0026 | 0.534 | 0.195 | 1.000 |
| 240m | up | test | momentum_logit | 318 | 0.506 | 0.24827 | -0.00295 (-0.00635 to +0.00001) | +0.0117 | 0.549 | 0.026 | 0.494 |
| 240m | up | test | full_logit | 318 | 0.506 | 0.25053 | -0.00069 (-0.00861 to +0.00747) | +0.0027 | 0.529 | 0.432 | 1.000 |

## Trading the direction forecast, test fold, before costs

| horizon | model | trades | hit rate | gross bps per trade (95% interval) |
|---|---|---|---|---|
| 5m | momentum_logit | 15481 | 0.515 | +0.20 (-0.22 to +0.62) |
| 5m | full_logit | 15481 | 0.511 | +0.19 (-0.24 to +0.61) |
| 30m | momentum_logit | 2577 | 0.496 | -1.98 (-4.65 to +0.47) |
| 30m | full_logit | 2577 | 0.498 | -1.57 (-3.97 to +0.76) |
| 240m | momentum_logit | 318 | 0.513 | -11.51 (-34.02 to +12.35) |
| 240m | full_logit | 318 | 0.516 | +0.42 (-21.90 to +22.64) |

## Ceiling: move size against round-trip cost

Mean absolute move is the gross per trade of perfect directional foresight. The break-even hit rate is (M + C) / 2M for a bet that wins or loses the mean absolute move M and pays C; above 1.00 no hit rate is enough.

Cost scenarios: 2 bps = repo default, maker in / maker out (what policy.py prices house at); 7 bps = repo default, maker in / taker out + 1 bp slippage; 12 bps = repo default, taker both ways + 1 bp slippage each; 140 bps = Coinbase US entry tier per PR #8, maker in / taker out (0.50% + 0.90%); 180 bps = Coinbase US entry tier per PR #8, taker both ways (0.90% x 2).

| symbol | horizon | windows | mean abs move | median | 95th pct | break-even hit rate at 2 bps | break-even hit rate at 7 bps | break-even hit rate at 12 bps | break-even hit rate at 140 bps | break-even hit rate at 180 bps |
|---|---|---|---|---|---|---|---|---|---|---|
| BTC-USD | 5m | 25870 | 7.3 | 4.7 | 22.4 | 0.636 | 0.977 | impossible | impossible | impossible |
| ETH-USD | 5m | 25869 | 9.6 | 6.3 | 29.2 | 0.604 | 0.865 | impossible | impossible | impossible |
| SOL-USD | 5m | 25868 | 12.4 | 8.5 | 36.7 | 0.581 | 0.783 | 0.985 | impossible | impossible |
| BTC-USD | 30m | 4311 | 17.5 | 11.3 | 53.9 | 0.557 | 0.699 | 0.842 | impossible | impossible |
| ETH-USD | 30m | 4311 | 23.0 | 14.3 | 68.8 | 0.544 | 0.652 | 0.761 | impossible | impossible |
| SOL-USD | 30m | 4311 | 29.4 | 19.7 | 88.9 | 0.534 | 0.619 | 0.704 | impossible | impossible |
| BTC-USD | 240m | 538 | 49.9 | 31.6 | 148.5 | 0.520 | 0.570 | 0.620 | impossible | impossible |
| ETH-USD | 240m | 538 | 68.1 | 41.9 | 228.5 | 0.515 | 0.551 | 0.588 | impossible | impossible |
| SOL-USD | 240m | 538 | 84.0 | 52.9 | 272.1 | 0.512 | 0.542 | 0.571 | impossible | impossible |
| BTC-USD | 1440m | 89 | 139.7 | 80.7 | 507.7 | 0.507 | 0.525 | 0.543 | impossible | impossible |
| ETH-USD | 1440m | 89 | 185.0 | 121.4 | 593.5 | 0.505 | 0.519 | 0.532 | 0.878 | 0.986 |
| SOL-USD | 1440m | 89 | 222.6 | 175.2 | 645.8 | 0.504 | 0.516 | 0.527 | 0.814 | 0.904 |
