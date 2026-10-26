// The report page: renders /api/pr and keeps review state in sync.

const TOKEN = document.querySelector('meta[name="reviewer-token"]').content;

const TIERS = [
  { key: "architecture", label: "Architecture", desc: "Structure and dependencies between components" },
  { key: "interface", label: "Interface", desc: "What callers and clients depend on" },
  { key: "behavior", label: "Behavior", desc: "Logic worth understanding" },
  { key: "detail", label: "Detail", desc: "Local changes that don't change what a component does" },
  { key: "unranked", label: "Not ranked yet", desc: "Run /review-pr to rank these" },
  { key: "hidden", label: "Hidden", desc: "Noise: nothing a reviewer needs to see" },
];
const OPEN_BY_DEFAULT = new Set(["architecture", "interface", "behavior", "unranked"]);
const NOISE = {
  comment: "Only comments changed",
  formatting: "Only formatting changed",
  docstring: "Only docstrings changed",
  imports: "Only the order of imports changed",
  "local-rename": "Only local names changed",
  "symbol-rename": "Private symbol renamed, body unchanged",
  move: "Private symbol moved, body unchanged",
  generated: "Generated file",
};
const NOISE_SHORT = {
  comment: "comments", formatting: "formatting", docstring: "docstrings", imports: "import order",
  "local-rename": "local renames", "symbol-rename": "private renames", move: "private moves", generated: "generated",
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
const TABS = ["overview", "architecture", "interfaces", "changes"];

let data = null;
const ui = {
  tab: "overview",
  filter: "all",
  closedTiers: new Set(["detail", "hidden"]),
  open: new Set(),
  view: new Map(), // qualname -> "since" | "full"
  gaps: new Set(), // expanded runs of unchanged lines
  focus: null,
};

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

// Text with `backticks` as nodes, the quoted parts as inline code.
function rich(text) {
  return String(text ?? "")
    .split(/`([^`\n]+)`/)
    .map((part, i) => (i % 2 ? el("code", { class: "inline", text: part }) : part))
    .filter((part) => part !== "");
}

function loadPrefs() {
  try {
    const prefs = JSON.parse(localStorage.getItem("reviewer:prefs") || "{}");
    if (FILTERS.some((f) => f.key === prefs.filter)) ui.filter = prefs.filter;
    if (Array.isArray(prefs.closedTiers)) ui.closedTiers = new Set(prefs.closedTiers);
  } catch {
    // Storage may be unavailable; defaults are fine.
  }
}

function savePrefs() {
  try {
    localStorage.setItem("reviewer:prefs", JSON.stringify({ filter: ui.filter, closedTiers: [...ui.closedTiers] }));
  } catch {
    // Ignore.
  }
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "X-Reviewer-Token": TOKEN, "Content-Type": "application/json" },
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
const tierInfo = (key) => TIERS.find((t) => t.key === key);
const visibleUnits = () => data.units.filter((u) => !u.noise);

function matchesFilter(u) {
  switch (ui.filter) {
    case "todo": return !u.noise && u.state !== "seen";
    case "changed": return !u.noise && u.state === "changed";
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

// Changed lines of the whole PR: reviewed, hidden by the noise rules, and left.
function lineStats() {
  const sum = (units) => units.reduce((n, u) => n + u.changed_lines, 0);
  const total = sum(data.units);
  const hidden = sum(data.units.filter((u) => u.noise));
  const reviewed = sum(visibleUnits().filter((u) => u.state === "seen"));
  return { total, hidden, reviewed, left: total - hidden - reviewed };
}

function percent(part, total) {
  return total ? Math.round((100 * part) / total) : 0;
}

function renderProgress() {
  const lines = lineStats();
  const units = visibleUnits();
  const seen = units.filter((u) => u.state === "seen").length;
  document.getElementById("progress-fill").style.width = `${(100 * lines.reviewed) / (lines.total || 1)}%`;
  document.getElementById("progress-hidden").style.width = `${(100 * lines.hidden) / (lines.total || 1)}%`;
  document.getElementById("progress-label").textContent =
    `${percent(lines.reviewed, lines.total)}% reviewed · ${percent(lines.hidden, lines.total)}% hidden`;
  document.getElementById("progress").title =
    `Of ${lines.total.toLocaleString()} changed lines: ${lines.reviewed.toLocaleString()} reviewed, ` +
    `${lines.hidden.toLocaleString()} hidden by noise rules, ${lines.left.toLocaleString()} left. ` +
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

// Overview

function renderOverview() {
  const panel = document.getElementById("panel-overview");
  const { summary } = data;
  const cards = [];

  if (summary) {
    cards.push(
      el("div", { class: "card" },
        el("h2", { text: "Summary" }),
        el("p", { class: "lead" }, rich(summary.overview)),
        summary.stale && el("p", { class: "notice", text: "This summary was written for an earlier head. Run /review-pr again to refresh it." }),
      ),
    );
  } else {
    cards.push(
      el("div", { class: "card empty" },
        "This PR hasn't been ranked yet. Run ", el("code", { text: "/review-pr" }), " in Claude Code to rank and summarize it.",
      ),
    );
  }

  const units = visibleUnits();
  const stats = TIERS.filter((t) => t.key !== "hidden").map((tier) => {
    const count = units.filter((u) => tierOf(u) === tier.key).length;
    if (tier.key === "unranked" && !count) return null;
    return el("button", { class: "stat", onclick: () => revealTier(tier.key) },
      el("div", { class: "value", text: count }),
      el("div", { class: "label" }, el("span", { class: "dot", style: { background: `var(--tier-${tier.key})` } }), tier.label),
    );
  });
  const hidden = data.units.filter((u) => u.noise);
  stats.push(
    el("button", { class: "stat", onclick: () => revealTier("hidden") },
      el("div", { class: "value", text: hidden.length }),
      el("div", { class: "label" }, el("span", { class: "dot", style: { background: "var(--tier-hidden)" } }), "Hidden as noise"),
    ),
  );
  cards.push(el("div", { class: "stats" }, stats));
  cards.push(progressCard(hidden));

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

  panel.replaceChildren(...cards);
}

function progressCard(hidden) {
  const lines = lineStats();
  const n = (value) => value.toLocaleString();
  const reasons = {};
  for (const u of hidden) {
    reasons[u.noise] ??= { units: 0, lines: 0 };
    reasons[u.noise].units++;
    reasons[u.noise].lines += u.changed_lines;
  }
  const meter = (className, value) =>
    el("div", { class: `meter-part ${className}`, style: { width: `${(100 * value) / (lines.total || 1)}%` } });
  return el("div", { class: "card" },
    el("h2", { text: "Changed lines" }),
    el("div", { class: "meter" }, meter("reviewed", lines.reviewed), meter("hidden", lines.hidden)),
    el("div", { class: "meter-legend" },
      el("span", {}, el("i", { class: "dot", style: { background: "var(--state-seen)" } }),
        el("b", { text: `${percent(lines.reviewed, lines.total)}%` }), ` reviewed (${n(lines.reviewed)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--tier-hidden)" } }),
        el("b", { text: `${percent(lines.hidden, lines.total)}%` }), ` hidden by rules (${n(lines.hidden)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--border-strong)" } }),
        el("b", { text: `${percent(lines.left, lines.total)}%` }), ` left (${n(lines.left)} lines)`),
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

  if (nodes.length && window.mermaid) {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "neutral" });
    try {
      const { svg } = await window.mermaid.render("arch-graph", arch.mermaid);
      diagram.innerHTML = svg;
    } catch (error) {
      diagram.replaceChildren(el("pre", { text: arch.mermaid }));
    }
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
    todo: data.units.filter((u) => !u.noise && u.state !== "seen").length,
    changed: data.units.filter((u) => !u.noise && u.state === "changed").length,
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

  const sections = [];
  for (const tier of TIERS) {
    if (tier.key === "hidden" && ui.filter !== "all") continue;
    const units = data.units.filter((u) => tierOf(u) === tier.key && matchesFilter(u));
    if (!units.length) continue;
    const open = !ui.closedTiers.has(tier.key);
    sections.push(
      el("section", { class: "tier", id: `tier-${tier.key}`, dataset: { open: String(open) } },
        el("button", { class: "tier-head", "aria-expanded": String(open), onclick: () => toggleTier(tier.key) },
          el("span", { class: "caret", text: "▾" }),
          el("span", { class: "name", style: { color: `var(--tier-${tier.key})` }, text: tier.label }),
          el("span", { class: "count", text: units.length }),
          el("span", { class: "desc", text: tier.desc }),
        ),
        el("div", { class: "tier-body" }, open ? units.map(unitCard) : []),
      ),
    );
  }
  panel.replaceChildren(toolbar, ...(sections.length ? sections : [el("div", { class: "card empty", text: "Nothing here." })]));
  if (ui.focus) setFocus(ui.focus, false);
}

function unitCard(u) {
  const open = ui.open.has(u.qualname);
  const tier = tierOf(u);
  const state = STATES[u.state];
  const card = el("article", {
    class: `unit ${u.state === "seen" && !u.noise ? "seen" : ""}`,
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
  for (const signal of u.signals) chips.push(el("span", { class: "chip signal", text: signal }));
  if (u.flagged) chips.push(el("span", { class: "chip removed", text: "⚑ flagged" }));

  const head = el("div", { class: "unit-head", onclick: (e) => { if (!e.target.closest("button")) toggleUnit(u.qualname); } },
    el("span", { class: `state ${u.noise ? "" : u.state}`, title: u.noise ? "Hidden" : state.title, text: u.noise ? "·" : state.icon }),
    el("div", { class: "unit-title" },
      el("span", { class: "qualname" }, module && el("span", { class: "module", text: module }), el("span", { class: "symbol", text: symbol })),
      chips,
    ),
    u.noise
      ? el("span")
      : el("div", { class: "unit-actions" },
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
            text: u.state === "seen" ? "✓ Reviewed" : "Mark reviewed",
            onclick: () => updateReview(u, { reviewed: u.state !== "seen" }, true),
          }),
        ),
    u.noise
      ? el("p", { class: "unit-summary none", text: NOISE[u.noise] || u.noise })
      : el("p", { class: `unit-summary ${u.summary ? "" : "none"}` }, u.summary ? rich(u.summary) : "Not ranked yet."),
  );
  card.append(head);
  if (open) card.append(unitBody(u));
  return card;
}

function unitBody(u) {
  const body = el("div", { class: "unit-body" });
  if (u.reason) body.append(el("p", { class: "reason" }, el("b", { text: `Why ${tierOf(u)}: ` }), rich(u.reason)));
  if (u.old_qualname || u.old_path) {
    body.append(el("p", { class: "moved", text: `${u.status === "renamed" ? "Renamed" : "Moved"} from ${u.old_qualname || u.qualname}${u.old_path ? ` (${u.old_path})` : ""}` }));
  }
  let rows = u.diff;
  if (u.since_review) {
    const view = ui.view.get(u.qualname) || "since";
    rows = view === "since" ? u.since_review : u.diff;
    body.append(
      el("div", { class: "segmented", role: "group" },
        [["since", "Since your review"], ["full", "Full diff"]].map(([key, label]) =>
          el("button", {
            "aria-pressed": String(view === key),
            text: label,
            onclick: () => { ui.view.set(u.qualname, key); rerenderUnit(u.qualname); },
          }),
        ),
      ),
    );
  }
  body.append(renderDiff(rows, u.language, u.qualname));
  return body;
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

function renderDiff(rows, language, key) {
  const tbody = el("tbody");
  const kinds = { " ": "ctx", "-": "del", "+": "add" };
  let i = 0;
  while (i < rows.length) {
    if (rows[i][0] === " ") {
      let end = i;
      while (end < rows.length && rows[end][0] === " ") end++;
      const gapKey = `${key}:${i}`;
      if (end - i >= GAP_MIN && !ui.gaps.has(gapKey)) {
        const keepStart = i === 0 ? 0 : GAP_KEEP;
        const keepEnd = end === rows.length ? 0 : GAP_KEEP;
        for (let k = i; k < i + keepStart; k++) tbody.append(diffRow(rows[k], kinds, language));
        const hidden = end - i - keepStart - keepEnd;
        tbody.append(
          el("tr", { class: "gap", onclick: () => { ui.gaps.add(gapKey); rerenderUnit(key); } },
            el("td", { colspan: "3", text: `⋯ ${plural(hidden, "unchanged line")}` }),
          ),
        );
        for (let k = end - keepEnd; k < end; k++) tbody.append(diffRow(rows[k], kinds, language));
        i = end;
        continue;
      }
    }
    tbody.append(diffRow(rows[i], kinds, language));
    i++;
  }
  return el("div", { class: "diff-wrap" }, el("table", { class: "diff" }, tbody));
}

function diffRow([kind, oldLine, newLine, text], kinds, language) {
  const code = el("code");
  highlight(code, text, language);
  return el("tr", { class: kinds[kind] },
    el("td", { class: "ln", text: oldLine ?? "" }),
    el("td", { class: "ln", text: newLine ?? "" }),
    el("td", { class: "code" }, el("span", { class: "sign", text: kind === " " ? "" : kind === "-" ? "−" : "+" }), code),
  );
}

// State changes

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
    renderOverview();
    renderChanges();
    if (next) setFocus(next);
  } catch (error) {
    toast(`Couldn't save: ${error.message}`);
  }
}

