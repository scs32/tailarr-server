import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { NtfyStatus } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { FlashView, useFlash } from "../components/Flash";
import { BellIcon, SpinnerIcon } from "../components/Icons";

// Notifications ride an ntfy SYSTEM pod: Tailarr installs and manages it
// (accounts, topics, deny-all ACL, funnel) — it never appears to user
// devices, is not shareable, and THIS page is its only control surface
// (system pods are hidden on the Network page and locked on pod cards).
// Setup deliberately includes the Funnel exposure, behind the warning
// dialog below: phone delivery IS the feature, and deny-all + tokens are
// what make the public endpoint safe.

export function Notifications() {
  const [status, setStatus] = useState<NtfyStatus | null>(null);
  const { flash, show, clear } = useFlash();
  const [busy, setBusy] = useState<
    "" | "setup" | "test" | "funnel" | "alerts"
  >("");
  const [confirming, setConfirming] = useState<"" | "setup" | "funnel">("");
  const [handout, setHandout] = useState<{
    url: string;
    topics: string[];
    token: string;
  } | null>(null);

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
      if (!r.ok) {
        show({ kind: "err", text: r.error ?? "Setup failed." });
      } else if (r.funnel_error) {
        show({
          kind: "err",
          text: `Set up, but the public endpoint could not be opened: ${r.funnel_error}. Re-run setup to retry.`,
        });
      } else {
        show({
          kind: "ok",
          text: r.test_error
            ? `Set up — but the test message failed: ${r.test_error}`
            : "Notifications are set up (a test message was delivered).",
        });
      }
      await refresh();
    } finally {
      setBusy("");
      setConfirming("");
    }
  }

  async function setFunnel(enabled: boolean) {
    setBusy("funnel");
    try {
      const r = await api.ntfyFunnel(enabled);
      show(
        r.ok
          ? {
              kind: "ok",
              text: enabled
                ? "Public endpoint is on."
                : "Public endpoint is off — phones outside the tailnet won't receive.",
            }
          : { kind: "err", text: r.error ?? "Toggle failed." },
      );
      await refresh();
    } finally {
      setBusy("");
      setConfirming("");
    }
  }

  async function issueAlerts() {
    setBusy("alerts");
    try {
      const r = await api.ntfyAlerts("issue");
      if (r.ok && r.url !== undefined) {
        setHandout({ url: r.url, topics: r.topics ?? [], token: r.token ?? "" });
      } else {
        show({ kind: "err", text: r.error ?? "Could not issue the credential." });
      }
      await refresh();
    } finally {
      setBusy("");
    }
  }

  async function revokeAlerts() {
    if (!window.confirm("Revoke phone access? The token stops working immediately; issue a new one any time.")) return;
    setBusy("alerts");
    try {
      const r = await api.ntfyAlerts("revoke");
      show(
        r.ok
          ? { kind: "ok", text: "Phone access revoked." }
          : { kind: "err", text: r.error ?? "Revoke failed." },
      );
      setHandout(null);
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

  const funnelWarning = (
    <>
      <p>
        The ntfy endpoint will be published to the <strong>internet</strong>{" "}
        over Tailscale Funnel (HTTPS). That is how phones receive
        notifications when they're away from the tailnet.
      </p>
      <p>
        It stays locked down: access is deny-all, so nothing is readable or
        writable without a token Tailarr issued. The pod itself remains
        invisible to your tailnet users.
      </p>
    </>
  );

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
            One click writes its server config (authentication on, deny-all
            access), creates the accounts Tailarr publishes with, and opens
            the token-protected public endpoint for phone delivery.
          </p>
          <button
            className="btn btn--primary"
            onClick={() => setConfirming("setup")}
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
                <div className="row__title">
                  Phone delivery{" "}
                  <span
                    className={
                      "chip" + (status.funnel_on ? " chip--installed" : "")
                    }
                  >
                    {status.funnel_on ? "public endpoint on" : "off"}
                  </span>
                </div>
                <div className="row__meta">
                  {status.funnel_on ? (
                    <>
                      Token-protected endpoint:{" "}
                      <code>{status.public_url || "enrolling…"}</code>
                    </>
                  ) : (
                    <>
                      Phones outside the tailnet can’t receive until the
                      public endpoint is on.
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
              {status.funnel_on ? (
                <button
                  className="btn"
                  onClick={() => setFunnel(false)}
                  disabled={!!busy}
                  style={{ marginLeft: "var(--sp-3)" }}
                >
                  {busy === "funnel" && <SpinnerIcon />} Turn public endpoint
                  off
                </button>
              ) : (
                <button
                  className="btn"
                  onClick={() => setConfirming("funnel")}
                  disabled={!!busy}
                  style={{ marginLeft: "var(--sp-3)" }}
                >
                  {busy === "funnel" && <SpinnerIcon />} Turn public endpoint
                  on
                </button>
              )}
              <button
                className="btn"
                onClick={() => setConfirming("setup")}
                disabled={!!busy}
                style={{ marginLeft: "var(--sp-3)" }}
                title="Safe to re-run: converges config, accounts, and the public endpoint without touching existing tokens."
              >
                Re-run setup
              </button>
            </div>
          </div>

          <div className="section-title">Alerts on your phone</div>
          <div className="card panel">
            <p className="field__hint">
              Subscribe your own phone with the free{" "}
              <a href="https://ntfy.sh/docs/subscribe/phone/" target="_blank" rel="noreferrer">
                ntfy app
              </a>{" "}
              using a read-only credential. It can read Tailarr topics and
              nothing else; revoke it here any time. The same details will
              configure the Tailarr app's notifications module when that
              ships.
            </p>
            {!status.funnel_on && (
              <Alert kind="err">
                The public endpoint is off — your phone can only subscribe
                while it can reach the tailnet. Turn the endpoint on above
                for delivery anywhere.
              </Alert>
            )}
            {handout ? (
              <>
                <div className="row-list" style={{ marginTop: "var(--sp-3)" }}>
                  <div>
                    <div className="row__title">Server</div>
                    <div className="row__meta">
                      <code>{handout.url || "(sidecar still enrolling — re-show in a moment)"}</code>
                    </div>
                  </div>
                  <div>
                    <div className="row__title">Topic</div>
                    <div className="row__meta">
                      <code>{handout.topics.join(", ")}</code>
                    </div>
                  </div>
                  <div>
                    <div className="row__title">Access token</div>
                    <div className="row__meta">
                      <code>{handout.token}</code>
                    </div>
                  </div>
                  <div>
                    <div className="row__title">Tailarr app config</div>
                    <div className="row__meta">
                      <code>
                        {JSON.stringify({
                          url: handout.url,
                          token: handout.token,
                          topics: handout.topics,
                        })}
                      </code>
                    </div>
                  </div>
                </div>
                <p className="field__hint" style={{ marginTop: "var(--sp-3)" }}>
                  In the ntfy app: add a subscription → “Use another server”
                  with the server and topic above, then add the access token
                  for that server under Settings → Manage users.
                </p>
              </>
            ) : null}
            <div style={{ marginTop: "var(--sp-4)" }}>
              {!handout && (
                <button
                  className="btn btn--primary"
                  onClick={issueAlerts}
                  disabled={!!busy}
                >
                  {busy === "alerts" && <SpinnerIcon />}
                  {status.alerts_issued ? "Show phone access details" : "Issue phone access"}
                </button>
              )}
              {(status.alerts_issued || handout) && (
                <button
                  className="btn"
                  onClick={revokeAlerts}
                  disabled={!!busy}
                  style={{ marginLeft: handout ? 0 : "var(--sp-3)" }}
                >
                  Revoke phone access
                </button>
              )}
            </div>
          </div>
        </>
      )}

      {!status && <div className="card panel">Loading…</div>}

      {confirming && (
        <ConfirmDialog
          title={
            confirming === "setup"
              ? "Set up notifications?"
              : "Open the public endpoint?"
          }
          confirmLabel={
            confirming === "setup" ? "Set up + go public" : "Go public"
          }
          busy={!!busy}
          onConfirm={() =>
            confirming === "setup" ? setup() : setFunnel(true)
          }
          onCancel={() => setConfirming("")}
        >
          {funnelWarning}
        </ConfirmDialog>
      )}
    </div>
  );
}
