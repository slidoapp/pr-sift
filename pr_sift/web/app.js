// The report page: renders /api/pr and keeps review state in sync.

const TOKEN = document.querySelector('meta[name="pr-sift-token"]').content;

const TIERS = [
  { key: "architecture", label: "Architecture", desc: "Structure and dependencies between components" },
  { key: "interface", label: "Interface", desc: "What callers and clients depend on" },
  { key: "behavior", label: "Behavior", desc: "Logic worth understanding" },
  { key: "detail", label: "Detail", desc: "Local changes that don't change what a component does" },
  { key: "unranked", label: "Not ranked yet", desc: "Run /pr-sift to rank these" },
  { key: "skim", label: "Skim", desc: "Repetitive or low-risk: the summary is enough" },
  { key: "hidden", label: "Hidden", desc: "Noise: nothing a reviewer needs to see" },
];
const OPEN_BY_DEFAULT = new Set(["architecture", "interface", "behavior", "unranked"]);
// The groups of the Changes tab, by how much attention a unit needs.
const GROUPS = [
  {
    key: "read", label: "Read", desc: "Logic and anything that can hide a mistake",
    has: (u) => isRead(u), lines: (u) => u.changed_lines - foldedLines(u),
  },
  {
    key: "skim", label: "Skim", desc: "Repetitive or low-risk: the summary is enough",
    has: (u) => !isHidden(u) && u.attention === "skim", lines: (u) => u.changed_lines,
  },
  {
    key: "hidden", label: "Hidden", desc: "Noise, or nothing to review",
    has: (u) => isHidden(u), lines: (u) => u.changed_lines,
  },
];
const NOISE = {
  comment: "Only comments changed",
  formatting: "Only formatting changed",
  docstring: "Only docstrings changed",
  imports: "Only imports changed, and none adds or removes a dependency",
  "local-rename": "Only local names changed",
  "symbol-rename": "Private symbol renamed, body unchanged",
  move: "Private symbol moved, body unchanged",
  generated: "Generated file",
};
const NOISE_SHORT = {
  comment: "comments", formatting: "formatting", docstring: "docstrings", imports: "import order",
  "local-rename": "local renames", "symbol-rename": "private renames", move: "private moves", generated: "generated",
  hide: "nothing to review (Claude)",
};
const CATEGORY_TITLES = {
  signature: "Signatures", "model-field": "Model fields", route: "Routes", cli: "CLI",
  env: "Environment variables", dependency: "Dependencies", migration: "Migrations",
};
const FILTERS = [
  { key: "all", label: "All" },
  { key: "todo", label: "To review" },
  { key: "changed", label: "Changed since review" },
  { key: "flagged", label: "Flagged" },
];
const STATES = {
  new: { icon: "●", title: "Not reviewed yet" },
  changed: { icon: "◐", title: "Changed since you reviewed it" },
  seen: { icon: "✓", title: "Reviewed" },
};
const MAX_AUTO_OPEN = 25;
const GAP_KEEP = 3; // unchanged lines kept around a collapsed run
const GAP_MIN = 8; // shortest run of unchanged lines that gets collapsed
const KEY_CONTEXT = 2; // lines shown around a highlight in the key-lines view of files without a skeleton
const TABS = ["walkthrough", "architecture", "interfaces", "changes"];

let data = null;
const ui = {
  tab: "walkthrough",
  filter: "all",
  closedTiers: new Set(["detail", "hidden"]),
  open: new Set(),
  view: new Map(), // qualname -> "since" | "full"
  gaps: new Set(), // expanded runs of unchanged lines
  unfolded: new Set(), // folds shown as lines, by "qualname:start"
  focus: null,
  composer: null, // {anchor, mode: "comment" | "ask", text}: the open draft of a comment or question
  mode: "ask", // the composer's last mode
  editing: new Map(), // comment id -> text being edited
  followUp: new Map(), // question id -> follow-up being written
  expanded: new Set(), // resolved questions shown in full
  streams: new Map(), // question id -> {status, text, sources, notices}: a running answer
  drawer: null, // "questions" | "drafts": the open side panel
  // The review being submitted: its event and summary, and the preview
  // the server made of it, which posting must match.
  submit: { event: "COMMENT", body: "", includeOrphaned: false, preview: null, busy: false, error: null, posted: null },
};
const sockets = new Map(); // question id -> EventSource

// Helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") for (const [p, v] of Object.entries(value)) node.style.setProperty(p, v);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Text with `backticks` as nodes, the quoted parts as inline code. Code
// citing a file line, like `pkg/mod.py:42`, links to it.
function rich(text) {
  return String(text ?? "")
    .split(/`([^`\n]+)`/)
    .map((part, i) => (i % 2 ? citation(part) || el("code", { class: "inline", text: part }) : part))
    .filter((part) => part !== "");
}

const CITATION = /^([\w.\/-]+\.\w+):(\d+)(?:[-–](\d+))?$/;

// A link for `path:line`: to the unit showing that line when it is part
// of the PR, else to the file on GitHub at the PR's head.
function citation(text) {
  const match = text.match(CITATION);
  if (!match || !data) return null;
  const [, path, first] = match;
  const line = Number(first);
  const unit = data.units.find((u) => u.path === path && u.diff.some((row) => row[2] === line));
  const base = data.pr.url ? data.pr.url.replace(/\/pull\/\d+\/?$/, "") : null;
  const href = base ? `${base}/blob/${data.pr.head_sha}/${path}#L${line}${match[3] ? `-L${match[3]}` : ""}` : null;
  if (!unit && !href) return null;
  return el("a", {
    class: "cite",
    href: href || "#changes",
    target: unit ? null : "_blank",
    rel: "noopener",
    title: unit ? `Show line ${line} of ${unit.qualname}` : "Open on GitHub",
    onclick: unit ? (e) => { e.preventDefault(); revealUnit(unit.qualname); showLine(unit.qualname, line); } : null,
  }, el("code", { class: "inline", text }));
}

function loadPrefs() {
  try {
    const prefs = JSON.parse(localStorage.getItem("pr-sift:prefs") || "{}");
    if (FILTERS.some((f) => f.key === prefs.filter)) ui.filter = prefs.filter;
    if (Array.isArray(prefs.closedTiers)) ui.closedTiers = new Set(prefs.closedTiers);
  } catch {
    // Storage may be unavailable; defaults are fine.
  }
}

function savePrefs() {
  try {
    localStorage.setItem("pr-sift:prefs", JSON.stringify({ filter: ui.filter, closedTiers: [...ui.closedTiers] }));
  } catch {
    // Ignore.
  }
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "X-PR-Sift-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

let toastTimer;
function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), 3500);
}

const tierOf = (u) => (u.noise ? "hidden" : u.tier || "unranked");
// The Changes section a unit is listed in: its tier when it's to be read.
const sectionOf = (u) => (isHidden(u) ? "hidden" : u.attention === "skim" ? "skim" : u.tier || "unranked");
// Hidden by the noise rules, or by Claude as having nothing to review.
const isHidden = (u) => Boolean(u.noise) || u.attention === "hide";
const isRead = (u) => !isHidden(u) && u.attention !== "skim";
const tierInfo = (key) => TIERS.find((t) => t.key === key);
const visibleUnits = () => data.units.filter((u) => !isHidden(u));

function matchesFilter(u) {
  switch (ui.filter) {
    case "todo": return !isHidden(u) && u.state !== "seen";
    case "changed": return !isHidden(u) && u.state === "changed";
    case "flagged": return u.flagged;
    default: return true;
  }
}

function plural(n, word, suffix = "s") {
  return `${n} ${word}${n === 1 ? "" : suffix}`;
}

// Header, progress, banner

function renderHeader() {
  const { pr } = data;
  document.title = `#${pr.number} ${pr.title} · Review`;
  document.getElementById("repo").textContent = pr.repo;
  document.getElementById("number").textContent = `#${pr.number}`;
  document.getElementById("title").textContent = pr.title;
  const analyzed = new Date(pr.analyzed_at);
  document.getElementById("meta").replaceChildren(
    el("span", {}, "head ", el("code", { text: pr.head_sha.slice(0, 7) })),
    el("span", { text: `analyzed ${analyzed.toLocaleString()}`, title: pr.analyzed_at }),
    el("a", { href: pr.url, target: "_blank", rel: "noopener", text: "Open on GitHub ↗" }),
  );
  document.getElementById("count-interfaces").textContent = data.interfaces.length || "";
  document.getElementById("count-changes").textContent = visibleUnits().length || "";
}

const foldedLines = (u) => u.folds.reduce((n, f) => n + f.changed_lines, 0);

// Changed lines of the whole PR: to read (reviewed or left), to skim
// (skim units and folded lines), and hidden by the noise rules.
function lineStats() {
  const sum = (units, lines = (u) => u.changed_lines) => units.reduce((n, u) => n + lines(u), 0);
  const read = data.units.filter(isRead);
  const toRead = (u) => u.changed_lines - foldedLines(u);
  const reviewed = sum(read.filter((u) => u.state === "seen"), toRead);
  const left = sum(read.filter((u) => u.state !== "seen"), toRead);
  return {
    total: sum(data.units),
    hidden: sum(data.units.filter(isHidden)),
    skim: sum(data.units.filter((u) => !isHidden(u) && u.attention === "skim")) + sum(read, foldedLines),
    reviewed,
    left,
    read: reviewed + left,
  };
}

function percent(part, total) {
  return total ? Math.round((100 * part) / total) : 0;
}

function renderProgress() {
  const lines = lineStats();
  const units = visibleUnits();
  const seen = units.filter((u) => u.state === "seen").length;
  const width = (value) => `${(100 * value) / (lines.total || 1)}%`;
  document.getElementById("progress-fill").style.width = width(lines.reviewed);
  document.getElementById("progress-skim").style.width = width(lines.skim);
  document.getElementById("progress-hidden").style.width = width(lines.hidden);
  document.getElementById("progress-label").textContent =
    `${percent(lines.reviewed, lines.read)}% of ${lines.read.toLocaleString()} lines to read`;
  const n = (value) => value.toLocaleString();
  document.getElementById("progress").title =
    `Of ${n(lines.total)} changed lines: ${n(lines.read)} to read (${n(lines.reviewed)} reviewed, ` +
    `${n(lines.left)} left), ${n(lines.skim)} to skim, ${n(lines.hidden)} hidden (noise, or nothing to review). ` +
    `${seen} of ${units.length} units reviewed.`;
}

function renderBanner() {
  const banner = document.getElementById("banner");
  const units = visibleUnits();
  const fresh = units.filter((u) => u.state === "new").length;
  const changed = units.filter((u) => u.state === "changed").length;
  if (!data.has_reviews || fresh + changed === 0) {
    banner.hidden = true;
    return;
  }
  const parts = [];
  if (fresh) parts.push(`${fresh} new`);
  if (changed) parts.push(`${changed} changed`);
  banner.replaceChildren(
    el("strong", { text: `${parts.join(" and ")} since your last review.` }),
    el("button", {
      class: "button",
      text: "Show them",
      onclick: () => {
        setFilter("todo");
        selectTab("changes");
      },
    }),
  );
  banner.hidden = false;
}

// The report shows one analysis; warn when the PR has commits it doesn't
// cover. A failed check shows nothing, as the report itself still works.
async function renderHeadBanner() {
  let head;
  try {
    head = await api("GET", "/api/head");
  } catch {
    return;
  }
  const banner = document.getElementById("head-banner");
  banner.hidden = !head.head_moved;
  if (!head.head_moved) return;
  banner.replaceChildren(
    el("strong", { text: "This report is out of date." }),
    " ",
    el("span", { text: `The PR is at ${head.remote_head_sha.slice(0, 7)}, but the report was built from ${head.head_sha.slice(0, 7)}. Run ` }),
    el("code", { class: "inline", text: "/pr-sift" }),
    el("span", { text: " again to see the new commits; your review marks and drafts are kept." }),
  );
}

