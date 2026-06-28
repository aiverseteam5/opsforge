import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getToken, setToken } from "./api";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/ui";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
});

function BrandMark() {
  return (
    <div className="flex items-center justify-center mb-5">
      <svg width="52" height="52" viewBox="0 0 52 52" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect width="52" height="52" rx="13" fill="url(#bg)" />
        <path d="M16 26h20M26 16v20" stroke="#e6edf3" strokeWidth="2.5" strokeLinecap="round" />
        <path d="M20 20l12 12M32 20L20 32" stroke="#7dd3fc" strokeWidth="1.5" strokeLinecap="round" strokeOpacity="0.55" />
        <defs>
          <linearGradient id="bg" x1="0" y1="0" x2="52" y2="52" gradientUnits="userSpaceOnUse">
            <stop stopColor="#0f2444" />
            <stop offset="1" stopColor="#0a1e33" />
          </linearGradient>
        </defs>
      </svg>
    </div>
  );
}

function Login({ onAuth }: { onAuth: () => void }) {
  const [value, setValue] = useState("");
  return (
    <div
      className="flex h-screen items-center justify-center"
      style={{ background: "radial-gradient(ellipse 80% 60% at 50% 40%, #0d1f35 0%, #0b0f14 70%)" }}
    >
      <div className="card w-[420px] space-y-4 border-edge/70 shadow-2xl">
        <BrandMark />
        <div className="text-center -mt-1">
          <div className="text-xl font-semibold tracking-tight">OpsForge</div>
          <div className="mt-0.5 text-sm text-muted">Agentic operations runtime</div>
        </div>
        <div className="border-t border-edge/50 pt-4 space-y-3">
          <p className="text-sm text-muted">
            Paste an API token to continue. Mint one with{" "}
            <code className="rounded bg-edge/60 px-1 py-0.5 text-[12px] text-sky-400">
              opsforge token create
            </code>
            .
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
            className="btn-primary w-full py-2"
            disabled={!value.trim()}
            onClick={() => {
              setToken(value);
              onAuth();
            }}
          >
            Continue →
          </button>
        </div>
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
      <ToastProvider>
        <App />
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