// Navigation

function selectTab(name) {
  if (!TABS.includes(name)) name = "overview";
  ui.tab = name;
  for (const tab of TABS) {
    document.getElementById(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    document.getElementById(`panel-${tab}`).hidden = tab !== name;
  }
  history.replaceState(null, "", `#${name}`);
}

function revealTier(key) {
  ui.filter = "all";
  ui.closedTiers.delete(key);
  savePrefs();
  renderChanges();
  selectTab("changes");
  document.getElementById(`tier-${key}`)?.scrollIntoView({ block: "start" });
}

function revealUnit(qualname) {
  const unit = data.units.find((u) => u.qualname === qualname);
  if (!unit) return;
  if (!matchesFilter(unit)) ui.filter = "all";
  ui.closedTiers.delete(tierOf(unit));
  ui.open.add(qualname);
  renderChanges();
  selectTab("changes");
  setFocus(qualname);
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
      if (unit && !unit.noise) updateReview(unit, { reviewed: unit.state !== "seen" }, true);
      break;
    case "f":
      if (unit && !unit.noise) updateReview(unit, { flagged: !unit.flagged });
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
  window.addEventListener("hashchange", () => { if (data) selectTab(location.hash.slice(1)); });

  try {
    data = await api("GET", "/api/pr");
  } catch (error) {
    document.getElementById("title").textContent = `Couldn't load the report: ${error.message}`;
    return;
  }

  // Open the units most worth reading first.
  const worth = data.units.filter((u) => !u.noise && u.state !== "seen" && OPEN_BY_DEFAULT.has(tierOf(u)));
  for (const u of worth.slice(0, MAX_AUTO_OPEN)) ui.open.add(u.qualname);

  renderHeader();
  renderProgress();
  renderBanner();
  renderOverview();
  renderInterfaces();
  renderChanges();
  selectTab(location.hash.slice(1) || (data.summary ? "overview" : "changes"));
  await renderArchitecture();
}

main();
