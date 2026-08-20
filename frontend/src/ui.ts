/**
 * Rendering.
 *
 * Two ideas carry this interface, and both come from the system underneath it:
 *
 * 1. Sources land before the answer. The pipeline finishes retrieval and
 *    reranking before it calls the LLM, so by the time a single word exists we
 *    already know which documents it will be written from. Showing them first
 *    replaces a spinner with evidence, and it is the reason to trust an answer
 *    about a contract.
 *
 * 2. A citation's colour is derived from its top folder, never configured —
 *    the same rule ingest follows when it derives the sector from the path. A
 *    new folder gets a colour on its first appearance, and the colour tells you
 *    which compartment a passage came from.
 */
import type { ReportedDocument, Source, Turn } from "./api";

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Stable hue per folder name. Derived, never configured — like the taxonomy. */
export function sectorColor(folder: string): string {
  if (!folder) return "var(--ink-soft)";
  let hash = 0;
  for (let i = 0; i < folder.length; i++) {
    hash = (hash * 31 + folder.charCodeAt(i)) | 0;
  }
  const hue = Math.abs(hash) % 360;
  // Constrained saturation and lightness so any folder lands somewhere legible
  // against both themes rather than somewhere merely distinct.
  return `hsl(${hue} 42% 46%)`;
}

/**
 * Fold a string down to what a search should compare.
 *
 * Accents are stripped, not preserved: someone looking for "férias" types
 * "ferias" as often as not, and a filter that misses their own conversation
 * over a diacritic is a filter they stop using. NFD splits a letter from its
 * combining mark so the marks can be dropped.
 */
export function searchable(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

/** A person with no photo, because there is no one signed in to have one. */
export function userIcon(): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "30");
  svg.setAttribute("height", "30");
  svg.setAttribute("aria-hidden", "true");
  const head = document.createElementNS(ns, "circle");
  head.setAttribute("cx", "12");
  head.setAttribute("cy", "9");
  head.setAttribute("r", "3.6");
  const shoulders = document.createElementNS(ns, "path");
  shoulders.setAttribute("d", "M4.8 20a7.2 7.2 0 0 1 14.4 0");
  for (const node of [head, shoulders]) {
    node.setAttribute("fill", "none");
    node.setAttribute("stroke", "currentColor");
    node.setAttribute("stroke-width", "1.6");
    node.setAttribute("stroke-linecap", "round");
  }
  svg.append(head, shoulders);
  return svg;
}

/**
 * The company logo from public/logo.png, or nothing.
 *
 * `onerror` matters: the file is supplied per deployment and may simply not be
 * there. Without the handler the browser draws its broken-image glyph, which
 * looks like a bug in the app rather than an absent optional asset — so a
 * missing logo removes itself and lets the caller's fallback stand.
 */
export function logo(className: string, onMissing?: () => void): HTMLImageElement {
  const img = document.createElement("img");
  img.className = className;
  img.src = "/logo.png";
  img.alt = "";
  img.addEventListener("error", () => {
    img.remove();
    onMissing?.();
  });
  return img;
}

/** An arrow up: send this, in the direction the conversation grows. */
export function sendIcon(): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M8 13V3.5M3.75 7.75 8 3.5l4.25 4.25");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.8");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.append(path);
  return svg;
}

export function searchIcon(): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("aria-hidden", "true");
  const lens = document.createElementNS(ns, "circle");
  lens.setAttribute("cx", "7");
  lens.setAttribute("cy", "7");
  lens.setAttribute("r", "4.5");
  const handle = document.createElementNS(ns, "path");
  handle.setAttribute("d", "M10.4 10.4 14 14");
  for (const node of [lens, handle]) {
    node.setAttribute("fill", "none");
    node.setAttribute("stroke", "currentColor");
    node.setAttribute("stroke-width", "1.5");
    node.setAttribute("stroke-linecap", "round");
  }
  svg.append(lens, handle);
  return svg;
}

/** A pennant: something here deserves a second look. */
export function flagIcon(): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M3.5 14V2.5M3.5 2.5h8.5l-2 3 2 3H3.5");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.4");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.append(path);
  return svg;
}

/** Inline SVG rather than a font icon: nothing here loads from a CDN. */
export function exitIcon(): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute(
    "d",
    "M6 2H3.5A1.5 1.5 0 0 0 2 3.5v9A1.5 1.5 0 0 0 3.5 14H6M10.5 11 14 8l-3.5-3M6.5 8H14",
  );
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.4");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.append(path);
  return svg;
}

