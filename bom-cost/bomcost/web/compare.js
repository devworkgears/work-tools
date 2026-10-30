// ③原価比較: 上に検討案ごとの数字のタイル、左に製品ごとの差の横棒、右に選んだ製品の6費目と変更の一覧。
// 差・合計は API が Decimal で出したものを使い、ここで数にするのは棒の長さと並べ替えだけ。app.js・screens.js の関数を使う。
"use strict";

// layout は グラフ（chart）か 表（table）。画面を開いている間は覚える。data・table はそれぞれの API の返事（要る時に読む）
const compareState = { query: null, data: null, table: null, layout: "chart", head: "MANUFACTURING", product: null };

async function enterCompare() {
  const version = document.getElementById("cmp-version");
  versionOptions(version, { value: version.value || state.summary?.scenario.version_id });
  await loadCompareNames();
}

async function loadCompareNames() {
  const version = document.getElementById("cmp-version").value;
  const box = document.getElementById("cmp-names");
  const checked = new Set([...box.querySelectorAll("input:checked")].map((i) => i.value));
  const names = version ? await api("GET", `/api/group/names?${qs({ version })}`) : [];
  box.replaceChildren(...names.map((n) =>
    el("label", {}, el("input", { type: "checkbox", value: n, checked: checked.has(n) }), ` ${n}`)));
}

async function drawCompareTable() {
  const version = document.getElementById("cmp-version").value;
  if (!version) throw new Error("版が空");
  const names = [...document.querySelectorAll("#cmp-names input:checked")].map((i) => i.value);
  if (!names.length) throw new Error("検討案を選ぶ");
  const ignore = document.getElementById("cmp-ignore").checked;
  compareState.query = {
    version, names: JSON.stringify(names), q: document.getElementById("cmp-q").value.trim(),
    ignore_amortization: ignore ? "1" : "",
  };
  compareState.data = null;
  compareState.table = null;
  compareState.product = null;
  await Promise.all([loadCompareLayout(), loadCodeNames(version)]);
  drawCompareView();
}

async function loadCompareLayout() {
  const query = qs(compareState.query);
  if (compareState.layout === "table") {
    compareState.table ??= await api("GET", `/api/group/compare/table?${query}`);
  } else {
    compareState.data ??= await api("GET", `/api/group/compare?${query}`);
  }
}

