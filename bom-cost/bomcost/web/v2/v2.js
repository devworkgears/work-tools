// 設計検討の画面。② から ③ はその場で、④〜⑩ は別の窓で開く（② は開いたまま）。入れた値はその場で一時保存する（⑦の［実行］は点検と計算だけで、書かない）。
"use strict";

const NUM = { 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧", 9: "⑨", 10: "⑩", 11: "⑪" };
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

// 別の窓: ② から ④〜⑩ を開く。窓の URL に画面・検討番号・版・処理を載せる
const NAMES = { 5: "構成一覧", 6: "変更一覧", 7: "結果", 8: "構成変更", 9: "仮品番", 10: "費目変更", 11: "部品変更" };
const PARAMS = new URLSearchParams(location.search);
const POPUP = PARAMS.has("screen");
// 窓どうしの知らせ。saved＝その検討を一時保存した（ほかの窓は戻った時に読み直す）・switch＝② が別の検討・版に替えた（別の窓は閉じる）
const channel = "BroadcastChannel" in window ? new BroadcastChannel("bomcost-v2") : null;
let stale = false;

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

// 処理中の表示。通信でも work でも、長引いた時だけ上に帯を出し、3秒を超えたら経過の秒も出す
const busy = {
  texts: [], // 動いている処理の文（後から始めた物が後ろ）
  shown: 0, // 帯を出している間、始めた時刻（出していなければ 0）
  timer: null,
  start(text) {
    const job = { text };
    this.texts.push(job);
    if (!this.timer) {
      const from = Date.now();
      this.timer = setInterval(() => {
        this.shown = from;
        this.draw();
      }, 300);
    } else if (this.shown) this.draw();
    return () => {
      this.texts.splice(this.texts.indexOf(job), 1);
      if (this.texts.length) return this.shown ? this.draw() : undefined;
      clearInterval(this.timer);
      this.timer = null;
      this.shown = 0;
      document.getElementById("busy").hidden = true;
    };
  },
  draw() {
    const box = document.getElementById("busy");
    const named = this.texts.findLast((t) => t.text);
    const sec = Math.floor((Date.now() - this.shown) / 1000);
    box.querySelector("span").textContent = `${named ? named.text : "処理中"}…${sec >= 3 ? ` ${sec}秒` : ""}`;
    box.hidden = false;
  },
};

// 時間のかかる操作。終わるまで画面を押せなくする（二重に押すと、版が2つできる・2回取り込むため）
let working = 0;
async function work(text, run) {
  const end = busy.start(text);
  const focused = document.activeElement;
  const lock = (on) => {
    for (const node of document.body.children) if (node.id !== "busy" && node.id !== "scrno") node.inert = on;
  };
  working += 1;
  lock(true);
  try {
    return await run();
  } finally {
    working -= 1;
    if (!working) {
      lock(false);
      if (focused && focused.isConnected && document.activeElement === document.body) focused.focus({ preventScroll: true });
    }
    end();
  }
}

// 通信はどれも処理中に数える（帯は長引いた時だけ出る）
async function request(path, options) {
  const end = busy.start("");
  try {
    return await fetch(path, options);
  } finally {
    end();
  }
}

async function api(method, path, params = {}, body) {
  const res = await request(path + query(params), {
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
  const res = await request(path + query(params));
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
  const params = S.number ? q() : {};
  const data = await api("POST", "/api/v2/lookup", params, { codes: need });
  for (const [code, info] of Object.entries(data.items)) items.set(code, info);
  return true;
}

function nameCell(code) {
  if (!code) return { text: "" };
  const info = items.get(code);
  if (!info) return { text: "", cls: "auto" };
  if (!info.registered) return { text: "未登録", cls: "bad" };
  if (info.temp) return { text: `${info.name}（仮）`, cls: "auto", title: "仮品番（⑨ で登録した品番）" };
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
  // 絞り込みは品番と名称の2つの欄（③ の品番検索と同じ並び）。両方に入れた時は両方に当たる部品だけ
  const codeBox = el("input", { class: "in", style: "width:100%;box-sizing:border-box;min-width:0" });
  const nameBox = el("input", { class: "in", style: "width:100%;box-sizing:border-box;min-width:0" });
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
    fill(mid, el("div", { class: "t" }, "部品", midNote), el("div", { class: "row" }, el("label", { class: "f", style: "flex:1;min-width:0" }, "品番", codeBox), el("label", { class: "f", style: "flex:1;min-width:0" }, "名称", nameBox)), midTable,
      el("div", { class: "row" }, el("span", { class: "sp" }), add));
  }
  const drawMid = () => {
    if (!mid) return;
    const code = codeBox.value.trim().toLowerCase();
    const name = nameBox.value.trim().toLowerCase();
    const shown = parts.filter((p) => (!code || p.code.toLowerCase().includes(code)) && (!name || (p.name || "").toLowerCase().includes(name)));
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
  codeBox.addEventListener("input", drawMid);
  nameBox.addEventListener("input", drawMid);
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
    chain = chain.then(save).then(() => {
      done("一時保存しました");
      tell("saved");
    }).catch(async (e) => {
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

// 別の窓 --------------------------------------------------------------

function tell(type) {
  if (channel) channel.postMessage({ type, number: S.number, version: S.version });
}

// 同じ名前の窓が同じ検討の同じ画面を出していれば前に出すだけ（読み直すと打ちかけが消えるため）
function openWindow(key) {
  const url = location.pathname + query({ screen: key, number: S.number, version: S.version ?? "", mode: S.mode });
  const w = window.open("", `bomcost-${key}`, "popup,width=1280,height=820");
  if (!w) return false;
  let same = false;
  try {
    same = w.location.pathname + w.location.search === url;
  } catch (_) {
    // 読めない窓は開き直す
  }
  if (!same) w.location.href = url;
  w.focus();
  return true;
}

if (channel) channel.onmessage = (e) => {
  const m = e.data || {};
  if (m.type === "switch" && POPUP && (m.number !== S.number || m.version !== S.version)) {
    leave().finally(() => window.close());
  } else if (m.type === "saved" && S.number && m.number === S.number && !stale) {
    stale = true;
    done("ほかの窓で一時保存がありました（この窓に戻ると読み直します）");
  }
};

// ほかの窓で保存があった時は、この窓に戻った時に読み直す。打ちかけ・送りかけがある間と ⑦（［実行］で計算し直す）は描き直さない
window.addEventListener("focus", async () => {
  if (!stale || !current || !S.number) return;
  if ((Grid.active && Grid.active.editing) || (current.saver && current.saver.pending)) return;
  if (document.querySelector(".overlay")) return; // 窓を開いている間は下の画面を描き直さない（閉じた後に読み直す）
  stale = false;
  items.clear();
  try {
    await loadStudy(S.number, S.version, false);
    if (current.key !== 7) openScreen(current.key);
  } catch (e) {
    fail(e);
  }
});

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
  if (POPUP) window.close();
  else openHub();
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
  if (S.number !== data.header.number || S.version !== data.version) {
    items.clear();
    if (!POPUP && S.number) tell("switch");
  }
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
    work("読み込んでいます", () => loadStudy(number, versionSelect.value || null)).catch(fail);
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
  const fields = [field("題目", "title", 260), field("担当", "owner"), field("承認者", "approver"), field("案件番号", "dev_number")];
  const state = S.number ? (S.version === null ? "仮" : `版${S.version}`) : "";
  const updated = h.updated_at ? h.updated_at.slice(0, 10).replaceAll("-", "/") : "";
  // 新規作成は ③ で対象品番を選んでから［実行］で作る。作った時に番号が付く
  const run = draft ? el("button", { class: "btn pri", style: "align-self:flex-end" }, "実行") : null;
  if (run) run.addEventListener("click", async () => {
    if (!draft.targets.some(Boolean)) return fail(new Error("対象品番を入れてから実行してください（［対象品番］で選ぶ）"));
    try {
      applyStudy(await work("検討を作成しています", () => api("POST", "/api/v2/studies", {}, { ...header(), products: draft.targets.filter(Boolean) })));
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
    onclick: async () => {
      // ④〜⑧ は別の窓（窓を開くのはクリックのすぐ後でないと止められるので、保存を送るより先）。開けなければこの窓で開く
      if (NAMES[key] && S.number && openWindow(key)) return leave(cur);
      await leave(cur);
      openScreen(key);
    },
  }, label, extra);
  const arrow = () => el("span", { class: "arrow" }, "→");
  const release = el("button", { class: "btn", disabled: !S.number || !canWrite() }, "版確定");
  release.addEventListener("click", async () => {
    await leave(cur);
    if (!confirm(`${S.number} の版を確定します。確定した版は書き換えできません。よろしいですか？`)) return;
    try {
      const taken = await work("版を確定しています", () => api("POST", "/api/v2/release", { number: S.number }, {}));
      await loadStudy(S.number, null);
      done(`版${taken.version} を確定しました`);
    } catch (e) {
      fail(e);
    }
  });

  // 部品表は全部の検討で共通なので、検討の操作と離して置く
  const bomNote = el("span", { class: "mini" });
  const showBom = async () => {
    const { counts } = await api("GET", "/api/v2/bom");
    const empty = !counts.item && !counts.bom_line;
    bomNote.className = empty ? "pill warn" : "mini";
    bomNote.textContent = empty ? "部品表なし" : `部品表 品目 ${fmt(counts.item)}・構成 ${fmt(counts.bom_line)}`;
  };
  showBom().catch(fail);
  const imported = async () => {
    items.clear();
    await showBom();
    if (!S.number) return;
    await leave(cur);
    tell("saved");
    await loadStudy(S.number, S.version);
  };

  main.append(
    el("div", { class: "row modebar" },
      el("span", { class: "mini" }, "処理"),
      el("div", { class: "chips" }, modes.map(([m, label]) =>
        el("button", { class: "chip" + (S.mode === m ? " on" : ""), onclick: () => setMode(m) }, label))),
      el("span", { class: "mini", style: "margin-left:8px" }, "検討番号"), numberInput,
      el("span", { class: "mini" }, "版"), versionSelect,
      el("button", { class: "btn", onclick: read, disabled: S.mode === "new" }, "読込"),
      el("button", { class: "btn", onclick: studyListDialog, disabled: S.mode === "new" }, "一覧から探す"),
      el("span", { class: "sp" }), bomNote,
      el("button", { class: "btn quiet", onclick: () => importDialog(() => imported().catch(fail)) }, "部品表データ取込（モック）")),
    el("div", { class: "card" },
      el("div", { class: "h" }, el("span", { class: "no" }, S.number || "　"), el("span", { class: "mini", style: "font-weight:400" }, "版"),
        el("input", { class: "in ro", value: state, readonly: true, style: "min-width:50px;width:60px" }), release),
      el("div", { class: "b" }, el("div", { class: "row", style: "margin:0" }, fields,
        el("label", { class: "f" }, "状態", el("input", { class: "in ro", value: state, readonly: true, style: "min-width:60px;width:70px" })),
        el("label", { class: "f" }, "最終更新日", el("input", { class: "in ro", value: updated, readonly: true, style: "min-width:100px;width:110px" })),
        run))),
    el("div", { class: "groups" },
      // 左から使う順。まん中（青）が打つ画面、右が見る画面
      el("fieldset", { class: "grp" }, big(3, "対象品番"), big(9, "仮品番")), arrow(),
      el("fieldset", { class: "grp edit" }, big(11, "部品変更"), big(8, "構成変更"), big(10, "費目変更")), arrow(),
      el("fieldset", { class: "grp" }, big(5, "構成一覧"), big(6, "変更一覧", S.problems ? [" ", el("span", { class: "pill warn" }, S.problems)] : null), big(7, "結果"))),
  );
}

async function setMode(mode) {
  await leave();
  if (mode === "new") {
    if (S.number) {
      S.number = "";
      tell("switch");
    }
    Object.assign(S, { number: "", version: null, header: null, versions: [], targets: [], problems: 0,
      draft: { header: {}, targets: [] } });
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
            work("読み込んでいます", () => loadStudy(s.number, null)).catch(fail);
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
  if (S.number) {
    const d = await api("GET", "/api/v2/targets", q());
    rows = d.targets.slice();
  }
  await lookup(rows);
  const body = el("div");
  const model = {
    head: () => ["", "品番", "名称", "系列", "タイプ", "図面", { text: "年間台数", cls: "r" }, { text: "材料費", cls: "r" }, { text: "製造原価", cls: "r" }],
    nRows: () => rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push(EMPTY_ROW()); return true; },
    nCols: () => 9,
    cell(r, c) {
      const code = rows[r] || "";
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: code, editable: !ro };
      if (!code) return { text: "" };
      if (c === 2) return nameCell(code);
      const info = items.get(code);
      if (!info || !info.registered) return { text: "" };
      if (c === 3) return { text: info.model, cls: "auto" };
      if (c === 4) return { text: info.product_spec, cls: "auto" };
      if (c === 5) return { text: info.drawing, cls: "auto" };
      if (c === 6) return { text: fmt(info.annual), cls: "auto r" };
      if (c === 7) return { text: fmt(info.material), cls: "auto r" };
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
    changed() {
      lookup(rows).then((got) => got && grid.render()).catch(fail);
    },
  };
  const grid = new Grid(body, model);
  const saver = autosaver(cur, async () => {
    const d = await api("PUT", "/api/v2/targets", { number: S.number }, { products: rows.filter(Boolean) });
    S.targets = d.targets;
  }, async () => {
    const d = await api("GET", "/api/v2/targets", q());
    rows = d.targets.slice();
    grid.render();
  });
  // 変えた時: 番号の前は S.draft に持ち、番号があればその場で一時保存
  function touched() {
    if (!draft) saver.schedule();
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
  ], [body]));
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
        el("tr", {}, ["選", "品番", "名称", "系列", "タイプ", "年間台数"].map((t) => el("th", {}, t))),
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
      el("label", { class: "f" }, "系列", model), el("label", { class: "f" }, "タイプ", spec),
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
    const [content, data] = await work("ファイルを読み込んでいます", async () => {
      const text = preview?.file === f ? preview.content : toBase64(await f.arrayBuffer());
      return [text, await api("POST", "/api/import/preview", {}, { filename: f.name, content: text, sheet: sheetName })];
    });
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
      const r = await work("確認しています", () => api("POST", "/api/v2/bom/import", {}, { ...body, check: true }));
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
      const r = await work("取り込んでいます", () => api("POST", "/api/v2/bom/import", {}, checked));
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
    status.samples ? el("button", { class: "btn", onclick: () => work("取り込んでいます", () => api("POST", "/api/v2/bom/import", {}, { sample: true })).then(finish, fail) }, "見本") : null,
    el("span", { class: "sp" }),
    checkButton,
    el("button", { class: "btn", onclick: () => d.close() }, "閉じる"),
  ]);
  setOptions(kind, Object.entries(TABLE_LABELS), "item");
  drawPending();
  if (status.counts) showNow(status.counts);
}

// ⑪ 部品変更 -----------------------------------------------------------------
// 対象品番の直下（1階層目）の、変えた後の部品を、品番か名称で絞って出す。品番を打ち替えると置き換える（1回きりの操作で、決まりとしては残さない）。
// まとめる＝部品ごとに1行・使う製品の全部で替わる。製品ごと＝部品×製品×風船番号に1行・その行だけ替わり、数量も変えられる（0 は消す）。
// 見せ方だけの違いで、変更は同じ物。「部品を足す」は、選んだ製品の直下にまとめて足す（風船番号は無し）。
// 戻す時は「置き換える前」を押す（打ち替えて戻すと、もともとその品番だった製品まで変わる）。
// 組立品の中は替えない（⑨ で流用した仮品番を作り、ここで組立品ごと置き換える）

async function searchReplaceScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let each = false; // false＝まとめる・true＝製品ごと
  let parts = []; // { code, name, used, before: 今の部品表で使う製品の数, products, was: 置き換える前の品番 }
  let places = []; // { product, balloon, part, quantity（0 は消した箇所）, was, before: 今の数量（足した箇所は null） }
  let deep = []; // 組立品の中にだけある部品 { code, name, used }
  let shown = [];
  const take = (d) => ({ parts, places, deep } = d);
  if (S.number) take(await api("GET", "/api/v2/after-parts", q({ plan })));
  const codeBox = el("input", { class: "in" });
  const nameBox = el("input", { class: "in" });
  const productBox = el("input", { class: "in" });
  const productNameBox = el("input", { class: "in" });
  const productFilter = el("span", { class: "row", style: "margin:0" }, el("label", { class: "f" }, "製品の品番", productBox), el("label", { class: "f" }, "製品の名称", productNameBox));
  const note = el("span", { class: "mini" });
  const norm = (s) => clean(s).toLowerCase();
  const nameOf = (code) => (items.get(code) && items.get(code).name) || "";
  const hit = (text, word) => !word || norm(text || "").includes(word);
  const added = (p) => !p.was.length && (each ? p.before === null : p.before === 0);
  const moved = (p) => p.was.length > 0 || added(p) || (each && !same(p.quantity, p.before));
  // 絞らない時は変えた行だけ、絞った時は当たる行を出す（置き換える前の品番・名称でも当たるので、打ち替えた行は消えない）
  const refilter = async () => {
    const c = norm(codeBox.value);
    const n = norm(nameBox.value);
    const pc = each ? norm(productBox.value) : "";
    const pn = each ? norm(productNameBox.value) : "";
    const searched = c || n || pc || pn;
    await lookup([...(each ? S.targets : []), ...parts.flatMap((p) => [p.code, ...p.was])]);
    const match = (p) => [p.code ?? p.part, ...p.was].some((code) => hit(code, c) && hit(nameOf(code), n))
      && (!each || (hit(p.product, pc) && hit(nameOf(p.product), pn)));
    const rows = each ? places : parts;
    // 打った行が動かないように、置き換える前の品番の順に並べる（同じ品番の中は対象品番の順）
    const order = (p) => p.was[0] ?? p.code ?? p.part;
    shown = rows.filter((p) => (searched || moved(p)) && match(p)).map((p, i) => [p, i])
      .sort(([x, i], [y, j]) => order(x).localeCompare(order(y)) || i - j).map(([p]) => p);
    const changed = shown.filter(moved);
    const all = rows.filter(moved).length;
    const unit = each ? "行" : "品番";
    const below = c || n ? deep.filter((p) => hit(p.code, c) && hit(p.name, n)).length : 0;
    note.textContent = searched
      ? `検索に当たる${unit} ${shown.length}（変えた${unit} ${changed.length} / ${all}）${below ? `・組立品の中に ${below}（仮品番で流用して替える）` : ""}`
      : `変えた${unit} ${all}（${each ? "部品か製品" : "品番か名称"}で絞ると、対象品番の直下の部品が出ます）`;
    await lookup(shown.flatMap((p) => [p.code ?? p.part, ...p.was]));
    grid.render();
  };
  const usersDialog = async (p) => {
    try {
      await lookup(p.products);
      const box = dialog(`使う製品 ${p.code}`,
        el("table", { class: "list" }, el("tr", {}, el("th", {}, "品番"), el("th", {}, "名称")),
          p.products.map((code) => el("tr", {}, el("td", {}, code), el("td", {}, nameCell(code).text)))),
        [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
    } catch (e) {
      fail(e);
    }
  };
  const send = async (path, body, text) => {
    try {
      take(await api("POST", path, { number: S.number, plan }, body));
      tell("saved");
      done(text);
    } catch (e) {
      fail(e);
    }
    await refilter().catch(fail);
  };
  // 貼り付けでは打ち替えが続けて来るので、溜めて changed() で1回に送る
  let typed = new Map(); // 行 → 新品番
  let counts = new Map(); // 行 → 数量（空は今の部品表に戻す）
  const apply = () => {
    const work = [...typed];
    const amounts = [...counts];
    typed = new Map();
    counts = new Map();
    if (!work.length && !amounts.length) return;
    if (each) {
      (async () => {
        // 数量は、打ち替える前の品番で先に送る
        if (amounts.length) await send("/api/v2/quantity", { rows: amounts.map(([p, v]) => ({ product: p.product, balloon: p.balloon, part: p.part, quantity: v })) },
          `${amounts.length} 行の数量を変えました`);
        if (work.length) await send("/api/v2/replace/apply", { rows: work.map(([p, code]) => ({ product: p.product, balloon: p.balloon, old: p.part, new: code })) },
          `${work.length} 行を置き換えました`);
      })();
      return;
    }
    (async () => {
      for (const [p, code] of work) await send("/api/v2/replace/apply", { old: p.code, new: code }, `${p.code} を ${code} に置き換えました（${p.used} 製品）`);
    })();
  };
  const revert = (p) => {
    const code = each ? p.part : p.code;
    const where = each ? `${p.product} の ` : "";
    if (added(p)) {
      const gone = each ? [p] : places.filter((x) => x.part === code && x.before === null);
      if (!confirm(`${where}${code} の追加（${gone.length}箇所）を取り消します。よろしいですか？`)) return;
      send("/api/v2/quantity", { rows: gone.map((x) => ({ product: x.product, balloon: x.balloon, part: x.part, quantity: "" })) }, `${where}${code} の追加を取り消しました`);
      return;
    }
    if (!confirm(`${where}${code} に置き換えた箇所を、今の部品表（${p.was.join("・")}）に戻します。よろしいですか？`)) return;
    send("/api/v2/replace/revert", each ? { part: code, product: p.product, balloon: p.balloon } : { part: code }, `${where}${code} に置き換えた箇所を今の部品表に戻しました`);
  };
  const materialCell = (code) => {
    const info = items.get(code);
    return { text: info && info.material != null ? fmt(info.material) : "", cls: "r" };
  };
  const backs = (p) => !ro && (p.was.length > 0 || added(p));
  const wasCell = (p) => ({ text: added(p) ? "（追加）" : p.was.join("・"), cls: backs(p) ? "auto link" : "auto", title: backs(p) ? "押すと今の部品表に戻す" : null });
  const set = (r, c, value) => {
    const p = shown[r];
    if (!p) return;
    if (c === 1 && value && value !== (p.code ?? p.part)) typed.set(p, value);
    if (each && c === 6 && !(value !== "" && same(value, p.quantity))) {
      if (value !== "" && !(isNumber(value) && Number(value) >= 0)) return alert(`数量は0以上の数で入力してください（${p.part}「${value}」）`);
      // 置き換えた行の空は、置き換える前の数量に戻す（今の部品表に無い品番なので、そのまま送ると消える）
      if (value === "" && p.was.length && p.before === null) return alert("置き換える前が1つに決まらないので、数量を入力してください");
      counts.set(p, value === "" && p.was.length ? p.before : value);
    }
  };
  const together = {
    head: () => ["", "品番", "名称", { text: "使う製品", cls: "r" }, { text: "材料費", cls: "r" }, "置き換える前"],
    nRows: () => shown.length,
    nCols: () => 6,
    cell(r, c) {
      const p = shown[r];
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: p.code, editable: !ro, title: ro ? null : "打ち替えると、使う製品の全部で置き換える" };
      if (c === 2) return nameCell(p.code);
      // 今の部品表で使う数より減っていれば、一部の製品だけ替えてある
      if (c === 3) return { text: p.used < p.before ? `${p.used}（全${p.before}）` : String(p.used), cls: "r link", title: "押すと製品の一覧" };
      if (c === 4) return materialCell(p.code);
      return wasCell(p);
    },
    set,
    changed: apply,
    action(r, c) {
      if (c === 3 && shown[r]) usersDialog(shown[r]);
      if (c === 5 && shown[r] && backs(shown[r])) revert(shown[r]);
      return false;
    },
  };
  const perProduct = {
    head: () => ["", "品番", "名称", "製品", "名称", { text: "風船", cls: "c" }, { text: "数量", cls: "r" }, "置き換える前"],
    nRows: () => shown.length,
    nCols: () => 8,
    cell(r, c) {
      const p = shown[r];
      switch (c) {
        case 0: return { text: String(r + 1), cls: "n" };
        case 1: return { text: p.part, editable: !ro && Number(p.quantity) !== 0, title: ro ? null : "打ち替えると、この行だけ置き換える" };
        case 2: return nameCell(p.part);
        case 3: return { text: p.product };
        case 4: return nameCell(p.product);
        case 5: return { text: p.balloon ?? "—", cls: "c" };
        case 6: return { text: p.quantity, editable: !ro, cls: "r" + (added(p) || !same(p.quantity, p.before) ? " own" : ""),
          title: p.before === null ? null : `今 ${fmt(p.before)}${ro ? "" : "（0 で消す・空で今に戻す）"}` };
        default: return wasCell(p);
      }
    },
    set,
    changed: apply,
    action(r, c) {
      if (c === 7 && shown[r] && backs(shown[r])) revert(shown[r]);
      return false;
    },
  };
  const body = el("div");
  let grid = new Grid(body, together);
  const modes = el("div", { class: "chips" });
  const drawModes = () => {
    productFilter.hidden = !each;
    productFilter.style.display = each ? "" : "none";
    fill(modes, [[false, "まとめる"], [true, "製品ごと"]].map(([k, text]) => el("button", { class: "chip" + (each === k ? " on" : ""), onclick: async () => {
      if (Grid.active) Grid.active.commit();
      each = k;
      shown = [];
      grid = new Grid(body, each ? perProduct : together);
      drawModes();
      await refilter().catch(fail);
      grid.focus();
    } }, text)));
  };
  drawModes();
  for (const box of [codeBox, nameBox, productBox, productNameBox]) box.addEventListener("input", () => refilter().catch(fail));
  // 部品を足す: 品番と数量を打ち、足す製品に印を付ける。もう直下にある製品には付けられない
  const addDialog = async () => {
    try {
      await lookup(S.targets);
    } catch (e) {
      return fail(e);
    }
    const part = el("input", { class: "in" });
    const amount = el("input", { class: "in", value: "1", style: "min-width:60px;width:60px" });
    const name = el("span", { class: "mini" });
    const code = el("input", { class: "in" });
    const title = el("input", { class: "in" });
    const uses = el("input", { class: "in" });
    const list = el("div");
    const count = el("span", { class: "mini" });
    const checked = new Set();
    const partCode = () => clean(part.value);
    const has = (product) => places.find((p) => p.product === product && p.part === partCode() && Number(p.quantity) !== 0);
    const visible = () => S.targets.filter((t) => hit(t, norm(code.value)) && hit(nameOf(t), norm(title.value))
      && (!norm(uses.value) || places.some((p) => p.product === t && hit(p.part, norm(uses.value)) && Number(p.quantity) !== 0)));
    const tally = () => { count.textContent = `印 ${[...checked].filter((t) => !has(t)).length} 製品`; };
    const drawList = () => {
      const open = visible().filter((t) => !has(t));
      const all = el("input", { type: "checkbox", checked: open.length > 0 && open.every((t) => checked.has(t)), onchange: () => {
        for (const t of open) all.checked ? checked.add(t) : checked.delete(t);
        drawList();
      } });
      fill(list, el("table", { class: "list" }, el("tr", {}, el("th", {}, all), el("th", {}, "製品"), el("th", {}, "名称"), el("th", {}, "今ある数量")),
        visible().map((t) => {
          const at = has(t);
          const box = el("input", { type: "checkbox", checked: !at && checked.has(t), disabled: !!at, onchange: () => {
            box.checked ? checked.add(t) : checked.delete(t);
            drawList();
          } });
          return el("tr", {}, el("td", {}, box), el("td", {}, t), el("td", {}, nameOf(t)), el("td", { class: "r" }, at ? fmt(at.quantity) : ""));
        })));
      tally();
    };
    part.addEventListener("change", async () => {
      try {
        if (partCode()) await lookup([partCode()]);
      } catch (e) {
        fail(e);
      }
      name.textContent = partCode() ? nameCell(partCode()).text : "";
      drawList();
    });
    for (const box of [code, title, uses]) box.addEventListener("input", drawList);
    const run = async () => {
      const products = S.targets.filter((t) => checked.has(t) && !has(t));
      if (!partCode()) return alert("足す品番を入力してください");
      if (!products.length) return alert("足す製品に印を付けてください");
      if (!isNumber(clean(amount.value)) || Number(clean(amount.value)) <= 0) return alert("数量は0より大きい数で入力してください");
      try {
        take(await work("部品を足しています", () => api("POST", "/api/v2/add-part", { number: S.number, plan }, { part: partCode(), quantity: clean(amount.value), products })));
        tell("saved");
        done(`${partCode()} を ${products.length} 製品に足しました`);
        box.close();
      } catch (e) {
        fail(e);
      }
      await refilter().catch(fail);
    };
    const box = dialog("部品を足す", [
      el("div", { class: "row" }, el("label", { class: "f" }, "足す品番", part), el("label", { class: "f" }, "数量", amount), name),
      el("div", { class: "row" }, el("label", { class: "f" }, "製品の品番", code), el("label", { class: "f" }, "製品の名称", title),
        el("label", { class: "f" }, "使っている品番", uses), count),
      list,
    ], [el("span", { class: "sp" }), el("button", { class: "btn pri", onclick: run }, "足す"), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
    drawList();
    part.focus();
  };
  main.append(card("部品変更", [el("button", { class: "btn", disabled: ro || !S.number, onclick: addDialog }, "部品を足す")], [
    el("div", { class: "row" }, modes, el("label", { class: "f" }, "品番", codeBox), el("label", { class: "f" }, "名称", nameBox), productFilter, note),
    body,
  ]));
  await refilter();
  grid.focus();
}

// ⑤ 構成一覧 ---------------------------------------------------------------
// 印の製品の、変えた後の直下の構成を1枚で見る（読むだけ）。行＝風船番号と部品を全部自動で出す（風船を外すと部品ごと）・列＝製品。変えたマスに色。
// 直すのは ⑪（品番・数量・部品を足す）と ⑧（1製品の構成）

async function listScreen(main) {
  let cols = [];
  let rows = []; // 出している行 { balloon, part, name, specification, lifecycle, cells: 製品 → { quantity, now, changed, problem } }
  let data = { rows: [], flat: [], main_product: null }; // rows＝風船番号と部品の行・flat＝部品ごとの行
  let balloons = true;
  let onlyChanged = false;
  const codeBox = el("input", { class: "in" });
  const nameBox = el("input", { class: "in" });
  const note = el("span", { class: "mini" });
  const body = el("div", { class: "listview" });
  const load = async () => {
    if (S.number) {
      const [list, flat] = await Promise.all(["1", "0"].map((balloon) => api("GET", "/api/v2/after-list", q({ plan: PLAN, balloon }))));
      data = { ...list, flat: flat.rows };
    }
    await lookup([...S.targets, ...data.rows.map((r) => r.part)]);
  };
  await load();
  // 左の列（風船は出し入れする）。その右が製品ごとの数量
  const left = () => [
    ...(balloons ? [["風船", (row) => ({ text: row.balloon ?? "—", cls: "c q" })]] : []),
    ["品番", (row) => ({ text: row.part })],
    ["名称", (row) => nameCell(row.part)],
    ["仕様", (row) => ({ text: row.specification, cls: "auto" })],
    ["状態", (row) => ({ text: row.lifecycle, cls: "auto c q" })],
  ];
  const model = {
    // 見出しは1段（表の1行目）。製品の列は品番と名称を縦書きにして、列の幅を数量の桁だけにする
    nRows: () => 1 + rows.length,
    nCols: () => left().length + cols.length,
    cell(r, c) {
      const L = left();
      const P = L.length; // 製品の列が始まる位置
      if (r === 0) {
        if (c < P) return { text: label(L[c][0]), th: true, cls: "foot" };
        const code = cols[c - P];
        const info = items.get(code);
        const name = info ? (info.registered ? info.name : "未登録") : "";
        const star = code === data.main_product ? "★ " : "";
        return { text: `${star}${code}\n${name}`, th: true, cls: "vh" + (info && !info.registered ? " bad" : ""), title: `${star}${code} ${name}` };
      }
      const row = rows[r - 1];
      if (c < P) return L[c][1](row);
      const cell = row.cells[cols[c - P]];
      if (!cell) return { text: "", cls: "r q" };
      return {
        text: fmt(cell.quantity), cls: "r q" + (cell.problem ? " bad" : cell.changed ? " own" : ""),
        title: cell.changed || cell.problem ? `今 ${fmt(cell.now)}${cell.problem ? "・要確認（変更一覧）" : ""}` : null,
      };
    },
    set() {},
  };
  const grid = new Grid(body, model);
  const norm = (s) => clean(s).toLowerCase();
  const draw = () => {
    const c = norm(codeBox.value);
    const n = norm(nameBox.value);
    const used = (balloons ? data.rows : data.flat).filter((r) => cols.some((p) => r.cells[p]));
    rows = used.filter((r) => (!c || norm(r.part).includes(c)) && (!n || norm(r.name || "").includes(n))
      && (!onlyChanged || cols.some((p) => r.cells[p] && r.cells[p].changed)));
    note.textContent = rows.length === used.length ? `行 ${used.length}` : `行 ${rows.length} / ${used.length}`;
    grid.render();
  };
  const pick = chooser({ chosen: S.targets, onProducts: (products) => {
    cols = products;
    draw();
    return true;
  } });
  await pick.ready;
  cols = pick.ordered();
  const toggle = (text, get, set) => {
    const button = el("button", { class: "chip" + (get() ? " on" : ""), onclick: () => {
      set(!get());
      button.classList.toggle("on", get());
      draw();
    } }, text);
    return button;
  };
  const whereUsed = () => {
    const at = rows[grid.sel.r - 1];
    if (!at) return alert("品番の行を選んでください");
    whereUsedDialog(at.part);
  };
  // 主対象は対象品番の中から1つ。替えると並びと★だけが変わる
  const mainPick = el("select", { class: "in", disabled: !canWrite(), onchange: async () => {
    try {
      await api("PUT", "/api/v2/main-product", { number: S.number }, { main_product: mainPick.value || null });
      await load();
      draw();
    } catch (e) {
      fail(e);
    }
  } }, el("option", { value: "" }, "（なし）"), S.targets.map((code) => el("option", { value: code, selected: code === data.main_product }, code)));
  for (const box of [codeBox, nameBox]) box.addEventListener("input", draw);
  main.append(card("構成一覧", [
    el("label", { class: "inline" }, "主対象 ", mainPick),
    el("button", { class: "btn", onclick: whereUsed, disabled: !S.number }, "使用先"),
    el("button", { class: "btn", onclick: conditionsDialog, disabled: !S.number }, "計算条件"),
  ], [
    el("div", { class: "row" }, el("label", { class: "f" }, "品番", codeBox), el("label", { class: "f" }, "名称", nameBox),
      el("div", { class: "chips" }, toggle("変えた所だけ", () => onlyChanged, (v) => { onlyChanged = v; }), toggle("風船", () => balloons, (v) => { balloons = v; })), note),
    el("div", { class: "chooser two" }, pick.left, el("div", { class: "work" }, body)),
  ]));
  draw();
  grid.focus();
}

async function whereUsedDialog(code) {
  try {
    const d = await api("GET", "/api/v2/where-used", q({ code }));
    const list = el("div");
    const open = async (group) => {
      try {
        const g = await api("GET", "/api/v2/where-used", q({ code, group }));
        fill(list, el("table", { class: "list", style: "margin-top:8px" },
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
          el("td", {}, el("button", { class: "btn", disabled: !d.outside, onclick: () => open("outside") }, "開く"))),
        d.deep ? el("tr", {}, el("td", {}, "組立品の中"), el("td", { class: "r" }, d.deep), el("td", {}, "")) : null),
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

// ⑧ 構成変更 -------------------------------------------------------------

// 1つの製品の直下（1階層目）だけを直す。組立品の中は「中を見る」で見るだけ（替える時は ⑨ で流用した仮品番を作る）

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
    head: () => ["", { text: "風船", cls: "c" }, "品番", "名称", { text: "数量", cls: "r" }, ""],
    nRows: () => rows.length + (ro || !product ? 0 : 1),
    grow: ro || !product ? undefined : () => {
      rows.push({ balloon: "", part: "", quantity: "", before_part: null, before_quantity: null, fresh: true });
      return true;
    },
    nCols: () => 6,
    cell(r, c) {
      const row = rows[r];
      const fresh = !row || row.fresh;
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: row ? row.balloon : "", editable: !ro && fresh, cls: "c" };
      if (c === 2) {
        const own = row && (row.fresh || row.part !== row.before_part);
        return { text: row ? row.part : "", editable: !ro, cls: own ? "own" : "" };
      }
      if (c === 3) return nameCell(row && row.part);
      if (c === 5) return row && row.assembly ? { text: "中を見る", cls: "link" } : { text: "" };
      const own = row && (row.fresh || !same(row.quantity, row.before_quantity));
      const bad = row && row.quantity !== "" && (!isNumber(row.quantity) || Number(row.quantity) < 0);
      return { text: row ? row.quantity : "", editable: !ro, cls: "r" + (bad ? " bad" : own ? " own" : "") };
    },
    set(r, c, text) {
      if (![1, 2, 4].includes(c)) return;
      if (r >= rows.length) {
        if (!text) return;
        rows.push({ balloon: "", part: "", quantity: "", before_part: null, before_quantity: null, fresh: true });
      }
      const row = rows[r];
      const field = { 1: "balloon", 2: "part", 4: "quantity" }[c];
      if (c === 1 && !row.fresh) return;
      // 数でない数量はその場で弾く（入れずに元の値のまま）
      if (c === 4 && text !== "" && !isNumber(text)) return alert(`数量は数で入力してください（${row.part || "品番なし"}「${text}」）`);
      row[field] = text;
      if (c === 2) row.assembly = false; // 打ち替えた品番が組立品かは、開き直した時に分かる
      saver.schedule();
    },
    action(r, c) {
      if (c === 5 && rows[r] && rows[r].assembly) insideDialog(rows[r].part);
      return false;
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
  // 品番のある行だけを送る（品番の無い行は打ちかけとして画面に残す）
  const saver = autosaver(cur, async () => {
    if (!product) return;
    const send = rows.filter((r) => r.part).map((r) => ({ balloon: r.balloon, part: r.part, quantity: r.quantity }));
    await api("PUT", "/api/v2/rewrite", { number: S.number, plan }, { product, rows: send });
  }, async () => {
    await load();
    grid.render();
  });
  main.append(card("構成変更", [], el("div", { class: "chooser two" }, pick.left, el("div", { class: "work" }, body))));
  grid.focus();
}

// 組立品の中（直下）を見る。中の組立品は、押すと続けて中へ降りる
async function insideDialog(code, path = []) {
  try {
    const d = await api("GET", "/api/v2/inside", q({ code }));
    const box = dialog(`中を見る ${[...path, code].join(" ＞ ")}`, [
      el("div", { class: "mini" }, d.name ?? "未登録"),
      el("table", { class: "list" }, el("tr", {}, el("th", {}, "風船"), el("th", {}, "品番"), el("th", {}, "名称"), el("th", { class: "r" }, "数量"), el("th", {}, "")),
        d.rows.map((r) => el("tr", {}, el("td", {}, r.balloon ?? "—"), el("td", {}, r.part), el("td", {}, r.name ?? "未登録"), el("td", { class: "r" }, fmt(r.quantity)),
          el("td", {}, r.assembly ? el("button", { class: "btn quiet", onclick: () => { box.close(); insideDialog(r.part, [...path, code]); } }, "中を見る") : null)))),
    ], [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
  } catch (e) {
    fail(e);
  }
}

// ⑥ 変更一覧 -------------------------------------------------------------

// 構成の変更を、部品から（した事〔置き換え・数量・追加・削除〕ごとに1行・製品の数を押すと製品ごとの箇所）か、
// 製品から（製品×した事ごとに1行・変更の無い対象品番も出す）で見る。
// 金額の変更（⑩ 製品の費目の書き換え）は下に分けて出す
async function changesScreen(main) {
  const plan = PLAN;
  let product = "";
  let view = "part"; // part＝部品から・product＝製品から
  let rows = [];
  let byProduct = [];
  let owns = [];
  let temps = {}; // 流用した仮品番 → { base, name, changes }
  const ownBox = el("div");
  const deepBox = el("div");
  const ro = !canWrite();
  const load = async () => {
    const d = await api("GET", "/api/v2/changes", q({ plan, product }));
    temps = d.temps;
    fill(deepBox, d.deep ? el("div", { class: "row" },
      el("span", { class: "pill warn" }, `2階層目の変更 ${d.deep} 件は計算に入りません`),
      el("span", { class: "mini" }, "組立品の中を替える時は、仮品番で流用して作り、直下で置き換えます"),
      ro ? null : el("button", { class: "btn", onclick: dropDeep }, "2階層目の変更を消す")) : null);
    rows = d.summary;
    byProduct = d.by_product;
    owns = d.rows.filter((r) => r.source === "own");
    await lookup([...byProduct.map((r) => r.product), ...owns.map((r) => r.product)]);
    fill(ownBox, owns.length ? [el("h3", {}, "製品の費目の書き換え"), el("table", { class: "list" },
      el("tr", {}, ["製品", "名称", "費目", "台帳の値", "書き換えた値"].map((t) => el("th", {}, t))),
      owns.map((r) => el("tr", {}, el("td", {}, r.product), el("td", {}, nameCell(r.product).text), el("td", {}, r.part),
        el("td", { class: "r" }, r.before === null ? "—" : fmt(r.before)), el("td", { class: "r" }, fmt(r.after)))))] : []);
  };
  const detailDialog = (row) => {
    const box = dialog(`${row.kind} ${[row.old, row.new].filter(Boolean).join(" → ")}`,
      el("table", { class: "list" }, el("tr", {}, ["製品", "名称", "親", "風船", row.new && row.old ? `元 ${row.old}` : "変更前", row.new && row.old ? `新 ${row.new}` : "変更後"].map((t) => el("th", {}, t))),
        row.details.map((d) => el("tr", {}, el("td", {}, d.product), el("td", {}, nameCell(d.product).text), el("td", {}, d.parent),
          el("td", {}, d.balloon ?? "—"), el("td", { class: "r" }, d.before === null ? "—" : fmt(d.before)), el("td", { class: "r" }, fmt(d.after))))),
      [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
  };
  const dropDeep = async () => {
    if (!confirm("2階層目の変更を全部の製品から消します。よろしいですか？")) return;
    try {
      await api("POST", "/api/v2/changes/drop-deep", { number: S.number, plan }, {});
      tell("saved");
      await load();
      grid.render();
      done("2階層目の変更を消しました");
    } catch (e) {
      fail(e);
    }
  };
  // 流用した仮品番の中の違い（流用元から）
  const tempDialog = (code) => {
    const info = temps[code];
    const box = dialog(`${code} の中（流用元 ${info.base}）`, info.changes.length
      ? el("table", { class: "list" }, el("tr", {}, ["変更", "風船", "元品番", "名称", "新品番", "名称", "数量", ""].map((x) => el("th", {}, x))),
        info.changes.flatMap((s) => s.details.map((d) => el("tr", {}, el("td", {}, s.kind), el("td", {}, d.balloon ?? "—"),
          el("td", {}, s.old ?? ""), el("td", {}, named(s.old, s.old_name).text), el("td", {}, s.new ?? ""), el("td", {}, named(s.new, s.new_name).text),
          el("td", { class: "r" }, d.quantity),
          el("td", {}, s.new && temps[s.new] ? el("button", { class: "btn quiet", onclick: () => { box.close(); tempDialog(s.new); } }, "中の違い") : s.problems.join("・"))))))
      : el("div", { class: "mini" }, "流用元と同じです"),
    [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
  };
  // 新品番のセル。流用した仮品番なら、押すと中の違い
  const newCell = (row) => (row.new && temps[row.new] ? { text: row.new, cls: "link", title: `流用元 ${temps[row.new].base}。押すと中の違い` } : { text: row.new ?? "" });
  const named = (code, name) => (code === null ? { text: "" } : name === null ? { text: "未登録", cls: "bad" } : { text: name });
  const body = el("div");
  const problemCell = (row) => ({ text: row.problems.join("・"), cls: row.problems.length ? "bad" : "" });
  const partModel = {
    head: () => ["", "変更", "元品番", "名称", "新品番", "名称", { text: "数量", cls: "r" }, { text: "製品", cls: "r" }, "要確認"],
    nRows: () => rows.length,
    nCols: () => 9,
    cell(r, c) {
      const row = rows[r];
      switch (c) {
        case 0: return { text: String(r + 1), cls: "n" };
        case 1: return { text: row.kind };
        case 2: return { text: row.old ?? "" };
        case 3: return named(row.old, row.old_name);
        case 4: return newCell(row);
        case 5: return named(row.new, row.new_name);
        case 6: return { text: row.quantity, cls: "r" };
        case 7: return { text: String(row.products.length), cls: "r link", title: "押すと製品ごとの箇所" };
        default: return problemCell(row);
      }
    },
    set() {},
    action(r, c) {
      if (c === 7 && rows[r]) detailDialog(rows[r]);
      if (c === 4 && rows[r] && temps[rows[r].new]) tempDialog(rows[r].new);
      return false;
    },
  };
  // 製品の品番と名称は、その製品の最初の行にだけ出す
  const productModel = {
    head: () => ["", "製品", "名称", "変更", "元品番", "名称", "新品番", "名称", { text: "数量", cls: "r" }, "親", { text: "風船", cls: "c" }, "要確認"],
    nRows: () => byProduct.length,
    nCols: () => 12,
    cell(r, c) {
      const row = byProduct[r];
      const first = r === 0 || byProduct[r - 1].product !== row.product;
      switch (c) {
        case 0: return { text: String(r + 1), cls: "n" };
        case 1: return { text: first ? row.product : "" };
        case 2: return first ? nameCell(row.product) : { text: "" };
        case 3: return { text: row.kind };
        case 4: return { text: row.old ?? "" };
        case 5: return named(row.old, row.old_name);
        case 6: return newCell(row);
        case 7: return named(row.new, row.new_name);
        case 8: return { text: row.quantity, cls: "r" };
        case 9: return { text: row.parent ?? "" };
        case 10: return { text: row.kind === "変更なし" ? "" : row.balloon ?? "—", cls: "c" };
        default: return problemCell(row);
      }
    },
    set() {},
    action(r, c) {
      if (c === 6 && byProduct[r] && temps[byProduct[r].new]) tempDialog(byProduct[r].new);
      return false;
    },
  };
  await load();
  let grid = new Grid(body, partModel);
  const views = el("div", { class: "chips" });
  const drawViews = () => fill(views, [["part", "部品から"], ["product", "製品から"]].map(([k, text]) =>
    el("button", { class: "chip" + (view === k ? " on" : ""), onclick: () => {
      view = k;
      drawViews();
      grid = new Grid(body, view === "part" ? partModel : productModel);
      grid.focus();
    } }, text)));
  drawViews();
  const productSelect = el("select", { class: "in", style: "min-width:90px" },
    el("option", { value: "" }, "全製品"), S.targets.map((p) => el("option", { value: p }, p)));
  productSelect.addEventListener("change", async () => {
    product = productSelect.value;
    await load().catch(fail);
    grid.render();
  });
  main.append(card("変更一覧", [productSelect,
    el("button", { class: "btn", onclick: () => exportStudy().catch(fail) }, "Excel"),
  ], [deepBox, el("div", { class: "row" }, views), body, ownBox]));
  grid.focus();
}

// 検討1つを1つの Excel に出す（⑥ と ⑦ で同じファイル）
function exportStudy() {
  return work("Excel を作っています", () => download("/api/v2/export/study", q({ plan: PLAN }), `検討_${S.number}.xlsx`));
}

// ⑦ 結果 -----------------------------------------------------------------

function signed(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return (n < 0 ? "−" : n > 0 ? "+" : "") + fmt(String(Math.abs(n)));
}

function tone(v) {
  const n = Number(v);
  return v === null || v === undefined ? "" : n < 0 ? " minus" : n > 0 ? " plus" : "";
}

// 今に対する差の割合。小さい差が 0.0% に見えないよう、1% 未満は小数2桁
function percent(diff, now) {
  if (diff === null || diff === undefined || now === null || now === undefined || Number(now) === 0) return "";
  const v = (Number(diff) / Number(now)) * 100;
  if (v === 0) return "0%";
  return `${v < 0 ? "−" : "+"}${Math.abs(v).toFixed(Math.abs(v) < 1 ? 2 : 1)}%`;
}

// 上に「変更ごとの影響」（製造原価への効き方）、下に製品ごとの費目全部（今・変更後・差）
async function resultScreen(main) {
  let data = null;
  const plan = PLAN;
  const stamp = el("span", { class: "mini", style: "font-weight:400" });
  const body = el("div");
  const incomplete = (on) => (on ? { class: "inc", title: "不完全" } : {});
  const productsDialog = (e) => {
    const box = dialog(`${e.kind} ${[e.old, e.new, e.head && label(e.head)].filter(Boolean).join(" → ")}`,
      el("table", { class: "list" }, el("tr", {}, ["製品", "名称"].map((x) => el("th", {}, x)), ["1台あたりの差", "年間台数", "年間の影響"].map((x) => el("th", { class: "r" }, x))),
        e.products.map((p) => el("tr", {}, el("td", {}, p.product), el("td", {}, p.name ?? "未登録"), el("td", { class: "r" }, signed(p.unit)),
          el("td", { class: "r" }, fmt(p.annual) || "—"), el("td", { class: "r" }, p.impact === null ? "—" : man(p.impact))))),
      [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
  };
  // した事ごとの効き方。足すと下の表の製造原価の年間の影響になる
  const drawEffects = () => {
    const total = data.heads.find((h) => h.key === "total");
    const base = total.base[plan];
    const unit = (e) => (e.unit_min === null ? "—" : Number(e.unit_min) === Number(e.unit_max) ? signed(e.unit_min) : `${signed(e.unit_min)} 〜 ${signed(e.unit_max)}`);
    const name = (code, text) => (code && text === null ? "未登録" : text ?? "");
    const cell = (e, text, v) => el("td", { class: "r" + tone(v) + (e.incomplete ? " inc" : ""), title: e.incomplete ? "不完全" : null }, text);
    if (!data.effects.length) return el("div", { class: "mini" }, "変更がありません");
    return el("table", { class: "res" },
      el("tr", {}, ["変更", "元品番", "名称", "新品番", "名称"].map((x) => el("th", {}, x)),
        ["製品", "1台あたりの差", "年間の影響", "現行比"].map((x) => el("th", { class: "r" }, x)), el("th", {}, "要確認")),
      data.effects.map((e) => el("tr", {},
        el("td", {}, e.kind), el("td", {}, e.old ?? ""), el("td", {}, name(e.old, e.old_name)), el("td", {}, e.new ?? ""),
        el("td", {}, e.head ?? name(e.new, e.new_name)),
        el("td", { class: "r", title: "押すと製品ごとの差", style: "cursor:pointer;text-decoration:underline dotted", onclick: () => productsDialog(e) }, String(e.products.length)),
        cell(e, unit(e), e.unit_max), cell(e, e.impact === null ? "—" : man(e.impact), e.impact), cell(e, percent(e.impact, base) || "—", e.impact),
        el("td", {}, e.problems.join("・")))),
      el("tr", {}, el("th", {}, "合計"), el("th", { colspan: "5" }, ""),
        el("th", { class: "r" + tone(total.weighted[plan]), title: "台数の加重平均" }, signed(total.weighted[plan])),
        el("th", { class: "r" + tone(total.impact[plan]) + (total.impact_incomplete[plan] ? " inc" : ""), title: total.impact_incomplete[plan] ? "不完全" : null }, man(total.impact[plan])),
        el("th", { class: "r" + tone(total.impact[plan]) }, percent(total.impact[plan], base) || "—"), el("th", {})));
  };
  // 費目全部: 製品ごとに 今・変更後・差 の3行、列＝費目
  const drawProducts = () => {
    const heads = data.heads;
    const rows = heads.length ? heads[0].rows : [];
    const diffOf = (r) => (r.plans[plan] === null || r.now === null ? null : String(Number(r.plans[plan]) - Number(r.now)));
    const line = (title, cells) => [el("td", { class: "mini" + (title === "差" ? "" : " low") }, title), cells];
    return el("table", { class: "res" },
      el("tr", {}, el("th", {}, "対象品番"), el("th", {}, "名称"), el("th", { class: "r" }, "年間台数"), el("th", {}), heads.map((h) => el("th", { class: "r" }, h.label))),
      rows.flatMap((r0, i) => [
        el("tr", {}, el("td", { rowspan: "3" }, r0.product), el("td", { rowspan: "3" }, r0.name ?? "未登録"), el("td", { class: "r", rowspan: "3" }, fmt(r0.annual)),
          line("今", heads.map((h) => el("td", { ...incomplete(h.rows[i].now_incomplete), class: "r low" + (h.rows[i].now_incomplete ? " inc" : "") }, fmt(h.rows[i].now))))),
        el("tr", {}, line("変更後", heads.map((h) => el("td", { ...incomplete(h.rows[i].incomplete[plan]), class: "r low" + (h.rows[i].incomplete[plan] ? " inc" : "") }, fmt(h.rows[i].plans[plan]))))),
        el("tr", {}, line("差", heads.map((h) => {
          const diff = diffOf(h.rows[i]);
          const rate = diff === null || Number(diff) === 0 ? "" : percent(diff, h.rows[i].now);
          const inc = h.rows[i].incomplete[plan];
          return el("td", { class: "r" + tone(diff) + (inc ? " inc" : ""), title: inc ? "不完全" : null }, diff === null ? "—" : signed(diff), rate ? `（${rate}）` : "");
        }))),
      ]),
      el("tr", {}, el("th", { colspan: "3" }, "年間の影響"), el("th", {}),
        heads.map((h) => el("th", { class: "r" + tone(h.impact[plan]) + (h.impact_incomplete[plan] ? " inc" : ""), title: h.impact_incomplete[plan] ? "不完全" : null },
          man(h.impact[plan]), Number(h.impact[plan]) ? `（${percent(h.impact[plan], h.base[plan])}）` : ""))),
      el("tr", {}, el("th", { colspan: "3" }, "台数の加重平均"), el("th", { class: "mini" }, "1台あたり"),
        heads.map((h) => el("th", { class: "r" + tone(h.weighted[plan]) + (h.impact_incomplete[plan] ? " inc" : ""), title: h.impact_incomplete[plan] ? "不完全" : null }, signed(h.weighted[plan])))));
  };
  const draw = () => fill(body, data ? [
    el("h3", {}, "変更ごとの影響", el("span", { class: "mini", style: "font-weight:400;margin-left:8px" }, "製造原価")),
    el("div", { style: "overflow:auto" }, drawEffects()),
    el("h3", {}, "製品ごとの費目"),
    el("div", { style: "overflow:auto" }, drawProducts())] : null);
  const compute = async () => {
    try {
      data = await work("計算しています", () => api("GET", "/api/v2/calc", q({ head: "all", plan })));
      stamp.textContent = `計算日時 ${data.computed_at.replace("T", " ").slice(0, 16).replaceAll("-", "/")}　${label("諸経費")} ${data.rates.adjustment}%・${label("間接費")} ${data.rates.indirect}%`;
      draw();
      done("計算しました");
    } catch (e) {
      fail(e);
    }
  };
  main.append(card(["結果", stamp], [
    el("button", { class: "btn", onclick: conditionsDialog }, "計算条件"),
    el("button", { class: "btn", onclick: () => exportStudy().catch(fail) }, "Excel"),
    el("button", { class: "btn pri", onclick: compute }, "実行"),
  ], body));
}

// ⑩ 費目変更 -----------------------------------------------------------
// 対象品番ごとに、製品自身の費目（加工費・償却費・直接費）を書き換える。空は台帳の値のまま

async function ownScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let heads = [];
  let rows = [];
  const load = async () => {
    const d = await api("GET", "/api/v2/own", q({ plan }));
    heads = d.heads;
    rows = d.rows.map((r) => ({ product: r.product, name: r.name,
      now: Object.fromEntries(heads.map((h) => [h.key, r.heads[h.key].now])),
      after: Object.fromEntries(heads.map((h) => [h.key, r.heads[h.key].after ?? ""])) }));
  };
  await load();
  const body = el("div");
  // 列: # | 対象品番 | 名称 | 費目ごとに 今（台帳・読むだけ）と 変更後（打つ）
  const model = {
    head: () => ["", "対象品番", "名称", ...heads.flatMap((h) => [{ text: `${h.label} 今`, cls: "r" }, { text: `${h.label} 変更後`, cls: "r" }])],
    nRows: () => rows.length,
    nCols: () => 3 + heads.length * 2,
    cell(r, c) {
      const row = rows[r];
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: row.product };
      if (c === 2) return { text: row.name ?? "未登録", cls: row.name === null ? "bad" : "auto" };
      const h = heads[Math.floor((c - 3) / 2)];
      if ((c - 3) % 2 === 0) return { text: fmt(row.now[h.key]), cls: "r auto" };
      const v = row.after[h.key];
      return { text: v, editable: !ro, cls: "r" + (v !== "" && !same(v, row.now[h.key]) ? " own" : "") };
    },
    set(r, c, text) {
      if (c < 3 || (c - 3) % 2 === 0) return;
      const h = heads[Math.floor((c - 3) / 2)];
      // 数でない値はその場で弾く（入れずに元の値のまま）
      if (text !== "" && !isNumber(text)) return alert(`${h.label}は数で入力してください（${rows[r].product}「${text}」）`);
      rows[r].after[h.key] = text;
      saver.schedule();
    },
  };
  const grid = new Grid(body, model);
  const saver = autosaver(cur, async () => {
    await api("PUT", "/api/v2/own", { number: S.number, plan },
      { rows: rows.flatMap((r) => heads.filter((h) => r.after[h.key] !== "").map((h) => ({ product: r.product, head: h.key, value: r.after[h.key] }))) });
  }, async () => {
    await load();
    grid.render();
  });
  main.append(card("費目変更", [], [
    el("div", { class: "row" }, el("span", { class: "mini" }, "空の欄は台帳の値で計算します")),
    body,
  ]));
  grid.render();
  grid.focus();
}

// ⑨ 仮品番 -------------------------------------------------------------
// 品目の台帳に無い品番の名称と材料費（1個あたり）。この検討の計算にだけ使う（部品の原価は製品の材料費にだけ入るので材料費だけ）。
// 流用元（部品表の組立品）を入れると、その中（直下）を変えた組立品になる。材料費は空なら流用元から計算する

async function tempScreen(main, cur) {
  const ro = !canWrite();
  const blank = () => ({ key: "", code: "", name: "", base: "", savedBase: "", material: "", derived: null, changed: 0, master: false, mark: false });
  let rows = [];
  let unregistered = [];
  // サーバーで決まる値（流用元から計算した材料費・中の変更の数・台帳にあるか）を行に移す
  const take = (row, got) => Object.assign(row, { key: got.code, savedBase: got.base ?? "", derived: got.derived, changed: got.changed, master: got.master });
  const load = async () => {
    const d = await api("GET", "/api/v2/temp-items", q());
    rows = d.rows.map((r) => take({ ...blank(), code: r.code, name: r.name, base: r.base ?? "", material: r.material ?? "" }, r));
    unregistered = d.unregistered;
  };
  await load();
  const note = el("span", { class: "mini" });
  const drawNote = () => {
    const shown = unregistered.slice(0, 5).join("・") + (unregistered.length > 5 ? "…" : "");
    note.textContent = unregistered.length ? `変更に出てくる未登録の品番 ${unregistered.length} 件（${shown}）` : "変更に出てくる未登録の品番はありません";
  };
  drawNote();
  const markAll = () => {
    const all = !(rows.length && rows.every((r) => r.mark));
    for (const r of rows) r.mark = all;
  };
  const body = el("div");
  const model = {
    head: () => [
      { text: rows.length && rows.every((r) => r.mark) ? "☑" : "☐", cls: "c", title: "全部の行に印", action: ro ? null : markAll },
      "", "品番", "名称", "流用元", { text: "材料費", cls: "r" }, { text: "流用元から計算", cls: "r" }, "中", "",
    ],
    nRows: () => rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push(blank()); return true; },
    nCols: () => 9,
    cell(r, c) {
      const row = rows[r];
      if (c === 0) return { text: row ? (row.mark ? "☑" : "☐") : "", cls: "act" };
      if (c === 1) return { text: String(r + 1), cls: "n" };
      if (c === 2) return { text: row ? row.code : "", editable: !ro };
      if (c === 3) return { text: row ? row.name : "", editable: !ro };
      if (c === 4) return { text: row ? row.base : "", editable: !ro, title: "部品表にある組立品の品番" };
      if (c === 5) return { text: row ? row.material : "", editable: !ro, cls: "r" };
      if (c === 6) {
        if (!row || !row.savedBase || row.material !== "" || row.master) return { text: "" };
        return row.derived === null ? { text: "不明", cls: "r bad", title: "流用元か、中の部品の材料費が分かりません" } : { text: fmt(row.derived), cls: "r auto" };
      }
      if (c === 7) return row && row.savedBase ? { text: row.changed ? `${row.changed} 件` : ro ? "変更なし" : "変える", cls: "link" } : { text: "" };
      return row && row.master ? { text: "台帳にあり（台帳の値を使う）", cls: "bad" } : { text: "" };
    },
    set(r, c, text) {
      if (c < 2 || c > 5) return;
      // 数でない材料費はその場で弾く（入れずに元の値のまま）
      if (c === 5 && text !== "" && !isNumber(text)) return alert(`材料費は数で入力してください（「${text}」）`);
      if (r >= rows.length) {
        if (!text) return;
        rows.push(blank());
      }
      const row = rows[r];
      if (c === 4 && row.changed && text !== row.savedBase && !confirm(`${row.code} の流用元を替えると、中の変更 ${row.changed} 件が消えます。よろしいですか？`)) return;
      row[["code", "name", "base", "material"][c - 2]] = text;
      saver.schedule();
    },
    action(r, c) {
      const row = rows[r];
      if (row && c === 7 && row.savedBase) openInside(row);
      if (!row || ro || c !== 0) return false;
      row.mark = !row.mark;
      return true;
    },
  };
  const grid = new Grid(body, model);
  // 品番を打った行だけを送る（名称だけ打った行は画面に残す）。台帳にあるかの印と未登録の一覧は返ってきた物で直す
  const saver = autosaver(cur, async () => {
    const d = await api("PUT", "/api/v2/temp-items", { number: S.number },
      { rows: rows.filter((r) => r.code).map((r) => ({ key: r.key || undefined, code: r.code, name: r.name, base: r.base, material: r.material })) });
    const got = new Map(d.rows.map((x) => [x.code, x]));
    for (const r of rows) {
      if (got.has(r.code)) take(r, got.get(r.code));
    }
    unregistered = d.unregistered;
    items.clear();
    drawNote();
    grid.render();
  }, async () => {
    await load();
    drawNote();
    grid.render();
  });
  // 流用した仮品番の中（直下）。打つたびに一時保存し、閉じたら材料費を計算し直す
  const openInside = async (row) => {
    try {
      if (Grid.active) Grid.active.commit();
      await saver.flush();
      const code = row.key;
      let lines = [];
      const read = (d) => { lines = d.rows.map((x) => ({ ...x, balloon: x.balloon ?? "", fresh: x.before_part === null })); };
      read(await api("GET", "/api/v2/temp-inside", q({ code })));
      await lookup(lines.map((x) => x.part));
      const box = el("div");
      const fresh = () => ({ balloon: "", part: "", quantity: "", before_part: null, before_quantity: null, fresh: true });
      const inner = {
        head: () => ["", { text: "風船", cls: "c" }, "品番", "名称", { text: "数量", cls: "r" }, "流用元の品番"],
        nRows: () => lines.length + (ro ? 0 : 1),
        grow: ro ? undefined : () => { lines.push(fresh()); return true; },
        nCols: () => 6,
        cell(r, c) {
          const x = lines[r];
          const isNew = !x || x.fresh;
          if (c === 0) return { text: String(r + 1), cls: "n" };
          if (c === 1) return { text: x ? x.balloon : "", editable: !ro && isNew, cls: "c" };
          if (c === 2) return { text: x ? x.part : "", editable: !ro, cls: x && (x.fresh || x.part !== x.before_part) ? "own" : "" };
          if (c === 3) return nameCell(x && x.part);
          if (c === 4) return { text: x ? x.quantity : "", editable: !ro, cls: "r" + (x && (x.fresh || !same(x.quantity, x.before_quantity)) ? " own" : "") };
          return { text: x && x.before_part !== null && x.part !== x.before_part ? x.before_part : "", cls: "auto" };
        },
        set(r, c, text) {
          if (![1, 2, 4].includes(c)) return;
          if (c === 4 && text !== "" && (!isNumber(text) || Number(text) < 0)) return alert(`数量は 0 以上の数で入力してください（「${text}」）`);
          if (r >= lines.length) {
            if (!text) return;
            lines.push(fresh());
          }
          if (c === 1 && !lines[r].fresh) return;
          lines[r][{ 1: "balloon", 2: "part", 4: "quantity" }[c]] = text;
          keep.schedule();
        },
        changed() {
          lookup(lines.map((x) => x.part)).then((got) => got && insideGrid.render()).catch(fail);
        },
      };
      const insideGrid = new Grid(box, inner);
      const keep = autosaver(cur, () => api("PUT", "/api/v2/temp-inside", { number: S.number },
        { code, rows: lines.filter((x) => x.part).map((x) => ({ balloon: x.balloon, part: x.part, quantity: x.quantity })) }),
      async () => {
        read(await api("GET", "/api/v2/temp-inside", q({ code })));
        insideGrid.render();
      });
      // × で閉じても、打ちかけを確定して送ってから表を直す
      const frame = dialog(`${code} の中（流用元 ${row.savedBase}）`, box, [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => close() }, "閉じる")]);
      const close = async () => {
        insideGrid.commit();
        await keep.flush();
        frame.close();
        cur.saver = saver;
        Grid.active = grid;
        items.clear();
        const d = await api("GET", "/api/v2/temp-items", q());
        const got = new Map(d.rows.map((x) => [x.code, x]));
        for (const r of rows) if (got.has(r.key)) take(r, got.get(r.key));
        unregistered = d.unregistered;
        drawNote();
        grid.render();
        grid.focus();
      };
      const cross = frame.overlay.querySelector(".close");
      cross.replaceWith(el("button", { class: "close", onclick: () => close().catch(fail) }, "×"));
      insideGrid.focus();
    } catch (e) {
      fail(e);
    }
  };
  const addUnregistered = el("button", { class: "btn", disabled: ro }, "未登録を表に入れる");
  addUnregistered.addEventListener("click", () => {
    if (Grid.active) Grid.active.commit();
    const fresh = unregistered.filter((code) => !rows.some((r) => r.code === code));
    if (!fresh.length) return done("表に入れる未登録の品番はありません");
    rows.push(...fresh.map((code) => ({ ...blank(), code })));
    saver.schedule();
    grid.render();
    done(`${fresh.length} 件を表に入れました（名称と材料費を入れてください）`);
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
  main.append(card("仮品番", [addUnregistered, del], [
    el("div", { class: "row" }, el("span", { class: "mini" }, "品目の台帳に無い品番の名称と材料費（1個あたり）。この検討の計算にだけ使います"), el("span", { class: "sp" }), note),
    body,
  ]));
  grid.render();
  grid.focus();
}

const SCREENS = {
  2: hubScreen,
  3: targetsScreen,
  5: listScreen,
  6: changesScreen,
  7: resultScreen,
  8: rewriteScreen,
  9: tempScreen,
  10: ownScreen,
  11: searchReplaceScreen,
};

// 別の窓は URL の検討を読んでその画面を開く
async function openFromUrl() {
  const key = Number(PARAMS.get("screen"));
  if (!NAMES[key]) return done("開く画面が違います");
  S.mode = ["edit", "view"].includes(PARAMS.get("mode")) ? PARAMS.get("mode") : "edit";
  try {
    await loadStudy(PARAMS.get("number") || "", PARAMS.get("version") || null, false);
  } catch (e) {
    return done(`読込に失敗しました（${e && e.message ? e.message : e}）`);
  }
  document.title = `${NUM[key]} ${label(NAMES[key])} ${S.number}`;
  openScreen(key);
}

// 語の読み替えを受け取ってから最初の画面を描く（受け取れなくても元の語で開く）
api("GET", "/api/v2/labels")
  .then((d) => Object.assign(LABELS, d.labels), fail)
  .finally(() => (POPUP ? openFromUrl() : openScreen(2)));