// Walkthrough: the story of the change, top-down, with code where needed

function renderWalkthrough() {
  const panel = document.getElementById("panel-walkthrough");
  const { summary } = data;
  const cards = [statsRow()];
  const stale = summary?.stale && el("p", { class: "notice", text: "This was written for an earlier head. Run /pr-sift again to refresh it." });

  cards.push(prThreads());
  if (summary?.walkthrough?.length) {
    cards.push(el("article", { class: "card story" }, stale, summary.walkthrough.map(storyBlockWithThreads)));
  } else if (summary) {
    cards.push(el("div", { class: "card" }, el("h2", { text: "Summary" }), el("p", { class: "lead" }, rich(summary.overview)), stale));
  } else {
    cards.push(
      el("div", { class: "card empty" },
        "This PR hasn't been ranked yet. Run ", el("code", { text: "/pr-sift" }), " in Claude Code to rank and summarize it.",
      ),
    );
  }

  if (summary?.components?.length) {
    cards.push(
      el("div", { class: "card" },
        el("h2", { text: "Behavior by component" }),
        el("div", { class: "components" },
          summary.components.map((c) =>
            el("div", {}, el("h3", {}, rich(c.name)), el("ul", {}, c.behavior.map((b) => el("li", {}, rich(b))))),
          ),
        ),
      ),
    );
  }
  cards.push(progressCard(data.units.filter(isHidden)));
  panel.replaceChildren(...cards);
  for (const diagram of panel.querySelectorAll(".story .diagram")) drawDiagram(diagram, "story-graph");
}

// A walkthrough block with its button to ask about it, and its threads.
function storyBlockWithThreads(block, index) {
  const node = storyBlock(block, index);
  if (!node) return null;
  const anchor = { kind: "walkthrough", block: index };
  return el("div", { class: "story-block", dataset: { block: String(index) } },
    node,
    data.agent.available && el("button", {
      class: "edge-ask",
      title: "Ask Claude about this part",
      text: "?",
      onclick: () => openComposer({ ...anchor, quote: blockQuote(block) }, "ask"),
    }),
    threadsAt(placeOf(anchor), "story"),
  );
}

function blockQuote(block) {
  if (block.type === "text") return block.text.length > 600 ? `${block.text.slice(0, 600)}…` : block.text;
  if (block.type === "code") return `Code of ${block.unit}${block.lines ? `, lines ${block.lines[0]}–${block.lines[1]}` : ""}`;
  if (block.type === "units") return block.title;
  return "The architecture diagram";
}

// Questions and general comments about the whole PR, with a box to ask one.
function prThreads() {
  const place = placeOf({ kind: "pr" });
  const composing = ui.composer && ui.composer.surface === "story" && placeOf(ui.composer.anchor) === place;
  return el("div", { class: "card pr-threads" },
    composing ? null : el("div", { class: "ask-bar" },
      el("button", {
        class: "ask-input",
        disabled: !data.agent.available,
        title: data.agent.available ? "Ask Claude about this PR (a)" : agentMissing(),
        onclick: () => openComposer({ kind: "pr" }, "ask"),
      }, "? Ask Claude about this PR…"),
      el("button", { class: "button", text: "💬 General comment", onclick: () => openComposer({ kind: "pr" }, "comment") }),
    ),
    threadsAt(place, "story"),
  );
}

function storyBlock(block, index) {
  switch (block.type) {
    case "text": return storyText(block.text);
    case "code": return storyCode(block, index);
    case "units": return storyUnits(block);
    case "architecture": {
      if (!Object.keys(data.architecture?.nodes || {}).length) return null;
      return el("figure", { class: "story-figure" }, el("div", { class: "diagram" }));
    }
    default: return null;
  }
}

function storyText(text) {
  return markdown(text, "story-text");
}

// Markdown, as walkthroughs and answers use it: blank-line paragraphs,
// "### " headings, "- " and "1. " lists, fenced code, **bold**, `code`
// and [links](https://...).
function markdown(text, className) {
  const nodes = [];
  const parts = String(text ?? "").split(/^```([\w+-]*)[^\n]*\n([\s\S]*?)^```[ \t]*$/m);
  for (let i = 0; i < parts.length; i += 3) {
    nodes.push(...blocks(parts[i]));
    if (i + 2 < parts.length) {
      const code = el("code");
      highlight(code, parts[i + 2].replace(/\n$/, ""), parts[i + 1] && window.hljs?.getLanguage(parts[i + 1]) ? parts[i + 1] : null);
      nodes.push(el("pre", { class: "md-code" }, code));
    }
  }
  return el("div", { class: className }, nodes);
}

function blocks(text) {
  const nodes = [];
  for (const paragraph of text.split(/\n\s*\n/)) {
    const lines = paragraph.split("\n").filter((line) => line.trim());
    if (!lines.length) continue;
    const item = (pattern) => lines.every((line) => pattern.test(line) || /^\s{2,}\S/.test(line)) && pattern.test(lines[0]);
    const items = (pattern) => {
      const result = [];
      for (const line of lines) {
        if (pattern.test(line)) result.push(line.replace(pattern, ""));
        else result[result.length - 1] += ` ${line.trim()}`;
      }
      return result.map((entry) => el("li", {}, inline(entry)));
    };
    if (item(/^\s*[-*] /)) {
      nodes.push(el("ul", {}, items(/^\s*[-*] /)));
    } else if (item(/^\s*\d+[.)] /)) {
      nodes.push(el("ol", {}, items(/^\s*\d+[.)] /)));
    } else if (lines.length === 1 && /^#{1,4} /.test(lines[0])) {
      nodes.push(el("h3", { class: "story-heading" }, inline(lines[0].replace(/^#+ /, ""))));
    } else {
      nodes.push(el("p", {}, inline(lines.join(" "))));
    }
  }
  return nodes;
}

// **bold**, `code` and [links](https://...) in a line of walkthrough text.
function inline(text) {
  return text.split(/(\[[^\]]+\]\(https?:\/\/[^\s)]+\))/).flatMap((chunk) => {
    const link = chunk.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
    if (link) return [el("a", { href: link[2], target: "_blank", rel: "noopener" }, rich(link[1]))];
    return chunk.split(/\*\*([^*]+)\*\*/).flatMap((part, i) => (i % 2 ? [el("strong", {}, rich(part))] : rich(part)));
  });
}

function unitLink(qualname, label = qualname) {
  return el("a", { href: "#changes", class: "unit-link", onclick: (e) => { e.preventDefault(); revealUnit(qualname); } }, label);
}

function shortName(u) {
  return u.module && u.qualname.startsWith(`${u.module}.`) ? u.qualname.slice(u.module.length + 1) : u.qualname;
}

function storyCode(block, index) {
  const u = data.units.find((x) => x.qualname === block.unit);
  if (!u) return el("div", { class: "story-code missing", text: `${block.unit} is no longer part of this PR.` });
  let rows = u.diff;
  let keyLines = u.key_lines;
  let sections = u.sections;
  if (block.lines) {
    const [first, last] = block.lines;
    const inRange = rows.map((row, i) => [i, row[0] === "-" ? row[1] : row[2]]).filter(([, line]) => line != null && line >= first && line <= last);
    if (inRange.length) rows = rows.slice(inRange[0][0], inRange[inRange.length - 1][0] + 1);
    keyLines = null;
    sections = sections.filter((section) => section.line >= first && section.line <= last);
  }
  const state = STATES[u.state];
  return el("figure", { class: "story-code" },
    el("figcaption", {},
      el("span", { class: `state ${u.state}`, title: state.title, text: state.icon }),
      unitLink(u.qualname, el("code", { class: "inline", text: shortName(u) })),
      block.lines && el("span", { class: "muted", text: `lines ${block.lines[0]}–${block.lines[1]}` }),
      block.stale && el("span", { class: "chip changed", text: "changed since this was written" }),
    ),
    renderDiff(rows, u.language, `story:${index}`, u.folds, u.highlights, keyLines, sections, u.qualname),
  );
}

function storyUnits(block) {
  return el("div", { class: "story-units" },
    el("div", { class: "story-units-title" }, inline(block.title)),
    el("ul", {}, block.units.map((name, i) => {
      const u = data.units.find((x) => x.qualname === name);
      return el("li", {},
        u ? unitLink(name, el("code", { class: "inline", text: shortName(u) })) : el("code", { class: "inline", text: name }),
        u?.summary && el("span", { class: "muted" }, " — ", rich(u.summary)),
        block.stale?.[i] && el("span", { class: "chip changed", text: "changed" }),
      );
    })),
  );
}

function statsRow() {
  const units = visibleUnits();
  const stats = TIERS.filter((t) => t.key !== "hidden").map((tier) => {
    const count = units.filter((u) => sectionOf(u) === tier.key).length;
    if (tier.key === "unranked" && !count) return null;
    return el("button", { class: "stat", onclick: () => revealTier(tier.key) },
      el("div", { class: "value", text: count }),
      el("div", { class: "label" }, el("span", { class: "dot", style: { background: `var(--tier-${tier.key})` } }), tier.label),
    );
  });
  const hidden = data.units.filter(isHidden);
  stats.push(
    el("button", { class: "stat", onclick: () => revealTier("hidden") },
      el("div", { class: "value", text: hidden.length }),
      el("div", { class: "label" }, el("span", { class: "dot", style: { background: "var(--tier-hidden)" } }), "Hidden"),
    ),
  );
  return el("div", { class: "stats compact" }, stats);
}

