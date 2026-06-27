import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CreatedToken, Token } from "../api";
import { Empty, ErrorState, Loading, PageHeader } from "../components/ui";

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function TokenRow({ token, onRevoke }: { token: Token; onRevoke: () => void }) {
  const [confirming, setConfirming] = useState(false);

  const expired =
    token.expires_at !== null && new Date(token.expires_at) < new Date();

  return (
    <div className="card flex items-start justify-between gap-4">
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex items-center gap-2">
          <span className="font-medium">{token.name ?? <span className="text-muted italic">unnamed</span>}</span>
          {expired && (
            <span className="chip border-rose-800 text-rose-400">expired</span>
          )}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted">
          <span>id: <code className="text-zinc-400">{token.id.slice(0, 8)}…</code></span>
          <span>created: {fmtDate(token.created_at)}</span>
          <span>last used: {fmtDate(token.last_used_at)}</span>
          {token.expires_at && <span>expires: {fmtDate(token.expires_at)}</span>}
        </div>
      </div>

      {confirming ? (
        <div className="flex shrink-0 items-center gap-2">
          <span className="text-xs text-rose-400">Revoke this token?</span>
          <button
            className="btn text-xs"
            style={{ borderColor: "#9f1239" }}
            onClick={onRevoke}
          >
            Yes, revoke
          </button>
          <button className="btn text-xs" onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      ) : (
        <button
          className="btn shrink-0 text-xs"
          style={{ borderColor: "#9f1239" }}
          onClick={() => setConfirming(true)}
        >
          Revoke
        </button>
      )}
    </div>
  );
}

function NewTokenBanner({ created }: { created: CreatedToken }) {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    navigator.clipboard.writeText(created.token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="rounded border border-green-700 bg-green-950/40 p-4 space-y-2">
      <div className="text-sm font-medium text-green-300">
        Token created — copy it now, it cannot be retrieved again
      </div>
      <div className="flex items-center gap-2">
        <code className="flex-1 break-all rounded bg-ink px-3 py-2 text-xs text-green-200">
          {created.token}
        </code>
        <button className="btn shrink-0 text-xs" onClick={copy}>
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
      {created.name && (
        <div className="text-xs text-muted">Name: {created.name}</div>
      )}
      {created.expires_at && (
        <div className="text-xs text-muted">Expires: {fmtDate(created.expires_at)}</div>
      )}
    </div>
  );
}

export function Tokens() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [justCreated, setJustCreated] = useState<CreatedToken | null>(null);

  const tokens = useQuery({
    queryKey: ["tokens"],
    queryFn: () => api.listTokens(),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createToken({
        name: name.trim() || undefined,
        expires_at: expiresAt || undefined,
      }),
    onSuccess: (data) => {
      setJustCreated(data);
      setName("");
      setExpiresAt("");
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeToken(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tokens"] }),
  });

  return (
    <div>
      <PageHeader title="API Tokens" sub="Create and revoke bearer tokens for API access" />

      {/* Create form */}
      <div className="card mb-6 space-y-3">
        <div className="text-sm font-medium">Create new token</div>
        <div className="flex flex-wrap gap-3">
          <input
            className="rounded border border-edge bg-surface px-2 py-1.5 text-sm
                       text-zinc-200 placeholder:text-zinc-600 focus:outline-none
                       focus:ring-1 focus:ring-sky-500 flex-1 min-w-40"
            placeholder="Name (optional)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={create.isPending}
          />
          <input
            type="datetime-local"
            className="rounded border border-edge bg-surface px-2 py-1.5 text-sm
                       text-zinc-200 focus:outline-none focus:ring-1 focus:ring-sky-500
                       [color-scheme:dark]"
            title="Expires at (optional)"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
            disabled={create.isPending}
          />
          <button
            className="btn"
            disabled={create.isPending}
            onClick={() => create.mutate()}
          >
            {create.isPending ? "Creating…" : "Create token"}
          </button>
        </div>
        {create.isError && (
          <div className="text-xs text-rose-400">
            {String(create.error instanceof Error ? create.error.message : create.error)}
          </div>
        )}
      </div>

      {/* One-time token display */}
      {justCreated && (
        <div className="mb-6">
          <NewTokenBanner created={justCreated} />
        </div>
      )}

      {/* Token list */}
      {tokens.isError && <ErrorState error={tokens.error} />}

      {tokens.isLoading ? (
        <Loading what="tokens" />
      ) : (tokens.data ?? []).length === 0 ? (
        <Empty>No API tokens yet. Create one above.</Empty>
      ) : (
        <div className="space-y-3">
          {(tokens.data ?? []).map((t) => (
            <TokenRow
              key={t.id}
              token={t}
              onRevoke={() => revoke.mutate(t.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
