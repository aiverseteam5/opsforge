import { NavLink, Route, Routes } from "react-router-dom";
import { clearToken } from "../api";
import { CommandPalette } from "./CommandPalette";
import { MissionControl } from "../pages/MissionControl";
import { RunDetail } from "../pages/RunDetail";
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

const NAV: { to?: string; label?: string; end?: boolean; section?: string }[] = [
  { to: "/", label: "Mission Control", end: true },
  { section: "Knowledge & Truth" },
  { to: "/knowledge", label: "Knowledge" },
  { to: "/findings", label: "Findings" },
  { to: "/processes", label: "Processes" },
  { to: "/approvals", label: "Approval gate" },
  { to: "/providers", label: "LLM Providers" },
  { section: "Operations" },
  { to: "/catalog", label: "Catalog" },
  { to: "/skills", label: "Skills" },
  { to: "/skills/proposed", label: "Proposed skills" },
  { to: "/connectors", label: "Connectors" },
  { to: "/schedules", label: "Schedules" },
  { to: "/audit", label: "Audit" },
];

export function Layout() {
  return (
    <div className="flex h-screen">
      <aside className="flex w-56 flex-col border-r border-edge bg-panel p-4">
        <div className="mb-6">
          <div className="text-lg font-semibold">OpsForge</div>
          <div className="text-xs text-muted">AI SRE</div>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map((n, i) =>
            n.section ? (
              <div key={`s${i}`} className="mt-3 px-3 pb-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-600">
                {n.section}
              </div>
            ) : (
              <NavLink
                key={n.to}
                to={n.to!}
                end={n.end}
                className={({ isActive }) =>
                  `rounded-md px-3 py-2 text-sm ${
                    isActive ? "bg-edge text-white" : "text-muted hover:bg-edge/40"
                  }`
                }
              >
                {n.label}
              </NavLink>
            ),
          )}
        </nav>
        <div className="mt-auto space-y-2">
          <div className="rounded-md border border-edge px-3 py-2 text-xs text-muted">
            Press <kbd className="text-sky-400">⌘K</kbd> to dispatch
          </div>
          <button
            className="btn w-full"
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
          <Route path="/runs/:id" element={<RunDetail />} />
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
        </Routes>
      </main>

      <CommandPalette />
    </div>
  );
}
