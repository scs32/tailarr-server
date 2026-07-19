# Tailarr controller image: the deployment engine plus the web UI.
# Runs as a pod on the host it manages, talking to the host's podman via a
# mounted socket (set CONTAINER_HOST=unix:///run/podman/podman.sock).

# ---- Stage 1: build the React/Vite SPA -----------------------------------
# The compiled output is arch-independent, so build it on the native build
# platform (fast, no QEMU) and copy the static assets into both arch images.
FROM --platform=$BUILDPLATFORM docker.io/library/node:22-alpine AS ui
WORKDIR /ui
COPY web/ui/package.json web/ui/package-lock.json ./
RUN npm ci
COPY web/ui/ ./
# design/tailarr.css is the design-system source of truth; refresh the app's
# copy from it at build time so releases always match the synced design.
COPY design/tailarr.css ./src/styles/tailarr.css
RUN npm run build

# ---- Stage 2: runtime ----------------------------------------------------
FROM docker.io/library/alpine:3.20

# skopeo: remote digest lookups for the daily image-update checks
# util-linux: nsenter, used by the NFS-export helper to run exportfs on the
# actual host (the controller manages /etc/exports.d through it)
RUN apk add --no-cache bash jq python3 podman skopeo util-linux
# uptime-kuma-api: socket.io client behind the Monitor tab (Kuma has no
# REST API for monitor CRUD)
RUN apk add --no-cache py3-pip \
    && pip install --no-cache-dir --break-system-packages uptime-kuma-api

WORKDIR /app
COPY create.sh error-handler.sh logging-utils.sh parse-service-config.sh \
     setup-service-env.sh generate-scripts.sh generate-run-template.sh \
     generate-diagnose-template.sh display-summary.sh homelab.js /app/
COPY catalogs/ /app/catalogs/
COPY web/app.py web/kuma_client.py /app/web/
COPY --from=ui /ui/dist /app/static

# Ship the boot-recovery script so a controller self-upgrade can refresh the
# host's /root/start-pods.sh. Its single source of truth is the heredoc in
# bootstrap-tailarr.sh (which must stay curl-able standalone) — extract it
# here rather than keeping a second copy that would drift. `test -s` +
# shebang check make a marker rename fail the build, not ship an empty file.
COPY bootstrap-tailarr.sh /tmp/bootstrap-tailarr.sh
RUN sed -n '\|^cat > /root/start-pods.sh|,\|^STARTEOF$|p' /tmp/bootstrap-tailarr.sh \
        | sed '1d;$d' > /app/start-pods.sh \
    && test -s /app/start-pods.sh \
    && head -1 /app/start-pods.sh | grep -q '^#!/bin/sh' \
    && chmod +x /app/start-pods.sh \
    && rm /tmp/bootstrap-tailarr.sh

ENV APP_DIR=/app \
    PODS_DIR=/root/Pods \
    STATIC_DIR=/app/static \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python3", "/app/web/app.py"]
