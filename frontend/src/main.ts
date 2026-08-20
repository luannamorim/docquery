/**
 * docquery — ask the company's documents a question.
 *
 * Deliberately one screen with no client-side router: the API serves this
 * bundle from its own origin at "/", and adding routes would mean teaching the
 * static mount to fall back to index.html for paths it does not have.
 */
import "./styles.css";
import {
  ask,
  conversation,
  deleteConversation,
  reportDocument,
  reportedDocuments,
  resolveReport,
  type Source,
} from "./api";
import { account, accessToken, initAuth, signIn, signOut } from "./auth";
import {
  answerBody,
  assistantColumn,
  el,
  exitIcon,
  markCitedSources,
  questionBubble,
  attachRewrite,
  sectorColor,
  logo,
  searchIcon,
  sendIcon,
  searchable,
  sourcesBlock,
  userIcon,
  turnBlock,
  flagIcon,
  reviewPanel,
} from "./ui";

type Config = {
  tenantId: string;
  clientId: string;
  apiClientId: string;
  appName: string;
  feedbackEnabled: boolean;
};

const root = document.getElementById("app")!;
let currentConversation: string | null = null;
let inFlight: AbortController | null = null;
let appName = "docquery";
let feedbackEnabled = false;
let conversationFilter = "";
let searchOpen = false;

/** The part of an address a person recognises themselves by.
 *  ana.silva@empresa.com.br is the same person in every row of the sidebar;
 *  the domain is the same for everyone and earns none of that width. */
/** Put the caret where the next thing to do is.
 *
 *  Queried rather than threaded through: refreshRail is called from the rail,
 *  from opening a conversation and from starting a new one, and passing the
 *  textarea down every one of those paths would be more wiring than the single
 *  element in the app is worth. */
function focusComposer(): void {
  root.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus();
}

function shortName(username: string): string {
  return username.split("@")[0] || username;
}

/** The flag-as-outdated handler, or undefined when the feature is off — the
 *  cards render no affordance at all rather than a button that would 404. */
function reportHandler() {
  return feedbackEnabled
    ? (source: string, comment: string) => reportDocument(source, comment)
    : undefined;
}

/** Config comes from the server so the image is built once and configured per
 *  deployment — a bundle with a tenant id baked in would need rebuilding per
 *  environment. Ids are public identifiers, not secrets. */
async function loadConfig(): Promise<Config> {
  const response = await fetch("/config");
  if (!response.ok) throw new Error("configuração indisponível");
  return response.json();
}

/**
 * The app behind frosted glass, with the way in centred on top of it.
 *
 * The fallback, not the front door: reached only when the automatic redirect
 * has already been tried and came back without a session, or after an explicit
 * sign-out. Both are cases where the user needs somewhere to read what happened
 * and something to press.
 *
 * The blurred layer is the real shell — rail, thread, composer — not a picture
 * of one, so what you glimpse is what you get. It is built from non-interactive
 * elements and hidden from assistive technology: there is nothing back there to
 * use yet, and a blurred control that cannot be pressed is worse than no
 * control.
 */
function wordmark(): HTMLElement {
  // Logo and name side by side. The name stays either way — a mark alone tells
  // a first-time user nothing, and the deployment may have supplied no logo.
  const row = el("div", "wordmark");
  row.append(logo("wordmark-logo"));
  row.append(el("span", undefined, appName));
  return row;
}

