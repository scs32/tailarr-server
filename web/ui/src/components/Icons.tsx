// Inline SVG icons (no icon CDN — the controller box is offline).
import type { SVGProps } from "react";

const base = (props: SVGProps<SVGSVGElement>) => ({
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  ...props,
});

export const GridIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <rect x="14" y="14" width="7" height="7" rx="1" />
  </svg>
);

export const StoreIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <path d="M3 9h18M9 21V9" />
    <rect x="3" y="3" width="18" height="18" rx="2" />
  </svg>
);

export const PlusIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const ShareIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <path d="M4 20h4l10-10a2 2 0 0 0-4-4L4 16v4z" />
  </svg>
);

export const GearIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-2.82 1.17V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

export const PodGlyph = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

export const SpinnerIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base({ strokeWidth: 2.5, ...p })}>
    <path d="M21 12a9 9 0 1 1-6.2-8.6" />
  </svg>
);

export const SearchIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <circle cx="11" cy="11" r="7" />
    <path d="M21 21l-4.3-4.3" />
  </svg>
);

export const RefreshIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />
  </svg>
);

export const NetworkIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
  </svg>
);

export const PulseIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
  </svg>
);

export const CheckIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <path d="M20 6 9 17l-5-5" />
  </svg>
);

// The Tailarr mark: a 3×3 tailnet mesh with a rising tail of four growing
// nodes (brand guide §01). Color comes from the parent (Signal Cyan).
export const TailarrMark = (p: SVGProps<SVGSVGElement>) => (
  <svg viewBox="0 0 120 120" style={{ overflow: "visible" }} {...p}>
    <g fill="currentColor" opacity="0.16">
      <circle cx="30" cy="30" r="3.2" /><circle cx="60" cy="30" r="3.2" /><circle cx="90" cy="30" r="3.2" />
      <circle cx="30" cy="60" r="3.2" /><circle cx="60" cy="60" r="3.2" /><circle cx="90" cy="60" r="3.2" />
      <circle cx="30" cy="90" r="3.2" /><circle cx="60" cy="90" r="3.2" /><circle cx="90" cy="90" r="3.2" />
    </g>
    <polyline
      points="26,96 49,73 73,52 98,28"
      fill="none"
      stroke="currentColor"
      strokeWidth="5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <g fill="currentColor">
      <circle cx="26" cy="96" r="5" /><circle cx="49" cy="73" r="6.5" />
      <circle cx="73" cy="52" r="8.5" /><circle cx="98" cy="28" r="11" />
    </g>
  </svg>
);

export const UsersIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base(p)}>
    <circle cx="9" cy="8" r="4" />
    <path d="M2 21v-1a7 7 0 0 1 14 0v1M17 4a4 4 0 0 1 0 8M22 21v-1a7 7 0 0 0-5-6.7" />
  </svg>
);
