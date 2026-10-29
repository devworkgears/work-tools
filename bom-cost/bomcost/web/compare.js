// ③原価比較: 上に検討案ごとの数字のタイル、左に製品ごとの差の横棒、右に選んだ製品の6費目と変更の一覧。
// 差・合計は API が Decimal で出したものを使い、ここで数にするのは棒の長さと並べ替えだけ。app.js・screens.js の関数を使う。
"use strict";

const compareState = { data: null, head: "MANUFACTURING", product: null };

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
  compareState.data = await api("GET", `/api/group/compare?${qs({
    version, names: JSON.stringify(names), q: document.getElementById("cmp-q").value.trim(),
    ignore_amortization: ignore ? "1" : "",
  })}`);
  compareState.product = null;
  drawCompareView();
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
  const r = compareState.data;
  const products = compareProducts(r);
  const withTrial = products.filter((p) => Object.keys(p.trials).length);
  const head = compareState.head;
  const key = (p) => Math.max(-1, ...r.names.map((n) => Math.abs(diffNumber(p.trials[n]?.differences[head]) ?? -1)));
  withTrial.sort((a, b) => key(b) - key(a));
  if (!withTrial.some((p) => p.code === compareState.product)) compareState.product = withTrial[0]?.code ?? null;

  const headSelect = el("select", {
    onchange: (e) => { compareState.head = e.target.value; drawCompareView(); },
  }, r.heads.map((h) => el("option", { value: h.id, selected: h.id === head }, h.label)));
  const left = el("section", { class: "cmp-left" },
    el("div", { class: "cmp-left-head" },
      el("h3", {}, `製品ごとの差（${countText(r.count, products.length, r.truncated)}）`),
      withTrial.length < products.length ? el("span", { class: "muted" }, `試算なし ${products.length - withTrial.length}`) : null,
      r.names.length > 1 ? compareLegend(r) : null),
    compareBars(r, withTrial, head));
  const right = el("section", { class: "cmp-right", id: "cmp-detail" });
  document.getElementById("cmp-body").replaceChildren(
    el("div", { class: "cmp-tools" }, el("label", {}, "費目 ", headSelect)),
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
  el("div", { class: "cmp-bar-label" }, el("span", { class: "code" }, p.code), el("span", { class: "name" }, p.name)),
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

function wireCompare() {
  document.getElementById("cmp-show").addEventListener("click", () => guarded(drawCompareTable));
  document.getElementById("cmp-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawCompareTable));
  document.getElementById("cmp-version").addEventListener("change", () => guarded(loadCompareNames));
}

wireCompare();
