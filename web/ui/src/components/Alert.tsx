import type { ReactNode } from "react";

const ICON = {
  ok: <path d="M20 6 9 17l-5-5" />,
  err: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="M15 9l-6 6M9 9l6 6" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </>
  ),
};

export function Alert({
  kind,
  children,
}: {
  kind: "ok" | "err" | "info";
  children: ReactNode;
}) {
  return (
    <div className={`alert alert--${kind}`}>
      <svg
        className="alert__icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {ICON[kind]}
      </svg>
      <div>{children}</div>
    </div>
  );
}