/**
 * When the document was last updated, said in the reader's locale.
 *
 * The API leaves the date empty when neither the library it came from nor the
 * file itself records one, and that emptiness is shown rather than hidden: "we
 * do not know when this changed" and "this changed today" are opposite answers,
 * and the ingest date — the one thing always available — is neither.
 */
function updatedLabel(modifiedAt: string): string {
  if (!modifiedAt) return "data desconhecida";
  const when = new Date(modifiedAt);
  if (Number.isNaN(when.getTime())) return "data desconhecida";
  // A date, not a verdict: "atualizado" read as a claim of freshness, which
  // sat in contradiction beside a document flagged as outdated.
  return `atualização: ${when.toLocaleDateString()}`;
}

/** What flagging a document does; undefined means the feature is off. */
export type ReportHandler = (source: string, comment: string) => Promise<void>;

/** The red state a flag shows once its document has an open report. */
function paintFlagged(flag: HTMLButtonElement): void {
  flag.dataset.flagged = "true";
  flag.title = "Sinalizado como desatualizado";
  flag.setAttribute("aria-label", "Sinalizado como desatualizado");
}

/**
 * A report covers the document, not the passage: once this asker reports one
 * card, every card of the same source on screen shows the red flag and stops
 * offering to report again. The next answer gets the same truth from the
 * server (`source.flagged`), so nothing here needs to persist.
 */
function markReported(source: string): void {
  document
    .querySelectorAll<HTMLButtonElement>("button.source-flag")
    .forEach((flag) => {
      if (flag.dataset.source === source) {
        paintFlagged(flag);
        flag.disabled = true;
      }
    });
}

/**
 * The flag affordance beside a source card.
 *
 * A sibling of the card, never a child: the card is a <button>, and a button
 * inside a button is invalid HTML that browsers will "fix" by reparenting.
 * Being a sibling also means clicking the flag never toggles the card.
 */
function flagButton(source: Source, row: HTMLElement, onReport: ReportHandler): HTMLElement {
  const flag = el("button", "source-flag");
  flag.type = "button";
  flag.dataset.source = source.source;
  flag.title = "Sinalizar como desatualizado";
  flag.setAttribute("aria-label", "Sinalizar como desatualizado");
  flag.append(flagIcon());
  if (source.flagged) {
    // Someone already reported this document. The flag shows it in red but
    // stays clickable: a report is per person, and another asker's report
    // must not stop this one from adding their own comment.
    paintFlagged(flag);
  }

  let form: HTMLElement | null = null;
  flag.addEventListener("click", () => {
    if (form) {
      form.remove();
      form = null;
      return;
    }
    form = el("div", "flag-form");
    const comment = el("input");
    comment.type = "text";
    comment.maxLength = 500;
    comment.placeholder = "comentário (opcional)";
    comment.setAttribute("aria-label", "Comentário para quem revisar");
    const confirm = el("button", "flag-confirm", "Sinalizar");
    confirm.type = "button";
    const cancel = el("button", "flag-cancel", "Cancelar");
    cancel.type = "button";
    form.append(comment, confirm, cancel);
    row.append(form);
    comment.focus();

    cancel.addEventListener("click", () => {
      form?.remove();
      form = null;
    });
    confirm.addEventListener("click", () => {
      confirm.disabled = true;
      onReport(source.source, comment.value.trim())
        .then(() => {
          form?.remove();
          form = null;
          row.append(el("div", "flag-done", "Sinalizado para revisão."));
          markReported(source.source);
        })
        .catch((error: Error) => {
          confirm.disabled = false;
          form?.querySelector(".error")?.remove();
          form?.append(el("span", "error", error.message));
        });
    });
  });
  return flag;
}