function signInScreen(problemText = ""): void {
  root.dataset.state = "gate";
  root.replaceChildren();

  const locked = el("div", "locked");
  locked.setAttribute("aria-hidden", "true");

  const rail = el("nav", "rail");
  rail.append(wordmark());
  rail.append(el("div", "new-chat", "+ Nova conversa"));
  rail.append(el("div", "rail-heading", "Conversas"));

  const thread = el("main", "thread");
  const scroll = el("div", "scroll");
  const turns = el("div", "turns");
  const intro = el("div", "empty");
  intro.append(el("h1", undefined, "Como posso ajudar?"));
  intro.append(el("p", undefined, "Faça uma pergunta. Eu leio os documentos por você."));
  turns.append(intro);
  scroll.append(turns);

  const composer = el("div", "composer");
  const inner = el("div", "composer-inner");
  inner.append(el("div", "composer-ghost", "Pergunte alguma coisa"));
  composer.append(inner);
  thread.append(scroll, composer);
  locked.append(rail, thread);

  const gate = el("div", "gate");
  const card = el("div", "gate-card");

  const portrait = el("div", "gate-portrait");
  // The logo if the deployment supplied one, the generic person if not.
  portrait.append(logo("gate-logo", () => portrait.append(userIcon())));
  card.append(portrait);
  card.append(el("h1", "gate-mark", appName));

  const button = el("button", "signin-button", "Entrar");
  button.type = "button";
  const problem = el("div", "error", problemText);
  problem.hidden = !problemText;
  button.addEventListener("click", () => {
    problem.hidden = true;
    // Awaited and caught: a failed redirect must say so on screen, not vanish
    // into an unhandled rejection that looks like a dead button.
    signIn().catch((error: Error) => {
      problem.textContent = `Não foi possível iniciar o login: ${error.message}`;
      problem.hidden = false;
      console.error("loginRedirect falhou", error);
    });
  });
  card.append(button, problem);
  gate.append(card);

  root.append(locked, gate);
  button.focus();
}

function shell(): { rail: HTMLElement; scroll: HTMLElement; turns: HTMLElement } {
  root.dataset.state = "ready";
  root.replaceChildren();

  const rail = el("nav", "rail");
  const thread = el("main", "thread");
  const scroll = el("div", "scroll");
  const turns = el("div", "turns");
  scroll.append(turns);

  const composer = el("form", "composer");
  const inner = el("div", "composer-inner");
  const input = el("textarea");
  input.rows = 1;
  input.placeholder = "Pergunte alguma coisa";
  input.setAttribute("aria-label", "Sua pergunta");
  const send = el("button", "send");
  send.type = "submit";
  // The icon is decorative; the button still needs a name to be announced and
  // to show a tooltip.
  send.setAttribute("aria-label", "Perguntar");
  send.title = "Perguntar";
  send.append(sendIcon());
  inner.append(input, send);
  composer.append(inner);

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  });
  input.addEventListener("keydown", (event) => {
    // Enter sends, Shift+Enter breaks the line — the convention people already
    // have from every other message box.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || inFlight) return;
    input.value = "";
    input.style.height = "auto";
    void submit(question, turns, scroll, send);
  });

  thread.append(scroll, composer);
  root.append(rail, thread);
  input.focus();
  return { rail, scroll, turns };
}

