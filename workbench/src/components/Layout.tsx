import { NavLink, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { clearToken, api } from "../api";
import { CommandPalette } from "./CommandPalette";
import {
  IconGrid, IconBook, IconAlertTriangle, IconFlow, IconShieldCheck,
  IconCpu, IconLayoutGrid, IconZap, IconStar, IconPlug, IconClock,
  IconList, IconKey, IconActivity, IconChat,
} from "./icons";
import { MissionControl } from "../pages/MissionControl";
import { RunDetail } from "../pages/RunDetail";
import { RunTimeline } from "../pages/RunTimeline";
import { Skills } from "../pages/Skills";
import { ProposedSkills } from "../pages/ProposedSkills";
import { Connectors } from "../pages/Connectors";
import { Schedules } from "../pages/Schedules";
import { Approvals } from "../pages/Approvals";
import { Audit } from "../pages/Audit";
import { Knowledge } from "../pages/Knowledge";
import { Findings } from "../pages/Findings";
import { Processes } from "../pages/Processes";
import { Providers } from "../pages/Providers";
import { Catalog } from "../pages/Catalog";
import { CatalogConnect } from "../pages/CatalogConnect";
import { Tokens } from "../pages/Tokens";
import { TrustLadder } from "../pages/TrustLadder";
import { Chat } from "../pages/Chat";

type NavEntry =
  | { kind: "section"; label: string }
  | { kind: "link"; to: string; label: string; icon: React.ReactNode; end?: boolean; badge?: "approvals" | "proposed" };

const NAV: NavEntry[] = [
  { kind: "link", to: "/", label: "Mission Control", icon: <IconGrid />, end: true },
  { kind: "link", to: "/chat", label: "Chat", icon: <IconChat /> },
  { kind: "section", label: "Knowledge & Truth" },
  { kind: "link", to: "/knowledge",  label: "Knowledge",     icon: <IconBook /> },
  { kind: "link", to: "/findings",   label: "Findings",      icon: <IconAlertTriangle /> },
  { kind: "link", to: "/processes",  label: "Processes",     icon: <IconFlow /> },
  { kind: "link", to: "/approvals",  label: "Approval gate", icon: <IconShieldCheck />, badge: "approvals" },
  { kind: "link", to: "/providers",  label: "LLM Providers", icon: <IconCpu /> },
  { kind: "section", label: "Operations" },
  { kind: "link", to: "/catalog",         label: "Catalog",         icon: <IconLayoutGrid /> },
  { kind: "link", to: "/skills",          label: "Skills",          icon: <IconZap /> },
  { kind: "link", to: "/skills/proposed", label: "Proposed skills", icon: <IconStar />, badge: "proposed" },
  { kind: "link", to: "/connectors",      label: "Connectors",      icon: <IconPlug /> },
  { kind: "link", to: "/schedules",       label: "Schedules",       icon: <IconClock /> },
  { kind: "link", to: "/audit",           label: "Audit",           icon: <IconList /> },
  { kind: "link", to: "/tokens",          label: "API Tokens",      icon: <IconKey /> },
  { kind: "link", to: "/trust-ladder",   label: "Trust Ladder",    icon: <IconShieldCheck /> },
];

function NavBadge({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <span className="ml-auto flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-600/80 px-1 text-[10px] font-semibold text-white">
      {count > 99 ? "99+" : count}
    </span>
  );
}

export function Layout() {
  const pendingApprovals = useQuery({
    queryKey: ["actions", "pending"],
    queryFn: () => api.listActions("awaiting_approval"),
    refetchInterval: 15_000,
    select: (d) => d.length,
  });
  const pendingProposed = useQuery({
    queryKey: ["proposed-count"],
    queryFn: () => api.listProposed(1, 1),
    refetchInterval: 30_000,
    select: (d) => d.total,
  });

  const badgeCount = (badge?: "approvals" | "proposed"): number => {
    if (badge === "approvals") return pendingApprovals.data ?? 0;
    if (badge === "proposed") return pendingProposed.data ?? 0;
    return 0;
  };

  return (
    <div className="flex h-screen">
      <aside className="flex w-56 flex-col border-r border-edge bg-panel">
        {/* Brand */}
        <div className="flex items-center gap-3 border-b border-edge/60 px-4 py-4">
          <svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" className="shrink-0">
            <rect width="32" height="32" rx="8" fill="url(#nav-grad)" />
            <path d="M9 16h14M16 9v14" stroke="#e6edf3" strokeWidth="1.8" strokeLinecap="round" />
            <path d="M11.5 11.5l9 9M20.5 11.5l-9 9" stroke="#7dd3fc" strokeWidth="1.2" strokeLinecap="round" strokeOpacity="0.5" />
            <defs>
              <linearGradient id="nav-grad" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
                <stop stopColor="#0f2444" />
                <stop offset="1" stopColor="#0a1e33" />
              </linearGradient>
            </defs>
          </svg>
          <div>
            <div className="text-sm font-semibold leading-tight">OpsForge</div>
            <div className="text-[10px] text-muted leading-tight">AI SRE</div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex flex-col gap-0.5 overflow-y-auto px-2 py-2 flex-1">
          {NAV.map((n, i) => {
            if (n.kind === "section") {
              return (
                <div
                  key={`s${i}`}
                  className="mt-3 mb-0.5 px-2 pb-0.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-600"
                >
                  {n.label}
                </div>
              );
            }
            const count = badgeCount(n.badge);
            return (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors ${
                    isActive
                      ? "bg-edge text-white"
                      : "text-muted hover:bg-edge/40 hover:text-zinc-200"
                  }`
                }
              >
                <span className="opacity-70">{n.icon}</span>
                <span className="flex-1">{n.label}</span>
                {count > 0 && <NavBadge count={count} />}
              </NavLink>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="border-t border-edge/60 p-3 space-y-2">
          <div className="flex items-center gap-2 rounded-md border border-edge/60 bg-ink/60 px-2.5 py-1.5 text-xs text-muted">
            <IconActivity size={12} className="opacity-60" />
            <span>Press <kbd className="rounded bg-edge px-1 text-sky-400">⌘K</kbd> to dispatch</span>
          </div>
          <button
            className="btn w-full text-xs"
            onClick={() => {
              clearToken();
              location.reload();
            }}
          >
            Sign out
          </button>
        </div>
      </aside>

      <main className="flex-1 overflow-auto p-6">
        <Routes>
          <Route path="/" element={<MissionControl />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/runs/:id/timeline" element={<RunTimeline />} />
          <Route path="/trust-ladder" element={<TrustLadder />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="/findings" element={<Findings />} />
          <Route path="/processes" element={<Processes />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/providers" element={<Providers />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="/catalog/:key/connect" element={<CatalogConnect />} />
          <Route path="/skills" element={<Skills />} />
          <Route path="/skills/proposed" element={<ProposedSkills />} />
          <Route path="/connectors" element={<Connectors />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/tokens" element={<Tokens />} />
        </Routes>
      </main>

      <CommandPalette />
    </div>
  );
}