function sourceCard(source: Source, onReport?: ReportHandler): HTMLElement {
  const card = el("button", "source");
  card.type = "button";
  card.setAttribute("aria-expanded", "false");
  card.dataset.index = String(source.index);

  const index = el("span", "source-index", `[${source.index}]`);
  const tab = el("span", "source-sector");
  tab.style.setProperty("--sector", sectorColor(source.folders[0] ?? ""));
  tab.title = source.folders[0] ? `setor: ${source.folders[0]}` : "sem setor";

  // The file name is what a reader recognises; the folders above it are already
  // said by the coloured sector tab. The full path stays the document's
  // identity, though — it distinguishes two files of the same name and it is
  // what you paste back to scope a follow-up — so it is one hover or one click
  // away rather than gone.
  const name = source.source.split("/").pop() || source.source;
  card.title = source.source;

  const body = el("span");
  body.append(el("span", "source-path", name));
  if (source.section) {
    body.append(el("span", "source-section", ` · ${source.section}`));
  }
  const updated = el("span", "source-date", ` · ${updatedLabel(source.modified_at)}`);
  updated.title = source.modified_at
    ? `data de atualização: ${source.modified_at}`
    : "nenhuma fonte registra quando este documento foi atualizado";
  body.append(updated);
  const detail = el("span", "source-text");
  detail.append(el("span", "source-full", source.source));
  detail.append(document.createTextNode(source.text));
  body.append(detail);

  card.append(index, tab, body);
  card.addEventListener("click", () => {
    const open = card.getAttribute("aria-expanded") === "true";
    card.setAttribute("aria-expanded", open ? "false" : "true");
  });

  // Always wrapped, with or without the flag, so the list has one DOM shape
  // for the border and hover rules to address.
  const row = el("div", "source-row");
  row.append(card);
  if (onReport) {
    row.classList.add("flaggable");
    row.append(flagButton(source, row, onReport));
  }
  return row;
}

/**
 * Once the answer exists, separate what it used from what it merely considered.
 *
 * The sources arrive before the first token — that is the point of this
 * interface — so at the moment they are drawn nobody knows which will be cited.
 * Leaving all of them on display afterwards reads as "here is what supports
 * this", and under an answer that cites none it looks like the system found
 * something relevant and failed to use it.
 *
 * The uncited ones are folded away rather than deleted: what retrieval reached
 * for and the answer declined is exactly what you want to see when the answer
 * is wrong.
 */
export function markCitedSources(block: HTMLElement, answer: string): void {
  const cited = new Set(Array.from(answer.matchAll(/\[(\d+)\]/g), (m) => m[1]));
  const cards = Array.from(block.querySelectorAll<HTMLElement>(".source"));
  if (!cards.length) return;

  const uncited = cards.filter((card) => !cited.has(card.dataset.index ?? ""));
  if (!uncited.length) return;

  // Hide the row, not the card: hiding only the inner button would leave its
  // sibling flag floating beside an empty slot.
  const shell = (card: HTMLElement) =>
    card.closest<HTMLElement>(".source-row") ?? card;
  uncited.forEach((card) => {
    card.dataset.uncited = "true";
    shell(card).hidden = true;
  });

  const head = block.querySelector(".sources-head");
  const count = cards.length - uncited.length;
  if (head) {
    head.textContent = count
      ? `${count} ${count === 1 ? "trecho citado" : "trechos citados"}`
      : "nenhum trecho citado";
  }

  const toggle = el(
    "button",
    "sources-toggle",
    `mostrar ${uncited.length} ${
      uncited.length === 1 ? "trecho consultado" : "trechos consultados"
    }`,
  );
  toggle.type = "button";
  let open = false;
  toggle.addEventListener("click", () => {
    open = !open;
    uncited.forEach((card) => (shell(card).hidden = !open));
    toggle.textContent = open
      ? "ocultar trechos não citados"
      : `mostrar ${uncited.length} ${
          uncited.length === 1 ? "trecho consultado" : "trechos consultados"
        }`;
  });
  block.append(toggle);
}

export function sourcesBlock(
  sources: Source[],
  onReport?: ReportHandler,
): HTMLElement {
  const box = el("div", "sources");
  const count = sources.length;
  box.append(
    el(
      "div",
      "sources-head",
      count === 1 ? "1 trecho encontrado" : `${count} trechos encontrados`,
    ),
  );
  sources.forEach((source) => box.append(sourceCard(source, onReport)));
  return box;
}

function citationMarker(index: string, container: HTMLElement): HTMLElement {
  const marker = el("button", "cite", `[${index}]`);
  marker.type = "button";
  const card = () =>
    container.querySelector<HTMLElement>(`.source[data-index="${index}"]`);
  const light = (on: boolean) => {
    const target = card();
    if (target) target.dataset.lit = String(on);
    marker.dataset.lit = String(on);
  };
  marker.addEventListener("mouseenter", () => light(true));
  marker.addEventListener("mouseleave", () => light(false));
  marker.addEventListener("focus", () => light(true));
  marker.addEventListener("blur", () => light(false));
  marker.addEventListener("click", () => {
    card()?.setAttribute("aria-expanded", "true");
    card()?.scrollIntoView({ block: "nearest" });
  });
  return marker;
}

