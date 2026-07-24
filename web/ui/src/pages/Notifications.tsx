import { useCallback, useEffect, useState } from "react";
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
    "" | "setup" | "test" | "funnel" | "wire"
  >("");
  const [recipe, setRecipe] = useState<{
    pod: string;
    server: string;
    username: string;
    password: string;
    topic: string;
  } | null>(null);
  const [confirming, setConfirming] = useState<"" | "setup" | "funnel">("");

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
      } else if (r.gateway_error) {
        show({
          kind: "err",
          text: `Set up, but the app self-config gateway failed to deploy: ${r.gateway_error}. Re-run setup to retry.`,
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

  async function wire(pod: string) {
    setBusy("wire");
    setRecipe(null);
    try {
      const r = await api.ntfyWire(pod);
      if (r.ok) {
        show({
          kind: "ok",
          text: `${pod} now publishes media events to ${r.topic}.`,
        });
      } else {
        show({ kind: "err", text: r.error ?? "Wiring failed." });
        if (r.recipe) setRecipe({ pod, ...r.recipe });
      }
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
        The notification endpoint will be published to the{" "}
        <strong>internet</strong>{" "}
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

      {status && !status.configured && (
        <div className="card panel">
          <p className="field__hint">
            Tailarr sends update, health, and lifecycle alerts through a
            small notification server it runs and manages itself — never
            listed with your pods, invisible to user devices, locked to
            deny-all, controlled only from this page.
            {status.installed
              ? ` (The service is deployed — ${status.state || "state unknown"} — but not yet configured.)`
              : ""}
          </p>
          <p className="field__hint">
            One click {status.installed ? "writes" : "deploys it, writes"}{" "}
            its server config (authentication on, deny-all access), creates
            the accounts Tailarr publishes with, and opens the
            token-protected public endpoint for phone delivery.
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
                  Notification service{" "}
                  <span
                    className={
                      "chip" +
                      (status.state === "running" ? " chip--installed" : "")
                    }
                  >
                    {status.state || "unknown"}
                  </span>
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
              <div>
                <div className="row__title">
                  Tailarr app auto-setup{" "}
                  <span
                    className={
                      "chip" + (status.gateway ? " chip--installed" : " chip--danger")
                    }
                  >
                    {status.gateway ? "working" : "not running"}
                  </span>
                </div>
                <div className="row__meta">
                  {status.gateway ? (
                    <>
                      Tailarr-app users get their notification setup
                      automatically — nothing to hand out.
                    </>
                  ) : (
                    <>
                      Automatic setup for the Tailarr app isn’t available
                      yet — <strong>Re-run setup</strong> to turn it on
                      (requires controller v0.22.1+).
                    </>
                  )}
                </div>
              </div>
            </div>
            <div
              style={{
                marginTop: "var(--sp-4)",
                display: "flex",
                flexWrap: "wrap",
                gap: "var(--sp-3)",
              }}
            >
              <button className="btn" onClick={test} disabled={!!busy}>
                {busy === "test" ? <SpinnerIcon /> : <BellIcon />} Send a test
                notification
              </button>
              {status.funnel_on ? (
                <button
                  className="btn"
                  onClick={() => setFunnel(false)}
                  disabled={!!busy}
                >
                  {busy === "funnel" && <SpinnerIcon />} Turn public endpoint
                  off
                </button>
              ) : (
                <button
                  className="btn"
                  onClick={() => setConfirming("funnel")}
                  disabled={!!busy}
                >
                  {busy === "funnel" && <SpinnerIcon />} Turn public endpoint
                  on
                </button>
              )}
              <button
                className="btn"
                onClick={() => setConfirming("setup")}
                disabled={!!busy}
                title="Safe to re-run: converges config, accounts, and the public endpoint without touching existing tokens."
              >
                Re-run setup
              </button>
            </div>
          </div>

          {status.arr.length > 0 && (
            <>
              <div className="section-title">Media events</div>
              <div className="card panel">
                <p className="field__hint">
                  Each media app publishes its download/import events to its
                  own topic — users who hold that service's badge receive
                  them automatically. Wiring is one click: Tailarr
                  configures the app's built-in ntfy connection for you.
                </p>
                <div className="row-list">
                  {status.arr.map((a) => (
                    <div key={a.name} className="row">
                      <div>
                        <div className="row__title">
                          {a.name}{" "}
                          {a.wired ? (
                            <span className="chip chip--installed">
                              wired ({a.wired})
                            </span>
                          ) : (
                            <span className="chip">not wired</span>
                          )}
                        </div>
                        <div className="row__meta">
                          topic <code>{a.topic}</code>
                        </div>
                      </div>
                      <div className="spacer" />
                      <button
                        className={
                          "btn btn--sm" +
                          (a.wired ? " btn--ghost" : " btn--primary") +
                          (busy === "wire" ? " btn--loading" : "")
                        }
                        disabled={!!busy}
                        title={
                          a.wired
                            ? "Safe to re-run: updates the existing connection"
                            : "Configure this app's ntfy connection automatically"
                        }
                        onClick={() => wire(a.name)}
                      >
                        {a.wired ? "Re-wire" : "Wire up"}
                      </button>
                    </div>
                  ))}
                </div>
                {recipe && (
                  <div
                    className="card"
                    style={{ marginTop: "var(--sp-3)", padding: "var(--sp-3)" }}
                  >
                    <div className="row__title">
                      Manual recipe for {recipe.pod}
                    </div>
                    <div className="row__meta">
                      In {recipe.pod}: Settings → Connect → add “ntfy” with
                      server <code>{recipe.server}</code>, username{" "}
                      <code>{recipe.username}</code>, password{" "}
                      <code>{recipe.password}</code>, topic{" "}
                      <code>{recipe.topic}</code>, and turn on “On Import” /
                      “On Upgrade”.
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
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
