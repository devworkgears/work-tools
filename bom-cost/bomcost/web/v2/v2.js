// 設計検討の画面。② から ③〜⑧ を開き、閉じると ② へ戻る。書くのは各画面の［実行］の時だけ。
"use strict";

const NUM = { 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧" };
const S = {
  mode: "edit", // new・edit・view
  number: "",
  version: null, // null は仮
  header: null,
  versions: [],
  plans: ["A"],
  targets: [],
  problems: 0,
  plan: "A",
};
const items = new Map(); // 品番 → 右の列の値（/api/v2/lookup）
let current = null; // 開いている画面 { key, dirty }

// 道具 ------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "value") node.value = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(label(String(child))));
  }
  return node;
}

// 中身を入れ替える。replaceChildren は配列を "[object …]" の文字に、null を "null" の文字にするので、平らにして空を落とす
function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

function query(params) {
  const pairs = Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "");
  return pairs.length ? "?" + new URLSearchParams(pairs).toString() : "";
}

async function api(method, path, params = {}, body) {
  const res = await fetch(path + query(params), {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      message = (await res.json()).error || message;
    } catch (_) {
      // JSON でない失敗はそのままの文
    }
    throw new Error(message);
  }
  return res.json();
}

async function download(path, params, name) {
  const res = await fetch(path + query(params));
  if (!res.ok) throw new Error((await res.json()).error);
  const url = URL.createObjectURL(await res.blob());
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function fail(e) {
  alert(e && e.message ? e.message : String(e));
}

function fmt(text) {
  if (text === null || text === undefined || text === "") return "";
  const n = Number(text);
  if (!Number.isFinite(n)) return String(text);
  return n.toLocaleString("ja-JP", { maximumFractionDigits: 2 });
}

function man(text) {
  if (text === null || text === undefined) return "";
  const n = Number(text) / 10000;
  const body = Math.abs(n).toLocaleString("ja-JP", { maximumFractionDigits: 1 });
  return (n < 0 ? "−" : n > 0 ? "+" : "") + body + "万";
}

function canWrite() {
  return S.mode !== "view" && S.version === null && !!S.number;
}

function q(extra = {}) {
  return { number: S.number, version: S.version === null ? "" : S.version, ...extra };
}

async function lookup(codes) {
  const need = [...new Set(codes.filter((c) => c && !items.has(c)))];
  if (!need.length) return false;
  const params = S.version === null ? {} : q();
  const data = await api("POST", "/api/v2/lookup", params, { codes: need });
  for (const [code, info] of Object.entries(data.items)) items.set(code, info);
  return true;
}

function nameCell(code) {
  if (!code) return { text: "" };
  const info = items.get(code);
  if (!info) return { text: "", cls: "auto" };
  if (!info.registered) return { text: "未登録", cls: "bad" };
  return { text: info.name, cls: "auto" };
}

function isNumber(text) {
  return text !== "" && Number.isFinite(Number(text));
}

function same(a, b) {
  const x = a ?? "";
  const y = b ?? "";
  if (x === "" || y === "") return x === y;
  return Number(x) === Number(y);
}

function dialog(title, body, buttons = []) {
  const overlay = el("div", { class: "overlay" });
  const close = () => overlay.remove();
  overlay.append(
    el("div", { class: "dialog" },
      el("div", { class: "h" }, title, el("span", { class: "sp" }), el("button", { class: "close", onclick: close }, "×")),
      el("div", { class: "b" }, body, buttons.length ? el("div", { class: "row", style: "margin:8px 0 0" }, buttons) : null)),
  );
  document.body.append(overlay);
  return { overlay, close };
}

function planSelect(value, onchange) {
  const select = el("select", { class: "in", style: "min-width:70px" },
    S.plans.map((p) => el("option", { value: p }, `案${p}`)));
  select.value = value;
  select.addEventListener("change", () => onchange(select.value, select));
  return select;
}

function confirmLeave(cur) {
  if (Grid.active) Grid.active.commit();
  const writable = cur.writable ?? canWrite();
  return !cur.dirty || !writable || confirm("未実行の変更を破棄します。よろしいですか？");
}

// L: 再読み込み・タブを閉じる時も、未実行があれば止める
window.addEventListener("beforeunload", (e) => {
  if (Grid.active) Grid.active.commit();
  if (current && current.dirty && (current.writable ?? canWrite())) {
    e.preventDefault();
    e.returnValue = "";
  }
});

function done(text = "実行しました") {
  const t = new Date();
  const hm = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  document.getElementById("status").textContent = `${text} ${hm}`;
}

// 画面の枠 --------------------------------------------------------------

function card(title, tools, body) {
  return el("div", { class: "card" },
    el("div", { class: "h" }, title, el("span", { class: "sp" }), tools,
      el("button", { class: "close", title: "閉じる", onclick: closeScreen }, "×")),
    el("div", { class: "b" }, body));
}

async function openScreen(key) {
  Grid.active = null;
  current = { key, dirty: false };
  document.getElementById("scrno").textContent = NUM[key];
  const main = document.getElementById("screen");
  main.replaceChildren();
  window.scrollTo(0, 0);
  try {
    await SCREENS[key](main, current);
  } catch (e) {
    current.dirty = false;
    fill(main, card(NUM[key], [], ""));
    done(`読込に失敗しました（${e && e.message ? e.message : e}）`);
  }
}

function closeScreen() {
  if (!confirmLeave(current)) return;
  openHub();
}

async function openHub() {
  if (S.number) {
    try {
      await loadStudy(S.number, S.version, false);
    } catch (e) {
      fail(e);
    }
  }
  openScreen(2);
}

async function loadStudy(number, version, render = true) {
  const data = await api("GET", "/api/v2/study", { number, version: version ?? "" });
  applyStudy(data);
  if (render) openScreen(2);
}

function applyStudy(data) {
  if (S.number !== data.header.number || S.version !== data.version) items.clear();
  S.number = data.header.number;
  S.header = data.header;
  S.version = data.version;
  S.versions = data.versions;
  S.targets = data.targets;
  S.problems = data.problems;
  S.plans = data.plans.length ? data.plans : ["A"];
  if (!S.plans.includes(S.plan)) S.plan = S.plans[0];
}

// 押した時点で空の案として保存する（ほかの画面の案の選択にすぐ出す）
async function addPlan() {
  const d = await api("POST", "/api/v2/plans", { number: S.number }, {});
  applyStudy(d);
  return d.plan;
}

// ② 設計検討 -------------------------------------------------------------

async function hubScreen(main, cur) {
  const ro = S.mode === "view" || S.version !== null;
  cur.writable = !ro;
  const modes = [["new", "新規作成"], ["edit", "修正"], ["view", "照会"]];
  const numberInput = el("input", { class: "in", style: "min-width:110px", value: S.number, disabled: S.mode === "new" });
  const versionSelect = el("select", { class: "in", style: "min-width:70px", disabled: S.mode === "new" },
    el("option", { value: "" }, "仮"), S.versions.map((v) => el("option", { value: String(v) }, String(v))));
  versionSelect.value = S.version === null ? "" : String(S.version);
  const read = () => {
    const number = numberInput.value.trim();
    if (!number || !confirmLeave(cur)) return;
    loadStudy(number, versionSelect.value || null).catch(fail);
  };
  numberInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") read();
  });

  const h = S.header || {};
  const field = (label, key, width) => {
    const input = el("input", { class: ro ? "in ro" : "in", value: h[key] || "", readonly: ro, tabindex: ro ? "-1" : null, style: width ? `min-width:${width}px` : null });
    input.dataset.key = key;
    input.addEventListener("input", () => { cur.dirty = true; });
    return el("label", { class: "f" }, label, input);
  };
  const fields = [field("題目", "title", 260), field("担当", "owner"), field("承認者", "approver"), field("開発番号", "dev_number")];
  const state = S.number ? (S.version === null ? "仮" : `版${S.version}`) : "";
  const updated = h.updated_at ? h.updated_at.slice(0, 10).replaceAll("-", "/") : "";
  const run = el("button", { class: "btn pri", style: "align-self:flex-end", disabled: ro || (S.mode !== "new" && !S.number) }, "実行");
  run.addEventListener("click", async () => {
    const body = {};
    for (const input of main.querySelectorAll("input[data-key]")) body[input.dataset.key] = input.value;
    try {
      if (S.mode === "new") {
        applyStudy(await api("POST", "/api/v2/studies", {}, body));
        S.mode = "edit";
      } else {
        applyStudy(await api("PUT", "/api/v2/study", { number: S.number }, body));
      }
      done();
      openScreen(2);
    } catch (e) {
      fail(e);
    }
  });

  const needStudy = !S.number;
  const big = (key, label, extra) => el("button", { class: "btn big", disabled: key !== 3 && needStudy, onclick: () => { if (confirmLeave(cur)) openScreen(key); } }, label, extra);
  const release = el("button", { class: "btn big", style: "margin-left:24px;align-self:center", disabled: !canWrite() }, "版数取得");
  release.addEventListener("click", async () => {
    if (!confirmLeave(cur) || !confirm(`${S.number} の版を取得します。取得した版は書き換えできません。よろしいですか？`)) return;
    try {
      const taken = await api("POST", "/api/v2/release", { number: S.number }, {});
      await loadStudy(S.number, null);
      done(`版${taken.version} を取得しました`);
    } catch (e) {
      fail(e);
    }
  });

  main.append(
    el("div", { class: "row modebar" },
      el("span", { class: "mini" }, "処理"),
      el("div", { class: "chips" }, modes.map(([m, label]) =>
        el("button", { class: "chip" + (S.mode === m ? " on" : ""), onclick: () => setMode(m) }, label))),
      el("span", { class: "mini", style: "margin-left:8px" }, "検討番号"), numberInput,
      el("span", { class: "mini" }, "版"), versionSelect,
      el("button", { class: "btn", onclick: read, disabled: S.mode === "new" }, "読込"),
      el("button", { class: "btn", onclick: studyListDialog, disabled: S.mode === "new" }, "一覧から探す")),
    el("div", { class: "card" },
      el("div", { class: "h" }, el("span", { class: "no" }, S.number || "　"), el("span", { class: "mini", style: "font-weight:400" }, "版"),
        el("input", { class: "in ro", value: state, readonly: true, style: "min-width:50px;width:60px" })),
      el("div", { class: "b" }, el("div", { class: "row", style: "margin:0" }, fields,
        el("label", { class: "f" }, "状態", el("input", { class: "in ro", value: state, readonly: true, style: "min-width:60px;width:70px" })),
        el("label", { class: "f" }, "最終更新日", el("input", { class: "in ro", value: updated, readonly: true, style: "min-width:100px;width:110px" })),
        run))),
    el("div", { class: "groups" },
      el("fieldset", { class: "grp" }, big(3, "対象品番")),
      el("fieldset", { class: "grp" }, big(4, "置換"), big(5, "グリッド画面"), big(8, "書き換え")),
      el("fieldset", { class: "grp" }, big(6, "変更一覧", S.problems ? [" ", el("span", { class: "pill warn" }, S.problems)] : null), big(7, "結果")),
      release),
  );
}

