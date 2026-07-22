import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { NtfyStatus } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { FlashView, useFlash } from "../components/Flash";
import { BellIcon, SpinnerIcon } from "../components/Icons";

// Notifications ride an ntfy SYSTEM pod: Tailarr installs and manages it
// (accounts, topics, deny-all ACL) — it never appears to user devices and
// is not shareable. Setup is zero-input: one button writes the server
// config, restarts the pod, and provisions the controller's accounts.

export function Notifications() {
  const [status, setStatus] = useState<NtfyStatus | null>(null);
  const { flash, show, clear } = useFlash();
  const [busy, setBusy] = useState<"" | "setup" | "test">("");

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.ntfy());
    } catch (e) {
      show({ kind: "err", text: String(e) });
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  async function setup() {
    setBusy("setup");
    try {
      const r = await api.ntfySetup();
      show(
        r.ok
          ? {
              kind: "ok",
              text: r.test_error
                ? `Set up — but the test message failed: ${r.test_error}`
                : "Notifications are set up (a test message was delivered).",
            }
          : { kind: "err", text: r.error ?? "Setup failed." },
      );
      await refresh();
    } finally {
      setBusy("");
    }
  }

  async function test() {
    setBusy("test");
    try {
      const r = await api.ntfyTest();
      show(
        r.ok
          ? { kind: "ok", text: "Test notification sent." }
          : { kind: "err", text: r.error ?? "Test failed." },
      );
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <div className="section-title">Notifications</div>
      <FlashView flash={flash} onClose={clear} />

      {status && !status.installed && (
        <div className="card panel">
          <p className="field__hint">
            Tailarr sends update, health, and lifecycle alerts through{" "}
            <strong>ntfy</strong>, a small notification server that runs as
            a managed system pod — invisible to user devices, locked to
            deny-all, controlled only from this page.
          </p>
          <Link className="btn btn--primary" to="/install/ntfy">
            Install ntfy
          </Link>
        </div>
      )}

      {status && status.installed && !status.configured && (
        <div className="card panel">
          <p className="field__hint">
            The ntfy pod is deployed ({status.state || "state unknown"}).
            One click writes its server config (authentication on,
            deny-all access), restarts it, and creates the accounts
            Tailarr publishes with.
          </p>
          <button
            className="btn btn--primary"
            onClick={setup}
            disabled={!!busy}
          >
            {busy === "setup" ? <SpinnerIcon /> : <BellIcon />} Set up
            notifications
          </button>
        </div>
      )}

      {status && status.configured && (
        <>
          {status.publish_error && (
            <Alert kind="err">
              The last notification failed to send: {status.publish_error}
            </Alert>
          )}
          <div className="card panel">
            <div className="row-list">
              <div>
                <div className="row__title">
                  {status.pod}{" "}
                  <span
                    className={
                      "chip" +
                      (status.state === "running" ? " chip--installed" : "")
                    }
                  >
                    {status.state || "unknown"}
                  </span>{" "}
                  <span className="chip">system pod</span>
                </div>
                <div className="row__meta">
                  Admin alerts publish to the <code>{status.ops_topic}</code>{" "}
                  topic: pod updates available, controller upgrades and
                  rollbacks, pods going down or recovering, identity-tag
                  problems.
                </div>
              </div>
              <div>
                <div className="row__title">Phone delivery</div>
                <div className="row__meta">
                  {status.funnel_on ? (
                    <>
                      Public endpoint (token-protected):{" "}
                      <code>{status.public_url || "enrolling…"}</code>
                    </>
                  ) : (
                    <>
                      Funnel is <strong>off</strong> — phones outside the
                      tailnet can’t receive. Turn it on for {status.pod} from
                      the <Link to="/network">Network page</Link>. Access
                      stays deny-all: only issued tokens can read topics.
                    </>
                  )}
                </div>
              </div>
            </div>
            <div style={{ marginTop: "var(--sp-4)" }}>
              <button className="btn" onClick={test} disabled={!!busy}>
                {busy === "test" ? <SpinnerIcon /> : <BellIcon />} Send a test
                notification
              </button>
              <button
                className="btn"
                onClick={setup}
                disabled={!!busy}
                style={{ marginLeft: "var(--sp-3)" }}
                title="Safe to re-run: converges config and accounts without touching existing tokens."
              >
                Re-run setup
              </button>
            </div>
          </div>
        </>
      )}

      {!status && <div className="card panel">Loading…</div>}
    </div>
  );
}
