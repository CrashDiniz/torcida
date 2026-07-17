#!/usr/bin/env bash
# One-shot: switch the live TxLINE feed from devnet to mainnet on the VPS.
# Idempotent — safe to re-run any number of times. Designed to run unattended
# (cron) on 19/07 after the World Cup final; see CONTINUAR.md item 1b.
# Usage: flip_mainnet.sh [--dry-run]
set -euo pipefail

VPS="root@80.190.72.181"
KEY="$HOME/.ssh/torcida_vps"
SSH_CMD="ssh -i $KEY -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new $VPS"
LOG="${FLIP_LOG:-$HOME/torcida-flip.log}"
DRY="${1:-}"

log() { echo "[$(date '+%F %T %z')] $*" | tee -a "$LOG"; }

log "== mainnet flip start (dry=${DRY:-no}) =="

CURRENT=$($SSH_CMD "grep -oP 'TXLINE_API_BASE=\K.*' /home/torcida/app/.env")
if [[ "$CURRENT" == "https://txline.txodds.com" ]]; then
  log "already on mainnet — nothing to do"
  exit 0
fi
log "current base: $CURRENT"

MAIN_TOKEN=$($SSH_CMD "grep -oP '(TXLINE_API_TOKEN|MAINNET_API_TOKEN)=\K.*' /home/torcida/.keys/mainnet-api.env | head -1")
if [[ -z "$MAIN_TOKEN" ]]; then
  log "ERROR: mainnet token not found in /home/torcida/.keys/mainnet-api.env"
  exit 1
fi

# sanity: mainnet API must answer with this token before we touch anything
JWT=$(curl -s -X POST https://txline.txodds.com/auth/guest/start \
      -H 'Content-Type: application/json' -d '{}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
CODE=$(curl -s -o /dev/null -w '%{http_code}' \
      "https://txline.txodds.com/api/fixtures/snapshot?competitionId=72&startEpochDay=$(( $(date +%s)/86400 - 3 ))" \
      -H "Authorization: Bearer $JWT" -H "X-Api-Token: $MAIN_TOKEN")
if [[ "$CODE" != "200" ]]; then
  log "ERROR: mainnet fixtures returned $CODE — aborting, feed stays on devnet"
  exit 1
fi
log "mainnet API sanity: 200"

if [[ "$DRY" == "--dry-run" ]]; then
  log "dry-run: would flip .env (base/token/jwt) + restart services"
  exit 0
fi

$SSH_CMD "set -e
  cp /home/torcida/app/.env /home/torcida/app/.env.bak-devnet
  sed -i 's#^TXLINE_API_BASE=.*#TXLINE_API_BASE=https://txline.txodds.com#' /home/torcida/app/.env
  sed -i \"s#^TXLINE_API_TOKEN=.*#TXLINE_API_TOKEN=$MAIN_TOKEN#\" /home/torcida/app/.env
  sed -i 's#^TXLINE_JWT=.*#TXLINE_JWT=#' /home/torcida/app/.env
  systemctl restart torcida-web torcida-bot"
log "env flipped + services restarted"

sleep 8
SVC=$($SSH_CMD "systemctl is-active torcida-web torcida-bot | tr '\n' ' '")
WEB=$(curl -s -o /dev/null -w '%{http_code}' https://app.torcida.app)
log "services: ${SVC}· app.torcida.app: $WEB"

if [[ "$SVC" == "active active " && "$WEB" == "200" ]]; then
  log "== FLIP OK — live feed now on MAINNET =="
else
  log "WARNING: post-flip check failed — rolling back to devnet"
  $SSH_CMD "cp /home/torcida/app/.env.bak-devnet /home/torcida/app/.env
    systemctl restart torcida-web torcida-bot"
  log "rolled back — feed back on devnet, investigate manually"
  exit 1
fi
