# Google Cloud add-ons

Optional. The desk, judge, risk and broker code do not import anything here, and nothing
here can place an order. Three pieces:

| piece | what it does | runs where | cost (see notes) |
|---|---|---|---|
| `audit_vm.sh` | Read-only check of the project against the Compute Engine free tier: machine type, region, disk type and size, external IPv4, service account, unused IPs, snapshots, budgets. | Cloud Shell | $0 |
| `export_bq.py` + `setup_bigquery.sh` | Copies the desk's daily JSONL logs into BigQuery every hour, one partition per UTC day, with views for decisions, answers, hypothetical trades, rejections and a daily summary. | VM (systemd timer) + Cloud Shell once | $0 expected |
| `budget_guard/` + `setup_guardrails.sh` | Monthly budget with email alerts at 50/90/100%, plus a function that **stops** the VM labelled `ltppxt=desk` when actual spend reaches the line. Ships in dry-run mode. | Cloud Run function | $0 expected |

## Order

1. **Audit** (Cloud Shell): `PROJECT=<id> bash cloud/audit_vm.sh`. Fix anything flagged that you
   don't want to pay for. Note the VM name and zone it prints.
2. **Guardrails**: `PROJECT=<id> VM=<name> ZONE=<zone> bash cloud/setup_guardrails.sh`. This deploys
   in dry-run mode. Publish the test message it prints, read the function log, and when it
   names the right VM, re-run with `DRY_RUN=0`. Pick `BUDGET_USD` above your normal monthly
   spend (default 10), or the guard will stop the desk every month.
3. **BigQuery**: `PROJECT=<id> bash cloud/setup_bigquery.sh`, then follow the two steps it prints
   (attach the narrow service account to the VM, which needs a one-minute stop, and turn on
   the hourly timer). Until this branch is merged, add `LTPPXT_REF=claude/gcp-bigquery-guardrails`
   to the setup.sh command so the VM checks out code that has the exporter.
4. On the VM: `ltppxt export` runs one export now; `ltppxt egress` estimates monthly egress.

## Why these, and not what the research report first suggested

- **The desk stays on the VM.** It polls Coinbase once a second per symbol and keeps ~17
  minutes of ticks in memory, so it is a long-running process, not a 15-minute Cloud Run
  job. The VM is the right shape; the job is to keep it inside the free tier.
- **Jev is TypeSafe's model, not Claude.** A second-opinion judge would sit beside Jev, not
  replace a Claude call. See "Not built" below.
- **Stop the VM, don't disable billing.** Google documents that the disable-billing kill
  switch turns off every service in the project, free ones included. Stopping one labelled
  VM is the narrower pattern Google also documents.

## Cost notes

Source for limits: Google's Free Tier table (page last updated 2026-10-27, read 2026-11-06),
the BigQuery and Cloud Scheduler pricing pages, and the research report in this conversation.

- **BigQuery**: load jobs are free. 10 GiB storage and 1 TiB query per billing account per
  month are free. The offline smoke test writes about 1 KB of log per block; at the default
  10-second block that is roughly 9 MB/day before candidates, questions and answers, which
  the live desk adds. Expect well under 10 GiB for months. *Estimate, not measured on the VM.*
  Do not use the BigQuery sandbox for this: its tables expire after 60 days.
- **External IPv4**: research cites ~$0.005/hour (~$3.65/month) on a standard VM. The audit
  flags it; confirm on your bill. Removing it needs Cloud NAT for outbound traffic, which is
  also billed, so compare before changing.
- **Egress** (unverified, worth checking): `desk/coinbase_feed.py` opens a new HTTPS
  connection for every book request (`urllib.request.urlopen`), three symbols once a second,
  about 7.8 million requests a month. If each costs on the order of 1 KB outbound with the
  TLS handshake, that is several GB a month, above the 1 GB free egress. This is arithmetic,
  not a measurement. Run `ltppxt egress` on the VM for a real number before changing anything.

## Not verified here

No Google Cloud credentials were available where this was written, so none of the scripts
were run against a real project. What was checked: unit tests for the exporter and the
guard's decision logic, the guard function end to end against a mocked Compute client
(google-cloud-compute 1.x), shellcheck, and a dry run of the exporter on offline smoke-test
logs. Not checked: the BigQuery DDL and views against BigQuery, the `GRANT ... ON SCHEMA`
statement, the budget create flags, the function's Pub/Sub trigger permissions, and the
systemd units on a real VM.

## Not built (proposals)

- **Gemini shadow judge.** Send the same battery to Gemini Flash-Lite and log its answers as
  a separate model, so `reports/calibration.py` (already per model) scores Jev and Gemini side
  by side. Market snapshots are public data, so the free tier's data-use terms are less of a
  concern, but it adds a second key and a second network dependency.
- **Keep-alive Coinbase polling** to cut egress, if `ltppxt egress` shows it matters.
- **Data Studio dashboard** on the `daily` and `hypothetical_trades` views.