// ファイル名は API の Content-Disposition（filename*）から取る。api() は応答の見出しを返さないので fetch を直に使う
async function exportCompareXlsx() {
  const res = await fetch(`/api/group/compare/table/xlsx?${qs(compareState.query)}`);
  if (!res.ok) {
    const type = res.headers.get("Content-Type") || "";
    throw new Error((type.includes("json") && (await res.json()).error) || `${res.status}`);
  }
  const named = (res.headers.get("Content-Disposition") || "").match(/filename\*=UTF-8''([^;]+)/);
  const url = URL.createObjectURL(await res.blob());
  const a = el("a", { href: url, download: named ? decodeURIComponent(named[1]) : "bom-cost-compare.xlsx" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function compareLayoutSeg() {
  const button = (value, label) => el("button", {
    type: "button", class: compareState.layout === value ? "on" : "",
    onclick: () => guarded(async () => {
      compareState.layout = value;
      await loadCompareLayout();
      drawCompareView();
    }),
  }, label);
  return el("div", { class: "seg cmp-layout" }, button("chart", "グラフ"), button("table", "表"));
}

// 製品ごとに、元の行と検討名 → 新の行をまとめる（API の並びのまま）
function compareProducts(r) {
  const products = [];
  for (const row of r.rows) {
    if (row.kind === "base") products.push({ code: row.product, name: row.product_name, base: row, trials: {} });
    else products[products.length - 1].trials[row.name] = row;
  }
  return products;
}

function seriesClass(r, name) {
  return r.names.length > 1 ? `series s${r.names.indexOf(name) % 4}` : "";
}

function diffNumber(text) {
  return text === null || text === undefined ? null : Number(text);
}

function isIgnored(r, head) {
  return head === "AMORTIZATION" && r.ignore_amortization;
}

function drawCompareView() {
  const isTable = compareState.layout === "table";
  const r = isTable ? compareState.table : compareState.data;
  const head = compareState.head;
  const headSelect = el("select", {
    onchange: (e) => { compareState.head = e.target.value; drawCompareView(); },
  }, r.heads.map((h) => el("option", { value: h.id, selected: h.id === head }, h.label)));
  const tools = el("div", { class: "cmp-tools" }, compareLayoutSeg(), el("label", {}, "費目 ", headSelect));
  if (isTable) {
    tools.append(el("button", { type: "button", onclick: () => guarded(exportCompareXlsx) }, "Excel に書き出す"));
    document.getElementById("cmp-body").replaceChildren(tools, ...compareGrid(r, head));
    return;
  }

  const products = compareProducts(r);
  const withTrial = products.filter((p) => Object.keys(p.trials).length);
  const key = (p) => Math.max(-1, ...r.names.map((n) => Math.abs(diffNumber(p.trials[n]?.differences[head]) ?? -1)));
  withTrial.sort((a, b) => key(b) - key(a));
  if (!withTrial.some((p) => p.code === compareState.product)) compareState.product = withTrial[0]?.code ?? null;

  const left = el("section", { class: "cmp-left" },
    el("div", { class: "cmp-left-head" },
      el("h3", {}, `製品ごとの差（${countText(r.count, products.length, r.truncated)}）`),
      withTrial.length < products.length ? el("span", { class: "muted" }, `試算なし ${products.length - withTrial.length}`) : null,
      r.names.length > 1 ? compareLegend(r) : null),
    compareBars(r, withTrial, head));
  const right = el("section", { class: "cmp-right", id: "cmp-detail" });
  document.getElementById("cmp-body").replaceChildren(
    tools,
    compareTiles(r, head),
    el("div", { class: "cmp-split" }, left, right),
  );
  drawCompareDetail();
}

function compareLegend(r) {
  return el("span", { class: "cmp-legend" },
    r.names.map((n) => el("span", {}, el("i", { class: `swatch ${seriesClass(r, n)}` }), n)));
}

function compareTiles(r, head) {
  const ignored = isIgnored(r, head);
  return el("div", { class: "cmp-tiles" }, r.names.map((n) => {
    const s = r.summary[n].heads[head];
    const allMissing = s.down + s.up + s.same === 0 && s.missing > 0;
    const total = ignored ? el("span", { class: "muted" }, "—")
      : allMissing ? el("span", { class: "missing" }, "未登録")
        : el("span", { class: signClass(s.difference) }, fmt(s.difference, { sign: true }));
    return el("div", { class: "tile-row" },
      r.names.length > 1 ? el("div", { class: "tile-name" }, el("i", { class: `swatch ${seriesClass(r, n)}` }), n) : null,
      tile("合計の差", total),
      tile("下がった", el("span", { class: s.down ? "neg" : "" }, s.down)),
      tile("上がった", el("span", { class: s.up ? "pos" : "" }, s.up)),
      tile("未登録", el("span", { class: s.missing ? "missing" : "" }, ignored ? "—" : s.missing)));
  }));
}

function tile(label, value) {
  return el("div", { class: "tile" }, el("div", { class: "tile-label" }, label), el("div", { class: "tile-value" }, value));
}

// 0 を中心に左（下がる）・右（上がる）へ伸ばす。max は棒の長さの基準（絶対値の最大）
function diffBar(text, max, extra) {
  const value = diffNumber(text);
  const track = el("div", { class: "bar-track" });
  if (value !== null && value !== 0) {
    const width = `${(Math.abs(value) / max) * 50}%`;
    track.append(el("span", {
      class: `bar-fill ${value < 0 ? "down" : "up"} ${extra}`,
      style: value < 0 ? `right:50%;width:${width}` : `left:50%;width:${width}`,
    }));
  }
  return track;
}

function compareBars(r, products, head) {
  if (!products.length) return el("p", { class: "muted" }, "なし");
  const values = products.flatMap((p) => r.names.map((n) => diffNumber(p.trials[n]?.differences[head])));
  const max = Math.max(...values.filter((v) => v !== null).map(Math.abs), 0) || 1;
  const ignored = isIgnored(r, head);
  return el("div", { class: "cmp-bars" }, products.map((p) => el("div", {
    class: `cmp-bar-row ${p.code === compareState.product ? "selected" : ""}`,
    "data-code": p.code,
    tabindex: "0",
    onclick: () => selectCompareProduct(p.code),
    onkeydown: (e) => e.key === "Enter" && selectCompareProduct(p.code),
  },
  el("div", { class: "cmp-bar-label" },
    el("span", { class: "code", title: codeTitle(r.version_id, p.code) }, p.code), el("span", { class: "name" }, p.name)),
  el("div", { class: "cmp-bar-lines" }, r.names.map((n) => {
    const trial = p.trials[n];
    const text = trial?.differences[head];
    const value = !trial ? el("span", { class: "muted" }, "なし")
      : ignored ? el("span", { class: "muted" }, "—")
        : el("span", { class: signClass(text) }, fmt(text, { sign: true }));
    return el("div", { class: "cmp-bar-line" }, diffBar(trial ? text : null, max, seriesClass(r, n)),
      el("span", { class: "num" }, value));
  })))));
}

function selectCompareProduct(code) {
  compareState.product = code;
  for (const row of document.querySelectorAll("#cmp-body .cmp-bar-row")) {
    row.classList.toggle("selected", row.dataset.code === code);
  }
  drawCompareDetail();
}

function drawCompareDetail() {
  const r = compareState.data;
  const box = document.getElementById("cmp-detail");
  const p = compareProducts(r).find((x) => x.code === compareState.product);
  if (!p) {
    box.replaceChildren(el("p", { class: "muted" }, "製品を選ぶ"));
    return;
  }
  const names = r.names.filter((n) => p.trials[n]);
  const multi = r.names.length > 1;
  const max = Math.max(...names.flatMap((n) => r.heads.map((h) => Math.abs(diffNumber(p.trials[n].differences[h.id]) ?? 0))), 0) || 1;
  const cell = (text, head, opts) => (isIgnored(r, head) ? el("td", { class: "num muted" }, "—") : numTd(text, opts));
  const rows = r.heads.map((h) => el("tr", { class: h.id === compareState.head ? "cmp-current-head" : "" },
    el("td", {}, h.label),
    cell(p.base.costs[h.id], h.id),
    names.map((n) => {
      const d = p.trials[n].differences[h.id];
      const diff = cell(d, h.id, { sign: true });
      if (signClass(d) && !isIgnored(r, h.id)) diff.classList.add(signClass(d));
      return [cell(p.trials[n].costs[h.id], h.id), diff];
    }),
    el("td", { class: "cmp-mini" }, names.map((n) =>
      diffBar(isIgnored(r, h.id) ? null : p.trials[n].differences[h.id], max, seriesClass(r, n))))));
  const heads = ["費目", numTh("現行"),
    names.flatMap((n) => [numTh(multi ? `変更後（${n}）` : "変更後"), numTh(multi ? `差（${n}）` : "差")]), el("th", {}, "")];
  box.replaceChildren(
    el("h3", {}, `${p.code} ${p.name}`),
    el("div", { class: "table-scroll" }, table(heads, rows)),
    el("div", {}, names.length ? names.map((n) => compareChanges(r, n, p.trials[n].changes)) : el("p", { class: "muted" }, "試算なし")),
  );
}

function compareChanges(r, name, changes) {
  const rows = changes.map((c) => el("tr", {},
    el("td", {}, el("span", { class: "tag" }, c.kind_label)),
    el("td", {}, c.text),
    el("td", { class: `num ${signClass(c.difference)}` }, fmt(c.difference, { sign: true })),
    el("td", { class: "muted" }, c.head_label)));
  return [
    el("h3", {}, r.names.length > 1 ? [el("i", { class: `swatch ${seriesClass(r, name)}` }), `変更（${name}）`] : "変更"),
    table(["種類", "内容", numTh("差"), "費目"],
      rows.length ? rows : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, "なし"))]),
  ];
}

