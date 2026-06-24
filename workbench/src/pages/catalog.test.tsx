/**
 * A1 catalog page proofs — the page cannot lie about capability:
 *   - it opens on a POPULATED zone-grouped catalog, never an empty state;
 *   - a stub/coming-soon connector is visibly distinguished and NOT connectable;
 *   - a connected connector never shows a "Connect" affordance;
 *   - the page sends no org/workspace id (workspace is bound by token only).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactElement } from "react";

const { mockApi } = vi.hoisted(() => ({ mockApi: {} as Record<string, any> }));
vi.mock("../api", () => ({ api: mockApi, getToken: () => "tok" }));

import { Catalog } from "./Catalog";
import { CatalogConnect } from "./CatalogConnect";

function renderPage(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{el}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ZONES = [
  {
    zone: "System-of-record",
    connectors: [
      { key: "servicenow", display_name: "ServiceNow", zone: "System-of-record",
        auth_type: "vault_credential", ingests: ["behaviour"], transport: "mcp_stdio",
        implementation_status: "implemented", description: "SoR", status: "connected",
        connectable: false },
    ],
  },
  {
    zone: "Observability",
    connectors: [
      { key: "splunk", display_name: "Splunk", zone: "Observability", auth_type: "api_key",
        ingests: ["telemetry"], transport: "mcp_http", implementation_status: "stub_coming_soon",
        description: "obs", status: "coming_soon", connectable: false },
      { key: "local_files", display_name: "Local files", zone: "Observability",
        auth_type: "none", ingests: ["knowledge"], transport: "local",
        implementation_status: "implemented", description: "files", status: "available",
        connectable: true },
    ],
  },
];

beforeEach(() => {
  for (const k of Object.keys(mockApi)) delete mockApi[k];
});

describe("the catalog page tells the truth about capability", () => {
  it("opens on a populated, zone-grouped catalog (never empty)", async () => {
    mockApi.getCatalog = vi.fn().mockResolvedValue({ zones: ZONES });
    renderPage(<Catalog />);
    expect(await screen.findByText("System-of-record")).toBeInTheDocument();
    expect(screen.getByText("Observability")).toBeInTheDocument();
    expect(screen.getByText("ServiceNow")).toBeInTheDocument();
  });

  it("a coming-soon stub is distinguished and NOT connectable", async () => {
    mockApi.getCatalog = vi.fn().mockResolvedValue({ zones: ZONES });
    renderPage(<Catalog />);
    const splunk = (await screen.findByText("Splunk")).closest(".card") as HTMLElement;
    const u = within(splunk);
    expect(u.getAllByText(/coming soon/i).length).toBeGreaterThan(0);
    // the only button is the disabled "Coming soon" — no enabled Connect path
    expect(u.getByRole("button", { name: /coming soon/i })).toBeDisabled();
    expect(u.queryByRole("button", { name: /^connect$/i })).toBeNull();
  });

  it("a connected connector shows no Connect affordance (can't re-connect what's live)", async () => {
    mockApi.getCatalog = vi.fn().mockResolvedValue({ zones: ZONES });
    renderPage(<Catalog />);
    const sn = (await screen.findByText("ServiceNow")).closest(".card") as HTMLElement;
    const u = within(sn);
    expect(u.getAllByText(/connected/i).length).toBeGreaterThan(0);
    expect(u.queryByRole("button")).toBeNull();
  });

  it("only a genuinely-available implemented connector gets a Connect button", async () => {
    mockApi.getCatalog = vi.fn().mockResolvedValue({ zones: ZONES });
    renderPage(<Catalog />);
    const lf = (await screen.findByText("Local files")).closest(".card") as HTMLElement;
    expect(within(lf).getByRole("button", { name: /^connect$/i })).toBeEnabled();
  });
});

describe("no cross-workspace affordance", () => {
  it("getCatalog is called with no arguments (workspace bound by token only)", async () => {
    mockApi.getCatalog = vi.fn().mockResolvedValue({ zones: ZONES });
    renderPage(<Catalog />);
    await screen.findByText("ServiceNow");
    expect(mockApi.getCatalog).toHaveBeenCalledWith();
  });
});

// The connect route is reachable directly by URL — it must obey the same capability truth.
function renderConnect(key: string, detail: any) {
  mockApi.getCatalogEntry = vi.fn().mockResolvedValue(detail);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/catalog/${key}/connect`]}>
        <Routes>
          <Route path="/catalog/:key/connect" element={<CatalogConnect />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const detail = (over: any) => ({
  key: "x", display_name: "X", zone: "Z", auth_type: "vault_credential", ingests: [],
  transport: "mcp_http", description: "d", config_requirements: [],
  config_fields: [
    { name: "endpoint", label: "Endpoint", secret: false, required: true },
    { name: "credential", label: "Credential / token", secret: true, required: true },
  ],
  instance_kind: "servicenow", instance_id: null, ...over,
});

describe("the connect route obeys capability truth + credential safety (A2)", () => {
  it("a stub key shows 'coming soon' and NO config form", async () => {
    renderConnect("confluence", detail({
      key: "confluence", display_name: "Confluence", instance_kind: null,
      implementation_status: "stub_coming_soon", status: "coming_soon", connectable: false,
    }));
    expect(await screen.findByText(/coming soon/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/credential/i)).toBeNull();
  });

  it("a connectable key renders the config form with the credential field WRITE-ONLY (password)", async () => {
    const { container } = renderConnect("servicenow", detail({
      key: "servicenow", display_name: "ServiceNow",
      implementation_status: "implemented", status: "available", connectable: true,
    }));
    await screen.findByText("ServiceNow");
    const inputs = Array.from(container.querySelectorAll("input"));
    const cred = inputs.find((i) => /credential/i.test(i.previousElementSibling?.textContent ?? "")
      || (i.getAttribute("placeholder") ?? "").length >= 0 && i.getAttribute("type") === "password");
    expect(cred?.getAttribute("type")).toBe("password");
    // and a Connect (create) action is present
    expect(screen.getByRole("button", { name: /connect/i })).toBeInTheDocument();
  });

  it("editing a configured connector NEVER prefills the secret (write-only) and offers test/disconnect", async () => {
    mockApi.listConnectors = vi.fn().mockResolvedValue([
      { id: "inst-1", name: "snow", kind: "servicenow", transport: "http",
        endpoint: "http://snow.local", tool_allowlist: [], field_mapping: null,
        discovered_schema: null, status: "healthy", last_health_at: null, created_at: "x" },
    ]);
    const { container } = renderConnect("servicenow", detail({
      key: "servicenow", display_name: "ServiceNow",
      implementation_status: "implemented", status: "connected", connectable: false,
      instance_id: "inst-1",
    }));
    // endpoint IS prefilled from the instance (wait for the connectors query)…
    await screen.findByDisplayValue("http://snow.local");
    // …the secret credential field is EMPTY (never prefilled)
    const inputs = Array.from(container.querySelectorAll("input"));
    const secret = inputs.find((i) => i.getAttribute("type") === "password");
    expect((secret as HTMLInputElement).value).toBe(""); // never prefilled
    expect(secret?.getAttribute("placeholder")).toMatch(/keep existing/i);
    expect(screen.getByRole("button", { name: /test connection/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeInTheDocument();
  });

  it("local-files shows an ingest path form, not a credential form", async () => {
    renderConnect("local_files", detail({
      key: "local_files", display_name: "Local files", transport: "local",
      auth_type: "none", instance_kind: null,
      implementation_status: "implemented", status: "available", connectable: true,
      config_fields: [{ name: "path", label: "Folder path", secret: false, required: true }],
    }));
    expect(await screen.findByText(/folder path/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ingest/i })).toBeInTheDocument();
    expect(screen.queryByText(/credential/i)).toBeNull();
  });
});