function setMode(mode) {
  if (!confirmLeave(current)) return;
  if (mode === "new") {
    Object.assign(S, { number: "", version: null, header: null, versions: [], plans: ["A"], targets: [], problems: 0, plan: "A" });
    items.clear();
  }
  S.mode = mode;
  openScreen(2);
}

async function studyListDialog() {
  try {
    const data = await api("GET", "/api/v2/studies");
    const d = dialog("一覧から探す",
      el("table", { class: "list" },
        el("tr", {}, ["検討番号", "題目", "担当", "最終更新日", "版"].map((t) => el("th", {}, t))),
        data.studies.map((s) => el("tr", {
          class: "pick",
          onclick: () => {
            if (!confirmLeave(current)) return;
            d.close();
            loadStudy(s.number, null).catch(fail);
          },
        }, el("td", {}, s.number), el("td", {}, s.title), el("td", {}, s.owner),
        el("td", {}, (s.updated_at || "").slice(0, 10).replaceAll("-", "/")), el("td", { class: "r" }, s.latest_version ?? "")))),
      [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => d.close() }, "閉じる")]);
  } catch (e) {
    fail(e);
  }
}

// ③ 対象品番 -------------------------------------------------------------

async function targetsScreen(main, cur) {
  const ro = !canWrite();
  const EMPTY_ROW = () => "";
  let rows = [];
  let mainProduct = null;
  if (S.number) {
    const d = await api("GET", "/api/v2/targets", q());
    rows = d.targets.slice();
    mainProduct = d.main_product;
  }
  await lookup(rows);
  const body = el("div");
  const model = {
    head: () => ["", "品番", "名称", "代表", "規格", "組立図", { text: "主対象", cls: "c" }, { text: "年間台数", cls: "r" }, { text: "材料費", cls: "r" }, { text: "製造原価", cls: "r" }],
    nRows: () => rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push(EMPTY_ROW()); return true; },
    nCols: () => 10,
    cell(r, c) {
      const code = rows[r] || "";
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: code, editable: !ro };
      if (!code) return { text: "" };
      if (c === 2) return nameCell(code);
      if (c === 6) return { text: mainProduct === code ? "●" : "○", cls: "act" };
      const info = items.get(code);
      if (!info || !info.registered) return { text: "" };
      if (c === 3) return { text: info.model, cls: "auto" };
      if (c === 4) return { text: info.product_spec, cls: "auto" };
      if (c === 5) return { text: info.drawing, cls: "auto" };
      if (c === 7) return { text: fmt(info.annual), cls: "auto r" };
      if (c === 8) return { text: fmt(info.material), cls: "auto r" };
      return { text: fmt(info.manufacturing), cls: "auto r" };
    },
    set(r, c, text) {
      if (c !== 1) return;
      if (r >= rows.length) {
        if (!text) return;
        rows.push(text);
      } else rows[r] = text;
      cur.dirty = true;
    },
    action(r, c) {
      if (c !== 6 || ro || !rows[r]) return false;
      mainProduct = rows[r];
      cur.dirty = true;
      return true;
    },
    changed() {
      lookup(rows).then((got) => got && grid.render()).catch(fail);
    },
  };
  const grid = new Grid(body, model);

  const addCodes = (codes) => {
    for (const code of codes) if (code && !rows.includes(code)) rows.push(code);
    cur.dirty = true;
    grid.changed();
  };
  const csvInput = el("input", { type: "file", accept: ".csv,.txt", style: "display:none" });
  csvInput.addEventListener("change", async () => {
    const file = csvInput.files[0];
    csvInput.value = "";
    if (!file) return;
    const buffer = await file.arrayBuffer();
    let text;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
    } catch (_) {
      text = new TextDecoder("shift_jis").decode(buffer);
    }
    const codes = text.replace(/^﻿/, "").split(/\r?\n/)
      .map((line) => line.split(/[,\t]/)[0].replace(/^"|"$/g, "").trim())
      .filter((code) => code && !code.includes("品番"));
    addCodes(codes);
  });
  const run = el("button", { class: "btn pri", disabled: ro }, "実行");
  run.addEventListener("click", async () => {
    try {
      const d = await api("PUT", "/api/v2/targets", { number: S.number }, { products: rows.filter(Boolean), main_product: mainProduct });
      rows = d.targets.slice();
      mainProduct = d.main_product;
      S.targets = d.targets;
      cur.dirty = false;
      done();
      grid.render();
    } catch (e) {
      fail(e);
    }
  });
  main.append(card("対象品番", [
    el("button", { class: "btn", disabled: ro, onclick: () => productSearchDialog(addCodes) }, "品番検索"),
    el("button", { class: "btn", disabled: ro, onclick: () => csvInput.click() }, "CSV取込"), csvInput,
    el("button", {
      class: "btn", disabled: ro, onclick: () => {
        if (grid.sel.r < rows.length) {
          rows.splice(grid.sel.r, 1);
          cur.dirty = true;
          grid.render();
        }
      },
    }, "行削除"),
    run,
  ], [body, el("div", { class: "row", style: "margin:8px 0 0" }, el("span", { class: "sp" }),
    el("button", { class: "btn quiet", onclick: () => importDialog(() => { items.clear(); lookup(rows).then(() => grid.render()).catch(fail); }) }, "部品表データ取込（モック）"))]));
  grid.focus();
}

