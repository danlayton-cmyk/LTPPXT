#!/usr/bin/env bash
# One-time setup for an always-on Debian or Ubuntu VM, such as a Google Cloud e2-micro.
#
#   curl -fsSL https://raw.githubusercontent.com/danlayton-cmyk/LTPPXT/main/deploy/setup.sh | sudo bash
#
# Installs the desk under /opt/ltppxt and runs two services that restart on failure and on
# reboot: the judge (the only process that can read the TypeSafe key) and the desk
# (shadow mode, house bot, live Coinbase prices by default). It asks for the TypeSafe key
# once, reading it from the terminal with echo off. Re-running it updates the code and
# restarts the services without asking again.
#
# Settings, all optional, passed as environment variables to the sudo command:
#   LTPPXT_BOT=house|gambler|spy   LTPPXT_MODE=shadow|paper   LTPPXT_REF=main
#   LTPPXT_ROTATE_KEY=1            ask for a new TypeSafe key
#
# Paper trading only: there is no live mode, and nothing here holds an exchange key.
set -euo pipefail

REPO_URL="${LTPPXT_REPO:-https://github.com/danlayton-cmyk/LTPPXT.git}"
REF="${LTPPXT_REF:-main}"
BOT="${LTPPXT_BOT:-house}"
MODE="${LTPPXT_MODE:-shadow}"
APP=/opt/ltppxt
ETC=/etc/ltppxt

die() { echo "setup: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run it with sudo"
case "$BOT" in gambler|house|spy) ;; *) die "LTPPXT_BOT must be gambler, house or spy" ;; esac
case "$MODE" in shadow|paper) ;; *) die "LTPPXT_MODE must be shadow or paper" ;; esac

echo "Installing packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv ca-certificates >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "needs Python 3.11 or newer (Debian 12+, Ubuntu 24.04+)"

# Two users, so the desk process cannot read the judge's environment or key file.
id ltppxt >/dev/null 2>&1 || useradd --system --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin ltppxt
id ltppxt-judge >/dev/null 2>&1 || useradd --system --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin ltppxt-judge

echo "Fetching code ($REF)..."
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$REF"
  git -C "$APP" checkout -q --detach FETCH_HEAD
else
  git clone -q --branch "$REF" "$REPO_URL" "$APP"
fi
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -e "$APP[judge]"

install -d -m 755 -o root -g root "$ETC"
if [ ! -s "$ETC/judge.env" ] || [ "${LTPPXT_ROTATE_KEY:-0}" = 1 ]; then
  [ -r /dev/tty ] || die "no terminal to read the TypeSafe key from; run this in an SSH session"
  printf 'Paste your TypeSafe API key (it will not show), then press Enter: ' >/dev/tty
  IFS= read -rs key </dev/tty
  printf '\n' >/dev/tty
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  [ -n "$key" ] || die "no key entered"
  # Reuse the desk secret on a key rotation so the desk does not need a restart for it.
  secret="$(sed -n 's/^DESK_JUDGE_SECRET=//p' "$ETC/desk.env" 2>/dev/null || true)"
  [ -n "$secret" ] || secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  (
    umask 077
    printf 'TYPESAFE_API_KEY=%s\nDESK_JUDGE_SECRET=%s\n' "$key" "$secret" >"$ETC/judge.env"
    printf 'DESK_JUDGE_SECRET=%s\n' "$secret" >"$ETC/desk.env"
  )
  unset key secret
fi
# Root-only. systemd reads these as root and passes each service only its own file.
chown root:root "$ETC/judge.env" "$ETC/desk.env"
chmod 600 "$ETC/judge.env" "$ETC/desk.env"

echo "Installing services ($MODE, $BOT bot)..."
for unit in ltppxt-judge ltppxt-desk; do
  sed -e "s|@APP@|$APP|g" -e "s|@BOT@|$BOT|g" -e "s|@MODE@|$MODE|g" \
    "$APP/deploy/$unit.service" >"/etc/systemd/system/$unit.service"
done
install -m 755 "$APP/deploy/ltppxt" /usr/local/bin/ltppxt
systemctl daemon-reload
systemctl enable -q ltppxt-judge ltppxt-desk
systemctl restart ltppxt-judge ltppxt-desk

sleep 3
systemctl --no-pager --lines=0 status ltppxt-judge ltppxt-desk || true
cat <<EOF

Done. The desk is running in $MODE mode with the $BOT bot on live Coinbase prices.
It backfills recent history on start, so decisions can begin within a minute.

  ltppxt status        are both services up, and the last few events
  ltppxt logs          recent service output
  ltppxt answers       recent Jev answers (look for "model":"jev-1.13.0")
  ltppxt daily         today's summary: decisions, trades, PnL, risk vetoes
  ltppxt calibration   predicted probability vs outcome so far
  ltppxt update        pull the latest code and restart
EOF
