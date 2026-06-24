import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getToken, setToken } from "./api";
import { Layout } from "./components/Layout";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
});

function Login({ onAuth }: { onAuth: () => void }) {
  const [value, setValue] = useState("");
  return (
    <div className="flex h-screen items-center justify-center">
      <div className="card w-[420px] space-y-4">
        <div>
          <div className="text-lg font-semibold">OpsForge</div>
          <div className="text-sm text-muted">Agentic operations runtime</div>
        </div>
        <p className="text-sm text-muted">
          Paste an API token to continue. Mint one with{" "}
          <code className="text-sky-400">opsforge token create</code>.
        </p>
        <input
          className="input"
          placeholder="ofg_…"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && value.trim()) {
              setToken(value);
              onAuth();
            }
          }}
        />
        <button
          className="btn w-full"
          disabled={!value.trim()}
          onClick={() => {
            setToken(value);
            onAuth();
          }}
        >
          Continue
        </button>
      </div>
    </div>
  );
}

function App() {
  const [authed, setAuthed] = useState(!!getToken());
  if (!authed) return <Login onAuth={() => setAuthed(true)} />;
  return (
    <BrowserRouter>
      <Layout />
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