function productSearchDialog(add) {
  const code = el("input", { class: "in" });
  const name = el("input", { class: "in" });
  const model = el("input", { class: "in" });
  const spec = el("select", { class: "in" }, el("option", { value: "" }, "全て"));
  const list = el("div");
  let found = [];
  const checks = new Map();
  const search = async () => {
    try {
      const d = await api("GET", "/api/v2/products", { code: code.value, name: name.value, model: model.value, product_spec: spec.value });
      const chosen = spec.value;
      fill(spec, el("option", { value: "" }, "全て"), d.product_specs.map((s) => el("option", { value: s }, s)));
      spec.value = chosen;
      found = d.products;
      checks.clear();
      list.replaceChildren(el("table", { class: "list" },
        el("tr", {}, ["選", "品番", "名称", "代表", "規格", "年間台数"].map((t) => el("th", {}, t))),
        found.map((p) => {
          const box = el("input", { type: "checkbox" });
          checks.set(p.code, box);
          return el("tr", {}, el("td", {}, box), el("td", {}, p.code), el("td", {}, p.name), el("td", {}, p.model),
            el("td", {}, p.product_spec), el("td", { class: "r" }, fmt(p.annual)));
        })));
    } catch (e) {
      fail(e);
    }
  };
  const d = dialog("品番検索", [
    el("div", { class: "row" }, el("label", { class: "f" }, "品番", code), el("label", { class: "f" }, "名称", name),
      el("label", { class: "f" }, "代表", model), el("label", { class: "f" }, "規格", spec),
      el("button", { class: "btn", style: "align-self:flex-end", onclick: search }, "検索")),
    list,
  ], [
    el("button", { class: "btn", onclick: () => checks.forEach((b) => { b.checked = true; }) }, "全選択"),
    el("span", { class: "sp" }),
    el("button", {
      class: "btn pri", onclick: () => {
        add(found.filter((p) => checks.get(p.code)?.checked).map((p) => p.code));
        d.close();
      },
    }, "追加"),
    el("button", { class: "btn", onclick: () => d.close() }, "閉じる"),
  ]);
  for (const input of [code, name, model]) input.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  search();
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

async function importDialog(after) {
  const files = el("input", { type: "file", multiple: true, accept: ".csv,.xlsx,.xlsm" });
  const out = el("div");
  const show = (d) => {
    const labels = { item: "品目", bom_line: "構成", cost: "原価", volume: "年間台数" };
    fill(out,
      el("table", { class: "list", style: "margin-top:8px" },
        el("tr", {}, Object.values(labels).map((t) => el("th", {}, t))),
        el("tr", {}, Object.keys(labels).map((k) => el("td", { class: "r" }, d.counts[k])))),
      d.warnings && d.warnings.length ? el("div", { class: "msg" }, d.warnings.join("\n")) : null);
  };
  const send = async (body) => {
    try {
      const d = await api("POST", "/api/v2/bom/import", {}, body);
      show(d);
      done("取込しました");
      after();
    } catch (e) {
      fail(e);
    }
  };
  let status = { samples: false, counts: null };
  try {
    status = await api("GET", "/api/v2/bom");
  } catch (e) {
    fail(e);
  }
  const d = dialog("部品表データ取込（モック）", [files, out], [
    status.samples ? el("button", { class: "btn", onclick: () => send({ sample: true }) }, "見本") : null,
    el("span", { class: "sp" }),
    el("button", {
      class: "btn pri", onclick: async () => {
        if (!files.files.length) return;
        const list = [];
        for (const f of files.files) list.push({ filename: f.name, content: toBase64(await f.arrayBuffer()) });
        send({ files: list });
      },
    }, "取込"),
    el("button", { class: "btn", onclick: () => d.close() }, "閉じる"),
  ]);
  if (status.counts) show({ counts: status.counts });
}

// ④ 置換 -----------------------------------------------------------------

async function replaceScreen(main, cur) {
  const ro = !canWrite();
  const EMPTY_ROW = () => ({ old: "", new: "" });
  let plan = S.plan;
  let rows = [];
  const load = async () => {
    const d = S.number ? await api("GET", "/api/v2/replace", q({ plan })) : { rows: [] };
    rows = d.rows.map((r) => ({ ...r }));
    await lookup(rows.flatMap((r) => [r.old, r.new]));
    cur.dirty = false;
  };
  await load();
  const body = el("div");
  const model = {
    head: () => ["", "元品番", "名称", "新品番", "名称", ""],
    nRows: () => rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push(EMPTY_ROW()); return true; },
    nCols: () => 6,
    cell(r, c) {
      const row = rows[r] || {};
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: row.old || "", editable: !ro };
      if (c === 2) return nameCell(row.old);
      if (c === 3) return { text: row.new || "", editable: !ro };
      if (c === 4) return nameCell(row.new);
      return row.overridden ? { text: "※", cls: "mark", title: "一部の製品は個別に変更あり" } : { text: "", cls: "mark" };
    },
    set(r, c, text) {
      if (c !== 1 && c !== 3) return;
      if (r >= rows.length) {
        if (!text) return;
        rows.push({ old: "", new: "" });
      }
      rows[r][c === 1 ? "old" : "new"] = text;
      rows[r].overridden = false;
      cur.dirty = true;
    },
    changed() {
      lookup(rows.flatMap((r) => [r.old, r.new])).then((got) => got && grid.render()).catch(fail);
    },
  };
  const grid = new Grid(body, model);
  const select = planSelect(plan, async (value, sel) => {
    if (!confirmLeave(cur)) {
      sel.value = plan;
      return;
    }
    plan = S.plan = value;
    await load().catch(fail);
    grid.render();
  });
  const run = el("button", { class: "btn pri", disabled: ro }, "実行");
  run.addEventListener("click", async () => {
    try {
      const d = await api("PUT", "/api/v2/replace", { number: S.number, plan },
        { rows: rows.filter((r) => r.old || r.new).map((r) => ({ old: r.old, new: r.new })) });
      rows = d.rows;
      cur.dirty = false;
      done();
      grid.render();
    } catch (e) {
      fail(e);
    }
  });
  main.append(card("置換", [select, run], body));
  grid.focus();
}

