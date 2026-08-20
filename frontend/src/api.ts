/**
 * The docquery API, from the browser.
 *
 * Served from the same origin as this bundle, so requests are relative and
 * there is no CORS in play — which is why app.py can keep saying it has none.
 */
import { accessToken } from "./auth";

export type Source = {
  index: number;
  source: string;
  chunk_index: number;
  score: number;
  text: string;
  section: string;
  folders: string[];
  modified_at: string;
  /** An open outdated report exists for this document — existence only,
   * comments stay in the review list. Absent/false when feedback is off. */
  flagged?: boolean;
};

export type Turn = {
  seq: number;
  question: string;
  answer: string;
  rewritten_question: string;
  citations: Source[];
  model: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  complete: boolean;
  created_at: string;
};

export type Conversation = { conversation_id: string; turns: Turn[] };

export type StreamEvent =
  | { type: "sources"; sources: Source[] }
  | { type: "token"; text: string }
  | { type: "done"; conversationId: string | null; rewritten: string | null; costUsd: number }
  | { type: "error"; detail: string };

async function authorized(): Promise<HeadersInit> {
  return {
    Authorization: `Bearer ${await accessToken()}`,
    "Content-Type": "application/json",
  };
}

export async function conversation(id: string): Promise<Conversation> {
  const response = await fetch(`/conversations/${id}`, {
    headers: await authorized(),
  });
  if (!response.ok) throw new Error(`conversa indisponível (${response.status})`);
  return response.json();
}

export type ReportComment = {
  comment: string;
  reported_at: string;
};

export type ReportedDocument = {
  source: string;
  sector: string;
  report_count: number;
  last_reported_at: string;
  comments: ReportComment[];
};

/** The reported documents in the caller's sectors, newest activity first. */
export async function reportedDocuments(): Promise<ReportedDocument[]> {
  const response = await fetch("/feedback", { headers: await authorized() });
  if (!response.ok) {
    throw new Error(`sinalizações indisponíveis (${response.status})`);
  }
  const { documents } = await response.json();
  return documents;
}

/** POST with a body, not DELETE with a query string: a source is an arbitrary
 *  URI, and a query string would put it in access and proxy logs. */
export async function resolveReport(source: string): Promise<void> {
  const response = await fetch("/feedback/resolve", {
    method: "POST",
    headers: await authorized(),
    body: JSON.stringify({ source }),
  });
  if (!response.ok) throw new Error(`não foi possível resolver (${response.status})`);
}

/** Flag a document as outdated. 200 and 201 are both success: a repeat flag
 *  by the same person updates the report rather than duplicating it. */
export async function reportDocument(source: string, comment: string): Promise<void> {
  const response = await fetch("/feedback", {
    method: "POST",
    headers: await authorized(),
    body: JSON.stringify({ source, comment }),
  });
  if (!response.ok) throw new Error(`não foi possível sinalizar (${response.status})`);
}

export async function deleteConversation(id: string): Promise<void> {
  const response = await fetch(`/conversations/${id}`, {
    method: "DELETE",
    headers: await authorized(),
  });
  if (!response.ok) throw new Error(`não foi possível apagar (${response.status})`);
}

/**
 * Ask, and receive the answer as it is written.
 *
 * POST, not EventSource. EventSource only issues GET requests, and a GET would
 * put the question in the URL — and from there into access logs, proxy logs and
 * browser history. The server takes the same care (it logs a hash of the query,
 * never its text), so the client must not undo it. That means parsing the SSE
 * frames by hand, which is the small cost of not leaking the question.
 */
export async function* ask(
  query: string,
  conversationId: string | null,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch("/query/stream", {
    method: "POST",
    headers: await authorized(),
    body: JSON.stringify({ query, conversation_id: conversationId }),
    signal,
  });

  if (!response.ok || !response.body) {
    const detail = await response.text().catch(() => "");
    yield {
      type: "error",
      detail: detail || `a consulta falhou (${response.status})`,
    };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by a blank line; anything after the last one is a
    // partial frame and has to wait for more bytes.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let name = "";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (!name) continue;
      const payload = data ? JSON.parse(data) : {};
      if (name === "sources") yield { type: "sources", sources: payload.sources };
      else if (name === "token") yield { type: "token", text: payload.t };
      else if (name === "error") yield { type: "error", detail: payload.detail };
      else if (name === "done")
        yield {
          type: "done",
          conversationId: payload.conversation_id ?? null,
          rewritten: payload.rewritten_query ?? null,
          costUsd: payload.cost_usd ?? 0,
        };
    }
  }
}
