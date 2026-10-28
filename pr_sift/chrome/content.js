// The PR Sift card on GitHub pull request pages: what pr-sift knows about
// the PR, with buttons to sift it and to open its report. It lives in a
// shadow root, so GitHub's styles and ours stay apart; colors come from
// GitHub's own theme variables, which reach into it.

(() => {
  const PR_PATH = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)(?:\/|$)/;
  const POLL_MS = 3000;
  const INSTALL = "pr-sift extension install";

  const STYLE = `
    :host { all: initial; }
    .card {
      position: fixed; right: 16px; bottom: 16px; z-index: 2147483000;
      width: 300px; box-sizing: border-box; padding: 12px 14px;
      font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
      color: var(--fgColor-default, #1f2328);
      background: var(--bgColor-default, #ffffff);
      border: 1px solid var(--borderColor-default, #d1d9e0);
      border-radius: 12px;
      box-shadow: var(--shadow-floating-large, 0 8px 24px rgba(66, 74, 83, 0.2));
    }
    .card.collapsed { width: auto; padding: 6px 12px 6px 10px; border-radius: 999px; cursor: pointer; }
    .head { display: flex; align-items: center; gap: 6px; font-weight: 600; }
    .head .title { flex: 1; }
    .collapsed .head .title { flex: none; }
    .logo { width: 16px; height: 16px; flex: none; color: var(--fgColor-success, #1a7f37); }
    .pill { font-weight: 400; color: var(--fgColor-muted, #59636e); }
    .icon {
      border: 0; background: none; padding: 2px 4px; border-radius: 6px; cursor: pointer;
      color: var(--fgColor-muted, #59636e); font: inherit; line-height: 1;
    }
    .icon:hover { background: var(--bgColor-neutral-muted, rgba(129, 139, 152, 0.12)); }
    .body { margin-top: 6px; }
    .line { margin: 2px 0; }
    .muted { color: var(--fgColor-muted, #59636e); }
    .warn { color: var(--fgColor-attention, #9a6700); }
    .error { color: var(--fgColor-danger, #d1242f); white-space: pre-wrap; word-break: break-word; max-height: 8em; overflow: auto; }
    code {
      font: 12px ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      padding: 1px 4px; border-radius: 4px; background: var(--bgColor-neutral-muted, rgba(129, 139, 152, 0.12));
    }
    .bar { height: 6px; margin: 8px 0 2px; border-radius: 3px; overflow: hidden; background: var(--bgColor-neutral-muted, rgba(129, 139, 152, 0.2)); }
    .bar > div { height: 100%; background: var(--bgColor-success-emphasis, #1f883d); }
    .actions { display: flex; gap: 8px; margin-top: 10px; }
    button.btn {
      font: inherit; font-weight: 500; padding: 4px 12px; border-radius: 6px; cursor: pointer;
      color: var(--button-default-fgColor-rest, #25292e);
      background: var(--button-default-bgColor-rest, #f6f8fa);
      border: 1px solid var(--button-default-borderColor-rest, #d1d9e0);
    }
    button.btn:hover { background: var(--button-default-bgColor-hover, #eff2f5); }
    button.primary {
      color: var(--button-primary-fgColor-rest, #ffffff);
      background: var(--button-primary-bgColor-rest, #1f883d);
      border-color: var(--button-primary-borderColor-rest, rgba(31, 35, 40, 0.15));
    }
    button.primary:hover { background: var(--button-primary-bgColor-hover, #1c8139); }
    button:disabled { opacity: 0.6; cursor: default; }
    .spinner {
      width: 10px; height: 10px; display: inline-block; vertical-align: -1px; margin-right: 6px;
      border: 2px solid var(--borderColor-default, #d1d9e0); border-top-color: var(--fgColor-accent, #0969da);
      border-radius: 50%; animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  `;
  // A sieve: a funnel with holes.
  const LOGO = `<svg class="logo" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
    <path d="M1.5 2h13a.5.5 0 0 1 .4.8L10 9.2V14a.5.5 0 0 1-.8.4l-2-1.5a.5.5 0 0 1-.2-.4V9.2L1.1 2.8A.5.5 0 0 1 1.5 2Z"/>
  </svg>`;

  const mount = document.createElement("div");
  mount.id = "pr-sift";
  const root = mount.attachShadow({ mode: "open" });
  root.innerHTML = `<style>${STYLE}</style><div class="card" role="region" aria-label="PR Sift"></div>`;
  const card = root.querySelector(".card");

  let current = null; // the PR's URL
  let status = null; // the last status pr-sift gave, or {error}
  let busy = false;
  let collapsed = false;
  let timer = null;

  function prUrl() {
    const m = location.pathname.match(PR_PATH);
    return m && `https://github.com/${m[1]}/${m[2]}/pull/${m[3]}`;
  }

  async function send(message) {
    try {
      return await chrome.runtime.sendMessage({ ...message, pr: current });
    } catch {
      return { ok: false, installed: true, error: "PR Sift was updated: reload this page." };
    }
  }

  async function refresh(checkHead = false) {
    clearTimeout(timer);
    const pr = current;
    const reply = await send({ type: "status", check_head: checkHead });
    if (pr !== current) return; // the page moved on to another PR
    // Keep what the last full check said about the head while polling.
    const head = status?.ok && !checkHead && status.head_sha === reply.head_sha
      ? { remote_head_sha: status.remote_head_sha, head_moved: status.head_moved } : {};
    status = { ...head, ...reply };
    render();
    if (reply.ok && reply.sift.state === "running") timer = setTimeout(refresh, POLL_MS);
  }

  async function act(message) {
    busy = true;
    render();
    const reply = await send(message);
    busy = false;
    if (!reply.ok) {
      status = { ...status, actionError: reply.error };
      render();
      return;
    }
    await refresh();
  }

  // Each PR gets the card once per visit; GitHub swaps pages without
  // loading them, and may replace the body while it does.
  function check() {
    const pr = prUrl();
    if (pr && !mount.isConnected) document.documentElement.append(mount);
    if (pr === current) return;
    current = pr;
    status = null;
    clearTimeout(timer);
    if (!pr) return mount.remove();
    render();
    refresh(true);
  }

  // Rendering

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "onclick") node.addEventListener("click", value);
      else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.flat().filter((c) => c != null && c !== false));
    return node;
  }

  const line = (cls, ...children) => el("div", { class: `line ${cls || ""}` }, ...children);
  const button = (label, onclick, primary = false) =>
    el("button", { class: primary ? "btn primary" : "btn", disabled: busy, onclick }, label);
  const short = (sha) => (sha || "").slice(0, 7);

  function elapsed(since) {
    const seconds = Math.max(0, Math.round((Date.now() - Date.parse(since)) / 1000));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function render() {
    card.classList.toggle("collapsed", collapsed);
    card.replaceChildren(...(collapsed ? collapsedView() : expandedView()));
  }

  function setCollapsed(value) {
    collapsed = value;
    chrome.storage.local.set({ collapsed });
    render();
  }

  function collapsedView() {
    card.onclick = () => setCollapsed(false);
    const head = el("div", { class: "head" });
    head.innerHTML = LOGO;
    head.append(el("span", { class: "title" }, "PR Sift"));
    const s = status?.ok && status;
    if (s?.sift.state === "running") head.append(el("span", { class: "pill" }, "sifting…"));
    else if (s?.analyzed) head.append(el("span", { class: "pill" }, `${s.reviewed}/${s.units} reviewed`));
    return [head];
  }

  function expandedView() {
    card.onclick = null;
    const head = el("div", { class: "head" });
    head.innerHTML = LOGO;
    head.append(
      el("span", { class: "title" }, "PR Sift"),
      el("button", { class: "icon", title: "Minimize", "aria-label": "Minimize", onclick: (e) => {
        e.stopPropagation();
        setCollapsed(true);
      } }, "–"),
    );
    return [head, el("div", { class: "body" }, ...bodyView())];
  }

  function bodyView() {
    if (!status) return [line("muted", el("span", { class: "spinner" }), "Checking…")];
    if (!status.ok && status.installed === false) {
      return [
        line("", "pr-sift isn't connected to this browser."),
        line("muted", "Run ", el("code", {}, INSTALL), " in a terminal, then try again."),
        el("div", { class: "actions" }, button("Try again", () => { status = null; render(); refresh(true); })),
      ];
    }
    if (!status.ok) {
      return [
        line("error", status.error),
        el("div", { class: "actions" }, button("Try again", () => { status = null; render(); refresh(true); })),
      ];
    }
    const s = status;
    const views = [];
    const sift = s.sift;
    if (sift.state === "running") {
      views.push(
        line("", el("span", { class: "spinner" }), `${sift.step}…`),
        line("muted", `Claude is sifting this PR · ${elapsed(sift.started_at)}`),
        el("div", { class: "actions" }, button("Cancel", () => act({ type: "cancel" }))),
      );
      return views;
    }
    if (sift.state === "failed") views.push(line("error", `Sifting failed: ${sift.error}`));
    if (sift.state === "cancelled") views.push(line("muted", "Sifting was cancelled."));
    if (s.actionError) views.push(line("error", s.actionError));

    if (!s.analyzed) {
      views.push(
        line("", "Not sifted yet."),
        line("muted", "Claude ranks the changes and writes a walkthrough. It takes a few minutes."),
        el("div", { class: "actions" }, button("Sift this PR", () => act({ type: "sift" }), true)),
      );
      return views;
    }

    const actions = [button("Open report", () => act({ type: "open" }), true)];
    if (!s.ranked) {
      views.push(line("warn", "Analyzed, but not ranked for its latest analysis."));
      actions.push(button("Sift again", () => act({ type: "sift" })));
    } else {
      views.push(line("", [`${s.read} to read`, s.skim && `${s.skim} to skim`, s.hidden && `${s.hidden} hidden`]
        .filter(Boolean).join(" · ")));
    }
    if (s.units) {
      views.push(
        el("div", { class: "bar", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": s.units, "aria-valuenow": s.reviewed },
          el("div", { style: `width: ${(100 * s.reviewed) / s.units}%` })),
        line("muted", `${s.reviewed} of ${s.units} reviewed` + (s.flagged ? ` · ${s.flagged} flagged` : "")),
      );
    }
    if (s.changed) views.push(line("warn", `${s.changed} changed since your review`));
    if (s.head_moved) {
      views.push(line("warn", `New commits since the sift (${short(s.head_sha)} → ${short(s.remote_head_sha)})`));
      if (s.ranked) actions.push(button("Sift again", () => act({ type: "sift" })));
    }
    views.push(el("div", { class: "actions" }, actions));
    return views;
  }

  // Starting, now that everything above is defined.

  chrome.storage.local.get("collapsed").then(({ collapsed: stored }) => {
    collapsed = Boolean(stored);
    render();
  });
  // Coming back from the report: show the review progress made there.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && current && status?.ok && status.sift.state !== "running") refresh();
  });
  document.addEventListener("turbo:load", check);
  setInterval(check, 1000);
  check();
})();
