// The report page: renders /api/pr and keeps review state in sync.

const TOKEN = document.querySelector('meta[name="reviewer-token"]').content;

const TIERS = [
  { key: "architecture", label: "Architecture", desc: "Structure and dependencies between components" },
  { key: "interface", label: "Interface", desc: "What callers and clients depend on" },
  { key: "behavior", label: "Behavior", desc: "Logic worth understanding" },
  { key: "detail", label: "Detail", desc: "Local changes that don't change what a component does" },
  { key: "unranked", label: "Not ranked yet", desc: "Run /review-pr to rank these" },
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
    has: (u) => !u.noise && u.attention === "skim", lines: (u) => u.changed_lines,
  },
  {
    key: "hidden", label: "Hidden", desc: "Noise: nothing a reviewer needs to see",
    has: (u) => Boolean(u.noise), lines: (u) => u.changed_lines,
  },
];
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
  unfolded: new Set(), // folds shown as lines, by "qualname:start"
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
// The Changes section a unit is listed in: its tier when it's to be read.
const sectionOf = (u) => (u.noise ? "hidden" : u.attention === "skim" ? "skim" : u.tier || "unranked");
const isRead = (u) => !u.noise && u.attention !== "skim";
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
    hidden: sum(data.units.filter((u) => u.noise)),
    skim: sum(data.units.filter((u) => !u.noise && u.attention === "skim")) + sum(read, foldedLines),
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
    `${n(lines.left)} left), ${n(lines.skim)} to skim, ${n(lines.hidden)} hidden by noise rules. ` +
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
    const count = units.filter((u) => sectionOf(u) === tier.key).length;
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
    el("div", { class: "meter" }, meter("reviewed", lines.reviewed), meter("skim", lines.skim), meter("hidden", lines.hidden)),
    el("div", { class: "meter-legend" },
      el("span", {}, el("i", { class: "dot", style: { background: "var(--state-seen)" } }),
        el("b", { text: `${percent(lines.reviewed, lines.total)}%` }), ` reviewed (${n(lines.reviewed)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--border-strong)" } }),
        el("b", { text: `${percent(lines.left, lines.total)}%` }), ` left to read (${n(lines.left)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--tier-skim)" } }),
        el("b", { text: `${percent(lines.skim, lines.total)}%` }), ` to skim: folded or summarized (${n(lines.skim)} lines)`),
      el("span", {}, el("i", { class: "dot", style: { background: "var(--tier-hidden)" } }),
        el("b", { text: `${percent(lines.hidden, lines.total)}%` }), ` hidden by rules (${n(lines.hidden)} lines)`),
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
    class: `unit ${u.state === "seen" && !u.noise ? "seen" : ""} ${compact ? "compact" : ""}`,
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

  const toggle = (e) => { if (!e.target.closest("button")) toggleUnit(u.qualname); };
  const actions = u.noise
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
          text: u.state === "seen" ? "✓" : compact ? "Reviewed?" : "Mark reviewed",
          onclick: () => updateReview(u, { reviewed: u.state !== "seen" }, true),
        }),
      );
  if (compact) {
    // One line: name, summary, and the reason to skim on hover.
    const summary = u.noise ? NOISE[u.noise] || u.noise : u.summary;
    card.append(
      el("div", { class: "unit-head", onclick: toggle, title: u.attention === "skim" && u.reason ? `Why skim: ${u.reason}` : null },
        el("span", { class: `state ${u.noise ? "" : u.state}`, title: u.noise ? "Hidden" : state.title, text: u.noise ? "·" : state.icon }),
        el("div", { class: "unit-line" },
          el("span", { class: "qualname" }, el("span", { class: "symbol", text: symbol })),
          u.highlights.length > 0 && el("span", { class: "chip highlight", text: `⚠ ${u.highlights.length}` }),
          u.flagged && el("span", { class: "chip removed", text: "⚑" }),
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
    el("span", { class: `state ${u.noise ? "" : u.state}`, title: u.noise ? "Hidden" : state.title, text: u.noise ? "·" : state.icon }),
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
  if (u.reason) body.append(el("p", { class: "reason" }, el("b", { text: u.attention === "skim" ? "Why skim: " : `Why ${tierOf(u)}: ` }), rich(u.reason)));
  if (u.highlights.length) body.append(callouts(u));
  if (u.old_qualname || u.old_path) {
    body.append(el("p", { class: "moved", text: `${u.status === "renamed" ? "Renamed" : "Moved"} from ${u.old_qualname || u.qualname}${u.old_path ? ` (${u.old_path})` : ""}` }));
  }
  let rows = u.diff;
  let folds = u.folds;
  if (u.since_review) {
    const view = ui.view.get(u.qualname) || "since";
    rows = view === "since" ? u.since_review : u.diff;
    if (view === "since") folds = [];
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
  body.append(renderDiff(rows, u.language, u.qualname, folds, u.highlights));
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

// The diff as tables of lines, with folds as blocks between them.
function renderDiff(rows, language, key, folds = [], highlights = []) {
  const wrap = el("div", { class: "diff-wrap" });
  let tbody = el("tbody");
  const flush = () => {
    if (tbody.children.length) wrap.append(el("div", { class: "diff-scroll" }, el("table", { class: "diff" }, tbody)));
    tbody = el("tbody");
  };
  const kinds = { " ": "ctx", "-": "del", "+": "add" };
  const notes = new Map(highlights.map((h) => [h.line, h.note]));
  const foldAt = new Map(folds.filter((f) => !ui.unfolded.has(`${key}:${f.start}`)).map((f) => [f.start, f]));
  const lineOf = (row) => (row[0] === "-" ? row[1] : row[2]);
  const emit = (row) => {
    tbody.append(diffRow(row, kinds, language, key, notes));
    const note = notes.get(lineOf(row));
    if (note) tbody.append(el("tr", { class: "hl-note" }, el("td", { colspan: "3" }, "⚠ ", rich(note))));
  };
  let i = 0;
  while (i < rows.length) {
    const fold = rows[i][2] !== null && foldAt.get(rows[i][2]);
    if (fold) {
      flush();
      wrap.append(foldView(fold, key, notes));
      while (i < rows.length && rows[i][2] !== null && rows[i][2] <= fold.end) i++;
      continue;
    }
    if (rows[i][0] === " ") {
      let end = i;
      while (end < rows.length && rows[end][0] === " " && !foldAt.has(rows[end][2])) end++;
      const gapKey = `${key}:${i}`;
      const highlighted = rows.slice(i, end).some((row) => notes.has(lineOf(row)));
      if (end - i >= GAP_MIN && !ui.gaps.has(gapKey) && !highlighted) {
        const keepStart = i === 0 ? 0 : GAP_KEEP;
        const keepEnd = end === rows.length || foldAt.has(rows[end]?.[2]) ? 0 : GAP_KEEP;
        for (let k = i; k < i + keepStart; k++) emit(rows[k]);
        const hidden = end - i - keepStart - keepEnd;
        tbody.append(
          el("tr", { class: "gap", onclick: () => { ui.gaps.add(gapKey); rerenderUnit(key); } },
            el("td", { colspan: "3", text: `⋯ ${plural(hidden, "unchanged line")}` }),
          ),
        );
        for (let k = end - keepEnd; k < end; k++) emit(rows[k]);
        i = end;
        continue;
      }
    }
    emit(rows[i]);
    i++;
  }
  flush();
  return wrap;
}

function diffRow(row, kinds, language, key, notes) {
  const [kind, oldLine, newLine, text] = row;
  const line = kind === "-" ? oldLine : newLine;
  const code = el("code");
  highlight(code, text, language);
  return el("tr", { class: `${kinds[kind]} ${notes.has(line) ? "hl" : ""}`, id: line != null ? `line-${key}-${line}` : null },
    el("td", { class: "ln", text: oldLine ?? "" }),
    el("td", { class: "ln", text: newLine ?? "" }),
    el("td", { class: "code" }, el("span", { class: "sign", text: kind === " " ? "" : kind === "-" ? "−" : "+" }), code),
  );
}

// A fold: repetitive lines shown as a table, or as a list of values.
function foldView(fold, key, notes) {
  const lines = fold.end - fold.start + 1;
  // The highlighted line inside a row, if any.
  const rowLine = (row) => {
    if (row.start == null) return null;
    for (let line = row.start; line <= row.end; line++) if (notes.has(line)) return line;
    return null;
  };
  const rowNote = (row) => notes.get(rowLine(row)) ?? null;
  const head = el("div", { class: "fold-head" },
    el("span", { class: "fold-icon", text: "▤" }),
    fold.title && el("code", { class: "inline", text: fold.title }),
    el("span", { class: "fold-summary", text: fold.summary }),
    el("span", { class: "muted", text: `· ${plural(lines, "line")} folded` }),
    el("button", { class: "button small", text: "Show lines", onclick: () => { ui.unfolded.add(`${key}:${fold.start}`); rerenderUnit(key); } }),
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
  const group = ["skim", "hidden"].includes(key) ? key : "read";
  ui.closedTiers.delete(group);
  ui.filter = "all";
  ui.closedTiers.delete(key);
  savePrefs();
  renderChanges();
  selectTab("changes");
  (document.getElementById(`tier-${key}`) ?? document.getElementById(`group-${key}`))?.scrollIntoView({ block: "start" });
}

function revealUnit(qualname) {
  const unit = data.units.find((u) => u.qualname === qualname);
  if (!unit) return;
  if (!matchesFilter(unit)) ui.filter = "all";
  ui.closedTiers.delete(sectionOf(unit));
  ui.closedTiers.delete(GROUPS.find((g) => g.has(unit)).key);
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
  const worth = data.units.filter((u) => !u.noise && u.state !== "seen" && OPEN_BY_DEFAULT.has(sectionOf(u)));
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
