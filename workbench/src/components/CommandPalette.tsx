import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

// ⌘K / Ctrl+K -> natural-language dispatch. The resolver picks the skill + entity,
// or returns candidates to disambiguate (never guesses).
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [candidates, setCandidates] = useState<{ slug: string; name: string }[]>([]);
  const navigate = useNavigate();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!open) return null;

  function close() {
    setOpen(false);
    setQuery("");
    setCandidates([]);
  }

  async function dispatch() {
    if (!query.trim() || busy) return;
    setBusy(true);
    try {
      const res = await api.createRunNl(query);
      if (res.status === "ambiguous" && res.candidates) {
        setCandidates(res.candidates);
      } else if (res.run_id) {
        close();
        navigate(`/runs/${res.run_id}`);
      }
    } finally {
      setBusy(false);
    }
  }

  async function pick(slug: string) {
    setBusy(true);
    try {
      const { run_id } = await api.createRun(slug, { query });
      close();
      navigate(`/runs/${run_id}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-32"
      onClick={close}
    >
      <div className="card w-[560px]" onClick={(e) => e.stopPropagation()}>
        <div className="mb-2 text-xs uppercase tracking-wide text-muted">
          Dispatch · natural language
        </div>
        <input
          autoFocus
          className="input"
          placeholder="why is payment-svc throwing 5xx… · investigate INC0012345"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && dispatch()}
        />
        {candidates.length > 0 && (
          <div className="mt-3">
            <div className="mb-1 text-xs text-muted">Which investigation?</div>
            <div className="flex flex-wrap gap-2">
              {candidates.map((c) => (
                <button key={c.slug} className="btn" onClick={() => pick(c.slug)}>
                  {c.name}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="mt-3 flex justify-between text-xs text-muted">
          <span>Enter to dispatch · Esc to close</span>
          <button className="btn" disabled={busy} onClick={dispatch}>
            {busy ? "Resolving…" : "Dispatch"}
          </button>
        </div>
      </div>
    </div>
  );
}