//: Bold, italic, inline code, and a citation — in that order, so ** is taken
//: before a single *.
const INLINE = /\*\*(.+?)\*\*|\*(.+?)\*|`([^`]+)`|\[(\d+)\]/g;

/**
 * One line of answer text, as nodes.
 *
 * Nodes are built, never parsed from a string of HTML. This text is written by
 * a model from passages of indexed documents, so a contract containing markup
 * would otherwise be handing us HTML to execute. Creating only text nodes and
 * elements chosen here means any markup in the answer is shown as the
 * characters it is.
 */
function inline(text: string, container: HTMLElement): DocumentFragment {
  const fragment = document.createDocumentFragment();
  let last = 0;
  let match: RegExpExecArray | null;
  INLINE.lastIndex = 0;

  while ((match = INLINE.exec(text)) !== null) {
    if (match.index > last) {
      fragment.append(document.createTextNode(text.slice(last, match.index)));
    }
    const [, bold, italic, code, citation] = match;
    if (bold !== undefined) fragment.append(el("strong", undefined, bold));
    else if (italic !== undefined) fragment.append(el("em", undefined, italic));
    else if (code !== undefined) fragment.append(el("code", undefined, code));
    else if (citation !== undefined) {
      fragment.append(citationMarker(citation, container));
    }
    last = INLINE.lastIndex;
  }
  if (last < text.length) fragment.append(document.createTextNode(text.slice(last)));
  return fragment;
}

const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*)$/;

/**
 * Answer text as blocks, with its [n] markers bound to the cards above it.
 *
 * A deliberately small slice of Markdown — paragraphs, bullet and numbered
 * lists, headings, bold, italic, inline code. It is what the model actually
 * produces when asked to answer with citations, and stopping there keeps this
 * a renderer rather than a parser to maintain. Tables are **not** handled: they
 * arrive as literal pipes, which is ugly but readable, and the alternative is
 * a table parser and its bugs.
 *
 * Hovering a citation lights its card and vice versa, so "where did this
 * sentence come from" is one glance rather than one click.
 */
export function answerBody(text: string, container: HTMLElement): HTMLElement {
  const body = el("div", "answer");
  const lines = text.split("\n");
  let paragraph: string[] = [];
  let list: HTMLElement | null = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const p = el("p");
    paragraph.forEach((line, i) => {
      if (i) p.append(el("br"));
      p.append(inline(line, container));
    });
    body.append(p);
    paragraph = [];
  };
  const flushAll = () => {
    flushParagraph();
    list = null;
  };

  for (const line of lines) {
    const bullet = line.match(BULLET);
    const numbered = line.match(NUMBERED);
    const heading = line.match(HEADING);

    if (!line.trim()) {
      flushAll();
    } else if (heading) {
      flushAll();
      body.append(
        (() => {
          const h = el("p", "answer-heading");
          h.append(inline(heading[1], container));
          return h;
        })(),
      );
    } else if (bullet || numbered) {
      flushParagraph();
      const wanted = bullet ? "UL" : "OL";
      if (!list || list.tagName !== wanted) {
        list = el(bullet ? "ul" : "ol");
        body.append(list);
      }
      const item = el("li");
      item.append(inline((bullet ?? numbered)![1], container));
      list.append(item);
    } else {
      list = null;
      paragraph.push(line);
    }
  }
  flushAll();
  return body;
}

/** What the person asked, as a bubble on their side of the thread. */
export function questionBubble(text: string): HTMLElement {
  const row = el("div", "row-user");
  row.append(el("div", "bubble", text));
  return row;
}

function infoIcon(): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("aria-hidden", "true");
  const ring = document.createElementNS(ns, "circle");
  ring.setAttribute("cx", "8");
  ring.setAttribute("cy", "8");
  ring.setAttribute("r", "6.25");
  const stem = document.createElementNS(ns, "path");
  stem.setAttribute("d", "M8 7.2v4");
  const dot = document.createElementNS(ns, "path");
  dot.setAttribute("d", "M8 4.9h.01");
  for (const node of [ring, stem, dot]) {
    node.setAttribute("fill", "none");
    node.setAttribute("stroke", "currentColor");
    node.setAttribute("stroke-width", "1.4");
    node.setAttribute("stroke-linecap", "round");
  }
  svg.append(ring, stem, dot);
  return svg;
}

/**
 * Mark a question that was rewritten before it was searched.
 *
 * The mark lives inside the bubble because it is a footnote on what was
 * *asked*, not a step in what was answered. Information, not warning: a
 * follow-up being resolved is the feature working, and an exclamation would
 * alarm people about ordinary behaviour every single turn.
 *
 * Still worth showing at all, because the rewrite is silent and can be wrong —
 * anchoring on the wrong contract in a conversation that named two — and this
 * is the only thing that would explain the answer.
 */
export function attachRewrite(row: HTMLElement, rewritten: string): void {
  const bubble = row.querySelector<HTMLElement>(".bubble");
  if (!bubble) return;

  const toggle = el("button", "rewrite-mark");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", "Ver a pergunta reescrita");
  toggle.title = "Ver a pergunta reescrita";
  toggle.append(infoIcon());

  const detail = el("div", "rewritten");
  const text = el("p", "rewritten-detail");
  text.append(el("code", undefined, rewritten));
  detail.append(text);
  detail.hidden = true;

  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", open ? "false" : "true");
    detail.hidden = open;
  });

  bubble.append(toggle);
  row.after(detail);
}

/**
 * The answer's side of the thread: left, and not a bubble.
 *
 * An answer here is a passage with citations inside it and a block of sources
 * above it. Wrapping that in a tinted balloon would fight its own content —
 * the bubble marks the short thing a person typed, the answer just needs room.
 */
export function assistantColumn(): HTMLElement {
  return el("div", "row-assistant");
}

export function incompleteNote(): HTMLElement {
  return el("div", "meta incomplete", "Resposta interrompida.");
}

/**
 * The documents people flagged as outdated, for whoever will review them.
 *
 * Scoped by the API to the reader's sectors, so what this lists is what the
 * reader is allowed to know was flagged. Comments are people's own words —
 * rendered as text nodes like everything else here, never as markup.
 */
export function reviewPanel(
  docs: ReportedDocument[],
  onResolve: (source: string) => Promise<void>,
): HTMLElement {
  const panel = el("div", "review");
  panel.append(el("h1", "review-title", "Documentos sinalizados"));

  if (!docs.length) {
    panel.append(el("p", "review-empty", "Nenhum documento sinalizado."));
    return panel;
  }

  for (const doc of docs) {
    const item = el("div", "review-item");

    const head = el("div", "review-head");
    const tab = el("span", "review-sector");
    tab.style.setProperty("--sector", sectorColor(doc.sector));
    tab.title = `setor: ${doc.sector}`;
    const name = el("span", "review-name", doc.source.split("/").pop() || doc.source);
    name.title = doc.source;
    head.append(tab, name);

    const resolve = el("button", "review-resolve", "Resolver");
    resolve.type = "button";
    resolve.title = "O documento foi revisado — apaga as sinalizações";
    resolve.addEventListener("click", () => {
      resolve.disabled = true;
      onResolve(doc.source)
        .then(() => item.remove())
        .catch((error: Error) => {
          resolve.disabled = false;
          item.querySelector(".error")?.remove();
          item.append(el("div", "error", error.message));
        });
    });
    head.append(resolve);
    item.append(head);

    const when = new Date(doc.last_reported_at);
    const last = Number.isNaN(when.getTime()) ? "" : when.toLocaleDateString();
    item.append(
      el(
        "div",
        "review-meta",
        `${doc.report_count} ${
          doc.report_count === 1 ? "sinalização" : "sinalizações"
        }${last ? ` · última em ${last}` : ""}`,
      ),
    );
    // The identity, selectable like .source-full: the reviewer's next step is
    // pasting it into a re-ingest or a scoped query.
    item.append(el("div", "review-source", doc.source));

    if (doc.comments.length) {
      const list = el("ul", "review-comments");
      for (const comment of doc.comments) {
        list.append(el("li", undefined, comment));
      }
      item.append(list);
    }
    panel.append(item);
  }
  return panel;
}

export function turnBlock(turn: Turn, onReport?: ReportHandler): HTMLElement {
  const block = el("article", "turn");
  const row = questionBubble(turn.question);
  block.append(row);
  if (turn.rewritten_question) attachRewrite(row, turn.rewritten_question);

  const answer = assistantColumn();
  answer.append(answerBody(turn.answer, answer));
  if (turn.citations.length) answer.append(sourcesBlock(turn.citations, onReport));
  // History renders a finished turn, so the split is known from the start.
  markCitedSources(answer, turn.answer);
  if (!turn.complete) answer.append(incompleteNote());

  block.append(answer);
  return block;
}
