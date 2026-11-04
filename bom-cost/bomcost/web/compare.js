// ③原価を見る: 上に検討案ごとの数字のタイル、左に製品ごとの差の横棒、右に選んだ製品の8費目（式の並び）と変更の一覧。
// 分析は製造原価か材料費の内訳（大きい順・差の寄与・使い回し）で、部品の下へ降りられる。製造原価の時は製品の費目をその場で変えられる。
// 差・合計は API が Decimal で出したものを使い、ここで数にするのは棒の長さと並べ替えだけ。app.js・screens.js・trial.js の関数を使う。
"use strict";

// layout は グラフ（chart）・表（table）・分析（analysis）。画面を開いている間は覚える。data・table はそれぞれの API の返事（要る時に読む）
// 分析: picks は選んだ製品（null は試算のある製品の全部）、by・weight は内訳のまとめ方と数え方、name は差を見る検討案、
// path は降りた部品の並び（{ codes, label }）、view は 大きい順（size）・差の寄与（diff）・使い回し（reach）、analysis はその API の返事。
// cost は 製造原価（manufacturing）・材料費（material）
const compareState = {
  query: null, data: null, table: null, layout: "chart", head: "MANUFACTURING", product: null,
  picks: null, by: "code", weight: "unit", name: null, path: [], view: "size", analysis: null, pickerOpen: false,
  cost: "manufacturing",
};

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
  compareState.picks = null;
  compareState.name = names[0];
  compareState.path = [];
  compareState.analysis = null;
  await Promise.all([loadCompareLayout(), loadCodeNames(version)]);
  drawCompareView();
}

// 表の返事は上の段の年間効果額にも使うので、グラフの時も読む。分析は製品の候補（試算なしも）にグラフの返事を使う
async function loadCompareLayout() {
  const query = qs(compareState.query);
  const needData = compareState.layout !== "table";
  [compareState.table, compareState.data] = await Promise.all([
    compareState.table ?? api("GET", `/api/group/compare/table?${query}`),
    needData ? compareState.data ?? api("GET", `/api/group/compare?${query}`) : compareState.data,
  ]);
  if (compareState.layout === "analysis" && !compareState.analysis) await loadAnalysis(false);
}

// 償却費の欄から、その検討案の試算を選んで型費配賦を開く。部品はその製品で置換・追加した最初の部品
async function compareToPart(version, name, product, scenarioId) {
  const changes = await api("GET", `/api/group/changes?${qs({ version, name, product, head: TRIAL_HEAD })}`);
  const c = changes.rows.find((x) => x.kind === "ADD" || (x.kind === "REPLACE" && x.replacements.length));
  await openPart({ part: c ? (c.kind === "ADD" ? c.part : c.replacements[0].code) : "", version, scenarioId, from: "compare" });
}

function moldLink(td, version, name, product, scenarioId) {
  td.replaceChildren(el("button", { type: "button", class: "look-link", title: "型費配賦",
    onclick: () => guarded(() => compareToPart(version, name, product, scenarioId)) }, ...td.childNodes));
  return td;
}

