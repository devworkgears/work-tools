// 設計検討の画面。② から ③〜⑧ を開き、閉じると ② へ戻る。入れた値はその場で一時保存する（⑦の［実行］は点検と計算だけで、書かない）。
"use strict";

const NUM = { 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧" };
const S = {
  mode: "edit", // new・edit・view
  number: "",
  version: null, // null は仮
  header: null,
  versions: [],
  targets: [],
  problems: 0,
  draft: null, // 新規作成で番号が付く前に入れた物 { header, targets, main }
};
const items = new Map(); // 品番 → 右の列の値（/api/v2/lookup）
let current = null; // 開いている画面 { key, saver }

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

// 案の切り替えは画面から外した（まずは案A だけで使う）。案の持ち方はサーバに残してある
const PLAN = "A";

// 左＝対象品番（印を付ける）、まん中＝印の製品の部品（絞り込み・印を付けて［表に入れる］）。
// onProducts(印の製品) は印が変わるたびに呼び、false を返せばその変更を取り消す。onAdd(部品の品番) は［表に入れる］で呼ぶ
function chooser({ chosen, single = false, onProducts = null, onAdd = null, ro = false, note = null }) {
  const checked = new Set(chosen.filter((c) => S.targets.includes(c)));
  const left = el("div", { class: "pane" });
  const mid = onAdd ? el("div", { class: "pane" }) : null;
  const filter = el("input", { class: "in", style: "width:100%;box-sizing:border-box", placeholder: "品番・名称で絞り込み" });
  let parts = [];
  let of = 0;
  const partChecks = new Map();
  const ordered = () => S.targets.filter((t) => checked.has(t));
  const drawLeft = () => {
    const rows = S.targets.map((code) => {
      const box = el("input", { type: single ? "radio" : "checkbox", name: single ? "chooser-product" : null, checked: checked.has(code) });
      box.addEventListener("change", () => {
        const before = new Set(checked);
        if (single) checked.clear();
        if (box.checked) checked.add(code);
        else checked.delete(code);
        if (onProducts && onProducts(ordered()) === false) {
          checked.clear();
          for (const c of before) checked.add(c);
        }
        changed();
      });
      const info = items.get(code);
      return el("tr", { class: checked.has(code) ? "on" : null }, el("td", {}, box), el("td", {}, code), el("td", {}, info && info.registered ? info.name : ""));
    });
    const all = single ? null : el("input", { type: "checkbox", checked: checked.size === S.targets.length && S.targets.length > 0 });
    if (all) all.addEventListener("change", () => {
      const before = new Set(checked);
      checked.clear();
      if (all.checked) for (const t of S.targets) checked.add(t);
      if (onProducts && onProducts(ordered()) === false) {
        checked.clear();
        for (const c of before) checked.add(c);
      }
      changed();
    });
    fill(left, el("div", { class: "t" }, "対象品番", el("span", { class: "mini", style: "font-weight:400" }, single ? "1つ選ぶ" : `${checked.size}/${S.targets.length}`)),
      el("div", { class: "scroll" }, el("table", { class: "list" }, el("tr", {}, el("th", {}, all), el("th", {}, "品番"), el("th", {}, "名称")), rows)));
  };
  // まん中の枠は1回だけ組み、絞り込みの欄は作り直さない（打っている途中で欄から外れないように）。描き直すのは件数と表だけ
  const midNote = el("span", { class: "mini", style: "font-weight:400" });
  const midTable = el("div", { class: "scroll" });
  if (mid) {
    const add = el("button", { class: "btn", disabled: ro }, "表に入れる →");
    add.addEventListener("click", () => {
      const codes = parts.filter((p) => partChecks.get(p.code)?.checked).map((p) => p.code);
      if (!codes.length) return alert("表に入れる部品に印を付けてください");
      onAdd(codes);
      for (const box of partChecks.values()) box.checked = false;
    });
    fill(mid, el("div", { class: "t" }, "部品", midNote), el("div", { class: "row" }, filter), midTable,
      el("div", { class: "row" }, el("span", { class: "sp" }), add));
  }
  const drawMid = () => {
    if (!mid) return;
    const word = filter.value.trim().toLowerCase();
    const shown = parts.filter((p) => !word || p.code.toLowerCase().includes(word) || (p.name || "").toLowerCase().includes(word));
    midNote.textContent = note ?? `印の ${of} 製品の部品`;
    fill(midTable, el("table", { class: "list" }, el("tr", {}, el("th", {}, ""), el("th", {}, "品番"), el("th", { class: "r", title: "印を付けた製品のうち、使う製品の数" }, "使う"), el("th", {}, "名称")),
      shown.map((p) => {
        if (!partChecks.has(p.code)) partChecks.set(p.code, el("input", { type: "checkbox" }));
        return el("tr", {}, el("td", {}, partChecks.get(p.code)), el("td", {}, p.code), el("td", { class: "r" }, `${p.used}/${of}`), el("td", {}, p.name ?? "未登録"));
      })));
  };
  const loadParts = async () => {
    if (!mid) return;
    const d = await api("GET", "/api/v2/parts", q({ products: ordered().join(",") }));
    parts = d.parts;
    of = d.of;
    drawMid();
  };
  const changed = () => {
    drawLeft();
    loadParts().catch(fail);
  };
  filter.addEventListener("input", drawMid);
  const ready = (async () => {
    await lookup(S.targets);
    drawLeft();
    await loadParts();
  })();
  return { left, mid, ordered, ready };
}

// 画面を離れる前に、打ちかけのセルを確定して一時保存を送り切る
async function leave(cur = current) {
  if (Grid.active) Grid.active.commit();
  if (cur && cur.saver) await cur.saver.flush();
}

// 再読み込み・タブを閉じる時に、まだ送っていない一時保存があれば送って引き止める
window.addEventListener("beforeunload", (e) => {
  if (Grid.active) Grid.active.commit();
  if (current && current.saver && current.saver.pending) {
    current.saver.flush();
    e.preventDefault();
    e.returnValue = "";
  }
});

function done(text) {
  const t = new Date();
  const hm = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  document.getElementById("status").textContent = `${text} ${hm}`;
}

// 知らせを窓で出し、下の帯にも残す
function notice(text) {
  done(text);
  alert(text);
}

// 入れた値をその場で一時保存する。続けて打った分はまとめて1回で送る。
// サーバに弾かれた時は知らせて、保存済みの状態を読み直す（弾かれた値は残らない）
function autosaver(owner, save, reload) {
  let timer = null;
  let chain = Promise.resolve();
  const run = () => {
    timer = null;
    chain = chain.then(save).then(() => done("一時保存しました")).catch(async (e) => {
      fail(e);
      try {
        await reload();
      } catch (e2) {
        fail(e2);
      }
    });
    return chain;
  };
  const saver = {
    schedule() {
      clearTimeout(timer);
      timer = setTimeout(run, 300);
    },
    flush() {
      if (timer === null) return chain;
      clearTimeout(timer);
      return run();
    },
    get pending() {
      return timer !== null;
    },
  };
  owner.saver = saver;
  return saver;
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
  current = { key };
  document.getElementById("scrno").textContent = NUM[key];
  const main = document.getElementById("screen");
  main.replaceChildren();
  window.scrollTo(0, 0);
  try {
    await SCREENS[key](main, current);
  } catch (e) {
    fill(main, card(NUM[key], [], ""));
    done(`読込に失敗しました（${e && e.message ? e.message : e}）`);
  }
}

async function closeScreen() {
  await leave();
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
}

// ② 設計検討 -------------------------------------------------------------

async function hubScreen(main, cur) {
  const ro = S.mode === "view" || S.version !== null;
  const draft = S.mode === "new" ? S.draft : null;
  const modes = [["new", "新規作成"], ["edit", "修正"], ["view", "照会"]];
  const numberInput = el("input", { class: "in", style: "min-width:110px", value: S.number, disabled: S.mode === "new" });
  const versionSelect = el("select", { class: "in", style: "min-width:70px", disabled: S.mode === "new" },
    el("option", { value: "" }, "仮"), S.versions.map((v) => el("option", { value: String(v) }, String(v))));
  versionSelect.value = S.version === null ? "" : String(S.version);
  const read = async () => {
    const number = numberInput.value.trim();
    if (!number) return;
    await leave(cur);
    loadStudy(number, versionSelect.value || null).catch(fail);
  };
  numberInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") read();
  });

  const h = draft ? draft.header : S.header || {};
  const field = (label, key, width) => {
    const input = el("input", { class: ro ? "in ro" : "in", value: h[key] || "", readonly: ro, tabindex: ro ? "-1" : null, style: width ? `min-width:${width}px` : null });
    input.dataset.key = key;
    input.addEventListener("input", () => {
      if (draft) draft.header[key] = input.value;
      else if (S.number) saver.schedule();
    });
    return el("label", { class: "f" }, label, input);
  };
  const header = () => Object.fromEntries([...main.querySelectorAll("input[data-key]")].map((i) => [i.dataset.key, i.value]));
  const saver = autosaver(cur, async () => applyStudy(await api("PUT", "/api/v2/study", { number: S.number }, header())),
    () => loadStudy(S.number, S.version));
  const fields = [field("題目", "title", 260), field("担当", "owner"), field("承認者", "approver"), field("開発番号", "dev_number")];
  const state = S.number ? (S.version === null ? "仮" : `版${S.version}`) : "";
  const updated = h.updated_at ? h.updated_at.slice(0, 10).replaceAll("-", "/") : "";
  // 新規作成は ③ で対象品番を選んでから［実行］で作る。作った時に番号が付く
  const run = draft ? el("button", { class: "btn pri", style: "align-self:flex-end" }, "実行") : null;
  if (run) run.addEventListener("click", async () => {
    if (!draft.targets.some(Boolean)) return fail(new Error("対象品番を入れてから実行してください（［対象品番］で選ぶ）"));
    try {
      applyStudy(await api("POST", "/api/v2/studies", {}, { ...header(), products: draft.targets.filter(Boolean), main_product: draft.main }));
      S.mode = "edit";
      S.draft = null;
      notice(`検討 ${S.number} を作成しました（対象品番 ${S.targets.length} 製品）`);
      openScreen(2);
    } catch (e) {
      fail(e);
    }
  });

  const needStudy = !S.number;
  const big = (key, label, extra) => el("button", {
    class: "btn big", disabled: key !== 3 && needStudy,
    onclick: async () => { await leave(cur); openScreen(key); },
  }, label, extra);
  const release = el("button", { class: "btn big", style: "margin-left:24px;align-self:center", disabled: !canWrite() }, "版数取得");
  release.addEventListener("click", async () => {
    await leave(cur);
    if (!confirm(`${S.number} の版を取得します。取得した版は書き換えできません。よろしいですか？`)) return;
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

async function setMode(mode) {
  await leave();
  if (mode === "new") {
    Object.assign(S, { number: "", version: null, header: null, versions: [], targets: [], problems: 0,
      draft: { header: {}, targets: [], main: null } });
    items.clear();
  } else S.draft = null;
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
          onclick: async () => {
            await leave();
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
  // 新規作成で番号が付く前は、選んだ物を S.draft に持つ（② の［実行］で検討と一緒に作る）
  const draft = S.mode === "new" && !S.number ? S.draft : null;
  const ro = !(canWrite() || draft);
  const EMPTY_ROW = () => "";
  let rows = draft ? draft.targets : [];
  let mainProduct = draft ? draft.main : null;
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
      touched();
    },
    action(r, c) {
      if (c !== 6 || ro || !rows[r]) return false;
      mainProduct = rows[r];
      touched();
      return true;
    },
    changed() {
      lookup(rows).then((got) => got && grid.render()).catch(fail);
    },
  };
  const grid = new Grid(body, model);
  const saver = autosaver(cur, async () => {
    const d = await api("PUT", "/api/v2/targets", { number: S.number }, { products: rows.filter(Boolean), main_product: mainProduct });
    S.targets = d.targets;
  }, async () => {
    const d = await api("GET", "/api/v2/targets", q());
    rows = d.targets.slice();
    mainProduct = d.main_product;
    grid.render();
  });
  // 変えた時: 番号の前は S.draft に持ち、番号があればその場で一時保存
  function touched() {
    if (draft) draft.main = mainProduct;
    else saver.schedule();
  }

  const addCodes = (codes) => {
    for (const code of codes) if (code && !rows.includes(code)) rows.push(code);
    touched();
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
  main.append(card("対象品番", [
    el("button", { class: "btn", disabled: ro, onclick: () => productSearchDialog(addCodes) }, "品番検索"),
    el("button", { class: "btn", disabled: ro, onclick: () => csvInput.click() }, "CSV取込"), csvInput,
    el("button", {
      class: "btn", disabled: ro, onclick: () => {
        if (grid.sel.r < rows.length) {
          rows.splice(grid.sel.r, 1);
          touched();
          grid.render();
        }
      },
    }, "行削除"),
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

const MAX_FILE_BYTES = 16 * 1024 * 1024;
const TABLE_LABELS = { item: "品目", bom_line: "構成", cost: "原価", volume: "年間台数" };

// ファイルから読んだ文字（見出し・値）はそのまま出す。el の文字は語の読み替えを通るため
function raw(text) {
  return document.createTextNode(text ?? "");
}

function setOptions(select, pairs, value) {
  fill(select, pairs.map(([v, t]) => el("option", { value: v }, raw(t))));
  select.value = value ?? "";
}

function countsTable(counts) {
  const keys = Object.keys(TABLE_LABELS).filter((k) => k in counts);
  return el("table", { class: "list", style: "margin:4px 0 8px" },
    el("tr", {}, keys.map((k) => el("th", {}, TABLE_LABELS[k]))),
    el("tr", {}, keys.map((k) => el("td", { class: "r" }, counts[k]))));
}

// ファイルを選ぶ → シートと表を選ぶ → 列の対応を確かめる → 一覧に加える → 確認 → 決定
async function importDialog(after) {
  const file = el("input", { type: "file", accept: ".csv,.xlsx,.xlsm" });
  const sheet = el("select", { class: "in" });
  const sheetLabel = el("label", { class: "mini", hidden: true }, "シート ", sheet);
  const kind = el("select", { class: "in" });
  const now = el("div");
  const mapping = el("div");
  const list = el("div");
  const check = el("div");
  const pending = [];
  let preview = null; // { file, content, data }
  let checked = null; // 確認した時に送った中身。決定で同じ物を送る
  const checkButton = el("button", { class: "btn pri", onclick: () => runCheck() }, "確認");
  // 確認を出している間は［決定］が主ボタン
  const clearCheck = () => { checked = null; fill(check); checkButton.classList.add("pri"); };

  const warningBox = (warnings) => (warnings && warnings.length ? el("div", { class: "msg" }, raw(warnings.join("\n"))) : null);

  const drawMapping = () => {
    if (!preview) { fill(mapping); return; }
    const d = preview.data;
    const def = d.tables.find((t) => t.id === kind.value) || d.tables[0];
    const selects = {};
    const rows = def.fields.map((f) => {
      const select = el("select", { class: "in" });
      setOptions(select, [["", "（使わない）"], ...d.headers.filter(Boolean).map((h) => [h, h])], (def.saved ?? def.guess)[f.name] || "");
      selects[f.name] = select;
      return el("tr", {}, el("td", {}, f.label, f.required ? el("span", { class: "pill warn", style: "margin-left:6px" }, "必須") : null), el("td", {}, select));
    });
    const add = () => {
      const missing = def.fields.filter((f) => f.required && !selects[f.name].value).map((f) => f.label);
      if (missing.length) { fail(new Error(`必須の列が対応づいていません: ${missing.join("・")}`)); return; }
      pending.push({
        filename: d.filename, content: preview.content, sheet: d.sheet || undefined, table: def.id,
        columns: Object.fromEntries(Object.entries(selects).filter(([, s]) => s.value).map(([k, s]) => [k, s.value])),
        fieldLabels: Object.fromEntries(def.fields.map((f) => [f.name, f.label])),
      });
      preview = null;
      file.value = "";
      sheetLabel.hidden = true;
      drawMapping();
      drawPending();
    };
    fill(mapping,
      el("div", { class: "mini" }, raw(`${d.filename}${d.sheet ? `（シート ${d.sheet}）` : ""}・${d.row_count} 行・列 ${d.headers.length}`)),
      el("table", { class: "list", style: "margin:4px 0 8px" }, el("tr", {}, el("th", {}, "モックの項目"), el("th", {}, "元の列")), rows),
      el("div", { class: "mini" }, `先頭 ${d.sample.length} 行`),
      el("div", { style: "overflow:auto" }, el("table", { class: "list" },
        el("tr", {}, d.headers.map((h) => el("th", {}, raw(h || "（空）")))),
        d.sample.map((r) => el("tr", {}, r.map((c) => el("td", {}, raw(c))))))),
      el("div", { class: "row", style: "margin:8px 0" }, el("button", { class: "btn", onclick: add }, "この内容で一覧に加える")));
  };

  const drawPending = () => {
    clearCheck(); // 一覧が変われば確認はやり直す
    fill(list,
      el("div", { class: "mini", style: "margin-top:8px" }, "取り込む一覧"),
      pending.length
        ? el("table", { class: "list", style: "margin:4px 0 8px" },
          el("tr", {}, ["ファイル", "表", "対応づけ", ""].map((t) => el("th", {}, t))),
          pending.map((f, i) => el("tr", {},
            el("td", {}, raw(`${f.filename}${f.sheet ? `（${f.sheet}）` : ""}`)),
            el("td", {}, TABLE_LABELS[f.table] ?? f.table),
            el("td", {}, raw(Object.entries(f.columns).map(([k, v]) => `${f.fieldLabels[k] ?? k}←${v}`).join("・"))),
            el("td", {}, el("button", { class: "btn quiet", onclick: () => { pending.splice(i, 1); drawPending(); } }, "外す")))))
        : el("div", { class: "mini" }, "なし"));
  };

  const load = async (sheetName) => {
    const f = file.files[0];
    if (!f) return;
    if (f.size > MAX_FILE_BYTES) throw new Error(`${f.name} が大きすぎます（上限 16 MB）`);
    const content = preview?.file === f ? preview.content : toBase64(await f.arrayBuffer());
    const data = await api("POST", "/api/import/preview", {}, { filename: f.name, content, sheet: sheetName });
    preview = { file: f, content, data };
    sheetLabel.hidden = !data.sheets.length;
    setOptions(sheet, data.sheets.map((s) => [s, s]), data.sheet);
    // 保存した対応づけが無い時は、見出しの当たりがいちばん強い表を仮に選ぶ
    setOptions(kind, data.tables.map((t) => [t.id, `${t.label}${t.saved ? "（保存した対応づけあり）" : ""}`]),
      data.suggested || data.guessed || data.tables[0].id);
    drawMapping();
  };

  const showNow = (counts, warnings) => fill(now, el("div", { class: "mini" }, "いま入っている件数"), countsTable(counts), warningBox(warnings));

  const runCheck = async () => {
    if (!pending.length) { fail(new Error("取り込む一覧が空です")); return; }
    const body = { files: pending.map((f) => ({ filename: f.filename, content: f.content, sheet: f.sheet, table: f.table, columns: f.columns })) };
    try {
      const r = await api("POST", "/api/v2/bom/import", {}, { ...body, check: true });
      checked = body;
      checkButton.classList.remove("pri");
      fill(check,
        el("div", { class: "mini" }, "取り込みの確認（読んだ件数。この表の中身を入れ替えます）"),
        countsTable(r.counts),
        warningBox(r.warnings),
        el("div", { class: "row" },
          el("button", { class: "btn pri", onclick: decide }, "決定"),
          el("button", { class: "btn", onclick: clearCheck }, "やめる")));
    } catch (e) {
      fail(e);
    }
  };

  const finish = (r) => {
    showNow(r.counts, r.warnings);
    done("取込しました");
    after();
  };

  const decide = async () => {
    try {
      const r = await api("POST", "/api/v2/bom/import", {}, checked);
      pending.length = 0;
      drawPending();
      finish(r);
    } catch (e) {
      fail(e);
    }
  };

  file.addEventListener("change", () => load().catch(fail));
  sheet.addEventListener("change", () => load(sheet.value).catch(fail));
  kind.addEventListener("change", drawMapping);

  let status = { samples: false, counts: null };
  try {
    status = await api("GET", "/api/v2/bom");
  } catch (e) {
    fail(e);
  }
  const d = dialog("部品表データ取込（モック）", [
    now,
    el("div", { class: "row" }, file, sheetLabel, el("label", { class: "mini" }, "表 ", kind)),
    mapping, list, check,
  ], [
    status.samples ? el("button", { class: "btn", onclick: () => api("POST", "/api/v2/bom/import", {}, { sample: true }).then(finish, fail) }, "見本") : null,
    el("span", { class: "sp" }),
    checkButton,
    el("button", { class: "btn", onclick: () => d.close() }, "閉じる"),
  ]);
  setOptions(kind, Object.entries(TABLE_LABELS), "item");
  drawPending();
  if (status.counts) showNow(status.counts);
}

// ④ 置換 -----------------------------------------------------------------

async function replaceScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  const products = S.targets.slice();
  const FIXED = 6; // 印・番号・元品番・名称・新品番・名称。その右に対象品番の列
  const blank = () => ({ old: "", new: "", overridden: false, uses: [], skip: new Set(), mark: false });
  let rows = []; // { old, new, overridden, uses: 元品番を使う対象品番（null はまだ聞いていない）, skip: 置き換えない製品, mark: 行削除の印 }
  const usesCache = new Map();
  const load = async () => {
    const d = S.number ? await api("GET", "/api/v2/replace", q({ plan })) : { rows: [] };
    rows = d.rows.map((r) => ({ old: r.old, new: r.new, overridden: r.overridden, uses: r.uses, skip: new Set(r.skip), mark: false }));
    for (const r of d.rows) usesCache.set(r.old, r.uses);
    await lookup(rows.flatMap((r) => [r.old, r.new]));
  };
  await load();
  // 元品番を打った・選んだ行の「使う製品」を埋める（埋めた行があれば true）
  const fillUses = async () => {
    let got = false;
    for (const r of rows) {
      if (!r.old || r.uses !== null) continue;
      if (!usesCache.has(r.old)) {
        const d = await api("GET", "/api/v2/where-used", q({ code: r.old, group: "inside" }));
        usesCache.set(r.old, d.products.map((x) => x.code));
      }
      r.uses = usesCache.get(r.old);
      got = true;
    }
    return got;
  };
  const markAll = () => {
    const all = !(rows.length && rows.every((r) => r.mark));
    for (const r of rows) r.mark = all;
  };
  const body = el("div");
  const model = {
    head: () => [
      { text: rows.length && rows.every((r) => r.mark) ? "☑" : "☐", cls: "c", title: "全部の行に印", action: ro ? null : markAll },
      "", "元品番", "名称", "新品番", "名称",
      ...products.map((code) => ({ text: code, cls: "c", title: items.get(code)?.name ?? null })),
    ],
    nRows: () => rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push(blank()); return true; },
    nCols: () => FIXED + products.length,
    cell(r, c) {
      const row = rows[r];
      if (c === 0) return { text: row ? (row.mark ? "☑" : "☐") : "", cls: "act" };
      if (c === 1) return { text: String(r + 1), cls: "n" };
      if (c === 2) return { text: row ? row.old : "", editable: !ro };
      if (c === 3) return nameCell(row && row.old);
      if (c === 4) return { text: row ? row.new : "", editable: !ro };
      if (c === 5) return nameCell(row && row.new);
      const product = products[c - FIXED];
      if (!row || !row.old || row.uses === null) return { text: "" };
      if (!row.uses.includes(product)) return { text: "—", cls: "none", title: "この製品は元品番を使っていない" };
      const on = !row.skip.has(product);
      return { text: on ? "☑" : "☐", cls: "act", title: on ? "この製品で置き換える" : "この製品では置き換えない" };
    },
    set(r, c, text) {
      if (c !== 2 && c !== 4) return;
      if (r >= rows.length) {
        if (!text) return;
        rows.push(blank());
      }
      const row = rows[r];
      if (c === 2 && row.old !== text) {
        row.old = text;
        row.uses = text ? null : [];
        row.skip = new Set();
      }
      if (c === 4) row.new = text;
      row.overridden = false;
      saver.schedule();
    },
    // 左端は行削除の印。製品の列は、押すと置き換える／置き換えないを切り替える
    action(r, c) {
      const row = rows[r];
      if (!row || ro) return false;
      if (c === 0) {
        row.mark = !row.mark;
        return true;
      }
      if (c < FIXED) return false;
      const product = products[c - FIXED];
      if (!row.uses || !row.uses.includes(product)) return false;
      if (row.skip.has(product)) row.skip.delete(product);
      else row.skip.add(product);
      saver.schedule();
      return true;
    },
    changed() {
      Promise.all([lookup(rows.flatMap((r) => [r.old, r.new])), fillUses()])
        .then(([a, b]) => (a || b) && grid.render()).catch(fail);
    },
  };
  const grid = new Grid(body, model);
  // 元品番と新品番の両方がそろった行だけを送る（片方だけの行は打ちかけとして画面に残す）
  const saver = autosaver(cur, async () => {
    const d = await api("PUT", "/api/v2/replace", { number: S.number, plan },
      { rows: rows.filter((r) => r.old && r.new).map((r) => ({ old: r.old, new: r.new, skip: [...r.skip] })) });
    const marks = new Map(d.rows.map((x) => [`${x.old}\t${x.new}`, x.overridden]));
    for (const r of rows) r.overridden = marks.get(`${r.old}\t${r.new}`) || false;
    grid.render();
  }, async () => {
    await load();
    grid.render();
  });
  const del = el("button", { class: "btn", disabled: ro }, "行削除");
  del.addEventListener("click", () => {
    if (Grid.active) Grid.active.commit();
    const n = rows.filter((r) => r.mark).length;
    if (!n) return alert("消す行の左端に印を付けてください");
    rows = rows.filter((r) => !r.mark);
    saver.schedule();
    grid.render();
    done(`${n} 行を消しました`);
  });
  // 左の部品の一覧（対象品番の全製品の部品）から選んだ部品を、元品番の行に足す（新品番は打つ）
  const pick = chooser({
    chosen: S.targets, ro, note: "対象品番の部品", onAdd: (codes) => {
      for (const code of codes) if (!rows.some((r) => r.old === code)) rows.push({ ...blank(), old: code, uses: null });
      grid.changed();
    },
  });
  await pick.ready;
  await fillUses();
  main.append(card("置換", [del], el("div", { class: "chooser parts" }, pick.mid, el("div", { class: "work" }, body))));
  grid.render();
  grid.focus();
}

// ⑤ グリッド画面 ---------------------------------------------------------------

async function gridScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let cols = []; // 列の製品（左で印を付けた対象品番＋使用先で足した製品）
  let rows = []; // { part, cells: 列の位置ごとの値, origin, problems }
  let original = new Map(); // 「親品番\t品番」 → 保存済みの値
  const key = (product, part) => `${product}\t${part}`;
  const load = async () => {
    const d = S.number ? await api("GET", "/api/v2/grid", q({ plan })) : { columns: [], rows: [] };
    cols = d.columns.slice();
    rows = d.rows.map((r) => ({ part: r.part, cells: cols.map((c) => r.cells[c] ?? ""), origin: r.origin, problems: r.problems || {}, locked: r.locked || {} }));
    original = new Map();
    for (const r of d.rows) {
      for (const [c, v] of Object.entries(r.cells)) original.set(key(c, r.part), v);
    }
    await lookup([...cols, ...rows.map((r) => r.part)]);
  };
  await load();
  const body = el("div");
  const model = {
    nRows: () => 2 + rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push({ part: "", cells: [], origin: null, problems: {}, locked: {} }); return true; },
    nCols: () => 4 + cols.length,
    cell(r, c) {
      if (r === 0) {
        if (c === 0) return { text: "", cls: "n" };
        if (c === 1) return { text: "親品番 →", th: true };
        if (c < 4) return { text: "", th: true };
        const code = cols[c - 4] || "";
        const outside = code && !S.targets.includes(code);
        return { text: code, th: true, cls: outside ? "bad" : "", title: outside ? "要確認（対象品番に無いので計算に入らない）" : null };
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
      if (r >= 2 && c === 1) {
        const i = r - 2;
        if (i >= rows.length) {
          if (!text) return;
          rows.push({ part: text, cells: [], origin: null, problems: {}, locked: {} });
        } else rows[i].part = text;
      } else if (r >= 2 && c >= 4) {
        const row = rows[r - 2];
        if (!row || !cols[c - 4]) return;
        // 数でない数量はその場で弾く（入れずに元の値のまま）
        if (text !== "" && !isNumber(text)) return alert(`数量は数で入力してください（${row.part} × ${cols[c - 4]}「${text}」）`);
        row.cells[c - 4] = text;
        row.problems = {};
      } else return;
      saver.schedule();
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

  // 保存済みの値と違うセルだけを送る。打っている途中の値は読み直さず、印（要確認・置換の行）だけ受け取る
  const saver = autosaver(cur, async () => {
    const now = new Map();
    const rowOf = new Map();
    rows.forEach((row, i) => {
      if (!row.part) return;
      cols.forEach((code, j) => {
        if (!code) return;
        const value = row.cells[j] ?? "";
        if (value !== "") now.set(key(code, row.part), value);
        rowOf.set(key(code, row.part), i);
      });
    });
    const cells = [];
    for (const k of new Set([...original.keys(), ...now.keys()])) {
      if (same(original.get(k), now.get(k))) continue;
      const [product, part] = k.split("\t");
      cells.push({ product, part, value: now.get(k) ?? null, row: rowOf.get(k) ?? 0 });
    }
    const d = await api("PUT", "/api/v2/grid", { number: S.number, plan },
      { columns: cols.filter(Boolean), rows: rows.map((r) => r.part).filter(Boolean), cells });
    for (const c of cells) {
      if (c.value === null) original.delete(key(c.product, c.part));
      else original.set(key(c.product, c.part), c.value);
    }
    const byPart = new Map(d.rows.map((r) => [r.part, r]));
    for (const row of rows) {
      const got = byPart.get(row.part);
      row.problems = got ? got.problems || {} : {};
      row.locked = got ? got.locked || {} : {};
      row.origin = got ? got.origin : null;
    }
    grid.render();
  }, async () => {
    await load();
    grid.render();
  });
  const whereUsed = () => {
    const row = rows[grid.sel.r - 2];
    if (!row || !row.part) return alert("品番の行を選んでください");
    whereUsedDialog(row.part, ro ? null : addColumns);
  };
  // 使用先で選んだ製品を、まだ無ければ列の右端に足す。足した数を返す
  const addColumns = (codes) => {
    const fresh = codes.filter((c) => !cols.includes(c));
    if (!fresh.length) return 0;
    // 見出しを消した右端の空き列は詰めてから足す（空き列の下に残った値も捨てる）
    while (cols.length && !cols[cols.length - 1]) cols.pop();
    for (const row of rows) row.cells.length = Math.min(row.cells.length, cols.length);
    cols.push(...fresh);
    saver.schedule();
    model.changed();
    grid.render();
    return fresh.length;
  };
  // 列は左で印を付けた対象品番の順。使用先で足した対象品番の外の製品は右に残す。
  // 数量の入った列の印を外す時は、その列の数量を消すかを聞く（隠したまま計算に入るのを防ぐ）
  const setColumns = (products) => {
    const extra = cols.filter((c) => c && !S.targets.includes(c));
    const next = [...products, ...extra];
    const dropped = cols.filter((c) => c && !next.includes(c));
    const filled = dropped.filter((code) => rows.some((row) => (row.cells[cols.indexOf(code)] ?? "") !== ""));
    if (filled.length && !confirm(`${filled.join("・")} の列に数量が入っています。列を外すと、その数量を消します。よろしいですか？`)) return false;
    rows = rows.map((row) => ({ ...row, cells: next.map((code) => (cols.includes(code) ? row.cells[cols.indexOf(code)] ?? "" : "")) }));
    cols = next;
    saver.schedule();
    model.changed();
    grid.render();
    return true;
  };
  // まん中で選んだ部品を行に足す
  const addRows = (codes) => {
    for (const code of codes) if (!rows.some((r) => r.part === code)) rows.push({ part: code, cells: [], origin: null, problems: {}, locked: {} });
    saver.schedule();
    grid.changed();
  };
  const first = cols.some((c) => S.targets.includes(c)) ? cols : S.targets;
  const pick = chooser({ chosen: first, ro, onProducts: ro ? () => false : setColumns, onAdd: addRows });
  await pick.ready;
  if (!ro && !cols.length && S.targets.length) setColumns(pick.ordered());
  main.append(card("グリッド画面", [
    el("button", { class: "btn", onclick: whereUsed, disabled: !S.number }, "使用先"),
    el("button", { class: "btn", onclick: conditionsDialog, disabled: !S.number }, "計算条件"),
  ], el("div", { class: "chooser" }, pick.left, pick.mid, el("div", { class: "work" }, body))));
  grid.focus();
}

// onAdd があれば、開いた製品に印を付けてグリッドの列に足せる（onAdd は足した数を返す）
async function whereUsedDialog(code, onAdd = null) {
  try {
    const d = await api("GET", "/api/v2/where-used", q({ code }));
    const list = el("div");
    const open = async (group) => {
      try {
        const g = await api("GET", "/api/v2/where-used", q({ code, group }));
        const checks = g.products.map((p) => el("input", { type: "checkbox", value: p.code }));
        const all = el("input", { type: "checkbox", onchange: () => checks.forEach((c) => { c.checked = all.checked; }) });
        const add = () => {
          const chosen = checks.filter((c) => c.checked).map((c) => c.value);
          if (!chosen.length) return alert("列に足す製品に印を付けてください");
          const n = onAdd(chosen);
          done(n ? `${n} 製品を列に足しました` : "選んだ製品はもう列にあります");
        };
        fill(list, el("table", { class: "list", style: "margin-top:8px" },
          el("tr", {}, onAdd ? el("th", {}, all) : null, el("th", {}, "品番"), el("th", {}, "名称")),
          g.products.map((p, i) => el("tr", {}, onAdd ? el("td", {}, checks[i]) : null, el("td", {}, p.code), el("td", {}, p.name ?? "")))),
          onAdd && g.products.length ? el("div", { class: "row", style: "margin:8px 0 0" },
            el("button", { class: "btn pri", onclick: add }, "選んだ製品を列に足す")) : null);
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
    const owner = {};
    const saver = autosaver(owner, async () => {
      const annual = {};
      for (const [p, input] of inputs) annual[p] = input.value.trim() || null;
      await api("PUT", "/api/v2/conditions", { number: S.number }, { annual });
    }, async () => {
      const again = await api("GET", "/api/v2/conditions", q());
      for (const r of again.rows) if (inputs.has(r.product)) inputs.get(r.product).value = r.annual ?? "";
    });
    const box = dialog("計算条件", el("table", { class: "list" },
      el("tr", {}, el("th", {}, "対象品番"), el("th", {}, "年間台数（部品表）"), el("th", {}, "年間台数")),
      d.rows.map((r) => {
        const input = el("input", { class: "in", value: r.annual ?? "", readonly: ro, style: "min-width:90px;width:100px;text-align:right" });
        let kept = input.value;
        // 数でない台数はその場で弾く（元の値に戻す）。通った値はその場で一時保存
        input.addEventListener("change", () => {
          const text = input.value.trim();
          if (text !== "" && !isNumber(text)) {
            alert(`年間台数は数で入力してください（${r.product}「${text}」）`);
            input.value = kept;
            return;
          }
          kept = input.value;
          saver.schedule();
        });
        inputs.set(r.product, input);
        return el("tr", {}, el("td", {}, r.product), el("td", { class: "r" }, fmt(r.bom_annual)), el("td", {}, input));
      })), [
      el("span", { class: "sp" }),
      el("button", { class: "btn", onclick: async () => { await saver.flush(); box.close(); } }, "閉じる"),
    ]);
  } catch (e) {
    fail(e);
  }
}

// ⑧ 書き換え -------------------------------------------------------------

async function rewriteScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let product = S.targets[0] || "";
  let rows = [];
  const load = async () => {
    rows = [];
    if (S.number && product) {
      const d = await api("GET", "/api/v2/rewrite", q({ plan, product }));
      rows = d.rows.map((r) => ({ ...r, balloon: r.balloon ?? "", fresh: r.before_part === null }));
    }
    await lookup(rows.map((r) => r.part));
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
      // 数でない数量はその場で弾く（入れずに元の値のまま）
      if (c === 5 && text !== "" && !isNumber(text)) return alert(`数量は数で入力してください（${row.part || "品番なし"}「${text}」）`);
      row[field] = text;
      saver.schedule();
    },
    changed() {
      lookup(rows.map((r) => r.part)).then((got) => got && grid.render()).catch(fail);
    },
  };
  const grid = new Grid(body, model);
  // 製品は左の対象品番から1つ選ぶ
  const pick = chooser({
    chosen: product ? [product] : [], single: true, onProducts: (chosen) => {
      const next = chosen[0] || "";
      if (next === product) return true;
      leave(cur).then(async () => {
        product = next;
        await load();
        grid.render();
      }).catch(fail);
      return true;
    },
  });
  await pick.ready;
  // 親と品番のそろった行だけを送る（親の無い行は打ちかけとして画面に残す）
  const saver = autosaver(cur, async () => {
    if (!product) return;
    const send = rows.filter((r) => r.part && r.parent)
      .map((r) => ({ parent: r.parent, balloon: r.balloon, part: r.part, quantity: r.quantity }));
    await api("PUT", "/api/v2/rewrite", { number: S.number, plan }, { product, rows: send });
  }, async () => {
    await load();
    grid.render();
  });
  main.append(card("書き換え", [], el("div", { class: "chooser two" }, pick.left, el("div", { class: "work" }, body))));
  grid.focus();
}

// ⑥ 変更一覧 -------------------------------------------------------------

async function changesScreen(main) {
  const plan = PLAN;
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
  main.append(card("変更一覧", [productSelect,
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
  const drawChips = () => fill(chips, [["total", "合計"], ["material", "材料"], ["processing", "加工"], ["amortization", "型費"]].map(([k, label]) =>
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
        })),
      // 年間の影響 ÷ 年間台数の合計（1台あたりの増減）
      el("tr", {}, el("th", {}, "台数の加重平均"), el("th", {}), el("th", { class: "mini" }, "1台あたり"),
        plans.map((p) => {
          const v = data.weighted[p];
          const n = Number(v);
          return el("th", {
            class: "r" + (v === null ? "" : n < 0 ? " minus" : n > 0 ? " plus" : "") + (data.impact_incomplete[p] ? " inc" : ""),
            title: data.impact_incomplete[p] ? "不完全" : null,
          }, v === null ? "—" : (n < 0 ? "−" : n > 0 ? "+" : "") + fmt(String(Math.abs(n))));
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
    el("button", { class: "btn pri", onclick: compute }, "実行"),
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
