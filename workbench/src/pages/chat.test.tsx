/**
 * G1 chat-surface proofs: the thread renders user + assistant turns, the assistant turn shows
 * the agent's grounded answer (report hypothesis + confidence + evidence), sending a turn calls
 * the API, and NO credential ever reaches the DOM.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";

const { mockApi } = vi.hoisted(() => ({ mockApi: {} as Record<string, any> }));
vi.mock("../api", () => ({ api: mockApi, getToken: () => "tok" }));
// the assistant turn streams run events; stub it to a no-op so the test is hermetic
vi.mock("../sse", () => ({ streamRunEvents: () => () => {} }));

import { Chat } from "./Chat";

function renderPage(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{el}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => { for (const k of Object.keys(mockApi)) delete mockApi[k]; });

describe("the chat surface", () => {
  it("renders the thread: user turn + the assistant's grounded answer", async () => {
    mockApi.listConversations = vi.fn().mockResolvedValue([{ id: "c1", title: "Ops", created_at: "x" }]);
    mockApi.getChatMessages = vi.fn().mockResolvedValue([
      { id: "m1", role: "user", content: "what's inconsistent in our incident process?", run_id: null, seq: 1, created_at: "x" },
      { id: "m2", role: "assistant", content: "", run_id: "r1", seq: 2, created_at: "x",
        run_status: "done",
        report: { hypothesis: "Two rollback pages disagree — one is stale.", confidence: "low",
                  evidence: [{ claim: "page 101 vs 102", raw_ref: "confluence://101" }] } },
    ]);
    renderPage(<Chat />);
    expect(await screen.findByText(/what's inconsistent/i)).toBeInTheDocument();
    expect(await screen.findByText(/two rollback pages disagree/i)).toBeInTheDocument();
    // low confidence is surfaced AS low (M6.5 honesty), not buried
    expect(screen.getByText(/confidence: low/i)).toBeInTheDocument();
  });

  it("surfaces the agent's actions legibly: gated → approve, executed reversible → undo", async () => {
    mockApi.listConversations = vi.fn().mockResolvedValue([{ id: "c1", title: "Ops", created_at: "x" }]);
    mockApi.getChatMessages = vi.fn().mockResolvedValue([
      { id: "m2", role: "assistant", content: "", run_id: "r1", seq: 1, created_at: "x",
        run_status: "done",
        report: { hypothesis: "Handled the deploy.", confidence: "high", evidence: [] },
        actions: [
          { id: "a1", tool: "kubernetes.delete_namespace", target_ref: "svc://prod",
            action_class: "destructive", state: "awaiting_approval",
            reason: "trust=awaiting_approval; gated:production", auto_executed: false,
            awaiting: true, undoable: false },
          { id: "a2", tool: "kubernetes.rollback_deploy", target_ref: "svc://staging",
            action_class: "reversible", state: "succeeded", reason: "auto:reversible_safe",
            auto_executed: true, awaiting: false, undoable: true },
        ] },
    ]);
    mockApi.approveAction = vi.fn().mockResolvedValue({});
    mockApi.undoAction = vi.fn().mockResolvedValue({});
    const { container } = renderPage(<Chat />);
    // a consequential action is shown gated, with WHY, and an inline approve
    expect(await screen.findByText(/kubernetes\.delete_namespace/)).toBeInTheDocument();
    expect(screen.getByText(/awaiting your approval/i)).toBeInTheDocument();
    // an auto-executed reversible action is shown with an undo
    expect(screen.getByText(/auto-executed/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() => expect(mockApi.approveAction).toHaveBeenCalledWith("a1"));
    fireEvent.click(screen.getByRole("button", { name: /^undo$/i }));
    await waitFor(() => expect(mockApi.undoAction).toHaveBeenCalledWith("a2"));
    expect(container.innerHTML).not.toMatch(/credential|api_key|sk-/i);
  });

  it("sending a message calls the API and never sends a credential anywhere visible", async () => {
    mockApi.listConversations = vi.fn().mockResolvedValue([{ id: "c1", title: "Ops", created_at: "x" }]);
    mockApi.getChatMessages = vi.fn().mockResolvedValue([]);
    mockApi.postChatMessage = vi.fn().mockResolvedValue({ message_id: "m", run_id: "r", run_status: "queued" });
    const { container } = renderPage(<Chat />);
    const input = await screen.findByPlaceholderText(/ask the operations assistant/i);
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() => expect(mockApi.postChatMessage).toHaveBeenCalledWith("c1", "hello"));
    expect(container.innerHTML).not.toMatch(/credential|api_key|sk-/i);
  });
});