async function refreshRail(rail: HTMLElement, turns: HTMLElement, scroll: HTMLElement) {
  rail.replaceChildren();
  rail.append(wordmark());

  const fresh = el("button", "new-chat", "+ Nova conversa");
  fresh.addEventListener("click", () => {
    currentConversation = null;
    turns.replaceChildren();
    showEmpty(turns);
    void refreshRail(rail, turns, scroll);
    // Starting a conversation is the act of wanting to type one.
    focusComposer();
  });
  rail.append(fresh);

  try {
    const response = await fetch("/conversations", {
      headers: { Authorization: `Bearer ${await accessToken()}` },
    });
    const { conversations } = await response.json();

    const rows: HTMLElement[] = [];
    const nothing = el("div", "rail-empty", "Nenhuma conversa com esse termo.");
    nothing.hidden = true;

    const apply = () => {
      const needle = searchable(conversationFilter.trim());
      let shown = 0;
      for (const row of rows) {
        const hit = !needle || searchable(row.dataset.title ?? "").includes(needle);
        row.hidden = !hit;
        if (hit) shown++;
      }
      nothing.hidden = shown > 0 || !needle;
    };

    if (conversations.length) {
      // Heading and field occupy the same row, one at a time. Costing no space
      // until it is wanted is what lets it be there from the first
      // conversation, instead of appearing at some threshold nobody can see.
      const heading = el("div", "rail-heading rail-heading-row");
      heading.append(el("span", undefined, "Conversas"));
      const magnifier = el("button", "rail-search-toggle");
      magnifier.type = "button";
      magnifier.setAttribute("aria-label", "Buscar conversa");
      magnifier.title = "Buscar conversa";
      magnifier.append(searchIcon());
      heading.append(magnifier);

      const search = el("input", "rail-search");
      search.type = "search";
      search.placeholder = "Buscar conversa…";
      search.setAttribute("aria-label", "Buscar conversa");
      search.value = conversationFilter;

      const setOpen = (open: boolean) => {
        searchOpen = open;
        heading.hidden = open;
        search.hidden = !open;
        if (open) search.focus();
      };
      magnifier.addEventListener("click", () => setOpen(true));
      search.addEventListener("input", () => {
        // Rows are hidden in place rather than the rail re-rendered, so the
        // field keeps its focus and caret while the list narrows.
        conversationFilter = search.value;
        apply();
      });
      search.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        conversationFilter = "";
        search.value = "";
        apply();
        setOpen(false);
      });
      search.addEventListener("blur", () => {
        // An empty field has nothing to show for the space it takes.
        if (!search.value) setOpen(false);
      });

      rail.append(heading, search);
      setOpen(searchOpen);
    } else {
      conversationFilter = "";
    }
    // Its own scroll container: the wordmark, the new-chat button and the
    // search stay put at the top while only the list moves, and the user
    // footer stays reachable at the bottom however long the list gets.
    const list = el("div", "rail-list");
    for (const item of conversations) {
      const button = el("button", "conversation", item.title || "sem título");
      button.title = item.title;
      button.dataset.title = item.title ?? "";
      if (item.id === currentConversation) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => void open(item.id, rail, turns, scroll));
      rows.push(button);
      list.append(button);
    }
    list.append(nothing);
    rail.append(list);
    apply();
  } catch {
    rail.append(el("div", "rail-heading", "Conversas indisponíveis"));
  }

  if (feedbackEnabled) {
    const review = el("button", "rail-review");
    review.append(flagIcon(), el("span", undefined, "Documentos sinalizados"));
    review.addEventListener("click", () => void showReview(turns, scroll));
    rail.append(review);
  }

  const username = account()?.username ?? "";
  const foot = el("div", "rail-foot");

  // Initial disc, coloured by the same hash the sector tabs use. One colour
  // language for "who" and "which compartment", rather than two inventions.
  const disc = el("span", "avatar", (shortName(username)[0] ?? "?").toUpperCase());
  disc.style.setProperty("--sector", sectorColor(username));

  const who = el("span", "who", shortName(username));
  who.title = username;

  const out = el("button", "signout");
  out.type = "button";
  out.title = "Sair";
  out.setAttribute("aria-label", "Sair");
  out.append(exitIcon(), el("span", "signout-label", "Sair"));
  out.addEventListener("click", () => {
    // Local sign-out does not navigate, so nothing else will repaint the page.
    signOut()
      .then(() => signInScreen())
      .catch((error) => console.error("logout falhou", error));
  });

  foot.append(disc, who, out);
  rail.append(foot);
}

/** The review list takes the thread's place — this app deliberately has no
 *  router, so "another screen" is the turns container showing something else,
 *  the same mechanics open() uses. Asking a question leaves it again. */
async function showReview(turns: HTMLElement, scroll: HTMLElement) {
  currentConversation = null;
  turns.replaceChildren();
  try {
    const docs = await reportedDocuments();
    turns.append(reviewPanel(docs, resolveReport));
  } catch (error) {
    turns.append(el("div", "error", (error as Error).message));
  }
  scroll.scrollTop = 0;
}