// 上の段: 検討案ごとの製造原価の差（1台あたりを製品で足した値）と年間効果額の合計。未登録で除いた製品の数を添える
function compareHeadline(t) {
  const value = (text) => (text === null ? el("span", { class: "missing" }, "未登録")
    : el("span", { class: signClass(text) }, fmt(text, { sign: true, yen: true })));
  const note = (count) => (count ? `未登録 ${count} 製品を除く` : null);
  return el("div", { class: "cmp-tiles cmp-headline" }, t.names.map((n) => {
    const d = t.totals[n].MANUFACTURING;
    const e = t.effect_totals[n].MANUFACTURING;
    return el("div", { class: "tile-row" },
      t.names.length > 1 ? el("div", { class: "tile-name" }, el("i", { class: `swatch ${seriesClass(t, n)}` }), n) : null,
      tile("差（1台あたり・製造原価）", value(d.difference), note(d.excluded)),
      tile("年間効果額（製造原価）", value(e.effect), note(e.excluded)));
  }));
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
  return el("div", { class: "seg cmp-layout" }, button("chart", "グラフ"), button("table", "表"), button("analysis", "分析"));
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

// 表で目立たせる費目（材料費小計・製造原価）と、文字を淡くするほかの費目
const CMP_KEY_HEADS = new Set(["MATERIAL", "MANUFACTURING"]);
const cmpHeadClass = (head) => (CMP_KEY_HEADS.has(head) ? "cmp-key" : "cmp-minor");

function drawCompareView() {
  if (compareState.layout === "analysis") {
    document.getElementById("cmp-body").replaceChildren(compareHeadline(compareState.table),
      el("div", { class: "cmp-tools" }, compareLayoutSeg()), el("section", { class: "cmp-an", id: "cmp-an" }));
    drawAnalysis();
    return;
  }
  const isTable = compareState.layout === "table";
  const r = isTable ? compareState.table : compareState.data;
  const head = compareState.head;
  const headSelect = el("select", {
    onchange: (e) => { compareState.head = e.target.value; drawCompareView(); },
  }, r.heads.map((h) => el("option", { value: h.id, selected: h.id === head }, h.label)));
  const tools = el("div", { class: "cmp-tools" }, compareLayoutSeg(), el("label", {}, "費目 ", headSelect));
  if (isTable) {
    tools.append(el("button", { type: "button", onclick: () => guarded(exportCompareXlsx) }, "Excel に書き出す"));
    document.getElementById("cmp-body").replaceChildren(compareHeadline(r), tools, ...compareGrid(r, head));
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
    compareHeadline(compareState.table),
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
        : el("span", { class: signClass(s.difference) }, fmt(s.difference, { sign: true, yen: true }));
    return el("div", { class: "tile-row" },
      r.names.length > 1 ? el("div", { class: "tile-name" }, el("i", { class: `swatch ${seriesClass(r, n)}` }), n) : null,
      tile("合計の差", total),
      tile("下がった", el("span", { class: s.down ? "neg" : "" }, s.down)),
      tile("上がった", el("span", { class: s.up ? "pos" : "" }, s.up)),
      tile("未登録", el("span", { class: s.missing ? "missing" : "" }, ignored ? "—" : s.missing)));
  }));
}

function tile(label, value, note = null) {
  return el("div", { class: "tile" }, el("div", { class: "tile-label" }, label), el("div", { class: "tile-value" }, value),
    note ? el("div", { class: "tile-note" }, note) : null);
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
        : el("span", { class: signClass(text) }, fmt(text, { sign: true, yen: true }));
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
  const cell = (text, head, opts) => (isIgnored(r, head) ? el("td", { class: "num muted" }, "—") : numTd(text, { yen: true, ...opts }));
  const rows = r.heads.map((h) => el("tr", { class: `${cmpHeadClass(h.id)} ${h.id === compareState.head ? "cmp-current-head" : ""}` },
    el("td", {}, h.label),
    cell(p.base.costs[h.id], h.id),
    names.map((n) => {
      const d = p.trials[n].differences[h.id];
      const diff = cell(d, h.id, { sign: true });
      if (signClass(d) && !isIgnored(r, h.id)) diff.classList.add(signClass(d));
      const after = cell(p.trials[n].costs[h.id], h.id);
      if (h.id === "AMORTIZATION") moldLink(after, r.version_id, n, p.code, p.trials[n].scenario_id);
      return [after, diff];
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
    el("td", { class: `num ${signClass(c.difference)}` }, fmt(c.difference, { sign: true, yen: true })),
    el("td", { class: "muted" }, c.head_label)));
  return [
    el("h3", {}, r.names.length > 1 ? [el("i", { class: `swatch ${seriesClass(r, name)}` }), `変更（${name}）`] : "変更"),
    table(["種類", "内容", numTh("差"), "費目"],
      rows.length ? rows : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, "なし"))]),
  ];
}

