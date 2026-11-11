-- Views over the raw events table. setup_bigquery.sh replaces @DS@ with project.dataset.
-- Every view filters on `day` where it can, so Data Studio date ranges prune partitions.

CREATE OR REPLACE VIEW `@DS@.decisions` AS
SELECT
  day, ts, bot, symbol, decision_id, model, mode, action, p,
  SAFE_CAST(JSON_VALUE(payload, '$.expected_move_bps') AS FLOAT64) AS expected_move_bps,
  SAFE_CAST(JSON_VALUE(payload, '$.cost_bps') AS FLOAT64) AS cost_bps,
  SAFE_CAST(JSON_VALUE(payload, '$.gross_edge_bps') AS FLOAT64) AS gross_edge_bps
FROM `@DS@.events`
WHERE type = 'decision';

CREATE OR REPLACE VIEW `@DS@.answers` AS
SELECT
  day, ts, bot, decision_id, model,
  JSON_VALUE(payload, '$.battery_version') AS battery_version,
  SAFE_CAST(JSON_VALUE(payload, '$.latency_s') AS FLOAT64) AS latency_s,
  SAFE_CAST(JSON_VALUE(payload, '$.usage.input_tokens') AS INT64) AS input_tokens,
  SAFE_CAST(JSON_VALUE(payload, '$.usage.output_tokens') AS INT64) AS output_tokens,
  JSON_QUERY(payload, '$.answers') AS answers_json
FROM `@DS@.events`
WHERE type = 'answer';

-- Simulated P&L from shadow mode. Never real money.
CREATE OR REPLACE VIEW `@DS@.hypothetical_trades` AS
SELECT
  day, ts, symbol, decision_id, mode,
  JSON_VALUE(payload, '$.side') AS side,
  JSON_VALUE(payload, '$.exit_reason') AS exit_reason,
  SAFE_CAST(JSON_VALUE(payload, '$.held_s') AS FLOAT64) AS held_s,
  SAFE_CAST(JSON_VALUE(payload, '$.notional_usd') AS FLOAT64) AS notional_usd,
  SAFE_CAST(JSON_VALUE(payload, '$.costs_usd') AS FLOAT64) AS costs_usd,
  gross_pnl_usd, net_pnl_usd,
  SAFE_CAST(JSON_VALUE(payload, '$.net_bps') AS FLOAT64) AS net_bps
FROM `@DS@.events`
WHERE type = 'hypothetical_trade_closed';

CREATE OR REPLACE VIEW `@DS@.rejections` AS
SELECT
  day, ts, bot, symbol,
  JSON_VALUE(payload, '$.stage') AS stage,
  JSON_VALUE(payload, '$.check') AS check_name,
  JSON_VALUE(payload, '$.detail') AS detail
FROM `@DS@.events`
WHERE type = 'rejection';

CREATE OR REPLACE VIEW `@DS@.daily` AS
SELECT
  day,
  COUNTIF(type = 'decision') AS decisions,
  COUNTIF(type = 'decision' AND action != 'hold') AS non_hold_decisions,
  COUNTIF(type = 'rejection') AS rejections,
  COUNTIF(type = 'veto') AS vetoes,
  COUNTIF(type IN ('error', 'feed_error', 'hypothetical_error')) AS errors,
  COUNTIF(type = 'stop_loss') AS stop_losses,
  COUNTIF(type = 'hypothetical_trade_closed') AS hypothetical_trades,
  ROUND(SUM(IF(type = 'hypothetical_trade_closed', net_pnl_usd, 0)), 2) AS hypothetical_net_pnl_usd,
  SAFE_DIVIDE(COUNTIF(type = 'hypothetical_trade_closed' AND net_pnl_usd > 0),
              COUNTIF(type = 'hypothetical_trade_closed')) AS hypothetical_win_rate,
  ARRAY_AGG(DISTINCT model IGNORE NULLS) AS models_seen
FROM `@DS@.events`
GROUP BY day;
