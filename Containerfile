# Podscale controller image: the deployment engine plus the web UI.
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
# design/podscale.css is the design-system source of truth; refresh the app's
# copy from it at build time so releases always match the synced design.
COPY design/podscale.css ./src/styles/podscale.css
RUN npm run build

# ---- Stage 2: runtime ----------------------------------------------------
FROM docker.io/library/alpine:3.20

# skopeo: remote digest lookups for the daily image-update checks
RUN apk add --no-cache bash jq python3 podman skopeo

WORKDIR /app
COPY create.sh error-handler.sh logging-utils.sh parse-service-config.sh \
     setup-service-env.sh generate-scripts.sh generate-run-template.sh \
     generate-diagnose-template.sh display-summary.sh homelab.js /app/
COPY web/app.py /app/web/app.py
COPY --from=ui /ui/dist /app/static

ENV APP_DIR=/app \
    PODS_DIR=/root/Pods \
    STATIC_DIR=/app/static \
    PORT=8080

EXPOSE 8080
CMD ["python3", "/app/web/app.py"]
