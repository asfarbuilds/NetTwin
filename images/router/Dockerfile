# NetTwin twin router: nftables + dnsmasq (local records only) + dropbear (banner only).
FROM alpine:3.20

# Host key is generated at build time: the root filesystem is read-only at runtime.
RUN apk add --no-cache nftables dnsmasq dropbear iproute2 \
 && mkdir -p /etc/dropbear \
 && dropbearkey -t ed25519 -f /etc/dropbear/dropbear_ed25519_host_key >/dev/null

COPY --chmod=0644 dnsmasq-default.conf /etc/dnsmasq-default.conf
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
