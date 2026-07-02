# HomePod Creator controller image: the deployment engine plus the web UI.
# Runs as a pod on the host it manages, talking to the host's podman via a
# mounted socket (set CONTAINER_HOST=unix:///run/podman/podman.sock).
FROM docker.io/library/alpine:3.20

RUN apk add --no-cache bash jq python3 podman

WORKDIR /app
COPY create.sh error-handler.sh logging-utils.sh parse-service-config.sh \
     setup-service-env.sh generate-scripts.sh generate-run-template.sh \
     generate-diagnose-template.sh display-summary.sh homelab.js /app/
COPY web/app.py /app/web/app.py

ENV APP_DIR=/app \
    PODS_DIR=/root/Pods \
    PORT=8080

EXPOSE 8080
CMD ["python3", "/app/web/app.py"]
