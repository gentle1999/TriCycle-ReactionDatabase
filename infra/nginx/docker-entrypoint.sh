#!/bin/sh
set -eu

certificate=/etc/nginx/tls/tls.crt
private_key=/etc/nginx/tls/tls.key
server_name=${NGINX_SERVER_NAME:-localhost}

if { [ -f "$certificate" ] && [ ! -f "$private_key" ]; } || \
   { [ ! -f "$certificate" ] && [ -f "$private_key" ]; }; then
    echo "TLS certificate and private key must be provided together" >&2
    exit 1
fi

if [ ! -f "$certificate" ]; then
    if [ "${NGINX_REQUIRE_PROVIDED_CERTIFICATE:-false}" = "true" ]; then
        echo "NGINX_REQUIRE_PROVIDED_CERTIFICATE=true but tls.crt/tls.key are missing" >&2
        exit 1
    fi
    echo "No TLS certificate found; generating a self-signed certificate for $server_name" >&2
    umask 077
    openssl req \
        -x509 \
        -nodes \
        -newkey rsa:3072 \
        -days "${NGINX_SELF_SIGNED_CERT_DAYS:-30}" \
        -keyout "$private_key" \
        -out "$certificate" \
        -subj "/CN=$server_name" \
        -addext "subjectAltName=DNS:$server_name,DNS:localhost,IP:127.0.0.1"
fi

envsubst '${NGINX_SERVER_NAME}' \
    < /etc/nginx/templates/tricycle.conf.template \
    > /tmp/tricycle.conf

exec nginx -c /tmp/tricycle.conf -g 'daemon off;'
