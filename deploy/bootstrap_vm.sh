#!/usr/bin/env bash
# NetTwin: prepare a fresh Ubuntu 24.04 Vultr VM for Phase 0 (spike) and later phases.
# Usage (from the repo root on the VM):  sudo ./deploy/bootstrap_vm.sh
# Idempotent: safe to re-run.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi
TARGET_USER="${SUDO_USER:?run via sudo from your normal user, not a root shell}"

step() { echo; echo "==> $*"; }

step "1/6 Base packages (git, curl, nftables CLI, dig)"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git nftables dnsutils

step "2/6 Docker Engine from Docker's apt repo (docs.docker.com/engine/install/ubuntu)"
if ! command -v docker >/dev/null || ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin
fi
systemctl enable --now docker
# Note: the docker group is root-equivalent. This is a single-tenant demo VM; documented in the README.
usermod -aG docker "$TARGET_USER"

step "3/6 Kernel modules the twin router needs (it has no SYS_MODULE, so they must be loaded on the host)"
MODULES=(nf_tables nf_conntrack nf_nat nft_ct nft_chain_nat nft_nat nft_limit nft_reject_inet nft_masq)
printf '%s\n' "${MODULES[@]}" > /etc/modules-load.d/nettwin.conf
for m in "${MODULES[@]}"; do
    # Some of these are built into nf_tables on newer kernels; a missing module is a warning, not fatal.
    modprobe "$m" 2>/dev/null || echo "   warn: modprobe $m failed (may be built-in; the spike's nft test is authoritative)"
done

step "4/6 Disable Ubuntu's stock nftables.service (its config starts with 'flush ruleset')"
if systemctl is-active --quiet nftables.service; then
    # Stopping it runs 'nft flush ruleset', which also wipes Docker's rules, so restart Docker afterwards.
    systemctl stop nftables.service
    systemctl restart docker
fi
systemctl disable nftables.service 2>/dev/null || true

step "5/6 Data dirs"
install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" /var/lib/nettwin /var/lib/nettwin/attempts
install -d -m 0750 -o root -g "$TARGET_USER" /etc/nettwin

step "6/6 uv (Python manager) for $TARGET_USER"
if ! sudo -u "$TARGET_USER" bash -lc 'command -v uv' >/dev/null 2>&1; then
    sudo -u "$TARGET_USER" bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

echo
echo "Done. Log out and back in (or run 'newgrp docker') so '$TARGET_USER' can use docker without sudo."
echo "Next: ./scripts/spike.sh 2>&1 | tee spike-output.log"
