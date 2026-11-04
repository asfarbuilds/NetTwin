#!/bin/sh
# Fixed router entrypoint. It never reads model output.
# /etc/nettwin is the per-attempt dir, bind-mounted read-only. dnsmasq.conf there is written by the
# control plane from validated data. The nft ruleset is loaded later by the executor via `nft -f`.
set -eu

CONF=/etc/nettwin/dnsmasq.conf
[ -f "$CONF" ] || CONF=/etc/dnsmasq-default.conf

# --no-daemon: stay in the foreground and skip the privilege drop entirely. With cap_drop=ALL, dnsmasq's
# setgroups()/setgid() fail even with --user=root --group=root (verified), and we refuse to add SETUID/SETGID.
dnsmasq --conf-file="$CONF" --no-daemon --log-facility=- &

# dropbear is PID 1. -s/-g: no password logins, -j/-k: no port forwarding. Only the banner matters.
exec dropbear -F -E -s -g -j -k -p 22 -P /run/dropbear.pid \
     -r /etc/dropbear/dropbear_ed25519_host_key
