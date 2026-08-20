FROM nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236

RUN apk add --no-cache openssl

COPY infra/nginx/container.conf.template /etc/nginx/templates/tricycle.conf.template
COPY infra/nginx/api-locations.conf /etc/nginx/includes/api-locations.conf
COPY infra/nginx/docker-entrypoint.sh /usr/local/bin/tricycle-nginx-entrypoint

RUN mkdir -p /etc/nginx/tls \
    && chmod 0555 /usr/local/bin/tricycle-nginx-entrypoint

EXPOSE 8080 8443
STOPSIGNAL SIGQUIT

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=6 \
    CMD wget -q --no-check-certificate -O /dev/null https://127.0.0.1:8443/health/live || exit 1

ENTRYPOINT ["/usr/local/bin/tricycle-nginx-entrypoint"]
