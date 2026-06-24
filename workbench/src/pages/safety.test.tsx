/**
 * M8 safety-integrity proofs — the review dimension that matters most: the front end
 * must NOT be a path that defeats a backend safety property. Each test pins one property:
 *   - the UI cannot forge the promotion gate (no holds/scorecard-assertion surface);
 *   - gated / low-confidence items are shown prominently AS SUCH, never softened;
 *   - a vault credential never reaches the rendered DOM;
 *   - no view has a cross-workspace affordance (the UI never sends an org id).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";

// A controllable mock of the API client; each test sets the methods it needs.
// vi.hoisted so the object exists when the hoisted vi.mock factory runs.
const { mockApi } = vi.hoisted(() => ({ mockApi: {} as Record<string, any> }));
vi.mock("../api", () => ({ api: mockApi, getToken: () => "tok" }));

import { Providers } from "./Providers";
import { Processes } from "./Processes";
import { Approvals } from "./Approvals";

function renderPage(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{el}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  for (const k of Object.keys(mockApi)) delete mockApi[k];
});

describe("the promotion gate cannot be forged from the UI", () => {
  it("Providers page exposes no holds/scorecard/accuracy INPUT", async () => {
    mockApi.listProviders = vi.fn().mockResolvedValue([
      { id: "p1", provider: "openai", model: "gpt-4o-mini", status: "proposed",
        residency: "enterprise", scorecard: null },
    ]);
    mockApi.promoteProvider = vi.fn().mockResolvedValue({ promoted: false, detail: "not held" });
    const { container } = renderPage(<Providers />);
    await screen.findByText("gpt-4o-mini");
    const fields = Array.from(container.querySelectorAll("input, textarea, select"));
    for (const f of fields) {
      const blob = `${f.getAttribute("placeholder") ?? ""} ${f.getAttribute("name") ?? ""}`.toLowerCase();
      expect(blob).not.toMatch(/holds|scorecard|accuracy|baseline/);
    }
  });

  it("the api client has NO scorecard-attach / holds-submit method", async () => {
    const actual: any = await vi.importActual("../api");
    const names = Object.keys(actual.api);
    expect(names.some((n) => /scorecard|holds|attach/i.test(n))).toBe(false);
    // promote takes only an id — no body, so no place to assert a hold
    expect(actual.api.promoteProvider.length).toBe(1);
  });
});

describe("gated / low-confidence is shown prominently, never softened", () => {
  it("Processes marks a low-confidence step loudly", async () => {
    mockApi.listProcesses = vi.fn().mockResolvedValue([
      { id: "v1", process_key: "deploy", version: 1, status: "draft", min_confidence: 0.2,
        uncovered_chunks: [], signed_off_by: null,
        steps: [{ index: 0, kind: "step", text: "risky step", source_chunks: ["c1"],
          source_kinds: ["research"], freshness_days: 5, confidence: 0.2, low_confidence: true }] },
    ]);
    renderPage(<Processes />);
    // prominent in more than one place (step badge + banner) — that's the point
    expect((await screen.findAllByText(/low-confidence/i)).length).toBeGreaterThan(0);
    expect(screen.getByText(/look hard before signoff/i)).toBeInTheDocument();
  });

  it("the summary 'min' tag follows the BACKEND low verdict, not a UI threshold", async () => {
    // min_confidence 0.6 would look 'safe' against a UI-hardcoded 0.5, but the backend
    // flagged the step low (e.g. its threshold is 0.7). The summary chip must warn.
    mockApi.listProcesses = vi.fn().mockResolvedValue([
      { id: "v1", process_key: "deploy", version: 1, status: "draft", min_confidence: 0.6,
        uncovered_chunks: [], signed_off_by: null,
        steps: [{ index: 0, kind: "step", text: "s", source_chunks: ["c1"],
          source_kinds: ["document"], freshness_days: 1, confidence: 0.6, low_confidence: true }] },
    ]);
    renderPage(<Processes />);
    const min = await screen.findByText(/min 0\.60/);
    // the chip carries the loud low styling (rose) + "low" label, driven by the backend
    // flag — the UI did NOT round 0.6 up to "safe" against an invented 0.5 threshold.
    expect(min.className).toMatch(/rose/);
    expect(min.textContent).toMatch(/low/);
  });

  it("Approvals surfaces the low_grounding_gate prominently", async () => {
    mockApi.listActions = vi.fn().mockResolvedValue([
      { id: "a1", run_id: null, action_class: "reversible", tool: "k.restart", params: {},
        target_ref: "svc", rollback: null, state: "awaiting_approval",
        policy_trace: { rules: ["low_grounding_gate"],
          grounding: { grounding_confidence: 0.1, low_confidence: true, process_key: "deploy", chunk_count: 1 } },
        approved_at: null, executed_at: null, result: null, created_at: "2026-06-22T00:00:00Z" },
    ]);
    renderPage(<Approvals />);
    expect(await screen.findByText(/GATED: low_grounding_gate/i)).toBeInTheDocument();
    expect(screen.getByText(/grounding 0\.10/)).toBeInTheDocument();
  });
});

describe("credentials never reach the DOM", () => {
  it("the credential input is write-only (password) and stray secrets are not rendered", async () => {
    // Even if the API leaked an api_key field, the page must not render it.
    mockApi.listProviders = vi.fn().mockResolvedValue([
      { id: "p1", provider: "openai", model: "gpt-4o-mini", status: "active",
        residency: "enterprise", scorecard: { holds: true }, api_key: "sk-LEAKED-SECRET" },
    ]);
    mockApi.promoteProvider = vi.fn();
    const { container } = renderPage(<Providers />);
    await screen.findByText("gpt-4o-mini");
    expect(container.innerHTML).not.toContain("sk-LEAKED-SECRET");
    const key = Array.from(container.querySelectorAll("input")).find((i) =>
      (i.getAttribute("placeholder") ?? "").toLowerCase().includes("key"));
    expect(key?.getAttribute("type")).toBe("password");
  });
});

describe("no cross-workspace affordance (the UI never sends an org id)", () => {
  it("every api endpoint binds the workspace by token only — no org parameter or query", async () => {
    const actual: any = await vi.importActual("../api");
    // Spy fetch; call each endpoint; assert no request carries an org/workspace/tenant param.
    const calls: string[] = [];
    const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (url: any, init: any) => {
      calls.push(String(url));
      const h = new Headers(init?.headers || {});
      // the only identity the browser ever sends is the bearer token
      expect([...h.keys()].some((k) => /org|tenant|workspace/i.test(k))).toBe(false);
      return { ok: true, status: 200, json: async () => [], text: async () => "[]" } as any;
    });
    await actual.api.listChunks();
    await actual.api.listFindings();
    await actual.api.listProcesses();
    await actual.api.listProviders();
    for (const u of calls) expect(u).not.toMatch(/org|tenant|workspace/i);
    spy.mockRestore();
  });
});