// ⑤ グリッド画面 ---------------------------------------------------------------

async function gridScreen(main, cur) {
  const ro = !canWrite();
  let plan = S.plan;
  let cols = []; // 親品番
  let rows = []; // { part, cells: 列の位置ごとの値, origin, problems }
  let original = new Map(); // 「親品番\t品番」 → 読んだ値
  const fromReplace = new Set(); // 置換から入った品番
  const key = (product, part) => `${product}\t${part}`;
  const load = async () => {
    const d = S.number ? await api("GET", "/api/v2/grid", q({ plan })) : { columns: [], rows: [] };
    cols = d.columns.slice();
    rows = d.rows.map((r) => ({ part: r.part, cells: cols.map((c) => r.cells[c] ?? ""), origin: r.origin, problems: r.problems || {}, locked: r.locked || {} }));
    original = new Map();
    fromReplace.clear();
    for (const r of d.rows) {
      for (const [c, v] of Object.entries(r.cells)) original.set(key(c, r.part), v);
      if (r.origin) fromReplace.add(r.part);
    }
    await lookup([...cols, ...rows.map((r) => r.part)]);
    cur.dirty = false;
  };
  await load();
  const body = el("div");
  const model = {
    nRows: () => 2 + rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push({ part: "", cells: [], origin: null, problems: {}, locked: {} }); return true; },
    nCols: () => 4 + cols.length + (ro ? 0 : 1),
    cell(r, c) {
      if (r === 0) {
        if (c === 0) return { text: "", cls: "n" };
        if (c === 1) return { text: "親品番 →", th: true };
        if (c < 4) return { text: "", th: true };
        const code = cols[c - 4] || "";
        const outside = code && !S.targets.includes(code);
        return { text: code, editable: !ro, cls: outside ? "bad" : "", title: outside ? "要確認" : null };
      }
      if (r === 1) {
        if (c === 0) return { text: "", cls: "n" };
        if (c === 1) return { text: "品番 ↓", th: true };
        if (c === 2) return { text: "名称", th: true };
        if (c === 3) return { text: "", th: true };
        const code = cols[c - 4];
        const info = code ? items.get(code) : null;
        return { text: info ? (info.registered ? info.name : "未登録") : "", th: true, cls: info && !info.registered ? "bad" : "" };
      }
      const i = r - 2;
      const row = rows[i];
      if (c === 0) return { text: String(i + 1), cls: "n" };
      if (c === 1) return { text: row ? row.part : "", editable: !ro };
      if (c === 2) return nameCell(row && row.part);
      if (c === 3) return row && row.origin ? { text: `④${row.origin}`, cls: "mark", title: `置換 ${row.origin}` } : { text: "", cls: "mark" };
      const code = cols[c - 4];
      const value = row ? row.cells[c - 4] ?? "" : "";
      const locked = !!(row && code && row.locked[code]);
      const bad = locked || (row && code && (row.problems[code] || (value !== "" && !isNumber(value))));
      return {
        text: value, editable: !ro && !!row && !!code && !locked, cls: "r" + (bad ? " bad" : ""),
        title: locked ? "要確認（複数の箇所にある・書き換えで直す）" : null,
      };
    },
    set(r, c, text) {
      if (r === 0 && c >= 4) {
        const idx = c - 4;
        if (idx >= cols.length) {
          if (!text) return;
          cols.push(text);
        } else cols[idx] = text;
      } else if (r >= 2 && c === 1) {
        const i = r - 2;
        if (i >= rows.length) {
          if (!text) return;
          rows.push({ part: text, cells: [], origin: null, problems: {}, locked: {} });
        } else rows[i].part = text;
      } else if (r >= 2 && c >= 4) {
        const row = rows[r - 2];
        if (!row || !cols[c - 4]) return;
        row.cells[c - 4] = text;
        row.problems = {};
      } else return;
      cur.dirty = true;
    },
    changed() {
      lookup([...cols, ...rows.map((r) => r.part)]).then((got) => got && grid.render()).catch(fail);
      refreshLocks().catch(fail);
    },
  };
  // 打った見出し・行のうち、製品の中で2か所以上にある品番のセルを、打つ前に書けなくする
  let lockAsked = "";
  const refreshLocks = async () => {
    if (!S.number) return;
    const columns = cols.filter(Boolean);
    const parts = rows.map((r) => r.part).filter(Boolean);
    const asked = JSON.stringify([plan, columns, parts]);
    if (asked === lockAsked) return;
    lockAsked = asked;
    const d = await api("POST", "/api/v2/grid/locked", q({ plan }), { columns, rows: parts });
    if (asked !== lockAsked) return; // 後から打った分の問い合わせが先に済んでいる
    for (const row of rows) row.locked = d.locked[row.part] || {};
    grid.render();
  };
  const grid = new Grid(body, model);

  const save = async () => {
    const now = new Map();
    const rowOf = new Map();
    rows.forEach((row, i) => {
      if (!row.part) return;
      cols.forEach((code, j) => {
        if (!code) return;
        const value = row.cells[j] ?? "";
        if (value !== "" && !isNumber(value)) throw new Error(`数量は数で入力してください（${row.part} × ${code}「${value}」）`);
        if (value !== "") now.set(key(code, row.part), value);
        rowOf.set(key(code, row.part), i);
      });
    });
    const cells = [];
    let touchesReplace = false;
    for (const k of new Set([...original.keys(), ...now.keys()])) {
      if (same(original.get(k), now.get(k))) continue;
      const [product, part] = k.split("\t");
      if (original.has(k) && fromReplace.has(part)) touchesReplace = true;
      cells.push({ product, part, value: now.get(k) ?? null, row: rowOf.get(k) ?? 0 });
    }
    if (touchesReplace && !confirm("置換から入った行を書き換えます。よろしいですか？")) return;
    await api("PUT", "/api/v2/grid", { number: S.number, plan },
      { columns: cols.filter(Boolean), rows: rows.map((r) => r.part).filter(Boolean), cells });
    await load();
    done();
    grid.render();
  };
  const run = el("button", { class: "btn pri", disabled: ro, onclick: () => save().catch(fail) }, "実行");
  const tabs = el("div", { class: "tabs" });
  const drawTabs = () => {
    fill(tabs,
      S.plans.map((p) => el("button", { class: p === plan ? "on" : "", onclick: () => switchPlan(p) }, `案${p}`)),
      ro ? null : el("button", {
        onclick: async () => {
          if (!confirmLeave(cur)) return;
          try {
            const p = await addPlan();
            cur.dirty = false;
            await switchPlan(p);
            done(`案${p} を追加しました`);
          } catch (e) {
            fail(e);
          }
        },
      }, "＋"));
  };
  const switchPlan = async (p) => {
    if (p === plan || !confirmLeave(cur)) return drawTabs();
    plan = S.plan = p;
    drawTabs();
    await load().catch(fail);
    grid.render();
  };
  drawTabs();
  const whereUsed = () => {
    const row = rows[grid.sel.r - 2];
    if (!row || !row.part) return alert("品番の行を選んでください");
    whereUsedDialog(row.part);
  };
  main.append(card(tabs, [
    el("button", { class: "btn", onclick: whereUsed, disabled: !S.number }, "使用先"),
    el("button", { class: "btn", onclick: conditionsDialog, disabled: !S.number }, "計算条件"),
    run,
  ], body));
  grid.focus();
}