// 表: 製品ごとに 現行・変更後・差・差%・年間効果額 の行、列は8費目、一番下に計。
// 計は API の合計で、片方でも未登録の製品はその費目の計から除く（除いた値と計に ※）
const CMP_MARK = "※";

function compareGrid(t, head) {
  const multi = t.names.length > 1;
  const ignored = (h) => h === "AMORTIZATION" && t.ignore_amortization;
  const current = (h) => `${cmpHeadClass(h)} ${h === head ? "cmp-grid-current" : ""}`;
  const mark = (title) => el("span", { class: "cmp-grid-mark", title }, CMP_MARK);
  const cell = (text, h, { sign = false, percent = false, known = true, excluded = null, partial = null } = {}) => {
    if (ignored(h)) return el("td", { class: `num muted ${current(h)}` }, "—");
    let shown;
    if (text !== null && text !== undefined) shown = `${fmt(text, { sign, yen: !percent })}${percent ? "%" : ""}`;
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
          t.heads.map((h) => {
            const td = cell(tr.after[h.id], h.id, { excluded: ex(h.id), partial });
            return h.id === "AMORTIZATION" ? moldLink(td, t.version_id, tr.name, p.product, tr.scenario_id) : td;
          })),
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

// 分析: 選んだ製品の製造原価か材料費の内訳（製品の直の子か、降りた部品の直の子）。並び・割合・累積・差・年間は API が出す。
// 製品の候補はグラフの返事の製品（試算なしも）で、始めは差を見る検討案の試算がある製品の全部
function analysisProducts() {
  return compareProducts(compareState.data);
}

function analysisPicks() {
  if (!compareState.picks) {
    compareState.picks = new Set(analysisProducts().filter((p) => p.trials[compareState.name]).map((p) => p.code));
  }
  return compareState.picks;
}

async function loadAnalysis(draw = true) {
  const picks = analysisPicks();
  const products = analysisProducts().map((p) => p.code).filter((c) => picks.has(c));
  const path = compareState.path;
  compareState.analysis = products.length ? await api("GET", `/api/group/compare/analysis?${qs({
    version: compareState.query.version, names: JSON.stringify([compareState.name]), products: JSON.stringify(products),
    by: compareState.by, weight: compareState.weight, parts: path.length ? JSON.stringify(path[path.length - 1].codes) : "",
    cost: compareState.cost, ignore_amortization: compareState.query.ignore_amortization,
  })}`) : null;
  if (draw) drawAnalysis();
}

// 費目を変えた後: 表・グラフ・分析の返事を読み直して描き直す（選んだ製品・見方はそのまま）
async function reloadCompare() {
  compareState.table = null;
  compareState.data = null;
  compareState.analysis = null;
  await loadCompareLayout();
  drawCompareView();
}

// reload を外すと読み直さずに描き直すだけ（見方の切り替え）
function analysisSeg(key, options, reload = true) {
  return el("div", { class: "seg" }, options.map(([value, label]) => el("button", {
    type: "button", class: compareState[key] === value ? "on" : "",
    onclick: () => guarded(async () => {
      compareState[key] = value;
      if (reload) await loadAnalysis();
      else drawAnalysis();
    }),
  }, label)));
}

function analysisPicker() {
  const products = analysisProducts();
  const picks = analysisPicks();
  const set = (codes, on) => guarded(async () => {
    for (const c of codes) {
      if (on) picks.add(c);
      else picks.delete(c);
    }
    await loadAnalysis();
  });
  const all = el("input", { type: "checkbox", checked: products.length > 0 && products.every((p) => picks.has(p.code)),
    onchange: (e) => set(products.map((p) => p.code), e.target.checked) });
  return el("details", { class: "cmp-an-picker", open: compareState.pickerOpen,
    ontoggle: (e) => { compareState.pickerOpen = e.target.open; } },
  el("summary", {}, `製品 ${picks.size}/${products.length} ▼`),
  el("div", { class: "cmp-an-menu" }, el("label", {}, all, " 全部"),
    products.map((p) => el("label", {}, el("input", { type: "checkbox", checked: picks.has(p.code),
      onchange: (e) => set([p.code], e.target.checked) }), ` ${p.code} `, el("span", { class: "muted" }, p.name)))));
}

function analysisNameSelect() {
  const names = JSON.parse(compareState.query.names);
  if (names.length < 2) return null;
  return el("label", {}, "検討案 ", el("select", {
    onchange: (e) => guarded(async () => { compareState.name = e.target.value; await loadAnalysis(); }),
  }, names.map((n) => el("option", { value: n, selected: n === compareState.name }, n))));
}

// 降りた所: 製品 › 部品 › … 。押すとその段へ戻る
function analysisCrumbs() {
  const path = compareState.path;
  const go = (n) => guarded(async () => { compareState.path = path.slice(0, n); await loadAnalysis(); });
  const crumb = (label, n) => (n === path.length ? el("b", {}, label)
    : el("button", { type: "button", class: "look-link", onclick: () => go(n) }, label));
  return el("div", { class: "cmp-an-crumbs" },
    path.length ? el("button", { type: "button", onclick: () => go(path.length - 1) }, "← 戻る") : null,
    crumb("製品", 0), path.map((p, i) => [" › ", crumb(p.label, i + 1)]));
}

function drillAnalysis(row) {
  const label = row.codes.length > 1 ? row.name || row.code : `${row.code} ${row.name}`.trim();
  compareState.path = [...compareState.path, { codes: row.codes, label }];
  return guarded(loadAnalysis);
}

// 品番の欄。子のある品目は押すとその下の内訳へ
function analysisCode(a, row) {
  const many = row.codes.length > 1;
  const text = many ? `${row.codes[0]} 他${row.codes.length - 1}` : row.code;
  return el("td", { title: many ? row.codes.join("・") : codeTitle(a.version_id, row.code) }, row.has_children
    ? el("button", { type: "button", class: "look-link", onclick: () => drillAnalysis(row) }, text) : text);
}

// ②の製品×部品の表示へ、その部品で探した状態で移る（名称でまとめた行は名称で探す）
function swapCell(row) {
  const word = row.codes.length > 1 ? row.name || row.code : row.code;
  return el("td", {}, el("button", { type: "button",
    onclick: () => guarded(() => openTrialPart(compareState.query.version, compareState.name, word)) }, "②で差し替える"));
}

function analysisAmount(row) {
  const td = numTd(row.amount, { yen: true });
  if (row.amount !== null && (row.missing || row.incomplete)) td.prepend(el("span", { class: "tag warn", title: "一部未登録" }, "一部"));
  return td;
}

function breakdownShare(row) {
  if (row.ignored) return el("td", { class: "num muted" }, "—");
  const missing = row.amount === null;
  return el("td", {}, el("div", { class: "cmp-bd-share" },
    // 割合が負（抜く部品など）の行は棒を出さない
    el("div", { class: "cmp-bd-track" }, Number(row.share) > 0 ? el("span", { class: "cmp-bd-fill", style: `width:${Math.min(100, Number(row.share))}%` }) : null),
    el("span", { class: `num ${missing ? "missing" : ""}` }, missing ? "分母外" : `${row.share ?? "—"}%`)));
}

const analysisHeads = (...more) => ["品番", "名称", "記号・寸法", ...more, ""];
const analysisTable = (heads, rows) => table(heads, rows.length ? rows
  : [el("tr", {}, el("td", { colspan: heads.length, class: "muted" }, "なし"))], "small cmp-bd-table");
const nameCells = (r) => [el("td", {}, r.name), el("td", {}, r.specification)];
const costLabel = (a) => (a.cost === "manufacturing" ? "製造原価" : "材料費");

// 製品の費目の行（製造原価の時・kind が head）。品番の欄は空で、償却費を 0 として通した時は金額を「—」に
const isHeadRow = (r) => r.kind === "head";
const rowCode = (a, r) => (isHeadRow(r) ? el("td", {}) : analysisCode(a, r));
const rowAmount = (r) => (r.ignored ? el("td", { class: "num muted" }, "—") : analysisAmount(r));
const rowClass = (r) => (isHeadRow(r) ? "cmp-bd-head" : "");

// 製品の費目の入力欄: 選んだ製品の全部に、検討案の費目変更（1台あたりの値）として入れる。値が1つにそろう時はそれを薄く出す
const EDITABLE_ANALYSIS_HEADS = new Set(["PROCESSING", "AMORTIZATION", "DIRECT"]);
function headEdit(a, row) {
  if (!EDITABLE_ANALYSIS_HEADS.has(row.head) || !compareState.name) return el("td", {});
  const after = a.series[1]?.rows.find((r) => r.key === row.key) ?? row;
  const values = [...new Set(Object.values(after.values))];
  const input = el("input", { inputmode: "decimal", size: 7, class: "trial-bulk-input",
    placeholder: values.length === 1 && values[0] !== null ? values[0] : "" });
  const send = () => guarded(async () => {
    const value = pastedNumber(input.value.trim());
    if (!value) throw new Error("値を入れてください。");
    await api("POST", "/api/group/heads/bulk", { version_id: compareState.query.version, name: compareState.name,
      head: TRIAL_HEAD, items: a.products.map((product) => ({ product, values: { [row.head]: value } })) });
    await reloadCompare();
  });
  input.addEventListener("keydown", (e) => e.key === "Enter" && send());
  return el("td", { class: "cmp-bd-edit" }, input, el("button", { type: "button", onclick: send }, "入れる"));
}
const rowAction = (a, r) => (isHeadRow(r) ? headEdit(a, r) : swapCell(r));

// 大きい順: 現行の金額の高い順に割合と累積（製造原価の時は部品の後に製品の費目）。差を見る検討案の変更後を横に（変更後に無い行は 0）。
// 変更後にだけある行（置換・追加で入った部品）は現行を 0 として部品の行の後ろに足す（変更後の列の和が計になる）
function analysisSize(a) {
  const [s, after] = a.series;
  const later = new Map((after?.rows ?? []).map((r) => [r.key, r]));
  const keys = new Set(s.rows.map((r) => r.key));
  const fresh = (after?.rows ?? []).filter((r) => !keys.has(r.key));
  const line = (r, now) => el("tr", { class: rowClass(r) }, rowCode(a, r), nameCells(r),
    el("td", { class: "num" }, isHeadRow(r) ? "" : fmt(now ? r.quantity : "0")),
    now ? rowAmount(r) : numTd("0", { yen: true }), now ? breakdownShare(r) : el("td", {}),
    el("td", { class: "num muted" }, now && r.cumulative ? `${r.cumulative}%` : ""),
    after ? (r.ignored ? el("td", { class: "num muted" }, "—")
      : numTd(later.has(r.key) ? later.get(r.key).amount : "0", { yen: true })) : null, rowAction(a, r));
  const rows = [
    ...s.rows.filter((r) => !isHeadRow(r)).map((r) => line(r, true)),
    ...fresh.filter((r) => !isHeadRow(r)).map((r) => line(r, false)),
    ...s.rows.filter(isHeadRow).map((r) => line(r, true)),
    ...fresh.filter(isHeadRow).map((r) => line(r, false)),
  ];
  if (rows.length) {
    rows.push(el("tr", { class: "cmp-bd-total" }, el("td", { colspan: 4 }, "計"), numTd(s.total, { yen: true }),
      el("td", { class: s.missing ? "missing" : "muted", colspan: 2 }, s.missing ? `未登録 ${s.missing} 行を除く` : ""),
      after ? numTd(after.total, { yen: true }) : null, el("td", {})));
  }
  return analysisTable(analysisHeads(numTh("数量"), numTh(costLabel(a)), "割合", numTh("累積"), after ? numTh("変更後") : null)
    .filter((h) => h), rows);
}

// 差の寄与: 現行 → 変更後 の差を行ごとに、差の大きい順
function analysisDiff(a) {
  const c = a.contributions[0];
  const list = c?.rows ?? [];
  const max = Math.max(...list.map((r) => Math.abs(diffNumber(r.difference) ?? 0)), 0) || 1;
  const rows = list.map((r) => el("tr", { class: rowClass(r) }, rowCode(a, r), nameCells(r),
    numTd(r.current, { yen: true }), numTd(r.after, { yen: true }),
    el("td", { class: `num ${signClass(r.difference)}` },
      r.incomplete && r.difference !== null ? el("span", { class: "tag warn", title: "一部未登録" }, "一部") : null,
      fmt(r.difference, { sign: true, yen: true })),
    el("td", { class: "cmp-mini" }, diffBar(r.difference, max, "")), rowAction(a, r)));
  if (rows.length) {
    rows.push(el("tr", { class: "cmp-bd-total" }, el("td", { colspan: 5 }, `計${c.missing ? `（未登録 ${c.missing} 行を除く）` : ""}`),
      el("td", { class: `num ${signClass(c.difference)}` }, fmt(c.difference, { sign: true, yen: true })), el("td", {}), el("td", {})));
  }
  return analysisTable(analysisHeads(numTh("現行"), numTh("変更後"), numTh("差"), ""), rows);
}

// 使い回し: 使う製品の多い順（同じなら年間の高い順）。年間は台数を掛けた現行の金額。製品の費目の行は出さない
function analysisReach(a) {
  const n = a.products.length;
  const value = (t) => (t === null ? -Infinity : Number(t));
  const list = a.series[0].rows.filter((r) => !isHeadRow(r))
    .sort((x, y) => y.products - x.products || value(y.annual) - value(x.annual));
  const rows = list.map((r) => el("tr", {}, analysisCode(a, r), nameCells(r), el("td", { class: "num" }, `${r.products}/${n}`),
    numTd(r.annual, { yen: true }), analysisAmount(r), swapCell(r)));
  return analysisTable(analysisHeads(numTh("製品数"), numTh("年間"), numTh(costLabel(a))), rows);
}

function drawAnalysis() {
  const box = document.getElementById("cmp-an");
  if (!box) return;
  const tools = el("div", { class: "cmp-an-tools" }, analysisPicker(), analysisNameSelect(),
    analysisSeg("cost", [["manufacturing", "製造原価"], ["material", "材料費"]]),
    analysisSeg("by", [["code", "品番"], ["name", "名称"], ["name_spec", "名称＋記号・寸法"]]),
    analysisSeg("weight", [["unit", "1台ずつ"], ["volume", "年間台数"]]));
  const a = compareState.analysis;
  if (!a) {
    box.replaceChildren(tools, el("p", { class: "muted" }, "製品を選ぶ"));
    return;
  }
  const after = a.series[1];
  const notes = [
    `${a.products.length} 製品`,
    a.excluded.length ? `台数未登録 ${a.excluded.length} 製品を除く` : null,
    after?.without_trial ? `試算なし ${after.without_trial}（現行で数える）` : null,
    compareState.view === "reach" && a.annual_excluded.length ? `年間は台数未登録 ${a.annual_excluded.length} 製品を除く` : null,
  ].filter((x) => x);
  const views = { size: analysisSize, diff: analysisDiff, reach: analysisReach };
  box.replaceChildren(tools, analysisCrumbs(),
    el("div", { class: "cmp-an-tools" },
      analysisSeg("view", [["size", "大きい順"], ["diff", "差の寄与"], ["reach", "使い回し"]], false),
      el("span", { class: "cmp-bd-notes muted" }, notes.join("・"))),
    el("div", { class: "table-scroll" }, views[compareState.view](a)));
}

function wireCompare() {
  document.getElementById("cmp-show").addEventListener("click", () => guarded(drawCompareTable));
  document.getElementById("cmp-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawCompareTable));
  document.getElementById("cmp-version").addEventListener("change", () => guarded(loadCompareNames));
}

wireCompare();
