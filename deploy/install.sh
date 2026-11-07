#!/usr/bin/env bash
# Phase 5 deploy: Caddy (HTTPS) + systemd units for NetTwin and the host isolation rule.
# Run on the VM from the repo root:   sudo bash deploy/install.sh <domain>
#   e.g. sudo bash deploy/install.sh 203-0-113-7.sslip.io
# Prerequisites: deploy/bootstrap_vm.sh done, images built, /etc/nettwin/env filled in.
set -euo pipefail

DOMAIN="${1:?usage: sudo bash deploy/install.sh <domain>}"
[[ "$DOMAIN" =~ ^[a-z0-9.-]+$ ]] || { echo "bad domain: $DOMAIN" >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
TARGET_USER="${SUDO_USER:?run with sudo from your normal user}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$REPO" == "$TARGET_HOME/nettwin" ]] || { echo "expected the repo at $TARGET_HOME/nettwin (found $REPO)" >&2; exit 1; }

step() { echo; echo "==> $*"; }

step "1/5 checks"
[[ -f /etc/nettwin/env ]] || { echo "/etc/nettwin/env missing" >&2; exit 1; }
grep -q '^VULTR_MODEL=.' /etc/nettwin/env || { echo "VULTR_MODEL not set in /etc/nettwin/env" >&2; exit 1; }
[[ -x "$TARGET_HOME/.local/bin/uv" ]] || { echo "uv not found at $TARGET_HOME/.local/bin/uv" >&2; exit 1; }
docker image inspect nettwin/router:1 nettwin/node:1 >/dev/null || { echo "build the images first" >&2; exit 1; }

step "2/5 host isolation rule (persistent)"
install -m 0644 -o root -g root "$REPO/deploy/nettwin-host.nft" /etc/nettwin/host.nft
install -m 0644 "$REPO/deploy/nettwin-host.service" /etc/systemd/system/nettwin-host.service

step "3/5 NetTwin service"
sed -e "s|@USER@|$TARGET_USER|g" -e "s|@HOME@|$TARGET_HOME|g" "$REPO/deploy/nettwin.service" \
    > /etc/systemd/system/nettwin.service
sudo -u "$TARGET_USER" bash -lc "cd '$REPO' && '$TARGET_HOME/.local/bin/uv' sync --no-dev --frozen"

step "4/5 Caddy"
command -v caddy >/dev/null || { apt-get update -q && apt-get install -y -q caddy; }
sed "s|@DOMAIN@|$DOMAIN|g" "$REPO/deploy/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
if ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp && ufw allow 443/tcp
fi

step "5/5 start everything"
systemctl daemon-reload
systemctl enable --now nettwin-host.service
systemctl enable --now nettwin.service
systemctl enable caddy && systemctl restart caddy
sleep 5
systemctl --no-pager --lines=0 status nettwin-host nettwin caddy | grep -E "●|Active:"
nft list table inet nettwin_host >/dev/null && echo "host rule: loaded"
curl -fsS http://127.0.0.1:8000/api/health && echo
echo
echo "Done. Open https://$DOMAIN (the first visit may take ~30 s while Caddy gets the certificate)."