function progressCard(hidden) {
  const lines = lineStats();
  const n = (value) => value.toLocaleString();
  const reasons = {};
  for (const u of hidden) {
    const reason = u.noise || "hide";
    reasons[reason] ??= { units: 0, lines: 0 };
    reasons[reason].units++;
    reasons[reason].lines += u.changed_lines;
  }
  const meter = (className, value) =>
    el("div", { class: `meter-part ${className}`, style: { width: `${(100 * value) / (lines.total || 1)}%` } });
  return el("div", { class: "card" },
    el("h2", { text: "Changed lines" }),
    el("div", { class: "meter" }, meter("reviewed", lines.reviewed), meter("skim", lines.skim), meter("hidden", lines.hidden)),
    el("div", { class: "meter-legend" },
      el("span", {}, el("i", { class: "dot", style: { background: "var(--state-seen)" } }),
        el("b", { text: `${percent(lines.reviewed, lines.total)}%` }), ` reviewed (${n(lines.reviewed)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--border-strong)" } }),
        el("b", { text: `${percent(lines.left, lines.total)}%` }), ` left to read (${n(lines.left)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--tier-skim)" } }),
        el("b", { text: `${percent(lines.skim, lines.total)}%` }), ` to skim: folded or summarized (${n(lines.skim)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--tier-hidden)" } }),
        el("b", { text: `${percent(lines.hidden, lines.total)}%` }), ` hidden: noise or nothing to review (${n(lines.hidden)} lines)`),
      el("span", { class: "muted", text: `of ${n(lines.total)} changed lines` }),
    ),
    hidden.length > 0 && el("div", { class: "hidden-breakdown" },
      Object.entries(reasons)
        .sort((a, b) => b[1].lines - a[1].lines)
        .map(([reason, r]) =>
          el("span", { class: "chip", text: `${NOISE_SHORT[reason] || reason}: ${plural(r.units, "unit")}, ${plural(r.lines, "line")}` }),
        ),
    ),
  );
}

// Architecture

async function renderArchitecture() {
  const panel = document.getElementById("panel-architecture");
  const arch = data.architecture;
  const cards = [];
  if (data.summary?.architecture_summary) {
    cards.push(el("div", { class: "card" }, el("h2", { text: "Summary" }), el("p", { class: "lead" }, rich(data.summary.architecture_summary))));
  }

  const diagram = el("div", { class: "diagram" });
  const nodes = Object.keys(arch?.nodes || {});
  if (!nodes.length) {
    cards.push(el("div", { class: "card empty", text: "No change to how modules depend on each other." }));
  } else {
    cards.push(
      el("div", { class: "card" },
        el("h2", { text: "Import graph" }),
        diagram,
        el("div", { class: "legend" },
          el("span", {}, el("i", { class: "swatch", style: { "border-color": "#1a7f37" } }), "added"),
          el("span", {}, el("i", { class: "swatch", style: { "border-color": "#cf222e", "border-style": "dashed" } }), "removed"),
          el("span", {}, el("i", { class: "swatch", style: { "border-color": "#9a6700" } }), "changed"),
          el("span", {}, el("i", { class: "line-swatch", style: { "border-color": "#1a7f37" } }), "new import"),
          el("span", {}, el("i", { class: "line-swatch", style: { "border-color": "#cf222e", "border-top-style": "dashed" } }), "removed import"),
          el("span", { text: `Modules grouped to depth ${arch.depth}` }),
        ),
      ),
    );
  }

  const added = arch?.external_added || [];
  const removed = arch?.external_removed || [];
  if (added.length || removed.length) {
    cards.push(
      el("div", { class: "card" },
        el("h2", { text: "Third-party packages" }),
        el("div", { class: "packages" },
          added.map((p) => el("code", { class: "chip added", text: `+ ${p}` })),
          removed.map((p) => el("code", { class: "chip removed", text: `− ${p}` })),
        ),
      ),
    );
  }
  panel.replaceChildren(...cards);
  if (nodes.length) await drawDiagram(diagram, "arch-graph");
}

async function drawDiagram(container, id) {
  const text = data.architecture?.mermaid;
  if (!text || !window.mermaid) return;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "neutral" });
  try {
    const { svg } = await window.mermaid.render(id, text);
    container.innerHTML = svg;
  } catch (error) {
    container.replaceChildren(el("pre", { text }));
  }
}

// Interfaces

function renderInterfaces() {
  const panel = document.getElementById("panel-interfaces");
  if (!data.interfaces.length) {
    panel.replaceChildren(el("div", { class: "card empty", text: "No interface changes detected." }));
    return;
  }
  const units = new Map(data.units.map((u) => [u.qualname, u]));
  const groups = Object.keys(CATEGORY_TITLES)
    .map((category) => [category, data.interfaces.filter((c) => c.category === category)])
    .filter(([, items]) => items.length);
  panel.replaceChildren(
    ...groups.map(([category, items]) =>
      el("div", { class: "card iface-group" },
        el("h2", { text: `${CATEGORY_TITLES[category]} (${items.length})` }),
        items.map((change) => {
          const unit = change.qualnames.map((q) => units.get(q)).find(Boolean)
            || data.units.find((u) => !change.qualnames.length && u.path === change.path);
          const name = unit
            ? el("a", { href: "#changes", text: change.name, onclick: (e) => { e.preventDefault(); revealUnit(unit.qualname); } })
            : change.name;
          return el("div", { class: "iface" },
            el("span", {}, el("span", { class: `chip ${change.status}`, text: change.status })),
            el("div", { class: "name" }, name),
            interfaceDetail(change, unit),
            el("div", { class: "values" }, interfaceValues(change.before, change.after)),
          );
        }),
      ),
    ),
  );
}

// One line on what the changed interface is: a field's comment, the
// docstring, else what Claude says it does, else the summary of its
// unit's change. A model field without a comment shows nothing, since
// its class's line already shows on the class's signature.
function interfaceDetail(change, unit) {
  if (change.doc) return el("p", { class: "iface-doc" }, el("span", { class: "doc-source", text: "comment" }), rich(change.doc));
  if (!unit || change.category === "model-field") return null;
  const [source, text] = unit.docstring
    ? ["docstring", unit.docstring]
    : unit.description
      ? ["what it does", unit.description]
      : unit.summary
        ? ["change", unit.summary]
        : [null, null];
  if (!text) return null;
  return el("p", { class: "iface-doc" }, el("span", { class: "doc-source", text: source }), rich(text));
}

const MAX_WORD_DIFF_TOKENS = 600;

function tokens(text) {
  return text.split(/(\s+|[()[\]{},:=|*])/).filter(Boolean);
}

// Mark the tokens of a and b that are not in their longest common subsequence.
function wordDiff(a, b) {
  const n = a.length, m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const keepA = new Array(n).fill(false), keepB = new Array(m).fill(false);
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { keepA[i++] = true; keepB[j++] = true; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) i++;
    else j++;
  }
  return [keepA, keepB];
}

function markedValue(className, parts, keep) {
  const node = el("div", { class: `value ${className}` });
  parts.forEach((part, k) => node.append(keep[k] ? document.createTextNode(part) : el("mark", { text: part })));
  return node;
}

function interfaceValues(before, after) {
  if (!before || !after) {
    return [
      before && el("div", { class: "value before", text: before }),
      after && el("div", { class: "value after", text: after }),
    ];
  }
  const a = tokens(before), b = tokens(after);
  if (a.length + b.length > MAX_WORD_DIFF_TOKENS) {
    return [el("div", { class: "value before", text: before }), el("div", { class: "value after", text: after })];
  }
  const [keepA, keepB] = wordDiff(a, b);
  return [markedValue("before", a, keepA), markedValue("after", b, keepB)];
}

// Changes

function renderChanges() {
  const panel = document.getElementById("panel-changes");
  const counts = {
    all: data.units.length,
    todo: data.units.filter((u) => !isHidden(u) && u.state !== "seen").length,
    changed: data.units.filter((u) => !isHidden(u) && u.state === "changed").length,
    flagged: data.units.filter((u) => u.flagged).length,
  };
  const toolbar = el("div", { class: "toolbar" },
    el("div", { class: "segmented", role: "group", "aria-label": "Filter units" },
      FILTERS.map((f) =>
        el("button", {
          "aria-pressed": String(ui.filter === f.key),
          onclick: () => setFilter(f.key),
        }, `${f.label} `, el("span", { class: "count", text: counts[f.key] })),
      ),
    ),
    el("div", { class: "group" },
      el("button", { class: "button", text: "Expand all", onclick: () => setAllOpen(true) }),
      el("button", { class: "button", text: "Collapse all", onclick: () => setAllOpen(false) }),
    ),
  );

  const groups = [];
  for (const group of GROUPS) {
    if (group.key === "hidden" && ui.filter !== "all") continue;
    const members = data.units.filter((u) => group.has(u));
    const shown = members.filter(matchesFilter);
    if (!members.length) continue;
    const lines = members.reduce((n, u) => n + group.lines(u), 0);
    const sections = group.key === "read"
      ? TIERS.filter((t) => !["skim", "hidden"].includes(t.key)).map((tier) => {
          const units = shown.filter((u) => sectionOf(u) === tier.key).sort(readOrder);
          return units.length ? tierSection(tier, units) : null;
        })
      : [el("div", { class: "tier list" }, el("div", { class: "tier-body" }, shown.map(unitCard)))];
    const open = !ui.closedTiers.has(group.key);
    groups.push(
      el("section", { class: `group group-${group.key}`, id: `group-${group.key}`, dataset: { open: String(open) } },
        el("button", { class: "group-head", "aria-expanded": String(open), onclick: () => toggleTier(group.key) },
          el("span", { class: "caret", text: "▾" }),
          el("span", { class: "name", text: group.label }),
          el("span", { class: "group-stats", text: `${plural(members.length, "unit")} · ${plural(lines, "line")}` }),
          el("span", { class: "desc", text: group.desc }),
        ),
        open && el("div", { class: "group-body" },
          shown.length ? sections : el("div", { class: "card empty", text: "Nothing here for this filter." }),
        ),
      ),
    );
  }
  panel.replaceChildren(toolbar, ...(groups.length ? groups : [el("div", { class: "card empty", text: "Nothing here." })]));
  if (ui.focus) setFocus(ui.focus, false);
}

// Within a tier: units with highlights first, then the bigger ones.
function readOrder(a, b) {
  return b.highlights.length - a.highlights.length || b.changed_lines - a.changed_lines;
}

function tierSection(tier, units) {
  const open = !ui.closedTiers.has(tier.key);
  return el("section", { class: "tier", id: `tier-${tier.key}`, dataset: { open: String(open) } },
    el("button", { class: "tier-head", "aria-expanded": String(open), onclick: () => toggleTier(tier.key) },
      el("span", { class: "caret", text: "▾" }),
      el("span", { class: "name", style: { color: `var(--tier-${tier.key})` }, text: tier.label }),
      el("span", { class: "count", text: units.length }),
      el("span", { class: "desc", text: tier.desc }),
    ),
    el("div", { class: "tier-body" }, open ? units.map(unitCard) : []),
  );
}

function unitCard(u) {
  const open = ui.open.has(u.qualname);
  const tier = sectionOf(u);
  const compact = !isRead(u);
  const state = STATES[u.state];
  const card = el("article", {
    class: `unit ${u.state === "seen" && !isHidden(u) ? "seen" : ""} ${compact ? "compact" : ""}`,
    id: `unit-${u.qualname}`,
    tabindex: "-1",
    dataset: { qualname: u.qualname, open: String(open) },
    style: { "--tier-color": `var(--tier-${tier})` },
  });

  let symbol = u.qualname;
  let module = null;
  if (u.module && u.qualname.startsWith(`${u.module}.`)) {
    module = `${u.module}.`;
    symbol = u.qualname.slice(module.length);
  }
  const chips = [el("span", { class: "chip", text: u.kind })];
  if (u.status !== "modified") chips.push(el("span", { class: `chip ${u.status === "removed" ? "removed" : u.status === "added" ? "added" : "changed"}`, text: u.status }));
  if (!compact) for (const signal of u.signals) chips.push(el("span", { class: "chip signal", text: signal }));
  if (u.highlights.length) chips.push(el("span", { class: "chip highlight", title: "Lines worth a look", text: `⚠ ${u.highlights.length}` }));
  if (u.folds.length && !compact) chips.push(el("span", { class: "chip", title: "Repetitive lines shown as tables", text: `▤ ${plural(foldedLines(u) || u.folds.length, foldedLines(u) ? "line" : "table")} folded` }));
  if (u.flagged) chips.push(el("span", { class: "chip removed", text: "⚑ flagged" }));
  chips.push(...threadChips(u));

  const toggle = (e) => { if (!e.target.closest("button")) toggleUnit(u.qualname); };
  const talk = [
    el("button", {
      class: "icon-button ask",
      title: data.agent.available ? "Ask Claude about this unit (a)" : agentMissing(),
      disabled: !data.agent.available,
      text: "?",
      onclick: () => openComposer({ kind: "unit", qualname: u.qualname }, "ask"),
    }),
    el("button", {
      class: "icon-button",
      title: "Comment on this unit (c)",
      text: "💬",
      onclick: () => openComposer({ kind: "unit", qualname: u.qualname }, "comment"),
    }),
  ];
  const actions = isHidden(u)
    ? el("div", { class: "unit-actions" }, talk)
    : el("div", { class: "unit-actions" },
        talk,
        el("button", {
          class: `icon-button flag ${u.flagged ? "on" : ""}`,
          title: u.flagged ? "Remove flag (f)" : "Flag to come back to (f)",
          "aria-pressed": String(u.flagged),
          text: "⚑",
          onclick: () => updateReview(u, { flagged: !u.flagged }),
        }),
        el("button", {
          class: `icon-button review ${u.state === "seen" ? "on" : ""}`,
          title: "Toggle reviewed (r)",
          "aria-pressed": String(u.state === "seen"),
          text: u.state === "seen" ? "✓" : compact ? "Reviewed?" : "Mark reviewed",
          onclick: () => updateReview(u, { reviewed: u.state !== "seen" }, true),
        }),
      );
  if (compact) {
    // One line: name, summary, and the reason to skim on hover.
    const summary = u.noise ? NOISE[u.noise] || u.noise : u.summary;
    const why = { skim: "Why skim", hide: "Why hidden" }[u.attention];
    card.append(
      el("div", { class: "unit-head", onclick: toggle, title: why && u.reason && !u.noise ? `${why}: ${u.reason}` : null },
        el("span", { class: `state ${isHidden(u) ? "" : u.state}`, title: isHidden(u) ? "Hidden" : state.title, text: isHidden(u) ? "·" : state.icon }),
        el("div", { class: "unit-line" },
          el("span", { class: "qualname" }, el("span", { class: "symbol", text: symbol })),
          u.highlights.length > 0 && el("span", { class: "chip highlight", text: `⚠ ${u.highlights.length}` }),
          u.flagged && el("span", { class: "chip removed", text: "⚑" }),
          threadChips(u),
          el("span", { class: "line-summary" }, summary ? rich(summary) : ""),
        ),
        actions,
      ),
    );
    // Highlights stay visible on a skimmed unit: they're the reason to look.
    if (u.highlights.length && !open) {
      card.append(el("div", { class: "compact-highlights" },
        u.highlights.map((h) =>
          el("button", { class: "callout", onclick: () => showLine(u.qualname, h.line) },
            el("span", { class: "callout-line", text: `⚠ line ${h.line}` }), el("span", {}, rich(h.note)),
          ),
        ),
      ));
    }
    if (open) card.append(unitBody(u));
    return card;
  }

  const head = el("div", { class: "unit-head", onclick: toggle },
    el("span", { class: `state ${isHidden(u) ? "" : u.state}`, title: isHidden(u) ? "Hidden" : state.title, text: isHidden(u) ? "·" : state.icon }),
    el("div", { class: "unit-title" },
      el("span", { class: "qualname" }, module && el("span", { class: "module", text: module }), el("span", { class: "symbol", text: symbol })),
      chips,
    ),
    actions,
    el("p", { class: `unit-summary ${u.summary ? "" : "none"}` }, u.summary ? rich(u.summary) : "Not ranked yet."),
  );
  card.append(head);
  if (open) card.append(unitBody(u));
  return card;
}

function unitBody(u) {
  const body = el("div", { class: "unit-body" });
  if (u.reason) body.append(el("p", { class: "reason" }, el("b", { text: u.attention === "skim" ? "Why skim: " : u.attention === "hide" ? "Why hidden: " : `Why ${tierOf(u)}: ` }), rich(u.reason)));
  if (u.highlights.length) body.append(callouts(u));
  const threads = threadsAt(placeOf({ kind: "unit", qualname: u.qualname }));
  if (threads) body.append(threads);
  if (u.old_qualname || u.old_path) {
    body.append(el("p", { class: "moved", text: `${u.status === "renamed" ? "Renamed" : "Moved"} from ${u.old_qualname || u.qualname}${u.old_path ? ` (${u.old_path})` : ""}` }));
  }
  // Key lines by default: the highlights with a little context. A
  // changed unit starts with what changed since you reviewed it.
  const view = ui.view.get(u.qualname) || (u.since_review ? "since" : "key");
  const views = [...(u.since_review ? [["since", "Since your review"]] : []), ["key", "Key lines"], ["full", "Full diff"]];
  body.append(
    el("div", { class: "segmented", role: "group", "aria-label": "Diff view" },
      views.map(([key, label]) =>
        el("button", {
          "aria-pressed": String(view === key),
          text: label,
          onclick: () => { ui.view.set(u.qualname, key); rerenderUnit(u.qualname); },
        }),
      ),
    ),
  );
  // Lines removed since the review have no number at head, so only the
  // current lines can be commented on.
  if (view === "since") body.append(renderDiff(u.since_review, u.language, u.qualname, [], u.highlights, null, []));
  else body.append(renderDiff(u.diff, u.language, u.qualname, u.folds, u.highlights, view === "key" ? u.key_lines : null, u.sections, u.qualname));
  return body;
}

// Open a unit if needed and scroll to one of its lines.
function showLine(qualname, line) {
  if (!ui.open.has(qualname)) toggleUnit(qualname, true);
  const target = document.getElementById(`line-${qualname}-${line}`);
  if (!target) return;
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  target.classList.add("flash");
  setTimeout(() => target.classList.remove("flash"), 1200);
}

function callouts(u) {
  return el("div", { class: "callouts" },
    el("div", { class: "callouts-title", text: "Worth a look" }),
    u.highlights.map((h) =>
      el("button", { class: "callout", onclick: () => showLine(u.qualname, h.line) },
        el("span", { class: "callout-line", text: `line ${h.line}` }), el("span", {}, rich(h.note)),
      ),
    ),
  );
}

function highlight(code, text, language) {
  if (language && window.hljs) {
    try {
      code.innerHTML = window.hljs.highlight(text, { language, ignoreIllegals: true }).value;
      return;
    } catch {
      // Fall back to plain text.
    }
  }
  code.textContent = text;
}

// The diff as tables of lines, with folds as blocks between them. With
// keyLines ({new, old} skeleton lines), only those, highlighted lines
// with KEY_CONTEXT lines around them, folds holding a highlight and
// lines with comments or questions are shown; the rest collapses into
// bars. With qualname, lines can be commented on and asked about, and
// their threads show under them.
function renderDiff(rows, language, key, folds = [], highlights = [], keyLines = null, sections = [], qualname = key) {
  const wrap = el("div", { class: "diff-wrap", dataset: qualname ? { qualname } : {} });
  let tbody = el("tbody");
  const flush = () => {
    if (tbody.children.length) wrap.append(el("div", { class: "diff-scroll" }, el("table", { class: "diff" }, tbody)));
    tbody = el("tbody");
  };
  const kinds = { " ": "ctx", "-": "del", "+": "add" };
  // Highlights by side and line: a removed and an added line can share a number.
  const notes = new Map(highlights.map((h) => [`${h.side || "new"}:${h.line}`, h.note]));
  const noteKey = (row) => (row[0] === "-" ? `old:${row[1]}` : `new:${row[2]}`);
  const foldAt = new Map(folds.filter((f) => !ui.unfolded.has(`${key}:${f.start}`)).map((f) => [f.start, f]));
  const lineOf = (row) => (row[0] === "-" ? row[1] : row[2]);
  // Titled blocks Claude split a long unit into; a title shows before the
  // block's first line, and that line always shows.
  const titles = new Map(sections.map((s, i) => [s.line, `${i + 1}. ${s.title}`]));
  const title = (row) => {
    const text = titles.get(lineOf(row));
    if (!text) return;
    titles.delete(lineOf(row));
    flush();
    wrap.append(el("div", { class: "diff-section" }, rich(text)));
  };
  // Threads by the line they end on.
  const threadLines = qualname ? linesWithThreads(qualname) : new Set();
  const surface = key.startsWith("story:") ? "story" : "changes";
  const threadsFor = (row) => (qualname && threadLines.has(noteKey(row)) ? threadsAt(`${qualname}|${noteKey(row)}`, surface) : null);
  const emit = (row) => {
    title(row);
    tbody.append(diffRow(row, kinds, language, key, notes, qualname));
    const note = notes.get(noteKey(row));
    if (note) tbody.append(el("tr", { class: "hl-note" }, el("td", { colspan: "3" }, "⚠ ", rich(note))));
    const threads = threadsFor(row);
    if (threads) tbody.append(el("tr", { class: "thread-row" }, el("td", { colspan: "3" }, threads)));
  };
  const renderRange = (from, to, collapse = true) => {
    let i = from;
    while (i < to) {
      const fold = rows[i][2] !== null && foldAt.get(rows[i][2]);
      if (fold) {
        title(rows[i]);
        flush();
        wrap.append(foldView(fold, key, notes));
        const start = i;
        while (i < to && rows[i][2] !== null && rows[i][2] <= fold.end) i++;
        // Threads on folded lines show under the fold.
        for (const row of rows.slice(start, i)) {
          const threads = threadsFor(row);
          if (threads) wrap.append(el("div", { class: "fold-threads" }, threads));
        }
        continue;
      }
      if (rows[i][0] === " ") {
        let end = i;
        while (end < to && rows[end][0] === " " && !foldAt.has(rows[end][2])) end++;
        const gapKey = `${key}:${i}`;
        const highlighted = rows.slice(i, end).some((row) => notes.has(noteKey(row)) || titles.has(lineOf(row)) || threadLines.has(noteKey(row)));
        if (collapse && end - i >= GAP_MIN && !ui.gaps.has(gapKey) && !highlighted) {
          const keepStart = i === from ? 0 : GAP_KEEP;
          const keepEnd = end === to || foldAt.has(rows[end]?.[2]) ? 0 : GAP_KEEP;
          for (let k = i; k < i + keepStart; k++) emit(rows[k]);
          tbody.append(gapRow(`⋯ ${plural(end - i - keepStart - keepEnd, "unchanged line")}`, gapKey));
          for (let k = end - keepEnd; k < end; k++) emit(rows[k]);
          i = end;
          continue;
        }
      }
      emit(rows[i]);
      i++;
    }
  };
  const gapRow = (text, gapKey) =>
    el("tr", { class: "gap", onclick: () => { ui.gaps.add(gapKey); refresh(key); } }, el("td", { colspan: "3", text }));

  if (!keyLines) {
    renderRange(0, rows.length);
    flush();
    return wrap;
  }
  const keyNew = new Set(keyLines.new);
  const keyOld = new Set(keyLines.old);
  const shown = new Set();
  rows.forEach((row, i) => {
    if (row[0] === "-" ? keyOld.has(row[1]) : keyNew.has(row[2])) shown.add(i);
    if (titles.has(lineOf(row)) || threadLines.has(noteKey(row))) shown.add(i);
  });
  // Code with a skeleton needs no context around highlights; other files do.
  const context = keyNew.size || keyOld.size ? 0 : KEY_CONTEXT;
  rows.forEach((row, i) => {
    if (notes.has(noteKey(row))) for (let k = i - context; k <= i + context; k++) shown.add(k);
  });
  for (const fold of folds) {
    if (!highlights.some((h) => h.line >= fold.start && h.line <= fold.end)) continue;
    rows.forEach((row, i) => { if (row[2] !== null && row[2] >= fold.start && row[2] <= fold.end) shown.add(i); });
  }
  let i = 0;
  while (i < rows.length) {
    const visible = shown.has(i);
    let end = i;
    while (end < rows.length && shown.has(end) === visible) end++;
    const gapKey = `${key}:key${i}`;
    if (visible || ui.gaps.has(gapKey)) {
      // Key lines were picked on purpose: don't collapse unchanged ones.
      renderRange(i, end, !visible);
    } else {
      const added = rows.slice(i, end).filter((r) => r[0] === "+").length;
      const removed = rows.slice(i, end).filter((r) => r[0] === "-").length;
      const changes = [added && `+${added}`, removed && `−${removed}`].filter(Boolean).join(" ");
      const row = gapRow(`⋯ ${plural(end - i, "line")}${changes ? ` (${changes})` : ""}`, gapKey);
      row.classList.add("key-gap");
      tbody.append(row);
    }
    i = end;
  }
  flush();
  return wrap;
}

function diffRow(row, kinds, language, key, notes, qualname) {
  const [kind, oldLine, newLine, text] = row;
  const line = kind === "-" ? oldLine : newLine;
  const side = kind === "-" ? "old" : "new";
  const code = el("code");
  highlight(code, text, language);
  const noted = notes.has(`${side}:${line}`);
  // Click to comment or ask about the line; shift-click a second line
  // for a range.
  const add = qualname && line != null && el("button", {
    class: "line-add",
    title: "Comment or ask about this line (shift-click another line for a range)",
    "aria-label": `Comment or ask about line ${line}`,
    text: "+",
    onclick: (e) => lineClicked(qualname, side, line, e.shiftKey, surfaceOf(e.target)),
  });
  const selected = qualname && line != null && inComposerRange(qualname, side, line);
  return el("tr", {
    class: `${kinds[kind]} ${noted ? "hl" : ""} ${selected ? "selected" : ""}`,
    id: line != null ? `line-${key}-${line}` : null,
    dataset: line != null ? { side, line: String(line) } : {},
  },
    el("td", { class: "ln", text: oldLine ?? "" }),
    el("td", { class: "ln", text: newLine ?? "" }),
    el("td", { class: "code" }, add, el("span", { class: "sign", text: kind === " " ? "" : kind === "-" ? "−" : "+" }), code),
  );
}

// A fold: repetitive lines shown as a table, or as a list of values.
function foldView(fold, key, notes) {
  const lines = fold.end - fold.start + 1;
  // The highlighted line inside a row, if any.
  const rowLine = (row) => {
    if (row.start == null) return null;
    for (let line = row.start; line <= row.end; line++) if (notes.has(`new:${line}`)) return line;
    return null;
  };
  const rowNote = (row) => notes.get(`new:${rowLine(row)}`) ?? null;
  const head = el("div", { class: "fold-head" },
    el("span", { class: "fold-icon", text: "▤" }),
    fold.title && el("code", { class: "inline", text: fold.title }),
    el("span", { class: "fold-summary", text: fold.summary }),
    el("span", { class: "muted", text: `· ${plural(lines, "line")} folded` }),
    el("button", { class: "button small", text: "Show lines", onclick: () => { ui.unfolded.add(`${key}:${fold.start}`); refresh(key); } }),
  );
  if (fold.columns.length === 1 && fold.columns[0] === "value" && !fold.rows.some((r) => r.comment || rowNote(r))) {
    return el("div", { class: "fold" }, head,
      el("div", { class: "fold-values" }, fold.rows.map((r) => el("code", { class: "inline", title: r.cells.value, text: r.cells.value }))),
    );
  }
  const notesColumn = fold.rows.some((r) => r.comment || rowNote(r));
  return el("div", { class: "fold" }, head,
    el("div", { class: "fold-table-wrap" },
      el("table", { class: "fold-table" },
        el("thead", {}, el("tr", {},
          el("th", { class: "ln", text: "line" }),
          fold.columns.map((c) => el("th", { text: c })),
          notesColumn && el("th", { text: "note" }),
        )),
        el("tbody", {}, fold.rows.map((row) => {
          const note = rowNote(row);
          const line = rowLine(row) ?? row.start;
          return el("tr", { class: note ? "hl" : "", id: line != null ? `line-${key}-${line}` : null },
            el("td", { class: "ln", text: row.start ?? "" }),
            fold.columns.map((c) => el("td", { title: row.cells[c] ?? "" }, el("span", { class: "cell", text: row.cells[c] ?? "" }))),
            notesColumn && el("td", { class: "fold-note" },
              note && el("span", { class: "hl-text" }, "⚠ ", rich(note)),
              row.comment && el("span", { class: "muted", text: row.comment }),
            ),
          );
        })),
      ),
    ),
  );
}

// Comments and questions
//
// Both attach to an anchor: a line or range of a unit's diff, a unit, a
// walkthrough block or the whole PR. A thread shows under its anchor; a
// "place" names that spot: "qualname|side:line" for the line a thread
// ends on, "qualname|unit", "wt|block" or "pr".

const agentMissing = () => `${data.agent.command} isn't on PATH, so questions can't be asked`;

function placeOf(anchor) {
  if (anchor.state === "orphaned") return "orphaned";
  switch (anchor.kind) {
    case "line":
    case "range":
      return `${anchor.qualname}|${anchor.end_side || anchor.side}:${anchor.end_line ?? anchor.line}`;
    case "unit": return `${anchor.qualname}|unit`;
    case "walkthrough": return `wt|${anchor.block}`;
    default: return "pr";
  }
}

// A resolved anchor as the page sends it back to the server.
function anchorInput(anchor) {
  const input = { kind: anchor.kind, quote: anchor.quote || undefined };
  if (anchor.qualname) input.qualname = anchor.qualname;
  if (anchor.kind === "line" || anchor.kind === "range") Object.assign(input, { side: anchor.side, line: anchor.line });
  if (anchor.kind === "range") Object.assign(input, { end_side: anchor.end_side, end_line: anchor.end_line });
  if (anchor.kind === "walkthrough") input.block = anchor.block;
  return input;
}

function anchorLabel(anchor, withUnit = false) {
  const unit = anchor.qualname && data.units.find((u) => u.qualname === anchor.qualname);
  const name = unit ? shortName(unit) : anchor.qualname;
  const removed = anchor.side === "old" ? " (removed)" : "";
  let where;
  switch (anchor.kind) {
    case "line": where = `line ${anchor.line}${removed}`; break;
    case "range": where = `lines ${anchor.line}–${anchor.end_line}${removed}`; break;
    case "unit": return name || "a unit";
    case "walkthrough": return `the walkthrough, block ${anchor.block + 1}`;
    default: return "the whole PR";
  }
  return withUnit && name ? `${name} · ${where}` : where;
}

function itemsAt(place) {
  const items = [
    ...data.questions.filter((q) => placeOf(q.anchor) === place).map((q) => ({ question: q, at: q.created_at })),
    ...data.comments.filter((c) => placeOf(c.anchor) === place).map((c) => ({ comment: c, at: c.created_at })),
  ];
  return items.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
}

// The threads at a place, and the composer when it was opened there on
// this surface ("story" for the walkthrough, "changes" for unit cards).
function threadsAt(place, surface = "changes") {
  const items = itemsAt(place);
  const composing = ui.composer && ui.composer.surface === surface && placeOf(ui.composer.anchor) === place;
  if (!items.length && !composing) return null;
  return el("div", { class: "threads" },
    items.map((item) => (item.question ? questionCard(item.question) : commentCard(item.comment))),
    composing && composerView(),
  );
}

// The "side:line" keys of a unit's lines that threads end on.
function linesWithThreads(qualname) {
  const keys = new Set();
  const add = (anchor) => {
    if (anchor.qualname !== qualname || !["line", "range"].includes(anchor.kind) || anchor.state === "orphaned") return;
    keys.add(placeOf(anchor).slice(qualname.length + 1));
  };
  for (const q of data.questions) add(q.anchor);
  for (const c of data.comments) add(c.anchor);
  if (ui.composer) add(ui.composer.anchor);
  return keys;
}

function threadChips(u) {
  const questions = data.questions.filter((q) => q.anchor.qualname === u.qualname).length;
  const drafts = data.comments.filter((c) => c.anchor.qualname === u.qualname).length;
  return [
    questions > 0 && el("span", { class: "chip ask", title: plural(questions, "question"), text: `? ${questions}` }),
    drafts > 0 && el("span", { class: "chip draft", title: plural(drafts, "draft comment"), text: `💬 ${drafts}` }),
  ].filter(Boolean);
}

// Index of a line in a unit's diff rows; unchanged lines are on the new side.
function rowIndex(u, side, line) {
  return u.diff.findIndex((row) => (side === "old" ? row[0] === "-" && row[1] === line : row[0] !== "-" && row[2] === line));
}

function inComposerRange(qualname, side, line) {
  const anchor = ui.composer?.anchor;
  if (!anchor || anchor.qualname !== qualname || !["line", "range"].includes(anchor.kind)) return false;
  const u = data.units.find((x) => x.qualname === qualname);
  const i = rowIndex(u, side, line);
  const a = rowIndex(u, anchor.side, anchor.line);
  const b = anchor.kind === "range" ? rowIndex(u, anchor.end_side, anchor.end_line) : a;
  return i >= Math.min(a, b) && i <= Math.max(a, b);
}

function surfaceOf(node) {
  return node?.closest?.("#panel-walkthrough") ? "story" : "changes";
}

function lineClicked(qualname, side, line, extend, surface) {
  const current = ui.composer?.anchor;
  if (extend && current && current.qualname === qualname && ["line", "range"].includes(current.kind)) {
    // Extend the open composer's line to a range, in diff order.
    const u = data.units.find((x) => x.qualname === qualname);
    let start = { side: current.side, line: current.line };
    let end = { side, line };
    if (rowIndex(u, end.side, end.line) < rowIndex(u, start.side, start.line)) [start, end] = [end, start];
    const anchor = rowIndex(u, start.side, start.line) === rowIndex(u, end.side, end.line)
      ? { kind: "line", qualname, ...start }
      : { kind: "range", qualname, ...start, end_side: end.side, end_line: end.line };
    return openComposer(anchor, ui.composer.mode, ui.composer.surface, ui.composer.text);
  }
  openComposer({ kind: "line", qualname, side, line }, ui.mode, surface);
}

function openComposer(anchor, mode, surface = ui.tab === "walkthrough" ? "story" : "changes", text = null) {
  if (anchor.kind === "walkthrough") mode = "ask";
  if (mode === "ask" && !data.agent.available) {
    if (anchor.kind === "walkthrough") return toast(agentMissing());
    mode = "comment";
  }
  const previous = ui.composer;
  ui.composer = { anchor, mode, surface, text: text ?? (previous && placeOf(previous.anchor) === placeOf(anchor) ? previous.text : "") };
  if (anchor.qualname && surface === "changes") {
    ui.open.add(anchor.qualname);
    if (ui.tab !== "changes") return revealUnit(anchor.qualname, () => afterComposerChange(anchor, previous));
  }
  afterComposerChange(anchor, previous);
}

function afterComposerChange(anchor, previous) {
  if (previous) refreshFor(previous.anchor);
  refreshFor(anchor);
  focusComposer();
}

function focusComposer() {
  const area = document.querySelector(`#panel-${ui.tab} .composer textarea`);
  if (!area) return;
  area.focus({ preventScroll: true });
  area.setSelectionRange(area.value.length, area.value.length);
  area.closest(".composer").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function closeComposer() {
  const previous = ui.composer;
  ui.composer = null;
  if (previous) refreshFor(previous.anchor);
}

function composerView() {
  const composer = ui.composer;
  const { anchor, mode } = composer;
  const canComment = anchor.kind !== "walkthrough";
  const area = el("textarea", {
    rows: "3",
    placeholder: mode === "ask" ? "Ask Claude: why, what if, where else…" : "Write a comment (Markdown). It's saved as a draft.",
    oninput: (e) => (composer.text = e.target.value),
    onkeydown: (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitComposer(); }
      if (e.key === "Escape") { e.preventDefault(); closeComposer(); }
    },
  });
  area.value = composer.text;
  const setMode = (next) => {
    composer.mode = ui.mode = next;
    refreshFor(anchor);
    focusComposer();
  };
  return el("div", { class: `composer ${mode}` },
    el("div", { class: "composer-head" },
      el("div", { class: "segmented small", role: "group", "aria-label": "Comment or ask" },
        canComment && el("button", { "aria-pressed": String(mode === "comment"), onclick: () => setMode("comment") }, "💬 Comment"),
        el("button", {
          "aria-pressed": String(mode === "ask"),
          disabled: !data.agent.available,
          title: data.agent.available ? null : agentMissing(),
          onclick: () => setMode("ask"),
        }, "? Ask Claude"),
      ),
      el("span", { class: "muted" }, mode === "ask" ? "about " : "on ", anchorLabel(anchor)),
    ),
    anchor.kind === "walkthrough" && anchor.quote && el("blockquote", { class: "composer-quote", text: anchor.quote }),
    area,
    el("div", { class: "composer-foot" },
      el("span", { class: "hint" },
        mode === "ask" ? "Private: questions are never posted. " : "",
        ["line", "range"].includes(anchor.kind) ? "Shift-click another line for a range. " : "",
        el("kbd", { text: "⌘↵" }), " to send, ", el("kbd", { text: "Esc" }), " to cancel",
      ),
      el("button", { class: "button", text: "Cancel", onclick: closeComposer }),
      el("button", { class: "button primary", text: mode === "ask" ? "Ask" : "Save draft", onclick: submitComposer }),
    ),
  );
}

async function submitComposer() {
  const composer = ui.composer;
  const text = composer?.text.trim();
  if (!text) return;
  try {
    if (composer.mode === "ask") {
      const question = await api("POST", "/api/questions", { anchor: anchorInput(composer.anchor), text });
      data.questions.push(question);
      listen(question.id);
    } else {
      data.comments.push(await api("POST", "/api/comments", { anchor: anchorInput(composer.anchor), body: text }));
      invalidatePreview();
    }
    ui.composer = null;
    refreshFor(composer.anchor);
  } catch (error) {
    toast(`Couldn't send: ${error.message}`);
  }
}

// Re-render everything that shows threads at an anchor.
function refreshFor(anchor) {
  if (anchor.qualname) {
    rerenderUnit(anchor.qualname);
    const story = data.summary?.walkthrough || [];
    if (story.some((b) => b.type === "code" && b.unit === anchor.qualname)) renderWalkthrough();
  }
  if (["walkthrough", "pr"].includes(anchor.kind) || anchor.state === "orphaned") renderWalkthrough();
  renderInbox();
  if (ui.drawer) renderDrawer();
}

const STATUS_LABELS = { queued: "Waiting for a free agent…", running: "Thinking…", failed: "Failed", cancelled: "Cancelled" };

function anchorChips(anchor) {
  return [
    anchor.state === "stale" && el("span", { class: "chip changed", title: "The code or walkthrough changed since", text: "changed since" }),
    anchor.state === "orphaned" && el("span", { class: "chip removed", title: "Its unit is no longer part of the PR", text: "orphaned" }),
  ];
}

function questionCard(q, inDrawer = false) {
  const running = q.status === "queued" || q.status === "running";
  const collapsed = q.resolved && !ui.expanded.has(q.id) && !running;
  const first = q.messages.find((m) => m.role === "you");
  const answer = [...q.messages].reverse().find((m) => m.role === "agent");
  const head = el("div", { class: "thread-head" },
    el("span", { class: "thread-icon", text: "?" }),
    inDrawer
      ? el("a", { href: "#", class: "thread-where", onclick: (e) => { e.preventDefault(); jumpTo(q.anchor); } }, anchorLabel(q.anchor, true))
      : el("span", { class: "thread-where", text: "Question" }),
    anchorChips(q.anchor),
    q.resolved && el("span", { class: "chip", text: "resolved" }),
    ["failed", "cancelled"].includes(q.status) && el("span", { class: "chip removed", text: q.status }),
    el("span", { class: "spacer" }),
    running && el("button", { class: "link-button", text: "Stop", onclick: () => cancelQuestion(q) }),
    !running && el("button", { class: "link-button", text: q.resolved ? "Reopen" : "Resolve", onclick: () => resolveQuestion(q, !q.resolved) }),
    el("button", { class: "link-button danger", title: "Delete the thread", text: "Delete", onclick: () => deleteQuestion(q) }),
  );
  const card = el("div", { class: `thread question ${q.resolved ? "resolved" : ""}`, dataset: { thread: `q:${q.id}` } }, head);
  if (collapsed) {
    card.append(el("button", { class: "thread-collapsed", onclick: () => { ui.expanded.add(q.id); refreshFor(q.anchor); } },
      first ? first.body : "", answer ? el("span", { class: "muted", text: " · answered" }) : null));
    return card;
  }
  if (inDrawer && q.anchor.quote) card.append(el("blockquote", { class: "composer-quote", text: q.anchor.quote }));
  for (const message of q.messages) card.append(messageView(message));
  if (running) card.append(streamView(q.id));
  if (q.error && !running) card.append(el("p", { class: "thread-error", text: q.error }));

  const followUp = ui.followUp.get(q.id);
  if (followUp !== undefined && !running) {
    const area = el("textarea", {
      rows: "2",
      placeholder: "Follow up…",
      oninput: (e) => ui.followUp.set(q.id, e.target.value),
      onkeydown: (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendFollowUp(q); }
        if (e.key === "Escape") { e.preventDefault(); ui.followUp.delete(q.id); refreshFor(q.anchor); }
      },
    });
    area.value = followUp;
    card.append(el("div", { class: "follow-up" }, area,
      el("div", { class: "composer-foot" },
        el("span", { class: "hint" }, el("kbd", { text: "⌘↵" }), " to send"),
        el("button", { class: "button", text: "Cancel", onclick: () => { ui.followUp.delete(q.id); refreshFor(q.anchor); } }),
        el("button", { class: "button primary", text: "Ask", onclick: () => sendFollowUp(q) }),
      ),
    ));
  } else if (!running) {
    card.append(el("div", { class: "thread-actions" },
      data.agent.available && el("button", {
        class: "button small",
        text: "Follow up",
        onclick: () => {
          ui.followUp.set(q.id, "");
          refreshFor(q.anchor);
          document.querySelector(`[data-thread="q:${q.id}"] .follow-up textarea`)?.focus();
        },
      }),
      answer && el("button", { class: "button small", title: "Start a comment draft from this answer", text: "→ Draft comment from this", onclick: () => draftFromAnswer(q, answer) }),
    ));
  }
  return card;
}

function messageView(message) {
  if (message.role === "you") return el("div", { class: "msg you" }, el("span", { class: "who", text: "You" }), markdown(message.body, "msg-body"));
  return el("div", { class: "msg agent" },
    el("span", { class: "who", text: "Claude" }),
    markdown(message.body, "msg-body"),
    sourcesView(message.sources),
  );
}

function sourcesView(sources) {
  if (!sources?.length) return null;
  const items = sources.map((s) => {
    if (s.kind === "url") {
      let label = s.url;
      try { const url = new URL(s.url); label = url.host + url.pathname.replace(/\/$/, ""); } catch { /* keep the raw URL */ }
      return el("a", { href: s.url, target: "_blank", rel: "noopener", title: s.url, text: label });
    }
    if (s.kind === "search") return el("span", { class: "source-search", title: "Web search", text: `🔍 ${s.query}` });
    return citation(s.path) || el("code", { class: "inline", text: s.path });
  });
  return el("div", { class: "sources" }, el("span", { class: "muted", text: "Sources: " }), items);
}

function streamView(id) {
  const stream = ui.streams.get(id) || { status: STATUS_LABELS.queued, text: "", sources: [], notices: [] };
  return el("div", { class: "msg agent streaming", dataset: { stream: String(id) } },
    el("span", { class: "who", text: "Claude" }),
    el("div", { class: "stream-status" }, el("span", { class: "spinner" }), stream.status),
    stream.notices.map((n) => el("p", { class: "notice", text: n })),
    stream.text && markdown(stream.text, "msg-body"),
    sourcesView(stream.sources),
  );
}

function updateStream(id) {
  for (const node of document.querySelectorAll(`[data-stream="${id}"]`)) node.replaceWith(streamView(id));
  renderInbox();
}

// Follow a running answer through server-sent events.
function listen(id) {
  if (sockets.has(id)) return;
  const source = new EventSource(`/api/questions/${id}/events?token=${encodeURIComponent(TOKEN)}`);
  sockets.set(id, source);
  // The server replays the run's events from the start on each connection.
  source.onopen = () => ui.streams.set(id, { status: STATUS_LABELS.queued, text: "", sources: [], notices: [] });
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    const stream = ui.streams.get(id) || { status: "", text: "", sources: [], notices: [] };
    ui.streams.set(id, stream);
    switch (event.type) {
      case "status": stream.status = event.text; break;
      case "text":
        if (event.reset) stream.text = "";
        stream.text += event.text;
        if (event.text.trim()) stream.status = "Writing…";
        break;
      case "source": stream.sources.push(event.source); break;
      case "notice": stream.notices.push(event.text); break;
      case "done":
      case "error":
        source.close();
        sockets.delete(id);
        ui.streams.delete(id);
        if (event.question) replaceQuestion(event.question);
        return;
      default: return;
    }
    updateStream(id);
  };
}

function replaceQuestion(question) {
  const index = data.questions.findIndex((q) => q.id === question.id);
  if (index === -1) return;
  data.questions[index] = question;
  refreshFor(question.anchor);
}

async function sendFollowUp(q) {
  const text = (ui.followUp.get(q.id) || "").trim();
  if (!text) return;
  try {
    const question = await api("POST", `/api/questions/${q.id}/messages`, { text });
    ui.followUp.delete(q.id);
    replaceQuestion(question);
    listen(q.id);
  } catch (error) {
    toast(`Couldn't ask: ${error.message}`);
  }
}

async function cancelQuestion(q) {
  try {
    await api("POST", `/api/questions/${q.id}/cancel`);
  } catch (error) {
    toast(`Couldn't stop it: ${error.message}`);
  }
}

async function resolveQuestion(q, resolved) {
  try {
    ui.expanded.delete(q.id);
    replaceQuestion(await api("PATCH", `/api/questions/${q.id}`, { resolved }));
  } catch (error) {
    toast(`Couldn't save: ${error.message}`);
  }
}

async function deleteQuestion(q) {
  if (!confirm("Delete this question and its answers?")) return;
  try {
    await api("DELETE", `/api/questions/${q.id}`);
    sockets.get(q.id)?.close();
    sockets.delete(q.id);
    data.questions = data.questions.filter((x) => x.id !== q.id);
    refreshFor(q.anchor);
  } catch (error) {
    toast(`Couldn't delete: ${error.message}`);
  }
}

// A comment draft at the question's anchor, with the answer as its body.
// The walkthrough isn't on GitHub, so such a draft goes to the PR and
// quotes what was asked about.
async function draftFromAnswer(q, answer) {
  let anchor = anchorInput(q.anchor);
  let body = answer.body;
  if (q.anchor.kind === "walkthrough" || q.anchor.state === "orphaned") {
    anchor = { kind: "pr" };
    if (q.anchor.quote) body = `${q.anchor.quote.split("\n").map((line) => `> ${line}`).join("\n")}\n\n${body}`;
  }
  try {
    const comment = await api("POST", "/api/comments", { anchor, body, question_id: q.id });
    data.comments.push(comment);
    invalidatePreview();
    ui.editing.set(comment.id, comment.body);
    refreshFor(comment.anchor);
    const area = document.querySelector(`[data-thread="c:${comment.id}"] textarea`);
    area?.focus();
    area?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (error) {
    toast(`Couldn't create the draft: ${error.message}`);
  }
}

function commentCard(c, inDrawer = false) {
  const editing = ui.editing.get(c.id);
  const head = el("div", { class: "thread-head" },
    el("span", { class: "thread-icon", text: "💬" }),
    inDrawer
      ? el("a", { href: "#", class: "thread-where", onclick: (e) => { e.preventDefault(); jumpTo(c.anchor); } }, anchorLabel(c.anchor, true))
      : el("span", { class: "thread-where", text: c.state === "draft" ? "Draft comment" : "Posted comment" }),
    anchorChips(c.anchor),
    c.state === "posted" && c.github_url && el("a", { href: c.github_url, target: "_blank", rel: "noopener", class: "chip added", text: "on GitHub ↗" }),
    c.question_id && el("span", { class: "chip ask", text: "from a question" }),
    el("span", { class: "spacer" }),
    c.state === "draft" && editing === undefined && el("button", { class: "link-button", text: "Edit", onclick: () => { ui.editing.set(c.id, c.body); refreshFor(c.anchor); } }),
    c.state === "draft" && el("button", { class: "link-button danger", text: "Delete", onclick: () => deleteComment(c) }),
  );
  const card = el("div", { class: `thread comment ${c.state}`, dataset: { thread: `c:${c.id}` } }, head);
  if (editing === undefined) {
    card.append(markdown(c.body, "msg-body"));
    return card;
  }
  const area = el("textarea", {
    rows: String(Math.min(12, Math.max(3, editing.split("\n").length + 1))),
    oninput: (e) => ui.editing.set(c.id, e.target.value),
    onkeydown: (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveComment(c); }
      if (e.key === "Escape") { e.preventDefault(); ui.editing.delete(c.id); refreshFor(c.anchor); }
    },
  });
  area.value = editing;
  card.append(area,
    el("div", { class: "composer-foot" },
      el("span", { class: "hint" }, el("kbd", { text: "⌘↵" }), " to save"),
      el("button", { class: "button", text: "Cancel", onclick: () => { ui.editing.delete(c.id); refreshFor(c.anchor); } }),
      el("button", { class: "button primary", text: "Save draft", onclick: () => saveComment(c) }),
    ),
  );
  return card;
}

async function saveComment(c) {
  const body = (ui.editing.get(c.id) || "").trim();
  if (!body) return toast("A comment can't be empty; delete it instead.");
  try {
    const updated = await api("PATCH", `/api/comments/${c.id}`, { body });
    ui.editing.delete(c.id);
    data.comments[data.comments.findIndex((x) => x.id === c.id)] = updated;
    invalidatePreview();
    refreshFor(updated.anchor);
  } catch (error) {
    toast(`Couldn't save: ${error.message}`);
  }
}

async function deleteComment(c) {
  if (!confirm("Delete this draft comment?")) return;
  try {
    await api("DELETE", `/api/comments/${c.id}`);
    ui.editing.delete(c.id);
    data.comments = data.comments.filter((x) => x.id !== c.id);
    invalidatePreview();
    refreshFor(c.anchor);
  } catch (error) {
    toast(`Couldn't delete: ${error.message}`);
  }
}

function jumpTo(anchor) {
  if (anchor.state === "orphaned") return toast("Its unit is no longer part of this PR.");
  if (anchor.kind === "line" || anchor.kind === "range") {
    revealUnit(anchor.qualname, () => showLine(anchor.qualname, anchor.end_line ?? anchor.line));
  } else if (anchor.kind === "unit") {
    revealUnit(anchor.qualname);
  } else {
    selectTab("walkthrough");
    const target = anchor.kind === "walkthrough"
      ? document.querySelector(`#panel-walkthrough [data-block="${anchor.block}"]`)
      : document.querySelector("#panel-walkthrough .pr-threads");
    target?.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

// The bar's Questions and Drafts buttons, and the side panel they open.

function renderInbox() {
  const running = data.questions.filter((q) => q.status === "queued" || q.status === "running").length;
  const open = data.questions.filter((q) => !q.resolved).length;
  const drafts = data.comments.filter((c) => c.state === "draft").length;
  const button = (key, label, count, extra) =>
    el("button", {
      class: `inbox-button ${ui.drawer === key ? "on" : ""}`,
      "aria-pressed": String(ui.drawer === key),
      onclick: () => { ui.drawer = ui.drawer === key ? null : key; renderDrawer(); renderInbox(); },
    }, label, el("span", { class: "count", text: count || "" }), extra);
  document.getElementById("inbox").replaceChildren(
    button("questions", "Questions", open, running > 0 && el("span", { class: "running", title: `${running} running` }, el("span", { class: "spinner" }), running)),
    button("drafts", "Drafts", drafts),
  );
}

function renderDrawer() {
  const drawer = document.getElementById("drawer");
  document.body.classList.toggle("with-drawer", Boolean(ui.drawer));
  if (!ui.drawer) {
    drawer.hidden = true;
    return;
  }
  const questions = [...data.questions].sort((a, b) => Number(a.resolved) - Number(b.resolved) || (a.created_at < b.created_at ? 1 : -1));
  const drafts = data.comments.filter((c) => c.state === "draft");
  const list = ui.drawer === "questions"
    ? questions.map((q) => questionCard(q, true))
    : drafts.map((c) => commentCard(c, true));
  const empty = ui.drawer === "questions"
    ? "No questions yet. Select code or text, or hover a line, and ask Claude about it."
    : "No draft comments yet. Hover a line and click +, or select lines, to write one.";
  drawer.replaceChildren(
    el("div", { class: "drawer-head" },
      el("div", { class: "segmented small", role: "group" },
        el("button", { "aria-pressed": String(ui.drawer === "questions"), onclick: () => { ui.drawer = "questions"; renderDrawer(); renderInbox(); } }, `Questions (${questions.length})`),
        el("button", { "aria-pressed": String(ui.drawer === "drafts"), onclick: () => { ui.drawer = "drafts"; renderDrawer(); renderInbox(); } }, `Drafts (${drafts.length})`),
      ),
      el("button", { class: "icon-button", title: "Close", text: "✕", onclick: () => { ui.drawer = null; renderDrawer(); renderInbox(); } }),
    ),
    el("div", { class: "drawer-body" },
      ui.drawer === "drafts" && (drafts.length > 0 || ui.submit.posted) && submitCard(drafts),
      list.length ? list : el("p", { class: "empty", text: empty }),
      ui.drawer === "drafts" && postedReviews(),
    ),
  );
  drawer.hidden = false;
}

// Submitting drafts as one GitHub review: write a summary, pick the
// event, preview where every draft goes, then post.

const EVENTS = [
  { key: "COMMENT", label: "Comment" },
  { key: "APPROVE", label: "Approve" },
  { key: "REQUEST_CHANGES", label: "Request changes" },
];

function invalidatePreview() {
  ui.submit.preview = null;
  ui.submit.error = null;
}

function submitCard(drafts) {
  const submit = ui.submit;
  const orphaned = drafts.filter((c) => c.anchor.state === "orphaned").length;
  const change = (update) => { Object.assign(submit, update); invalidatePreview(); renderDrawer(); };
  const card = el("div", { class: "submit-card" }, el("h3", { text: "Submit review" }));
  if (submit.posted && !drafts.length) {
    card.append(el("p", { class: "posted-note" }, "✓ Posted. ", el("a", { href: submit.posted, target: "_blank", rel: "noopener", text: "View the review on GitHub ↗" })));
    return card;
  }
  const summary = el("textarea", {
    rows: "3",
    placeholder: "Summary for the review body (optional for a comment or approval)",
    disabled: submit.busy,
    oninput: (e) => { submit.body = e.target.value; if (submit.preview) { invalidatePreview(); renderDrawer(); restoreFocus(".submit-card textarea"); } },
  });
  summary.value = submit.body;
  card.append(...[
    summary,
    el("div", { class: "segmented small", role: "group", "aria-label": "Review event" },
      EVENTS.map((e) => el("button", { "aria-pressed": String(submit.event === e.key), disabled: submit.busy, onclick: () => change({ event: e.key }) }, e.label)),
    ),
    orphaned > 0 && el("label", { class: "check" },
      el("input", { type: "checkbox", checked: submit.includeOrphaned, onchange: (e) => change({ includeOrphaned: e.target.checked }) }),
      ` Put ${plural(orphaned, "orphaned draft")} in the review body`,
    ),
  ].filter(Boolean));
  if (submit.error) card.append(el("p", { class: "thread-error", text: submit.error }));
  if (!submit.preview) {
    card.append(el("div", { class: "composer-foot" },
      el("span", { class: "hint", text: "Nothing is posted until you confirm the preview." }),
      el("button", { class: "button primary", disabled: submit.busy, onclick: previewReview }, submit.busy ? "Checking…" : "Preview review"),
    ));
    return card;
  }
  card.append(previewView(submit.preview));
  const label = EVENTS.find((e) => e.key === submit.preview.event).label;
  card.append(el("div", { class: "composer-foot" },
    el("span", { class: "hint", text: `Posts as you, through gh, to ${data.pr.repo}#${data.pr.number}.` }),
    el("button", { class: "button", disabled: submit.busy, text: "Back", onclick: () => { invalidatePreview(); renderDrawer(); } }),
    el("button", {
      class: `button primary ${submit.preview.event === "REQUEST_CHANGES" ? "danger" : ""}`,
      disabled: submit.busy || submit.preview.head_moved,
      onclick: postReview,
    }, submit.busy ? "Posting…" : `Post: ${label}`),
  ));
  return card;
}

function restoreFocus(selector) {
  const node = document.querySelector(selector);
  if (!node) return;
  node.focus();
  node.setSelectionRange?.(node.value.length, node.value.length);
}

function previewView(preview) {
  const drafts = new Map(data.comments.map((c) => [c.id, c]));
  const group = (where, title) => {
    const places = preview.placed.filter((p) => p.where === where);
    if (!places.length) return null;
    return el("div", { class: "preview-group" },
      el("div", { class: "preview-title", text: `${title} (${places.length})` }),
      el("ul", {}, places.map((p) => el("li", {},
        el("code", { class: "inline", text: p.label }),
        el("span", { class: "muted", text: ` ${excerpt(drafts.get(p.draft)?.body)}` }),
        p.reason && el("div", { class: "preview-reason", text: p.reason }),
      ))),
    );
  };
  return el("div", { class: "preview" },
    preview.head_moved && el("p", { class: "notice danger" },
      `The PR has new commits (${preview.remote_head_sha.slice(0, 7)}) since it was analyzed at ${preview.head_sha.slice(0, 7)}. `,
      "Run /pr-sift again and check your drafts before posting."),
    group("inline", "Inline comments"),
    group("body", "In the review body"),
    group("skipped", "Not posted"),
    el("div", { class: "preview-title", text: "Review body" }),
    preview.body ? el("div", { class: "preview-body" }, markdown(preview.body, "msg-body")) : el("p", { class: "muted", text: "No body." }),
  );
}

function excerpt(text, length = 70) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  return flat.length > length ? `${flat.slice(0, length)}…` : flat;
}

async function previewReview() {
  const submit = ui.submit;
  submit.busy = true;
  submit.error = null;
  renderDrawer();
  try {
    submit.preview = await api("POST", "/api/review/preview", { event: submit.event, body: submit.body, include_orphaned: submit.includeOrphaned });
  } catch (error) {
    submit.error = error.message;
  }
  submit.busy = false;
  renderDrawer();
}

async function postReview() {
  const submit = ui.submit;
  const preview = submit.preview;
  submit.busy = true;
  submit.error = null;
  renderDrawer();
  try {
    const result = await api("POST", "/api/review", {
      event: preview.event,
      body: submit.body,
      include_orphaned: submit.includeOrphaned,
      drafts: preview.drafts,
      head_sha: preview.head_sha,
    });
    Object.assign(data, { comments: result.comments, questions: result.questions, submissions: result.submissions });
    Object.assign(submit, { body: "", preview: null, posted: result.url || "#" });
    renderWalkthrough();
    renderChanges();
    renderInbox();
    toast("Review posted to GitHub.");
  } catch (error) {
    submit.error = error.message;
  }
  submit.busy = false;
  renderDrawer();
}

function postedReviews() {
  if (!data.submissions?.length) return null;
  const labels = Object.fromEntries(EVENTS.map((e) => [e.key, e.label]));
  return el("div", { class: "posted-reviews" },
    el("div", { class: "preview-title", text: "Posted reviews" }),
    el("ul", {}, [...data.submissions].reverse().map((s) =>
      el("li", {},
        s.url ? el("a", { href: s.url, target: "_blank", rel: "noopener", text: `${labels[s.event] || s.event} ↗` }) : labels[s.event],
        el("span", { class: "muted", text: ` · ${new Date(s.created_at).toLocaleString()}` }),
      ),
    )),
  );
}

// Selecting code in a diff, or text in the walkthrough, offers to comment
// on it or ask about it.

let selectionAnchor = null;

function anchorFromSelection() {
  const selection = window.getSelection();
  const text = selection?.toString().trim();
  if (!text || !selection.rangeCount) return null;
  const range = selection.getRangeAt(0);
  const node = (n) => (n.nodeType === 1 ? n : n.parentElement);
  const start = node(range.startContainer), end = node(range.endContainer);
  if (start.closest(".threads, .composer, input, textarea") || end.closest(".threads, .composer")) return null;
  const diff = start.closest(".diff-wrap[data-qualname]");
  if (diff && diff === end.closest(".diff-wrap[data-qualname]")) {
    const first = start.closest("tr[data-line]") || [...diff.querySelectorAll("tr[data-line]")].find((tr) => range.intersectsNode(tr));
    const last = end.closest("tr[data-line]") || [...diff.querySelectorAll("tr[data-line]")].reverse().find((tr) => range.intersectsNode(tr));
    if (!first || !last) return null;
    const qualname = diff.dataset.qualname;
    const a = { side: first.dataset.side, line: Number(first.dataset.line) };
    const b = { side: last.dataset.side, line: Number(last.dataset.line) };
    const anchor = first === last
      ? { kind: "line", qualname, ...a }
      : { kind: "range", qualname, ...a, end_side: b.side, end_line: b.line };
    return { anchor: { ...anchor, quote: text }, rect: range.getBoundingClientRect(), surface: surfaceOf(diff) };
  }
  const block = start.closest(".story-block");
  if (block && block === end.closest(".story-block")) {
    return { anchor: { kind: "walkthrough", block: Number(block.dataset.block), quote: text }, rect: range.getBoundingClientRect(), surface: "story" };
  }
  return null;
}

function onSelection() {
  document.getElementById("select-actions")?.remove();
  selectionAnchor = anchorFromSelection();
  if (!selectionAnchor) return;
  const { anchor, rect, surface } = selectionAnchor;
  const pick = (mode) => (e) => {
    e.preventDefault();
    document.getElementById("select-actions")?.remove();
    window.getSelection()?.removeAllRanges();
    selectionAnchor = null;
    openComposer(anchor, mode, surface);
  };
  const bar = el("div", { id: "select-actions", class: "select-actions", style: { left: `${window.scrollX + rect.left}px`, top: `${window.scrollY + rect.bottom + 6}px` } },
    anchor.kind !== "walkthrough" && el("button", { onmousedown: (e) => e.preventDefault(), onclick: pick("comment"), title: "Comment on the selection (c)" }, "💬 Comment"),
    el("button", {
      onmousedown: (e) => e.preventDefault(),
      onclick: pick("ask"),
      disabled: !data.agent.available,
      title: data.agent.available ? "Ask Claude about the selection (a)" : agentMissing(),
    }, "? Ask Claude"),
  );
  document.body.append(bar);
}

// State changes

// Re-render what a diff belongs to: a walkthrough block or a unit card.
function refresh(key) {
  if (key.startsWith("story:")) renderWalkthrough();
  else rerenderUnit(key);
}

function rerenderUnit(qualname) {
  const node = document.getElementById(`unit-${qualname}`);
  const unit = data.units.find((u) => u.qualname === qualname);
  if (!node || !unit) return;
  const fresh = unitCard(unit);
  node.replaceWith(fresh);
  if (ui.focus === qualname) fresh.classList.add("focused");
}

function toggleUnit(qualname, open = !ui.open.has(qualname)) {
  if (open) ui.open.add(qualname);
  else ui.open.delete(qualname);
  rerenderUnit(qualname);
}

function toggleTier(key) {
  if (ui.closedTiers.has(key)) ui.closedTiers.delete(key);
  else ui.closedTiers.add(key);
  savePrefs();
  renderChanges();
}

function setAllOpen(open) {
  for (const u of data.units) {
    if (!matchesFilter(u)) continue;
    if (open) ui.open.add(u.qualname);
    else ui.open.delete(u.qualname);
  }
  renderChanges();
}

function setFilter(key) {
  ui.filter = key;
  savePrefs();
  renderChanges();
}

async function updateReview(unit, change, advance = false) {
  try {
    const updated = await api("PUT", `/api/units/${encodeURIComponent(unit.qualname)}/review`, {
      fingerprint: unit.fingerprint,
      ...change,
    });
    const index = data.units.findIndex((u) => u.qualname === unit.qualname);
    data.units[index] = updated;
    data.has_reviews = data.has_reviews || updated.state === "seen";
    const next = advance && change.reviewed ? neighborUnit(unit.qualname, 1) : null;
    if (change.reviewed) ui.open.delete(unit.qualname);
    renderProgress();
    renderBanner();
    renderWalkthrough();
    renderChanges();
    if (next) setFocus(next);
  } catch (error) {
    toast(`Couldn't save: ${error.message}`);
  }
}

// Navigation

function selectTab(name) {
  if (!TABS.includes(name)) name = "walkthrough";
  ui.tab = name;
  for (const tab of TABS) {
    document.getElementById(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    document.getElementById(`panel-${tab}`).hidden = tab !== name;
  }
  history.replaceState(null, "", `#${name}`);
}

function revealTier(key) {
  const group = ["skim", "hidden"].includes(key) ? key : "read";
  ui.closedTiers.delete(group);
  ui.filter = "all";
  ui.closedTiers.delete(key);
  savePrefs();
  renderChanges();
  selectTab("changes");
  (document.getElementById(`tier-${key}`) ?? document.getElementById(`group-${key}`))?.scrollIntoView({ block: "start" });
}

function revealUnit(qualname, then = null) {
  const unit = data.units.find((u) => u.qualname === qualname);
  if (!unit) return;
  if (!matchesFilter(unit)) ui.filter = "all";
  ui.closedTiers.delete(sectionOf(unit));
  ui.closedTiers.delete(GROUPS.find((g) => g.has(unit)).key);
  ui.open.add(qualname);
  renderChanges();
  selectTab("changes");
  setFocus(qualname);
  then?.();
}

function unitNodes() {
  return [...document.querySelectorAll("#panel-changes .unit")];
}

function neighborUnit(qualname, step) {
  const nodes = unitNodes();
  const index = nodes.findIndex((n) => n.dataset.qualname === qualname);
  const next = nodes[index + step] || nodes[index - step];
  return next ? next.dataset.qualname : null;
}

function setFocus(qualname, scroll = true) {
  ui.focus = qualname;
  for (const node of unitNodes()) node.classList.toggle("focused", node.dataset.qualname === qualname);
  const node = document.getElementById(`unit-${qualname}`);
  if (node && scroll) {
    node.focus({ preventScroll: true });
    node.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function moveFocus(step) {
  const nodes = unitNodes();
  if (!nodes.length) return;
  const index = nodes.findIndex((n) => n.dataset.qualname === ui.focus);
  const next = index === -1 ? (step > 0 ? 0 : nodes.length - 1) : Math.min(nodes.length - 1, Math.max(0, index + step));
  setFocus(nodes[next].dataset.qualname);
}

function focusedUnit() {
  return data.units.find((u) => u.qualname === ui.focus);
}

function onKey(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest("input, textarea, select, [contenteditable]")) return;
  const help = document.getElementById("help");
  if (!help.hidden) {
    if (event.key === "Escape" || event.key === "?") help.hidden = true;
    return;
  }
  if (event.key === "?") {
    help.hidden = false;
    document.getElementById("help-close").focus();
    return;
  }
  if (/^[1-4]$/.test(event.key)) {
    selectTab(TABS[Number(event.key) - 1]);
    return;
  }
  if (event.key === "Escape" && ui.drawer) {
    ui.drawer = null;
    renderDrawer();
    renderInbox();
    return;
  }
  if (event.key === "a" || event.key === "c") {
    const mode = event.key === "a" ? "ask" : "comment";
    const selected = anchorFromSelection();
    if (selected) {
      document.getElementById("select-actions")?.remove();
      openComposer(selected.anchor, mode, selected.surface);
    } else if (ui.tab === "changes" && focusedUnit()) {
      openComposer({ kind: "unit", qualname: focusedUnit().qualname }, mode, "changes");
    } else if (ui.tab === "walkthrough") {
      openComposer({ kind: "pr" }, mode, "story");
    } else {
      return;
    }
    event.preventDefault();
    return;
  }
  if (ui.tab !== "changes") return;
  const unit = focusedUnit();
  switch (event.key) {
    case "j": moveFocus(1); break;
    case "k": moveFocus(-1); break;
    case "o":
    case "Enter":
      if (unit) toggleUnit(unit.qualname);
      else moveFocus(1);
      break;
    case "r":
      if (unit && !isHidden(unit)) updateReview(unit, { reviewed: unit.state !== "seen" }, true);
      break;
    case "f":
      if (unit && !isHidden(unit)) updateReview(unit, { flagged: !unit.flagged });
      break;
    default:
      return;
  }
  event.preventDefault();
}

// Start

async function main() {
  loadPrefs();
  for (const tab of TABS) document.getElementById(`tab-${tab}`).addEventListener("click", () => selectTab(tab));
  document.getElementById("help-close").addEventListener("click", () => (document.getElementById("help").hidden = true));
  document.getElementById("help").addEventListener("click", (e) => { if (e.target.id === "help") e.target.hidden = true; });
  document.addEventListener("keydown", onKey);
  document.addEventListener("mouseup", () => setTimeout(onSelection, 0));
  document.addEventListener("mousedown", (e) => {
    if (!e.target.closest("#select-actions")) document.getElementById("select-actions")?.remove();
  });
  window.addEventListener("hashchange", () => { if (data) selectTab(location.hash.slice(1)); });

  try {
    data = await api("GET", "/api/pr");
  } catch (error) {
    document.getElementById("title").textContent = `Couldn't load the report: ${error.message}`;
    return;
  }

  // Open the units most worth reading first.
  const worth = data.units.filter((u) => !isHidden(u) && u.state !== "seen" && OPEN_BY_DEFAULT.has(sectionOf(u)));
  for (const u of worth.slice(0, MAX_AUTO_OPEN)) ui.open.add(u.qualname);

  for (const q of data.questions) if (q.status === "queued" || q.status === "running") listen(q.id);

  renderHeader();
  renderInbox();
  renderProgress();
  renderBanner();
  renderHeadBanner();
  renderWalkthrough();
  renderInterfaces();
  renderChanges();
  selectTab(location.hash.slice(1) || (data.summary ? "walkthrough" : "changes"));
  await renderArchitecture();
}

main();
