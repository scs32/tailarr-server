#!/bin/bash
set -euo pipefail

# Tailarr uninstaller. Reverses the install manifest ($PODS_DIR/.manifest.json)
# the controller writes: stops and removes the pods, undoes the boot wiring and
# host files it recorded, and deletes the data root (which holds ALL config and
# secrets). Works even when the controller is already gone — it reads the
# manifest file directly and falls back to Tailarr's known locations if there
# is none.
#
# DRY-RUN BY DEFAULT: prints the exact plan and changes nothing. Pass --yes to
# execute. Run it the same way as the installer:
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/uninstall.sh)"
#   # add --yes once you've read the plan:
#   sudo env UNINSTALL_YES=1 bash -c "$(curl -fsSL .../uninstall.sh)"

PODS_DIR="${PODS_DIR:-/root/Pods}"
MANIFEST="$PODS_DIR/.manifest.json"
DO_IT="${UNINSTALL_YES:-0}"       # env fallback: --yes is awkward through curl|bash
KEEP_DATA=0
PURGE_PACKAGES=0

for arg in "$@"; do
    case "$arg" in
        --yes|-y) DO_IT=1 ;;
        --dry-run) DO_IT=0 ;;
        --keep-data) KEEP_DATA=1 ;;
        --purge-packages) PURGE_PACKAGES=1 ;;
        -h|--help)
            sed -n '3,16p' "$0" 2>/dev/null || echo "See the header of uninstall.sh."
            exit 0 ;;
        *) echo "[ERROR] unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERROR] Run as root — it removes root-owned pods, boot units, and $PODS_DIR." >&2
    echo "        sudo env UNINSTALL_YES=1 bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/uninstall.sh)\"" >&2
    exit 1
fi

NEED_RELOAD=0
NEED_EXPORTFS=0
SEP=$'\037'   # unit separator: a non-whitespace field delimiter, so EMPTY
              # fields survive `read` (tab, being IFS-whitespace, collapses them)

# Echo every step; run it only when not a dry-run. A failure never aborts the
# rest — a half-installed host should still get as clean as possible.
step() {
    echo "  \$ $*"
    if [[ "$DO_IT" -eq 1 ]]; then
        "$@" || echo "    (failed, continuing)"
    fi
}

if [[ "$DO_IT" -eq 1 ]]; then
    echo "=== Tailarr uninstall — EXECUTING ==="
else
    echo "=== Tailarr uninstall — DRY RUN (nothing will change; pass --yes to execute) ==="
fi
echo "Data root: $PODS_DIR"
echo ""