function showEmpty(turns: HTMLElement) {
  const empty = el("div", "empty");
  empty.append(el("h1", undefined, "Como posso ajudar?"));
  empty.append(el("p", undefined, "Faça uma pergunta. Eu leio os documentos por você."));

  // Nothing beyond the one line. An empty screen that explains itself is a
  // screen nobody reads twice.
  turns.append(empty);
}

async function open(
  id: string,
  rail: HTMLElement,
  turns: HTMLElement,
  scroll: HTMLElement,
) {
  currentConversation = id;
  turns.replaceChildren();
  try {
    const data = await conversation(id);
    data.turns.forEach((turn) => turns.append(turnBlock(turn, reportHandler())));
  } catch (error) {
    turns.append(el("div", "error", (error as Error).message));
  }
  await refreshRail(rail, turns, scroll);
  scroll.scrollTop = scroll.scrollHeight;
  // Reopening a conversation is almost always continuing it.
  focusComposer();
}

async function submit(
  question: string,
  turns: HTMLElement,
  scroll: HTMLElement,
  send: HTMLButtonElement,
) {
  turns.querySelector(".empty")?.remove();
  turns.querySelector(".review")?.remove();

  const block = el("article", "turn");
  const questionRow = questionBubble(question, new Date());
  block.append(questionRow);
  const answerSide = assistantColumn();
  // A slot the streaming text re-renders into, placed before the sources so
  // the answer stays above them without anything moving when it finishes.
  const slot = el("div", "answer-slot");
  answerSide.append(slot);
  block.append(answerSide);
  turns.append(block);
  scroll.scrollTop = scroll.scrollHeight;

  send.disabled = true;
  inFlight = new AbortController();
  let answer = "";
  let sources: Source[] = [];
  let body: HTMLElement | null = null;

  try {
    for await (const event of ask(question, currentConversation, inFlight.signal)) {
      if (event.type === "sources") {
        sources = event.sources;
        // Rendered before the first token, which is the whole point: the reader
        // sees which documents the answer is coming from while it is still
        // being written.
        // Appended after the answer slot: they still arrive before the first
        // token, filling the wait, but they sit under the text rather than
        // above it — and nothing jumps when the answer completes.
        if (sources.length) answerSide.append(sourcesBlock(sources, reportHandler()));
      } else if (event.type === "token") {
        answer += event.text;
        // Re-rendered per token so the [n] markers stay bound as they arrive.
        body?.remove();
        body = answerBody(answer, answerSide);
        slot.append(body);
        scroll.scrollTop = scroll.scrollHeight;
      } else if (event.type === "error") {
        answerSide.append(el("div", "error", event.detail));
      } else {
        // The answer is complete, so which sources it actually used is now
        // knowable — and only now.
        markCitedSources(answerSide, answer);
        currentConversation = event.conversationId ?? currentConversation;
        if (event.rewritten) {
          // A mark inside the bubble, revealed on click.
          attachRewrite(questionRow, event.rewritten);
        }
      }
    }
  } catch (error) {
    if ((error as Error).name !== "AbortError") {
      block.append(el("div", "error", (error as Error).message));
    }
  } finally {
    inFlight = null;
    send.disabled = false;
  }
}

async function start() {
  try {
    const config = await loadConfig();
    appName = config.appName || appName;
    feedbackEnabled = config.feedbackEnabled ?? false;
    document.title = appName;
    const signedIn = await initAuth(config);
    // Sign-in is always a deliberate click. Redirecting on load saved a click
    // and cost the only place an error could be read — and left no way to
    // stay signed out.
    if (!signedIn) return signInScreen();

    const { rail, scroll, turns } = shell();
    showEmpty(turns);
    await refreshRail(rail, turns, scroll);
  } catch (error) {
    root.replaceChildren(el("p", "boot", (error as Error).message));
  }
}

void start();

export { deleteConversation };