// 表: 製品ごとに 現行・変更後・差・差%・年間効果額 の行、列は6費目、一番下に計。
// 計は API の合計で、片方でも未登録の製品はその費目の計から除く（除いた値と計に ※）
const CMP_MARK = "※";

function compareGrid(t, head) {
  const multi = t.names.length > 1;
  const ignored = (h) => h === "AMORTIZATION" && t.ignore_amortization;
  const current = (h) => (h === head ? "cmp-grid-current" : "");
  const mark = (title) => el("span", { class: "cmp-grid-mark", title }, CMP_MARK);
  const cell = (text, h, { sign = false, percent = false, known = true, excluded = null, partial = null } = {}) => {
    if (ignored(h)) return el("td", { class: `num muted ${current(h)}` }, "—");
    let shown;
    if (text !== null && text !== undefined) shown = `${fmt(text, { sign })}${percent ? "%" : ""}`;
    else shown = percent && known ? "—" : "未登録";  // 差% は現行が 0 の時も出せない
    const cls = shown === "未登録" ? "missing" : sign ? signClass(text) : "";
    const part = shown !== "未登録" && partial?.includes(h) ? el("span", { class: "tag warn", title: "一部未登録" }, "一部") : null;
    return el("td", { class: `num ${cls} ${current(h)}` }, part, shown, excluded ? mark(excluded) : null);
  };
  const out = (p, tr, h) => p.current[h] === null || tr.after[h] === null;
  const label = (text, name) => el("td", { class: "cmp-grid-label" }, multi && name ? `${text}（${name}）` : text);

  const rows = [];
  for (const p of t.products) {
    const span = 1 + p.trials.length * 4;
    rows.push(el("tr", { class: "cmp-grid-first" },
      el("td", { rowspan: span }, codeSpan(t.version_id, p.product)),
      el("td", { rowspan: span }, p.product_name, el("div", { class: `cmp-grid-volume ${p.volume === null ? "missing" : ""}` },
        p.volume === null ? "年間台数 未登録" : `年間 ${fmt(p.volume)} 台`)),
      el("td", { rowspan: span }, p.specification),
      label("現行"),
      t.heads.map((h) => cell(p.current[h.id], h.id,
        { excluded: p.trials.some((tr) => out(p, tr, h.id)) && "計から除く", partial: p.incomplete }))));
    for (const tr of p.trials) {
      const ex = (h) => out(p, tr, h) && "計から除く";
      const partial = tr.incomplete;
      rows.push(
        el("tr", {}, label("変更後", tr.name),
          t.heads.map((h) => cell(tr.after[h.id], h.id, { excluded: ex(h.id), partial }))),
        el("tr", {}, label("差", tr.name),
          t.heads.map((h) => cell(tr.difference[h.id], h.id, { sign: true, excluded: ex(h.id), partial }))),
        el("tr", {}, label("差%", tr.name), t.heads.map((h) => cell(tr.percent[h.id], h.id,
          { sign: true, percent: true, known: tr.difference[h.id] !== null, excluded: ex(h.id), partial }))),
        el("tr", {}, label("年間効果額", tr.name), t.heads.map((h) => cell(tr.effect[h.id], h.id,
          { sign: true, excluded: tr.effect[h.id] === null && "計から除く", partial }))));
    }
  }
  t.names.forEach((n, i) => {
    const s = t.totals[n];
    const e = t.effect_totals[n];
    const ex = (h) => s[h].excluded > 0 && `未登録 ${s[h].excluded} 製品を除く`;
    const partial = [...new Set(t.products.flatMap((p) => p.trials.filter((tr) => tr.name === n).flatMap((tr) => tr.incomplete)))];
    rows.push(
      el("tr", { class: `cmp-grid-total ${i === 0 ? "cmp-grid-first" : ""}` },
        el("th", { colspan: 3, rowspan: 5 }, multi ? `計（${n}）` : "計"), label("現行"),
        t.heads.map((h) => cell(s[h.id].current, h.id, { excluded: ex(h.id), partial }))),
      el("tr", { class: "cmp-grid-total" }, label("変更後"),
        t.heads.map((h) => cell(s[h.id].after, h.id, { excluded: ex(h.id), partial }))),
      el("tr", { class: "cmp-grid-total" }, label("差"),
        t.heads.map((h) => cell(s[h.id].difference, h.id, { sign: true, excluded: ex(h.id), partial }))),
      el("tr", { class: "cmp-grid-total" }, label("差%"), t.heads.map((h) => cell(s[h.id].percent, h.id,
        { sign: true, percent: true, known: s[h.id].difference !== null, excluded: ex(h.id), partial }))),
      el("tr", { class: "cmp-grid-total" }, label("年間効果額"), t.heads.map((h) => cell(e[h.id].effect, h.id,
        { sign: true, excluded: e[h.id].excluded > 0 && `未登録 ${e[h.id].excluded} 製品を除く`, partial }))));
  });
  const heads = ["品番", "名称", "記号・寸法", "", ...t.heads.map((h) => el("th", { class: `num ${current(h.id)}` }, h.label))];
  const shown = t.products.length + t.without_trial;
  return [
    el("div", { class: "cmp-left-head" },
      el("h3", {}, `製品（${countText(t.count, shown, t.truncated)}）`),
      t.without_trial ? el("span", { class: "muted" }, `試算なし ${t.without_trial}`) : null),
    el("div", { class: "table-scroll" }, t.products.length
      ? table(heads, rows, "small cmp-grid")
      : el("p", { class: "muted" }, "なし")),
  ];
}

function wireCompare() {
  document.getElementById("cmp-show").addEventListener("click", () => guarded(drawCompareTable));
  document.getElementById("cmp-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawCompareTable));
  document.getElementById("cmp-version").addEventListener("change", () => guarded(loadCompareNames));
}

wireCompare();