# --- 1. Stop and remove Tailarr's containers ------------------------------
# The controller (tailarr), the fixed infra pods, every service pod (a
# directory under the data root), and each pod's tailscale-<svc> sidecar.
echo "[1/5] Containers"
if command -v podman >/dev/null 2>&1; then
    names=(tailarr tailarr-upgrade tailarr-gate ntfy tailscale-tailarr)
    if [[ -d "$PODS_DIR" ]]; then
        for d in "$PODS_DIR"/*/; do
            [[ -d "$d" ]] || continue
            svc="$(basename "$d")"
            case "$svc" in .*) continue ;; esac
            names+=("$svc" "tailscale-$svc")
        done
    fi
    # de-dup
    mapfile -t names < <(printf '%s\n' "${names[@]}" | awk '!seen[$0]++')
    for n in "${names[@]}"; do
        if podman container exists "$n" 2>/dev/null; then
            step podman rm -f "$n"
        fi
    done
else
    echo "  (podman not found — skipping container removal)"
fi
echo ""

# --- 2. Reverse the recorded host mutations -------------------------------
echo "[2/5] Boot wiring + host files"
reverse_manifest() {
    # kind SEP target SEP marker SEP installed_by, one entry per line.
    while IFS="$SEP" read -r kind target marker installed_by; do
        case "$kind" in
            systemd-unit)
                step systemctl disable --now "$(basename "$target")"
                step rm -f "$target"; NEED_RELOAD=1 ;;
            systemd-dropin)
                step rm -f "$target"; NEED_RELOAD=1 ;;
            file)
                step rm -f "$target" ;;
            nfs-export)
                step rm -f "$target"; NEED_EXPORTFS=1 ;;
            file-line)
                # Strip only Tailarr's appended line(s) — never remove the
                # shared file. Match a non-comment line carrying the marker.
                if [[ -n "$marker" && -f "$target" ]]; then
                    step sed -i "/^[^#]*$marker/d" "$target"
                fi ;;
            package)
                if [[ "$installed_by" == "tailarr" && "$PURGE_PACKAGES" -eq 1 ]]; then
                    step apt-get -y remove "$target"
                else
                    echo "  (package '$target' left in place — installed_by=${installed_by:-unknown};" \
                         "remove by hand if nothing else needs it)"
                fi ;;
            data-root) : ;;  # handled in step 5
            *) echo "  (unknown manifest entry '$kind $target' — skipped)" ;;
        esac
    done
}

if command -v jq >/dev/null 2>&1 && [[ -f "$MANIFEST" ]]; then
    echo "  (from the manifest: $MANIFEST)"
    reverse_manifest < <(jq -r \
        ".entries[] | [.kind, .target, (.detail.marker // \"\"), (.detail.installed_by // \"\")] | join(\"$SEP\")" \
        "$MANIFEST")
else
    echo "  (no manifest — falling back to Tailarr's known locations)"
    reverse_manifest < <(printf '%s\n' \
        "systemd-unit${SEP}/etc/systemd/system/tailarr-pods.service${SEP}${SEP}" \
        "systemd-dropin${SEP}/etc/systemd/system/tailarr-pods.service.d/50-tailarr-mounts.conf${SEP}${SEP}" \
        "file${SEP}/root/start-pods.sh${SEP}${SEP}" \
        "nfs-export${SEP}/etc/exports.d/tailarr.exports${SEP}${SEP}" \
        "file-line${SEP}/etc/containers/containers.conf${SEP}network_cmd_options${SEP}")
    # The known drop-in also has a directory to remove.
    if [[ -d /etc/systemd/system/tailarr-pods.service.d ]]; then
        step rmdir --ignore-fail-on-non-empty /etc/systemd/system/tailarr-pods.service.d
    fi
fi
if [[ "$NEED_RELOAD" -eq 1 ]] && command -v systemctl >/dev/null 2>&1; then
    step systemctl daemon-reload
fi
if [[ "$NEED_EXPORTFS" -eq 1 ]] && command -v exportfs >/dev/null 2>&1; then
    step exportfs -ra
fi
echo ""

# --- 3. Packages ----------------------------------------------------------
echo "[3/5] Packages: only removed with --purge-packages AND when Tailarr"
echo "      recorded that IT installed them (see notes above)."
echo ""

# --- 4. Tailnet (manual — the honest limit) -------------------------------
echo "[4/5] Tailscale"
echo "  Tailarr's devices (tagged tag:tailarr*) and the fenced ACL regions"
echo "  (// >>> tailarr-managed:grants|tagowners|nodeattrs) remain in your"
echo "  admin console. Removing the data root below erases the local identity,"
echo "  but the cloud objects are only deletable with your OAuth client —"
echo "  delete the tag:tailarr* devices and the fenced blocks there (30 sec)."
echo ""

# --- 5. The data root (all config + secrets + image storage) --------------
echo "[5/5] Data root"
if [[ "$KEEP_DATA" -eq 1 ]]; then
    echo "  --keep-data: leaving $PODS_DIR in place."
elif [[ -d "$PODS_DIR" ]]; then
    step rm -rf "$PODS_DIR"
else
    echo "  ($PODS_DIR already gone)"
fi
echo ""

if [[ "$DO_IT" -eq 1 ]]; then
    echo "=== Uninstall complete. ==="
else
    echo "=== Dry run only — nothing changed. Re-run with --yes (or"
    echo "    UNINSTALL_YES=1) to execute the plan above. ==="
fi
