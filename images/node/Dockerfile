# NetTwin twin node: admin, staff, hr, nvr, attacker all use this image.
FROM alpine:3.20

# busybox-extras provides httpd (the NVR stand-in); Alpine's base busybox doesn't ship it.
RUN apk add --no-cache nmap netcat-openbsd bind-tools curl iproute2 iputils busybox-extras \
 && mkdir -p /srv/www \
 && echo '<html><body><h1>NVR web UI</h1><p>NetTwin stand-in</p></body></html>' > /srv/www/index.html

# Idle by default; nvr overrides this with: busybox-extras httpd -f -p 8443 -h /srv/www
CMD ["sleep", "2147483647"]
