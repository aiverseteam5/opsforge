// Fetch-based SSE reader. EventSource can't send an Authorization header, so we
// stream the response body ourselves and parse the `event:`/`data:` frames.
import { getToken } from "./api";

export interface StreamEvent {
  event: string;
  data: any;
}

function parseFrame(frame: string): StreamEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { event, data: dataLines.join("\n") };
  }
}

export function streamRunEvents(
  runId: string,
  onEvent: (e: StreamEvent) => void,
  onDone?: (status: any) => void,
): () => void {
  const ctrl = new AbortController();
  (async () => {
    const res = await fetch(`/api/v1/runs/${runId}/events`, {
      headers: { Authorization: `Bearer ${getToken()}` },
      signal: ctrl.signal,
    });
    if (!res.body) return;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const ev = parseFrame(frame);
        if (!ev) continue;
        if (ev.event === "done") {
          onDone?.(ev.data);
          return;
        }
        onEvent(ev);
      }
    }
  })().catch(() => {
    /* aborted or network end */
  });
  return () => ctrl.abort();
}
