/* Online Appendix site: search palette, index filters, exhibit navigation, compare. No dependencies. */
(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const store = {
    get(k) { try { return window.localStorage.getItem("oa:" + k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem("oa:" + k, v); } catch (e) { /* private mode */ } },
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const typing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");
  const sayTimers = new WeakMap();
  // Polite status update, debounced so typing does not queue one announcement per keystroke.
  function say(el, msg) {
    if (!el) return;
    clearTimeout(sayTimers.get(el));
    sayTimers.set(el, setTimeout(() => { el.textContent = msg; }, 400));
  }

  // Make everything outside `el` inert (except the nodes in `keep`) while an overlay is open; returns the undo.
  function isolate(el, keep) {
    const done = [];
    for (let n = el; n && n.parentElement && n !== document.body; n = n.parentElement) {
      for (const sib of n.parentElement.children) {
        if (sib === n || sib.inert || (keep && keep.includes(sib)) || /^(SCRIPT|STYLE|LINK)$/.test(sib.tagName)) continue;
        sib.inert = true;
        done.push(sib);
      }
    }
    return () => done.forEach((s) => { s.inert = false; });
  }

  // Controls that only work with script are hidden in the markup, so a page without JavaScript shows no dead buttons.
  $$("[data-needs-js]").forEach((el) => { el.hidden = false; });

  // ---------------------------------------------------------------- search data + matching
  let dataPromise = null;
  function loadData() {
    if (!dataPromise) {
      dataPromise = fetch("/data/search.json", { credentials: "same-origin" })
        .then((r) => r.json())
        .then((rows) => rows.map((r) => Object.assign(r, {
          _t: norm(r.t), _n: r.n.toLowerCase(), _all: norm([r.note, r.body, r.cells, r.fig, r.refs].join(" ")),
        })));
    }
    return dataPromise;
  }
  function norm(s) {
    return String(s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/−/g, "-")
      .toLowerCase();
  }
  function tokens(q) { return norm(q).split(/[\s,;]+/).filter(Boolean); }

  function search(rows, q) {
    const qq = norm(q).trim();
    if (!qq) return [];
    let kind = null;
    let rest = qq;
    const km = qq.match(/^(fig(?:ure)?s?|tab(?:le)?s?)\.?\s*(.*)$/);
    if (km) { kind = km[1].startsWith("fig") ? "Figure" : "Table"; rest = km[2]; }
    const toks = tokens(rest);
    const out = [];
    for (const r of rows) {
      if (kind && r.k !== kind) continue;
      let score = 0;
      if (!toks.length) { score = 1; }
      const numQ = toks.length === 1 ? toks[0] : "";
      if (numQ && numQ === r._n) score += 1000;
      else if (numQ && /^[a-f]\d*$/.test(numQ) && r._n.startsWith(numQ)) score += 400 - r._n.length;
      let all = true;
      for (const t of toks) {
        if (t === r._n) continue;
        const inTitle = r._t.indexOf(t);
        if (inTitle >= 0) { score += (inTitle === 0 || r._t[inTitle - 1] === " ") ? 60 : 30; continue; }
        const inAll = r._all.indexOf(t);
        if (inAll >= 0) { score += 8; continue; }
        all = false; break;
      }
      if (!all || score <= 0) continue;
      out.push({ r, score });
    }
    out.sort((a, b) => b.score - a.score || a.r.n.localeCompare(b.r.n, undefined, { numeric: true }));
    return out.map((x) => x.r);
  }

  function snippet(r, q) {
    const toks = tokens(q).filter((t) => t.length > 1 && t !== r._n && r._t.indexOf(t) < 0);
    if (!toks.length) return "";
    const fields = [["Note", r.note], ["Text", r.body], ["Table", r.cells], ["In the figure", r.fig]];
    for (const [label, src] of fields) {
      if (!src) continue;
      const i = norm(src).indexOf(toks[0]);
      if (i < 0) continue;
      const a = Math.max(0, i - 60), b = Math.min(src.length, i + 90);
      const s = (a > 0 ? "…" : "") + src.slice(a, b) + (b < src.length ? "…" : "");
      return '<span class="pal-src">' + label + "</span> " + highlight(s, toks);
    }
    return "";
  }
  function highlight(s, toks) {
    let html = esc(s);
    for (const t of toks) {
      if (t.length < 2) continue;
      const re = new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      html = html.replace(re, "<mark>$1</mark>");
    }
    return html;
  }

  // ---------------------------------------------------------------- palette
  // The input is a combobox over the result listbox: focus stays in the input, arrows move the active option.
  const pal = $("[data-palette]");
  const palInput = pal && $("[data-pal-input]", pal);
  const palList = pal && $("[data-pal-list]", pal);
  const palStatus = pal && $("[data-pal-status]", pal);
  let palIdx = 0, palRows = [], lastFocus = null, palRestore = null;
  function openPalette(initial) {
    if (!pal) return;
    if (!pal.hidden) { palInput.focus(); return; }
    closeToc();
    lastFocus = document.activeElement;
    pal.hidden = false;
    palRestore = isolate(pal);
    palInput.value = initial || "";
    palInput.focus();
    renderPalette();
  }
  function closePalette() {
    if (!pal || pal.hidden) return;
    pal.hidden = true;
    if (palRestore) { palRestore(); palRestore = null; }
    palInput.setAttribute("aria-expanded", "false");
    palInput.removeAttribute("aria-activedescendant");
    say(palStatus, "");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function renderPalette() {
    loadData().then((rows) => {
      const q = palInput.value;
      palRows = q.trim() ? search(rows, q).slice(0, 30) : [];
      palIdx = 0;
      if (!q.trim()) {
        palList.innerHTML = '<li class="pal-empty" role="presentation">Type a number such as F12, a title word, a variable name, or a heuristic.</li>';
        say(palStatus, "");
      } else if (!palRows.length) {
        palList.innerHTML = '<li class="pal-empty" role="presentation">No exhibit matches.</li>';
        say(palStatus, "No exhibit matches.");
      } else {
        palList.innerHTML = palRows.map((r, i) =>
          '<li role="option" id="pal-opt-' + i + '" aria-selected="' + (i === 0) + '"' + (i === 0 ? ' class="on"' : "") + '><a href="' + esc(r.u) + '" tabindex="-1">' +
          '<span class="pal-k">' + esc(r.k) + " " + esc(r.n) + "</span>" +
          '<span class="pal-t">' + highlight(r.t, tokens(q)) + "</span>" +
          (snippet(r, q) ? '<span class="pal-s">' + snippet(r, q) + "</span>" : "") + "</a></li>").join("");
        say(palStatus, plural(palRows.length, "exhibit") + " found.");
      }
      palInput.setAttribute("aria-expanded", palRows.length ? "true" : "false");
      if (palRows.length) palInput.setAttribute("aria-activedescendant", "pal-opt-0");
      else palInput.removeAttribute("aria-activedescendant");
    });
  }
  function movePalette(d) {
    const items = $$("li[role=option]", palList);
    if (!items.length) return;
    items[palIdx].classList.remove("on"); items[palIdx].setAttribute("aria-selected", "false");
    palIdx = (palIdx + d + items.length) % items.length;
    items[palIdx].classList.add("on"); items[palIdx].setAttribute("aria-selected", "true");
    palInput.setAttribute("aria-activedescendant", items[palIdx].id);
    items[palIdx].scrollIntoView({ block: "nearest" });
  }
  if (pal) {
    palInput.addEventListener("input", renderPalette);
    palInput.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown") { ev.preventDefault(); movePalette(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); movePalette(-1); }
      else if (ev.key === "Enter") {
        const a = $("li.on a", palList);
        if (a) { ev.preventDefault(); window.location.href = a.getAttribute("href"); }
      } else if (ev.key === "Escape") { ev.preventDefault(); closePalette(); }
      else if (ev.key === "Tab") { ev.preventDefault(); } // the input is the dialog's only stop
    });
    pal.addEventListener("click", (ev) => { if (ev.target === pal) closePalette(); });
  }
  $$("[data-open-search]").forEach((b) => b.addEventListener("click", () => openPalette("")));

  // ---------------------------------------------------------------- global keys
  const overlayOpen = () => (pal && !pal.hidden) || (toc && toc.classList.contains("open"));
  document.addEventListener("keydown", (ev) => {
    if (ev.defaultPrevented) return;
    const k = ev.key;
    if ((k === "k" || k === "K") && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); openPalette(""); return; }
    if (typing(ev.target)) return;
    if (k === "/" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) { ev.preventDefault(); openPalette(""); return; }
    if (k === "Escape") { closePalette(); closeToc(); return; }
    const art = $("[data-exhibit]");
    if (art && !ev.altKey && !ev.ctrlKey && !ev.metaKey && !ev.shiftKey) {
      if (overlayOpen()) return;
      if (ev.target && ev.target.closest && ev.target.closest(".tscroll")) return;
      if (k === "ArrowLeft" && art.dataset.prev) { window.location.href = art.dataset.prev; }
      if (k === "ArrowRight" && art.dataset.next) { window.location.href = art.dataset.next; }
    }
  });

  // ---------------------------------------------------------------- index page
  const ix = $("#contents");
  if (ix) {
    const qv = new URLSearchParams(window.location.search).get("view");
    let kind = "all", ids = null;
    let groupBy = qv === "paper" || qv === "section" ? qv : (store.get("groupBy") === "paper" ? "paper" : "section");
    const views = { section: $('[data-view="section"]', ix), paper: $('[data-view="paper"]', ix) };
    const filter = $("[data-ix-filter]", ix);
    const empty = $("[data-ix-empty]", ix);
    const status = $("[data-ix-status]", ix);
    let announce = false; // only kind and text filters change what is listed; say so after those
    function apply() {
      views.section.hidden = groupBy !== "section";
      views.paper.hidden = groupBy !== "paper";
      $$("[data-group-by]", ix).forEach((b) => { const on = b.dataset.groupBy === groupBy; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
      $$("[data-kind-filter]", ix).forEach((b) => { const on = b.dataset.kindFilter === kind; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
      const view = views[groupBy];
      let shown = 0;
      $$(".te-x[data-id]", view).forEach((row) => {
        const ok = (kind === "all" || row.dataset.kind === kind) && (!ids || ids.has(row.dataset.id));
        row.hidden = !ok;
        if (ok) shown++;
      });
      const filtering = kind !== "all" || !!ids;
      $$("[data-text-row]", view).forEach((row) => { row.hidden = filtering; });
      $$("[data-sub-head]", view).forEach((head) => {
        head.hidden = filtering && !$$('.te-x[data-sub="' + head.dataset.subHead + '"]', view).some((r) => !r.hidden);
      });
      $$(".ix-sec", view).forEach((sec) => {
        sec.hidden = filtering && !$$(".te-x[data-id]", sec).some((r) => !r.hidden);
      });
      empty.hidden = shown > 0;
      if (announce) say(status, shown ? plural(shown, "exhibit") + " shown." : "No figures or tables match.");
      announce = false;
    }
    $$("[data-kind-filter]", ix).forEach((b) => b.addEventListener("click", () => { kind = b.dataset.kindFilter; announce = true; apply(); }));
    $$("[data-group-by]", ix).forEach((b) => b.addEventListener("click", () => {
      groupBy = b.dataset.groupBy; store.set("groupBy", groupBy);
      history.replaceState(null, "", groupBy === "paper" ? "?view=paper#contents" : window.location.pathname + "#contents");
      apply();
    }));
    if (filter) {
      filter.addEventListener("input", () => {
        const q = filter.value;
        announce = true;
        if (!q.trim()) { ids = null; apply(); return; }
        loadData().then((rows) => { ids = new Set(search(rows, q).map((r) => r.id)); announce = true; apply(); });
      });
    }
    apply();
  }

  // ---------------------------------------------------------------- exhibit page
  // Below 1200px the contents list is a drawer: while it is open the page behind is inert.
  const toc = $("[data-toc]");
  const scrim = $(".toc-scrim");
  const tocBtn = $("[data-toc-open]");
  const wide = window.matchMedia("(min-width: 1200px)");
  let tocRestore = null;
  function openToc() {
    if (!toc || wide.matches) return;
    toc.classList.add("open");
    if (scrim) scrim.hidden = false;
    if (tocBtn) tocBtn.setAttribute("aria-expanded", "true");
    tocRestore = isolate(toc, [scrim]);
    const cur = $('[aria-current="page"]', toc);
    const visibleCur = cur && cur.getClientRects().length ? cur : null;
    if (visibleCur) visibleCur.scrollIntoView({ block: "center" });
    (visibleCur || $(".toc-head a", toc)).focus({ preventScroll: true });
  }
  function closeToc() {
    if (!toc || !toc.classList.contains("open")) return;
    const hadFocus = toc.contains(document.activeElement);
    toc.classList.remove("open");
    if (scrim) scrim.hidden = true;
    if (tocBtn) tocBtn.setAttribute("aria-expanded", "false");
    if (tocRestore) { tocRestore(); tocRestore = null; }
    if (hadFocus && tocBtn) tocBtn.focus();
  }
  $$("[data-toc-open]").forEach((b) => b.addEventListener("click", openToc));
  $$("[data-toc-close]").forEach((b) => b.addEventListener("click", closeToc));
  wide.addEventListener("change", () => { if (wide.matches) closeToc(); });
  if (toc) { const cur = $('[aria-current="page"]', toc); if (cur && wide.matches) cur.scrollIntoView({ block: "center" }); }


  // ---------------------------------------------------------------- compare
  const cmp = $("[data-compare]");
  if (cmp) {
    const sel = { a: $('[data-cmp-select="a"]', cmp), b: $('[data-cmp-select="b"]', cmp) };
    const pane = { a: $('[data-cmp-pane="a"]', cmp), b: $('[data-cmp-pane="b"]', cmp) };
    const sync = $("[data-cmp-sync]", cmp), notes = $("[data-cmp-notes]", cmp), sug = $("[data-cmp-suggest]", cmp);
    const status = $("[data-cmp-status]", cmp), flip = $("[data-cmp-flip]", cmp), panes = $(".cmp-panes", cmp);
    const cache = {};
    const url = (v) => "/" + v.replace("-", "/") + "/";
    function fetchExhibit(v) {
      if (!cache[v]) {
        cache[v] = fetch(url(v), { credentials: "same-origin" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.text(); })
          .then((html) => new DOMParser().parseFromString(html, "text/html"));
      }
      return cache[v];
    }
    function show(side, tell) {
      const v = sel[side].value;
      const box = pane[side];
      const where = side === "a" ? "on the left" : "on the right";
      if (!v) { box.innerHTML = '<p class="cmp-empty">Choose an exhibit ' + where + ".</p>"; return; }
      fetchExhibit(v).then((doc) => {
        const art = doc.querySelector("article[data-exhibit]");
        if (!art) throw new Error("no exhibit");
        const frag = document.createElement("div");
        ["h1.ex-title", "[data-ex-body]"].forEach((s) => { const n = art.querySelector(s); if (n) frag.appendChild(document.importNode(n, true)); });
        $$(".note", frag).forEach((n) => { n.hidden = !notes.checked; n.classList.add("cmp-note"); });
        $$("[id]", frag).forEach((n) => n.removeAttribute("id"));
        const open = document.createElement("a");
        open.className = "open"; open.href = url(v); open.textContent = "Open the exhibit page";
        frag.appendChild(open);
        box.innerHTML = ""; box.appendChild(frag);
        if (tell) say(status, sideName(side) + " is shown " + where + ".");
      }).catch(() => {
        box.innerHTML = '<p class="cmp-empty">This exhibit could not be loaded.</p>';
        if (tell) say(status, "The exhibit " + where + " could not be loaded.");
      });
    }
    // Narrow screens stack the panes; the flip control shows one at a time and keeps the scroll position.
    const sideName = (s) => { const o = sel[s].selectedOptions[0]; return o && o.value ? o.textContent.split(":")[0].trim() : (s === "a" ? "Left" : "Right"); };
    function updateFlip() {
      const both = !!(sel.a.value && sel.b.value);
      flip.hidden = !both;
      if (!both) { delete panes.dataset.show; return; }
      if (!panes.dataset.show) panes.dataset.show = "a";
      $$("[data-cmp-show]", flip).forEach((b) => {
        const on = b.dataset.cmpShow === panes.dataset.show;
        b.textContent = sideName(b.dataset.cmpShow);
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on);
      });
    }
    flip.addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-cmp-show]");
      if (!b) return;
      const y = window.scrollY;
      panes.dataset.show = b.dataset.cmpShow;
      updateFlip();
      window.scrollTo(0, y);
    });
    function updateUrl() {
      const p = new URLSearchParams();
      if (sel.a.value) p.set("a", sel.a.value);
      if (sel.b.value) p.set("b", sel.b.value);
      history.replaceState(null, "", "?" + p.toString());
      updateFlip();
      suggest();
    }
    function suggest() {
      loadData().then((rows) => {
        const a = sel.a.value;
        const row = rows.find((r) => r.id === (a ? a.replace("-", "/") : ""));
        if (!row || !row.sug.length) { sug.hidden = true; return; }
        const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
        sug.innerHTML = '<span class="cmp-sug-lead">Often compared with ' + esc(row.k) + " " + esc(row.n) + ":</span> " + row.sug.map((id) => {
          const r = byId[id];
          return r ? '<button type="button" data-pick="' + esc(id.replace("/", "-")) + '">' + esc(r.k + " " + r.n) + "</button>" : "";
        }).join(" ");
        sug.hidden = false;
      });
    }
    sug.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-pick]");
      if (!b) return;
      sel.b.value = b.dataset.pick; panes.dataset.show = "b"; show("b", true); updateUrl();
    });
    ["a", "b"].forEach((s) => sel[s].addEventListener("change", () => { panes.dataset.show = s; show(s, true); updateUrl(); }));
    $("[data-cmp-swap]", cmp).addEventListener("click", () => {
      const t = sel.a.value; sel.a.value = sel.b.value; sel.b.value = t; show("a"); show("b"); updateUrl();
      say(status, "Swapped: " + sideName("a") + " on the left, " + sideName("b") + " on the right.");
    });
    notes.addEventListener("change", () => { $$(".cmp-note", cmp).forEach((n) => { n.hidden = !notes.checked; }); });
    let lock = false;
    ["a", "b"].forEach((s) => pane[s].addEventListener("scroll", () => {
      if (!sync.checked || lock) return;
      const o = pane[s === "a" ? "b" : "a"];
      lock = true;
      const src = pane[s];
      const f = src.scrollTop / Math.max(1, src.scrollHeight - src.clientHeight);
      o.scrollTop = f * (o.scrollHeight - o.clientHeight);
      requestAnimationFrame(() => { lock = false; });
    }));
    const q = new URLSearchParams(window.location.search);
    ["a", "b"].forEach((s) => {
      const v = q.get(s);
      if (v && $$("option", sel[s]).some((o) => o.value === v)) { sel[s].value = v; show(s); }
    });
    updateFlip();
    suggest();
  }
})();
