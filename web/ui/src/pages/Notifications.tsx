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
          text: `Set up, but phone delivery couldn’t be enabled: ${r.funnel_error}. Re-run setup to retry.`,
        });
      } else if (r.gateway_error) {
        show({
          kind: "err",
          text: `Set up, but automatic app setup couldn’t start: ${r.gateway_error}. Re-run setup to retry.`,
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
                ? "Phone delivery is on."
                : "Phone delivery is off — phones away from home won't receive.",
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
        To deliver to phones away from home, the notification service is
        published to the <strong>internet</strong> over secure HTTPS. That’s
        how notifications reach a phone that isn’t on your network.
      </p>
      <p>
        It stays locked down: nothing is readable or writable without a
        credential Tailarr issued, and the service stays invisible to your
        users.
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
            Tailarr sends update, health, and event alerts through a small
            notification service it runs and manages itself — never listed
            with your services, invisible to user devices, and controlled
            only from this page.
            {status.installed
              ? ` (It's deployed — ${status.state || "state unknown"} — but not yet set up.)`
              : ""}
          </p>
          <p className="field__hint">
            One click {status.installed ? "sets it up" : "deploys and sets it up"}
            , creates the accounts it needs, and turns on phone delivery so
            notifications reach you anywhere.
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
          {status.state !== "running" && (
            <Alert kind="err">
              Notifications aren’t sending right now — the notification
              service isn’t running. Try <strong>Re-run setup</strong> below.
            </Alert>
          )}
          {!status.gateway && (
            <Alert kind="err">
              Automatic setup for the Tailarr app isn’t working —{" "}
              <strong>Re-run setup</strong> to turn it on (requires v0.22.1+).
            </Alert>
          )}
          <div className="card panel">
            <div className="row-list">
              <div>
                <div className="row__title">
                  Notifications{" "}
                  <span
                    className={
                      "chip" +
                      (status.state === "running"
                        ? " chip--installed"
                        : " chip--danger")
                    }
                  >
                    {status.state === "running" ? "on" : "off"}
                  </span>
                </div>
                <div className="row__meta">
                  You’re alerted about service updates, Tailarr upgrades and
                  rollbacks, a service going down or recovering, and sign-in
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
                    {status.funnel_on ? "on" : "off"}
                  </span>
                </div>
                <div className="row__meta">
                  {status.funnel_on
                    ? "Notifications reach your phone even when you’re away from home."
                    : "Phones away from home can’t receive until phone delivery is on."}
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
                  {busy === "funnel" && <SpinnerIcon />} Turn phone delivery
                  off
                </button>
              ) : (
                <button
                  className="btn"
                  onClick={() => setConfirming("funnel")}
                  disabled={!!busy}
                >
                  {busy === "funnel" && <SpinnerIcon />} Turn phone delivery on
                </button>
              )}
              <button
                className="btn"
                onClick={() => setConfirming("setup")}
                disabled={!!busy}
                title="Safe to re-run: reconciles setup and phone delivery without touching anything already working."
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
                  Each app’s downloads and imports reach the people you’ve
                  given that service to — automatically. Turning it on is one
                  click: Tailarr sets up the app’s notifications for you.
                </p>
                <div className="row-list">
                  {status.arr.map((a) => (
                    <div key={a.name} className="row">
                      <div>
                        <div className="row__title">
                          {a.name}{" "}
                          {a.wired ? (
                            <span className="chip chip--installed">
                              {a.wired === "manual" ? "on (manual)" : "on"}
                            </span>
                          ) : (
                            <span className="chip">off</span>
                          )}
                        </div>
                        <div className="row__meta">
                          Sends downloads and imports
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
                            ? "Safe to re-run: updates the existing setup"
                            : "Set up this app's notifications automatically"
                        }
                        onClick={() => wire(a.name)}
                      >
                        {a.wired ? "Redo setup" : "Turn on"}
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
              : "Turn on phone delivery?"
          }
          confirmLabel={
            confirming === "setup" ? "Set up + turn on" : "Turn on"
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
