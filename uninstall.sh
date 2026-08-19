#!/bin/bash
set -euo pipefail

# Tailarr uninstaller. Reverses the install manifest ($PODS_DIR/.manifest.json)
# the controller writes: stops and removes the pods, removes every podman named
# volume carrying Tailarr's ownership label, undoes the boot wiring and host
# files it recorded, and deletes the data root (which holds ALL config and
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
UNSWEPT_VOLUMES=0   # set when podman could not be asked about named volumes
WORK_TMP="$(mktemp -d)"
trap 'rm -rf "$WORK_TMP"' EXIT
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
echo "[1/6] Containers"
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

# --- 2. Podman named volumes (DERIVED, and UNCONDITIONAL) -----------------
# Every volume Tailarr owns carries one ownership label, and this asks podman
# which volumes carry it. Two properties, both deliberate:
#
#   DERIVED — no volume name appears here, so a volume introduced by a later
#   release is removed by this same line with no edit to this script.
#
#   UNCONDITIONAL — it does NOT read the manifest. The volume is created by the
#   Go controller, which never writes $PODS_DIR/.manifest.json, and a host
#   upgraded from a release predating the receipt has a manifest that lacks it
#   entirely. Driving the sweep off the receipt therefore left the volume behind
#   on precisely the hosts that already exist, while the dry-run told the
#   operator the host was clean. Running it always is safe: on a host with no
#   such volumes it prints one line and does nothing.
#
# Named volumes live OUTSIDE the data root, so step 6's `rm -rf` never reaches
# them.
VOLUME_SELECTOR="label=io.tailarr.managed=1"
echo "[2/6] Podman named volumes ($VOLUME_SELECTOR)"
if command -v podman >/dev/null 2>&1; then
    # ⚠️ CAPTURE THE EXIT CODE. "There are none" and "I could not ask" are
    # different facts and must never print the same way — a locked bolt DB or a
    # broken storage.conf (both plausible right after step 1 tore the containers
    # down) makes `volume ls` fail, and swallowing that reported the host CLEAN
    # while tailarr-recyclarr-state leaked. The read-side code already refuses to
    # collapse these (podvol.List, collect_volumes); the DESTRUCTIVE consumer is
    # the one that must not.
    vol_err="$WORK_TMP/volume-ls.err"
    # ⚠️ `cmd || var=$?` is the one form that gets BOTH of these right:
    #   * the left side of || is exempt from errexit, so a failure is not fatal
    #     (a bare `vols="$(...)"` capture kills the whole script under `set -e`);
    #     A bare capture would abort the uninstall at step 2 with only the step
    #     header printed, leaving the boot wiring, the NFS export and the data
    #     root all in place — strictly worse than the swallow this replaced.
    #   * $? on the right is the LEFT command's status. Inside `if ! cmd; then`
    #     it would instead be the NEGATION's status, i.e. 0 — which reads as
    #     success and re-collapses the failure into "there are none".
    # Both mistakes were made here in turn. Gated by tests/uninstall-volumes.sh
    # §6, which asserts the run still COMPLETES and reaches [6/6].
    vol_rc=0
    vols="$(podman volume ls --filter "$VOLUME_SELECTOR" --format '{{.Name}}' 2>"$vol_err")" || vol_rc=$?
    if [[ "$vol_rc" -ne 0 ]]; then
        echo "  [WARNING] \`podman volume ls\` FAILED (exit $vol_rc) — Tailarr's named"
        echo "            volumes were NOT swept and may still exist on this host."
        if [[ -s "$vol_err" ]]; then
            # A read loop rather than `sed`: one fewer external command in the
            # destructive path, and podman's own reason must reach the operator
            # even on a host where sed is odd.
            while IFS= read -r line; do echo "            $line"; done < "$vol_err"
        fi
        echo "            Re-run this script, or remove them by hand:"
        echo "              podman volume ls --filter $VOLUME_SELECTOR"
        echo "              podman volume rm -f <name>"
        # A partial stdout before the failure is NOT a partial sweep: removing
        # some of them would leave the operator believing the step succeeded.
        UNSWEPT_VOLUMES=1
    elif [[ -z "$vols" ]]; then
        echo "  (no podman volumes carry $VOLUME_SELECTOR)"
    else
        while IFS= read -r vol; do
            [[ -n "$vol" ]] || continue
            step podman volume rm -f "$vol"
        done <<< "$vols"
    fi
else
    echo "  (podman not found — named volumes matching $VOLUME_SELECTOR left in place)"
    UNSWEPT_VOLUMES=1
fi
echo ""

# --- 3. Reverse the recorded host mutations -------------------------------
echo "[3/6] Boot wiring + host files"
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
                #
                # ⚠️ RESTORED. Moving the volume sweep out of this loop deleted
                # this case as collateral: the old sweep body sat immediately
                # above it, and removing the pair left `file-line` falling to the
                # `*)` catch-all. The manifest records one — the appended
                # `network_cmd_options` MTU line — so an uninstall printed
                # "unknown manifest entry" and left Tailarr's line in the SHARED
                # /etc/containers/containers.conf forever, after saying
                # "Uninstall complete."
                if [[ -n "$marker" && -f "$target" ]]; then
                    step sed -i "/^[^#]*$marker/d" "$target"
                fi ;;
            podman-volumes)
                # Acknowledged, deliberately NOT acted on here. The volume sweep
                # runs UNCONDITIONALLY in step 2 below, because correctness must
                # not depend on this receipt existing: the volume is created by
                # the Go controller, which never writes the manifest, and a box
                # upgraded from a release that predates this entry has a manifest
                # WITHOUT it. Acting only here left the volume on exactly the
                # boxes that already exist. Kept as a case so the entry does not
                # print as "unknown".
                : ;;
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

# --- 4. Packages ----------------------------------------------------------
echo "[4/6] Packages: only removed with --purge-packages AND when Tailarr"
echo "      recorded that IT installed them (see notes above)."
echo ""

# --- 5. Tailnet (manual — the honest limit) -------------------------------
echo "[5/6] Tailscale"
echo "  Tailarr's devices (tagged tag:tailarr*) and the fenced ACL regions"
echo "  (// >>> tailarr-managed:grants|tagowners|nodeattrs) remain in your"
echo "  admin console. Removing the data root below erases the local identity,"
echo "  but the cloud objects are only deletable with your OAuth client —"
echo "  delete the tag:tailarr* devices and the fenced blocks there (30 sec)."
echo ""

# --- 6. The data root (all config + secrets + image storage) --------------
echo "[6/6] Data root"
if [[ "$KEEP_DATA" -eq 1 ]]; then
    echo "  --keep-data: leaving $PODS_DIR in place."
elif [[ -d "$PODS_DIR" ]]; then
    step rm -rf "$PODS_DIR"
else
    echo "  ($PODS_DIR already gone)"
fi
echo ""

if [[ "$UNSWEPT_VOLUMES" -eq 1 ]]; then
    echo "⚠️  Podman named volumes were NOT verified as removed (see [2/6] above)."
    echo "    They live OUTSIDE the data root, so deleting it did not remove them."
    echo ""
fi
if [[ "$DO_IT" -eq 1 ]]; then
    echo "=== Uninstall complete. ==="
else
    echo "=== Dry run only — nothing changed. Re-run with --yes (or"
    echo "    UNINSTALL_YES=1) to execute the plan above. ==="
fi