async function whereUsedDialog(code) {
  try {
    const d = await api("GET", "/api/v2/where-used", q({ code }));
    const list = el("div");
    const open = async (group) => {
      try {
        const g = await api("GET", "/api/v2/where-used", q({ code, group }));
        list.replaceChildren(el("table", { class: "list", style: "margin-top:8px" },
          el("tr", {}, el("th", {}, "品番"), el("th", {}, "名称")),
          g.products.map((p) => el("tr", {}, el("td", {}, p.code), el("td", {}, p.name ?? "")))));
      } catch (e) {
        fail(e);
      }
    };
    const box = dialog(`使用先 ${d.code}`, [
      el("table", { class: "list" },
        el("tr", {}, el("th", {}, ""), el("th", {}, "件数"), el("th", {}, "")),
        el("tr", {}, el("td", {}, "範囲内"), el("td", { class: "r" }, d.inside),
          el("td", {}, el("button", { class: "btn", disabled: !d.inside, onclick: () => open("inside") }, "開く"))),
        el("tr", {}, el("td", {}, "範囲外"), el("td", { class: "r" }, d.outside),
          el("td", {}, el("button", { class: "btn", disabled: !d.outside, onclick: () => open("outside") }, "開く")))),
      list,
    ], [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
  } catch (e) {
    fail(e);
  }
}

async function conditionsDialog() {
  try {
    const d = await api("GET", "/api/v2/conditions", q());
    const inputs = new Map();
    const ro = !canWrite();
    const box = dialog("計算条件", el("table", { class: "list" },
      el("tr", {}, el("th", {}, "対象品番"), el("th", {}, "年間台数（部品表）"), el("th", {}, "年間台数")),
      d.rows.map((r) => {
        const input = el("input", { class: "in", value: r.annual ?? "", readonly: ro, style: "min-width:90px;width:100px;text-align:right" });
        inputs.set(r.product, input);
        return el("tr", {}, el("td", {}, r.product), el("td", { class: "r" }, fmt(r.bom_annual)), el("td", {}, input));
      })), [
      el("span", { class: "sp" }),
      el("button", {
        class: "btn pri", disabled: ro, onclick: async () => {
          const annual = {};
          for (const [p, input] of inputs) annual[p] = input.value.trim() || null;
          try {
            await api("PUT", "/api/v2/conditions", { number: S.number }, { annual });
            box.close();
            done();
          } catch (e) {
            fail(e);
          }
        },
      }, "実行"),
      el("button", { class: "btn", onclick: () => box.close() }, "閉じる"),
    ]);
  } catch (e) {
    fail(e);
  }
}

// ⑧ 書き換え -------------------------------------------------------------

async function rewriteScreen(main, cur) {
  const ro = !canWrite();
  let plan = S.plan;
  let product = S.targets[0] || "";
  let rows = [];
  const load = async () => {
    rows = [];
    if (S.number && product) {
      const d = await api("GET", "/api/v2/rewrite", q({ plan, product }));
      rows = d.rows.map((r) => ({ ...r, balloon: r.balloon ?? "", fresh: r.before_part === null }));
    }
    await lookup(rows.map((r) => r.part));
    cur.dirty = false;
  };
  await load();
  const body = el("div");
  const model = {
    head: () => ["", "親", { text: "風船", cls: "c" }, "品番", "名称", { text: "数量", cls: "r" }],
    nRows: () => rows.length + (ro || !product ? 0 : 1),
    grow: ro || !product ? undefined : () => {
      rows.push({ parent: "", balloon: "", part: "", quantity: "", before_part: null, before_quantity: null, fresh: true });
      return true;
    },
    nCols: () => 6,
    cell(r, c) {
      const row = rows[r];
      const fresh = !row || row.fresh;
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: row ? row.parent : "", editable: !ro && fresh };
      if (c === 2) return { text: row ? row.balloon : "", editable: !ro && fresh, cls: "c" };
      if (c === 3) {
        const own = row && (row.fresh || row.part !== row.before_part);
        return { text: row ? row.part : "", editable: !ro, cls: own ? "own" : "" };
      }
      if (c === 4) return nameCell(row && row.part);
      const own = row && (row.fresh || !same(row.quantity, row.before_quantity));
      const bad = row && row.quantity !== "" && (!isNumber(row.quantity) || Number(row.quantity) < 0);
      return { text: row ? row.quantity : "", editable: !ro, cls: "r" + (bad ? " bad" : own ? " own" : "") };
    },
    set(r, c, text) {
      if (c < 1 || c === 4 || c > 5) return;
      if (r >= rows.length) {
        if (!text) return;
        rows.push({ parent: "", balloon: "", part: "", quantity: "", before_part: null, before_quantity: null, fresh: true });
      }
      const row = rows[r];
      const field = { 1: "parent", 2: "balloon", 3: "part", 5: "quantity" }[c];
      if ((c === 1 || c === 2) && !row.fresh) return;
      row[field] = text;
      cur.dirty = true;
    },
    changed() {
      lookup(rows.map((r) => r.part)).then((got) => got && grid.render()).catch(fail);
    },
  };
  const grid = new Grid(body, model);
  const productSelect = el("select", { class: "in", style: "min-width:90px" }, S.targets.map((p) => el("option", { value: p }, p)));
  productSelect.value = product;
  productSelect.addEventListener("change", async () => {
    if (!confirmLeave(cur)) {
      productSelect.value = product;
      return;
    }
    product = productSelect.value;
    await load().catch(fail);
    grid.render();
  });
  const select = planSelect(plan, async (value, sel) => {
    if (!confirmLeave(cur)) {
      sel.value = plan;
      return;
    }
    plan = S.plan = value;
    await load().catch(fail);
    grid.render();
  });
  const run = el("button", { class: "btn pri", disabled: ro || !product }, "実行");
  run.addEventListener("click", async () => {
    try {
      const send = rows.filter((r) => r.part).map((r) => {
        if (!r.parent) throw new Error(`親を入力してください（${r.part}）`);
        return { parent: r.parent, balloon: r.balloon, part: r.part, quantity: r.quantity };
      });
      await api("PUT", "/api/v2/rewrite", { number: S.number, plan }, { product, rows: send });
      await load();
      done();
      grid.render();
    } catch (e) {
      fail(e);
    }
  });
  main.append(card("書き換え", [productSelect, select, run], body));
  grid.focus();
}

// ⑥ 変更一覧 -------------------------------------------------------------

async function changesScreen(main) {
  let plan = S.plan;
  let product = "";
  let rows = [];
  const load = async () => {
    rows = (await api("GET", "/api/v2/changes", q({ plan, product }))).rows;
  };
  await load();
  const body = el("div");
  const model = {
    head: () => ["", "製品", "親", { text: "風船", cls: "c" }, "品番", "名称", { text: "変更前", cls: "r" }, { text: "変更後", cls: "r" }, { text: "差", cls: "r" }, "要確認"],
    nRows: () => rows.length,
    nCols: () => 10,
    cell(r, c) {
      const row = rows[r];
      switch (c) {
        case 0: return { text: String(r + 1), cls: "n" };
        case 1: return { text: row.product };
        case 2: return { text: row.parent };
        case 3: return { text: row.balloon ?? "—", cls: "c" };
        case 4: return { text: row.part, cls: row.added ? "own" : "" };
        case 5: return row.name === null ? { text: "未登録", cls: "bad" } : { text: row.name };
        case 6: return { text: row.before === null ? "—" : fmt(row.before), cls: row.before === null ? "c" : "r" };
        case 7: return { text: fmt(row.after), cls: "r" + (Number(row.after) < 0 ? " bad" : "") };
        case 8: return { text: fmt(row.delta), cls: "r" + (Number(row.delta) < 0 ? " bad" : "") };
        default: return { text: row.problems.join("・"), cls: row.problems.length ? "bad" : "" };
      }
    },
    set() {},
  };
  const grid = new Grid(body, model);
  const productSelect = el("select", { class: "in", style: "min-width:90px" },
    el("option", { value: "" }, "全製品"), S.targets.map((p) => el("option", { value: p }, p)));
  productSelect.addEventListener("change", async () => {
    product = productSelect.value;
    await load().catch(fail);
    grid.render();
  });
  const select = planSelect(plan, async (value) => {
    plan = S.plan = value;
    await load().catch(fail);
    grid.render();
  });
  main.append(card("変更一覧", [productSelect, select,
    el("button", { class: "btn", onclick: () => download("/api/v2/export/changes", q({ plan, product }), `変更一覧_${S.number}.xlsx`).catch(fail) }, "Excel"),
  ], body));
  grid.focus();
}

// ⑦ 結果 -----------------------------------------------------------------

async function resultScreen(main) {
  let head = "total";
  let data = null;
  const stamp = el("span", { class: "mini", style: "font-weight:400" });
  const table = el("div");
  const chips = el("div", { class: "chips" });
  const drawChips = () => fill(chips, [["total", "合計"], ["material", "材料"]].map(([k, label]) =>
    el("button", { class: "chip" + (head === k ? " on" : ""), onclick: () => { head = k; drawChips(); if (data) compute(); } }, label)));
  const draw = () => {
    if (!data) return table.replaceChildren();
    const plans = data.plans;
    table.replaceChildren(el("table", { class: "res" },
      el("tr", {}, el("th", {}, "対象品番"), el("th", { class: "r" }, "年間台数"), el("th", { class: "r" }, "今"),
        plans.map((p) => el("th", { class: "r" }, `案${p}`))),
      data.rows.map((r) => el("tr", {},
        el("td", {}, r.product),
        el("td", { class: "r" }, fmt(r.annual)),
        el("td", { class: "r" + (r.now_incomplete ? " inc" : ""), title: r.now_incomplete ? "不完全" : null }, fmt(r.now)),
        plans.map((p) => el("td", { class: "r" + (r.incomplete[p] ? " inc" : ""), title: r.incomplete[p] ? "不完全" : null }, fmt(r.plans[p]))))),
      el("tr", {}, el("th", {}, "年間の影響"), el("th", {}), el("th", {}),
        plans.map((p) => {
          const v = data.impact[p];
          const n = Number(v);
          return el("th", {
            class: "r" + (n < 0 ? " minus" : n > 0 ? " plus" : "") + (data.impact_incomplete[p] ? " inc" : ""),
            title: data.impact_incomplete[p] ? "不完全" : null,
          }, man(v));
        }))));
  };
  const compute = async () => {
    try {
      data = await api("GET", "/api/v2/calc", q({ head }));
      stamp.textContent = `計算日時 ${data.computed_at.replace("T", " ").slice(0, 16).replaceAll("-", "/")}`;
      draw();
      done("計算しました");
    } catch (e) {
      fail(e);
    }
  };
  drawChips();
  main.append(card(["結果", stamp], [
    el("button", { class: "btn", onclick: conditionsDialog }, "計算条件"),
    el("button", { class: "btn", onclick: () => download("/api/v2/export/result", q({ head }), `結果_${S.number}.xlsx`).catch(fail) }, "Excel"),
    el("button", { class: "btn pri", onclick: compute }, "計算"),
  ], [el("div", { class: "row" }, el("span", { class: "mini" }, "費目"), chips), table]));
}

const SCREENS = {
  2: hubScreen,
  3: targetsScreen,
  4: replaceScreen,
  5: gridScreen,
  6: changesScreen,
  7: resultScreen,
  8: rewriteScreen,
};

// 語の読み替えを受け取ってから最初の画面を描く（受け取れなくても元の語で開く）
api("GET", "/api/v2/labels")
  .then((d) => Object.assign(LABELS, d.labels), fail)
  .finally(() => openScreen(2));
