import React from "react";

interface IconProps { className?: string; size?: number }

const I = ({ size = 16, className = "", children }: IconProps & { children: React.ReactNode }) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.75}
    strokeLinecap="round"
    strokeLinejoin="round"
    className={`shrink-0 ${className}`}
  >
    {children}
  </svg>
);

export const IconGrid = (p: IconProps) => (
  <I {...p}>
    <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
    <rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
  </I>
);

export const IconBook = (p: IconProps) => (
  <I {...p}>
    <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
    <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" />
  </I>
);

export const IconAlertTriangle = (p: IconProps) => (
  <I {...p}>
    <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
    <line x1="12" y1="9" x2="12" y2="13" />
    <line x1="12" y1="17" x2="12.01" y2="17" />
  </I>
);

export const IconFlow = (p: IconProps) => (
  <I {...p}>
    <rect x="3" y="3" width="5" height="5" />
    <rect x="16" y="3" width="5" height="5" />
    <rect x="9" y="16" width="6" height="5" />
    <path d="M5.5 8v4a2 2 0 002 2h9a2 2 0 002-2V8" />
    <line x1="12" y1="14" x2="12" y2="16" />
  </I>
);

export const IconShieldCheck = (p: IconProps) => (
  <I {...p}>
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    <polyline points="9 12 11 14 15 10" />
  </I>
);

export const IconCpu = (p: IconProps) => (
  <I {...p}>
    <rect x="4" y="4" width="16" height="16" rx="2" />
    <rect x="9" y="9" width="6" height="6" />
    <line x1="9" y1="1" x2="9" y2="4" /><line x1="15" y1="1" x2="15" y2="4" />
    <line x1="9" y1="20" x2="9" y2="23" /><line x1="15" y1="20" x2="15" y2="23" />
    <line x1="1" y1="9" x2="4" y2="9" /><line x1="1" y1="15" x2="4" y2="15" />
    <line x1="20" y1="9" x2="23" y2="9" /><line x1="20" y1="15" x2="23" y2="15" />
  </I>
);

export const IconLayoutGrid = (p: IconProps) => (
  <I {...p}>
    <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
    <rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
  </I>
);

export const IconZap = (p: IconProps) => (
  <I {...p}><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></I>
);

export const IconStar = (p: IconProps) => (
  <I {...p}><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" /></I>
);

export const IconPlug = (p: IconProps) => (
  <I {...p}>
    <path d="M7 8H4v6h3" /><path d="M17 8h3v6h-3" />
    <rect x="7" y="5" width="10" height="14" rx="2" />
    <line x1="10" y1="5" x2="10" y2="3" /><line x1="14" y1="5" x2="14" y2="3" />
  </I>
);

export const IconClock = (p: IconProps) => (
  <I {...p}><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></I>
);

export const IconList = (p: IconProps) => (
  <I {...p}>
    <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" />
    <line x1="8" y1="18" x2="21" y2="18" />
    <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" />
    <line x1="3" y1="18" x2="3.01" y2="18" />
  </I>
);

export const IconKey = (p: IconProps) => (
  <I {...p}>
    <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
  </I>
);

export const IconCheckCircle = (p: IconProps) => (
  <I {...p}>
    <path d="M22 11.08V12a10 10 0 11-5.93-9.14" />
    <polyline points="22 4 12 14.01 9 11.01" />
  </I>
);

export const IconXCircle = (p: IconProps) => (
  <I {...p}>
    <circle cx="12" cy="12" r="10" />
    <line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" />
  </I>
);

export const IconInfo = (p: IconProps) => (
  <I {...p}>
    <circle cx="12" cy="12" r="10" />
    <line x1="12" y1="8" x2="12" y2="12" />
    <line x1="12" y1="16" x2="12.01" y2="16" />
  </I>
);

export const IconX = (p: IconProps) => (
  <I {...p}>
    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
  </I>
);

export const IconActivity = (p: IconProps) => (
  <I {...p}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></I>
);

export const IconTerminal = (p: IconProps) => (
  <I {...p}>
    <polyline points="4 17 10 11 4 5" />
    <line x1="12" y1="19" x2="20" y2="19" />
  </I>
);
