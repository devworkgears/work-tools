// 設計検討の画面。② から ③〜⑪ を別の窓で開く（② は開いたまま）。入れた値はその場で一時保存する（⑦の［実行］は点検と計算だけで、書かない）。
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

// 別の窓: ② から ③〜⑪ を開く。窓の URL に画面・検討番号・版・処理（新規作成の ③ は下書きの印 draft）を載せる
const NAMES = { 3: "対象品番", 5: "構成一覧", 6: "変更一覧", 7: "結果", 8: "構成変更", 9: "仮品番", 10: "費目変更", 11: "部品変更" };
const PARAMS = new URLSearchParams(location.search);
const POPUP = PARAMS.has("screen");
// 窓どうしの知らせ。saved＝その検討を一時保存した（ほかの窓は戻った時に読み直す）・switch＝② が別の検討・版に替えた（別の窓は閉じる）。
// 新規作成の下書き: draft-ask＝③ の窓が中身を聞く・draft＝② の返事・draft-set＝③ で変えた・draft-end＝作った・やめた（③ の窓は閉じる）
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
  if (info.temp) return { text: info.name, cls: "auto", title: "仮品番（⑨ で登録した品番）" };
  return { text: info.name, cls: "auto" };
}

// 見出しを押すと並べ替える（もう一度押すと逆順・空は後ろ）。並べ替えるのは押した時だけで、打っている間は動かない
function sortable(apply) {
  let at = null; // { id, value, number, down }
  return {
    reset() { at = null; },
    head(text, value, number = false, id = text) {
      return { text: `${label(text)}${at && at.id === id ? (at.down ? " ▼" : " ▲") : ""}`, title: "押すと並べ替える", action: () => {
        at = { id, value, number, down: !!at && at.id === id && !at.down };
        apply();
      } };
    },
    // 並べた物を返す。key は並べる1つ分（行の組など）から値を見る行を取り出す
    apply(list, key = (x) => x) {
      if (!at) return list;
      const { value, number, down } = at;
      return [...list].sort((a, b) => {
        const [x, y] = [value(key(a)) ?? "", value(key(b)) ?? ""];
        if (x === "" || y === "") return (x === "") - (y === "");
        const n = number ? Number(x) - Number(y) : String(x).localeCompare(String(y), "ja", { numeric: true });
        return down ? -n : n;
      });
    },
  };
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
  const close = () => {
    overlay.remove();
    catchUp();
  };
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
  let running = 0;
  let chain = Promise.resolve();
  const run = () => {
    timer = null;
    running += 1;
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
    }).finally(() => { running -= 1; });
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
      return timer !== null || running > 0;
    },
  };
  owner.saver = saver;
  return saver;
}

// 打ち替えを、打った順に1つずつ送る（送っている間も打てる）。閉じる時は全部終わるのを待つ
function sendQueue(owner, waiting = () => false) {
  let chain = Promise.resolve();
  let jobs = 0;
  owner.saver = {
    async flush() {
      for (let last = null; last !== chain;) await (last = chain);
    },
    get pending() {
      return jobs > 0 || waiting();
    },
  };
  return (job) => {
    jobs += 1;
    chain = chain.then(job).catch(fail).finally(() => { jobs -= 1; });
    return chain;
  };
}

// 組立品の中を変えた返事で、付いた品番と、材料費を打ってある仮品番を知らせる文
function madeText(d) {
  const made = d.made || [];
  const split = made.filter((m) => m.typed).map((m) => m.new);
  return (made.length ? `。組立品 ${made.length} つに新しい品番を付けました（${made.map((m) => `${m.code} → ${m.new}`).join("・")}）` : "")
    + (split.length ? `。${split.join("・")} は、元の仮品番に入力した材料費を使わず、流用元から計算します` : "")
    + (d.typed && d.typed.length ? `。${d.typed.join("・")} は材料費を入力してあるので、金額は変わりません` : "");
}

// 別の窓 --------------------------------------------------------------

function tell(type) {
  if (channel) channel.postMessage({ type, number: S.number, version: S.version });
}

// 同じ名前の窓が同じ検討の同じ画面を出していれば前に出すだけ（読み直すと打ちかけが消えるため）
function openWindow(key, extra = {}, name = "") {
  const url = location.pathname + query({ screen: key, number: S.number, version: S.version ?? "", mode: S.mode, draft: S.draft ? S.draft.id : "", ...extra });
  const w = window.open("", `bomcost-${key}${name}`, "popup,width=1280,height=820");
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
  if (m.type && m.type.startsWith("draft")) {
    if (!S.draft || m.id !== S.draft.id) return;
    if (m.type === "draft-ask" && !POPUP) channel.postMessage({ type: "draft", id: m.id, targets: S.draft.targets });
    else if (m.type === "draft" && POPUP) S.draft.take(m.targets);
    else if (m.type === "draft-set" && !POPUP) {
      S.draft.targets = m.targets;
      done(`対象品番 ${m.targets.filter(Boolean).length} 製品を選んでいます`);
    } else if (m.type === "draft-end" && POPUP) window.close();
    return;
  }
  if (m.type === "switch" && POPUP && (m.number !== S.number || m.version !== S.version)) {
    leave().finally(() => window.close());
  } else if (m.type === "saved" && S.number && m.number === S.number && !stale) {
    stale = true;
    if (document.hasFocus()) catchUp();
    else done("ほかの窓で一時保存がありました（この窓に戻ると読み直します）");
  }
};

// ほかの窓で保存があった時は、この窓に戻った時に読み直す。reload を持つ画面は、打ちかけ・絞り込みを残して中身だけ読み直す。
// ほかの画面は、打ちかけ・送りかけがある間は描き直さない。⑦ は［実行］で計算し直す
async function catchUp() {
  const cur = current;
  if (!stale || !cur || !S.number) return;
  if (document.querySelector(".overlay")) return; // 窓を開いている間は下の画面を描き直さない（閉じた後に読み直す）
  if (!cur.reload && ((Grid.active && Grid.active.editing) || (cur.saver && cur.saver.pending))) return;
  stale = false;
  items.clear();
  try {
    await loadStudy(S.number, S.version, false);
    if (current !== cur) return;
    if (cur.reload) await cur.reload();
    else if (cur.key !== 7) openScreen(cur.key);
  } catch (e) {
    fail(e);
  }
}
window.addEventListener("focus", catchUp);

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
      endDraft();
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
      // 別の窓（窓を開くのはクリックのすぐ後でないと止められるので、保存を送るより先）。開けなければこの窓で開く
      if (NAMES[key] && (S.number || draft) && openWindow(key)) return leave(cur);
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
      el("button", { class: "btn", onclick: studyListDialog, disabled: S.mode === "new" }, "一覧検索"),
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
      el("fieldset", { class: "grp edit" }, big(8, "構成変更"), big(10, "費目変更")), arrow(),
      el("fieldset", { class: "grp" }, big(5, "構成一覧"), big(6, "変更一覧", S.problems ? [" ", el("span", { class: "pill warn" }, S.problems)] : null), big(7, "結果"))),
  );
}

function endDraft() {
  if (channel && S.draft) channel.postMessage({ type: "draft-end", id: S.draft.id });
}

async function setMode(mode) {
  await leave();
  endDraft();
  if (mode === "new") {
    if (S.number) {
      S.number = "";
      tell("switch");
    }
    Object.assign(S, { number: "", version: null, header: null, versions: [], targets: [], problems: 0,
      draft: { id: crypto.randomUUID ? crypto.randomUUID() : String(Math.random()), header: {}, targets: [] } });
    items.clear();
  } else S.draft = null;
  S.mode = mode;
  openScreen(2);
}

async function studyListDialog() {
  try {
    const data = await api("GET", "/api/v2/studies");
    const d = dialog("一覧検索",
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
  const value = (code, key) => (items.get(code) ? items.get(code)[key] : "");
  // 並びは対象品番の順そのもの（ほかの画面の並びにもなる）
  const sort = sortable(() => {
    const ordered = sort.apply(rows.filter(Boolean));
    rows.splice(0, rows.length, ...ordered);
    if (!ro) touched();
  });
  const body = el("div");
  const model = {
    head: () => ["", sort.head("品番", (code) => code), ...[["名称", "name"], ["系列", "model"], ["タイプ", "product_spec"], ["図面", "drawing"]].map(([t, k]) => sort.head(t, (code) => value(code, k))),
      ...[["年間台数", "annual"], ["材料費", "material"], ["製造原価", "manufacturing"]].map(([t, k]) => ({ ...sort.head(t, (code) => value(code, k), true), cls: "r" }))],
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
    else if (POPUP) channel.postMessage({ type: "draft-set", id: draft.id, targets: rows.slice() });
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

// 同じ箇所（親・風船番号・子）の数量が行によって違う物。先頭の行の数量で取り込む
const MAX_CONFLICTS = 100;
function conflictTable(conflicts) {
  if (!conflicts.length) return null;
  return el("div", {},
    el("div", { class: "msg" }, `同じ箇所の数量が、行によって違います: ${conflicts.length} 箇所（先頭の行の数量で取り込みます）`),
    el("div", { style: "max-height:240px;overflow:auto;margin:4px 0 8px" }, el("table", { class: "list" },
      el("tr", {}, ["親品番", "風船番号", "サブ番号", "子品番", "取り込む数量", "ほかの数量"].map((t) => el("th", {}, t))),
      conflicts.slice(0, MAX_CONFLICTS).map((c) => el("tr", {}, el("td", {}, raw(c.parent)), el("td", {}, raw(balloonText(c.balloon))), el("td", {}, raw(branchText(c.balloon))), el("td", {}, raw(c.child)),
        el("td", { class: "r" }, fmt(c.quantities[0])), el("td", { class: "r" }, c.quantities.slice(1).map(fmt).join("・")))))),
    conflicts.length > MAX_CONFLICTS ? el("div", { class: "mini" }, `先頭の ${MAX_CONFLICTS} 箇所を出しています`) : null);
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
      el("div", { class: "row", style: "margin:8px 0" }, el("button", { class: "btn", onclick: add }, "一覧追加")));
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
      return [text, await api("POST", "/api/v2/bom/preview", {}, { filename: f.name, content: text, sheet: sheetName })];
    });
    preview = { file: f, content, data };
    sheetLabel.hidden = !data.sheets.length;
    setOptions(sheet, data.sheets.map((s) => [s, s]), data.sheet);
    // 保存した対応づけが無い時は、見出しの当たりがいちばん強い表を仮に選ぶ
    setOptions(kind, data.tables.map((t) => [t.id, `${t.label}${t.saved ? "（保存した対応づけあり）" : ""}`]),
      data.suggested || data.tables[0].id);
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
        conflictTable(r.conflicts),
        warningBox(r.warnings),
        el("div", { class: "row" },
          el("button", { class: "btn pri", onclick: decide }, "決定"),
          el("button", { class: "btn", onclick: clearCheck }, "取消")));
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
// 対象品番の部品（全階層）を、品番か名称で絞って、左に現行・右に変更後で出す。変更後の空欄＝現行のまま。打つと置き換え、空にすると現行に戻す
// （1回きりの操作で、決まりとしては残さない）。行の細かさは3つで、打った行に入っている箇所が全部替わる:
// 一括＝現行の部品ごと・親品番ごと＝部品×入っている組立品ごと・製品ごと＝部品×製品×風船番号ごと（直下の行は数量も変えられる。0 は消す）。
// 組立品の中を替えると、上の組立品に新しい品番（流用した仮品番）が自動で付き、変更後の親品番に出る（その場で打ち直せる）。
// 「部品追加」は、選んだ製品の直下にまとめて足す（風船番号は無し）
const MAX_PART_ROWS = 1000;

async function searchReplaceScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let grain = "part"; // part＝一括・parent＝親品番ごと・product＝製品ごと
  let places = []; // 直下の箇所 { product, balloon, part, quantity（0 は消した箇所）, was, before: 現行の数量（足した箇所は null）, now: 箇所が1つなら現行の品番 }
  let inside = []; // 組立品の中の部品 { path: 直下の組立品から親まで, part, name, lines: [{ balloon, quantity, was }] }
  let tops = {}; // 直下の組立品 → 使う箇所 [{ product, balloon }]
  let bases = {}; // 流用した仮品番 → 流用元
  let groups = []; // 行 { now, parent, parentAfter, upper, products, direct, deep, … }
  let shown = [];
  const take = (d) => {
    ({ places, inside, tops, bases } = d);
    groups = null;
  };
  if (S.number) take(await api("GET", "/api/v2/after-parts", q({ plan })));
  const codeBox = el("input", { class: "in" });
  const nameBox = el("input", { class: "in" });
  const productBox = el("input", { class: "in" });
  const productNameBox = el("input", { class: "in" });
  const productFilter = el("span", { class: "row", style: "margin:0" }, el("label", { class: "f" }, "製品品番", productBox), el("label", { class: "f" }, "製品名称", productNameBox));
  const note = el("span", { class: "mini" });
  const norm = (s) => clean(s).toLowerCase();
  const nameOf = (code) => (items.get(code) && items.get(code).name) || "";
  const hit = (text, word) => !word || norm(text || "").includes(word);
  const base = (code) => bases[code] ?? code;
  // 直下の箇所の現行の品番。足した箇所は無し。2つ以上が替わった箇所は並べて出す
  const nowOf = (p) => (p.was.length ? p.was.join("・") : p.before === null ? "" : p.part);
  const gone = (p) => Number(p.quantity) === 0;
  const movedPlace = (p) => p.was.length > 0 || p.before === null || !same(p.quantity, p.before);
  const atoms = function* () {
    for (const p of places) yield { now: nowOf(p), after: gone(p) ? null : p.part, direct: p, path: [], moved: movedPlace(p) };
    for (const r of inside) for (const x of r.lines) yield { now: x.was.length ? x.was.join("・") : r.part, after: r.part, path: r.path, line: x, moved: x.was.length > 0 };
  };
  const key = (a) => `${a.now || `+${a.after}`} ${grain === "part" ? "" : a.path.join(" ")}`;
  const collect = (list) => {
    const found = new Map();
    const order = [];
    for (const a of list) {
      const k = grain === "product" ? Symbol("row") : key(a);
      if (!found.has(k)) {
        const last = a.path.at(-1);
        found.set(k, { now: a.now, products: new Set(), direct: [], deep: [], afters: new Set(), codes: new Set(), moved: false, gone: 0, made: new Set(),
          // 並びは、替えても変わらない物（現行の品番・現行の組立品・製品・風船番号）で決める
          at: [a.path.map(base).join(" "), a.direct ? a.direct.product : a.places ? a.places[0].product : "", (a.direct ? a.direct.balloon : a.line.balloon) ?? ""],
          parent: last ? base(last) : "", parentAfter: last && bases[last] ? last : "", upper: a.path.slice(0, -1).map(base).join(" ＞ ") });
        order.push(found.get(k));
      }
      const g = found.get(k);
      if (a.direct) {
        g.direct.push(a.direct);
        g.products.add(a.direct.product);
        if (gone(a.direct)) g.gone += 1;
      } else {
        g.deep.push(a);
        for (const t of a.places || tops[a.path[0]] || []) g.products.add(t.product);
        if (a.moved) for (const c of a.path) if (bases[c]) g.made.add(c);
      }
      if (a.after !== null) g.afters.add(a.after);
      for (const c of [a.after, a.direct && a.direct.part, ...a.now.split("・")]) if (c) g.codes.add(c);
      g.moved ||= a.moved;
    }
    return order;
  };
  // 変更後の品番: 全部同じに替わっていればその品番・現行のままなら空・混ざっていれば null
  const afterOf = (g) => {
    if (!g.afters.size) return g.gone ? "" : null;
    if (g.afters.size > 1 || (g.gone && !g.now)) return null;
    const [code] = g.afters;
    return code === g.now ? "" : code;
  };
  const refilter = async () => {
    const c = norm(codeBox.value);
    const n = norm(nameBox.value);
    const pc = grain === "product" ? norm(productBox.value) : "";
    const pn = grain === "product" ? norm(productNameBox.value) : "";
    const searched = c || n || pc || pn;
    await lookup([...(grain === "product" ? S.targets : []), ...places.flatMap((p) => [p.part, ...p.was]), ...inside.flatMap((r) => [r.part, ...r.lines.flatMap((x) => x.was)])]);
    const codeHit = (code) => hit(code, c) && hit(nameOf(code), n);
    const atomHit = (a) => (searched ? [a.after, a.direct && a.direct.part, ...a.now.split("・")].some((x) => x && codeHit(x)) : a.moved);
    const productHit = (p) => hit(p, pc) && hit(nameOf(p), pn);
    let hits;
    if (grain === "product") {
      // 中の部品は製品の数だけ増えるので、部品で絞ってから製品に広げる
      const list = [...atoms()].filter(atomHit).flatMap((a) => (a.direct ? (productHit(a.direct.product) ? [a] : [])
        : (tops[a.path[0]] || []).filter((x) => productHit(x.product)).map((x) => ({ ...a, places: [x] }))));
      hits = collect(list);
    } else {
      groups ??= collect(atoms());
      hits = groups.filter((g) => (searched ? [...g.codes].some(codeHit) : g.moved));
    }
    // 打った行が動かないように、現行の品番の順に並べる（足した行は後ろ。同じ品番の中は、直下が先・対象品番の順・風船番号の順）
    const rank = new Map(S.targets.map((code, i) => [code, i]));
    hits = hits.map((g, i) => [g, i]).sort(([x, i], [y, j]) => (x.now === "") - (y.now === "") || x.now.localeCompare(y.now) || x.at[0].localeCompare(y.at[0])
      || (grain === "product" ? rank.get(x.at[1]) - rank.get(y.at[1]) || x.at[2].localeCompare(y.at[2], "ja", { numeric: true }) : 0) || i - j).map(([g]) => g);
    if (grid.editing) { // 打っている行の中身を入れ替えない
      late = true;
      return;
    }
    shown = hits.slice(0, MAX_PART_ROWS);
    note.textContent = `${searched ? `該当行 ${hits.length}・` : ""}変更行 ${hits.filter((g) => g.moved).length}${hits.length > shown.length ? `・先頭の ${MAX_PART_ROWS} 行を出しています` : ""}`;
    await lookup(shown.flatMap((g) => [...g.codes, g.parent, g.parentAfter]));
    grid.render();
  };
  const usersDialog = async (g) => {
    const products = [...g.products];
    try {
      await lookup(products);
      const box = dialog(`使用製品 ${g.now || [...g.afters][0]}`,
        el("table", { class: "list" }, el("tr", {}, el("th", {}, "品番"), el("th", {}, "名称")),
          products.map((code) => el("tr", {}, el("td", {}, code), el("td", {}, nameCell(code).text)))),
        [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
    } catch (e) {
      fail(e);
    }
  };
  let late = false; // 打っている間に来て、描き直しを待っている物があるか
  const later = sendQueue(cur);
  // 送って、返ってきた物で描き直す。うまくいけば true
  const send = async (path, body, text) => {
    try {
      const d = await api("POST", path, { number: S.number, plan }, body);
      items.clear(); // 仮品番の品番・材料費が変わる
      take(d);
      tell("saved");
      done(text + madeText(d));
      return true;
    } catch (e) {
      fail(e);
      return false;
    }
  };
  // 貼り付けでは打ち替えが続けて来るので、溜めて changed() で1回に送る
  let typed = new Map(); // 行 → 変更後の品番（空は現行に戻す）
  let counts = new Map(); // 行 → 変更後の数量（空は現行に戻す）
  let renames = new Map(); // 仮品番 → 新しい品番
  const apply = () => {
    const work = [...typed];
    const amounts = [...counts];
    const names = [...renames];
    typed = new Map();
    counts = new Map();
    renames = new Map();
    if (work.length || amounts.length || names.length) later(() => run(work, amounts, names));
  };
  const run = async (work, amounts, names) => {
    const quantity = amounts.map(([g, v]) => ({ ...slot(g.direct[0]), part: g.direct[0].part, quantity: v }));
    const replace = [];
    const deep = [];
    for (const [g, v] of work) {
      const to = v || g.now;
      for (const p of g.direct) {
        if (gone(p)) continue;
        if (!to) quantity.push({ ...slot(p), part: p.part, quantity: "" }); // 足した箇所の取り消し
        else if (p.part !== to) replace.push({ ...slot(p), old: p.part, new: to });
      }
      for (const a of g.deep) {
        if (a.after !== to) deep.push({ path: a.path, old: a.after, new: to, balloon: a.line.balloon, ...(a.places ? { product: a.places[0].product, top: a.places[0].balloon } : {}) });
      }
    }
    // 数量は、打ち替える前の品番で先に送る。中の行は、直下の後（同じ組立品が混ざっていれば、サーバが断る）
    let ok = !quantity.length || await send("/api/v2/quantity", { rows: quantity }, `${quantity.length} 箇所の数量を変えました`);
    if (ok && replace.length) ok = await send("/api/v2/replace/apply", { rows: replace }, `${replace.length} 箇所を置き換えました`);
    if (ok && deep.length) ok = await send("/api/v2/inside/replace", { rows: deep }, `組立品の中の ${deep.length} 箇所を置き換えました`);
    for (const [code, to] of names) if (ok) ok = await send("/api/v2/temp-rename", { code, new: to }, `${code} を ${to} にしました`);
    await refilter().catch(fail);
  };
  const slot = (p) => ({ product: p.product, balloon: p.balloon, ...(p.now === undefined ? {} : { now: p.now }) });
  const text = (code) => ({ text: code });
  const many = (g) => g.now.includes("・"); // 現行が1つに決まらない行は、ここでは直さない
  const price = (code) => ({ text: items.get(code) && items.get(code).material != null ? fmt(items.get(code).material) : "", cls: "r" });
  const nowName = (g) => (g.now && !many(g) ? nameCell(g.now) : text(""));
  const afterCell = (g) => {
    const code = afterOf(g);
    return { text: code ?? "", editable: !ro && !many(g) && g.afters.size > 0, cls: "split" + (code ? " own" : "") };
  };
  const afterName = (g) => {
    const code = afterOf(g);
    if (code) return nameCell(code);
    if (code === null) return { text: "一部だけ", cls: "auto" };
    return g.gone ? { text: g.afters.size ? "一部を削除" : "削除", cls: "auto" } : text("");
  };
  const parentAfter = (g) => ({ text: g.parentAfter, editable: !ro && !!g.parentAfter, title: g.parentAfter ? "打ち直すと、この組立品の新しい品番が変わる" : null });
  const only = (g) => g.direct[0]; // 製品ごとの、直下の行の箇所
  const line = (g) => g.deep[0] && g.deep[0].line;
  const productCell = (g) => [...g.products][0];
  const amount = (g) => {
    const p = only(g);
    if (!p) return { text: "", cls: "r", title: "中の数量は、仮品番の中で変える" };
    const changed = p.before === null || !same(p.quantity, p.before);
    return { text: changed ? p.quantity : "", editable: !ro, cls: "r" + (changed ? " own" : ""), title: ro || p.before === null ? null : "0 で消す・空で現行に戻す" };
  };
  // [見出し, 中身]。after の付いた列から右が変更後
  const columns = () => [
    ["部品品番", (g) => text(g.now)], ["部品名称", nowName],
    ...(grain === "part" ? [[{ text: "材料費", cls: "r" }, (g) => (many(g) ? text("") : price(g.now))]] : [
      ["親品番", (g) => text(g.parent || "（製品）")], ["親名称", (g) => (g.parent ? nameCell(g.parent) : text(""))], ["さらに上", (g) => ({ text: g.upper, cls: "auto" })]]),
    ...(grain === "product" ? [["製品品番", (g) => text(productCell(g))], ["製品名称", (g) => nameCell(productCell(g))],
      [{ text: "風船", cls: "c" }, (g) => ({ text: balloonText(only(g) ? only(g).balloon : line(g).balloon), cls: "c" })],
      [{ text: "サブ番号", cls: "c" }, (g) => ({ text: branchText(only(g) ? only(g).balloon : line(g).balloon), cls: "c" })],
      [{ text: "数量", cls: "r" }, (g) => ({ text: fmt(only(g) ? only(g).before : line(g).quantity), cls: "r" })]]
      : [[{ text: "製品", cls: "r" }, (g) => ({ text: String(g.products.size), cls: "r link", title: "押すと製品の一覧" })]]),
    [{ text: "部品品番", cls: "after split" }, afterCell, "code"],
    [{ text: "部品名称", cls: "after" }, afterName],
    ...(grain === "part" ? [[{ text: "材料費", cls: "after r" }, (g) => (afterOf(g) ? price(afterOf(g)) : text(""))],
      [{ text: "組立品の新品番", cls: "after" }, (g) => ({ text: [...g.made].slice(0, 2).join("・") + (g.made.size > 2 ? ` ほか ${g.made.size - 2}` : ""), cls: "auto", title: [...g.made].join("・") })]]
      : [...(grain === "product" ? [[{ text: "数量", cls: "after r" }, amount, "amount"]] : []), [{ text: "親品番", cls: "after" }, parentAfter, "parent"]]),
  ];
  let cols = columns();
  const model = {
    groups: () => {
      const at = cols.findIndex((x) => x[2] === "code");
      return [{ text: "", span: 1 }, { text: "現行", span: at }, { text: "変更後", span: cols.length - at, cls: "after split" }];
    },
    head: () => ["", ...cols.map((x) => x[0])],
    nRows: () => shown.length,
    nCols: () => 1 + cols.length,
    cell: (r, c) => (c === 0 ? { text: String(r + 1), cls: "n" } : cols[c - 1][1](shown[r])),
    set(r, c, value) {
      const g = shown[r];
      const kind = g && c > 0 ? cols[c - 1][2] : null;
      if (kind === "code" && value !== (afterOf(g) ?? "") && (value !== "" || g.moved)) typed.set(g, value === g.now ? "" : value);
      if (kind === "parent" && value && value !== g.parentAfter) renames.set(g.parentAfter, value);
      if (kind === "amount" && value !== amount(g).text) {
        const p = only(g);
        if (value !== "" && !(isNumber(value) && Number(value) >= 0)) return alert(`数量は0以上の数で入力してください（${p.part}「${value}」）`);
        if (value === "" && p.was.length > 1) return alert("現行が1つに決まらないので、数量を入力してください");
        counts.set(g, value);
      }
    },
    changed: () => apply(),
    idle() {
      if (!late) return;
      late = false;
      later(refilter);
    },
    action(r, c) {
      if (shown[r] && c > 0 && cols[c - 1][0].text === "製品") usersDialog(shown[r]);
      return false;
    },
  };
  const body = el("div", { style: "overflow-x:auto" }); // 製品ごとは横に長い
  const grid = new Grid(body, model);
  const modes = el("div", { class: "chips" });
  const drawModes = () => {
    productFilter.hidden = grain !== "product";
    productFilter.style.display = grain === "product" ? "" : "none";
    fill(modes, [["part", "一括"], ["parent", "親品番別"], ["product", "製品別"]].map(([k, name]) => el("button", { class: "chip" + (grain === k ? " on" : ""), onclick: async () => {
      if (Grid.active) Grid.active.commit();
      grain = k;
      groups = null;
      cols = columns();
      shown = [];
      drawModes();
      await refilter().catch(fail);
      grid.firstEditable();
      grid.render();
      grid.focus();
    } }, name)));
  };
  drawModes();
  for (const box of [codeBox, nameBox, productBox, productNameBox]) box.addEventListener("input", () => refilter().catch(fail));
  // 部品追加: 品番と数量を打ち、足す製品に印を付ける。もう直下にある製品には付けられない
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
      fill(list, el("table", { class: "list" }, el("tr", {}, el("th", {}, all), el("th", {}, "製品"), el("th", {}, "名称"), el("th", {}, "使用数量")),
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
        take(await work("部品を足しています", async () => {
          await cur.saver.flush();
          return api("POST", "/api/v2/add-part", { number: S.number, plan }, { part: partCode(), quantity: clean(amount.value), products });
        }));
        tell("saved");
        done(`${partCode()} を ${products.length} 製品に足しました`);
        box.close();
      } catch (e) {
        fail(e);
      }
      await refilter().catch(fail);
    };
    const box = dialog("部品追加", [
      el("div", { class: "row" }, el("label", { class: "f" }, "追加品番", part), el("label", { class: "f" }, "数量", amount), name),
      el("div", { class: "row" }, el("label", { class: "f" }, "製品品番", code), el("label", { class: "f" }, "製品名称", title),
        el("label", { class: "f" }, "使用品番", uses), count),
      list,
    ], [el("span", { class: "sp" }), el("button", { class: "btn pri", onclick: run }, "追加"), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
    drawList();
    part.focus();
  };
  cur.reload = () => later(async () => {
    take(await api("GET", "/api/v2/after-parts", q({ plan })));
    await refilter();
  });
  main.append(card("部品変更", [el("button", { class: "btn", disabled: ro || !S.number, onclick: addDialog }, "部品追加")], [
    el("div", { class: "row" }, modes, el("label", { class: "f" }, "部品品番", codeBox), el("label", { class: "f" }, "部品名称", nameBox), productFilter, note),
    body,
  ]));
  await refilter();
  grid.firstEditable();
  grid.render();
  grid.focus();
}

// ⑤ 構成一覧・⑧ 構成変更 ---------------------------------------------------
// どちらも、親を横に並べて、その直下の1階層だけを出す。最初の親は図面が同じ製品で、組立品の行の［開く］で、同じ風船番号の組立品を
// 親にした同じ画面を別の窓で開く。⑧ は変えた後の構成をそのまま打ち替える画面、⑤ は左に現行・右に変更後を並べて見るだけの画面

const MAX_STRUCTURE_ROWS = 1000;

const NO_BALLOON = "なし"; // 風船番号の無い箇所の見せ方
// サブ番号は風船番号の下の番号。サーバは「風船番号#サブ番号」の1つの文字で持つ（同じかの比べはそのまま）。見せる時と並べる時だけ分ける
const BRANCH_SEP = "#";

function splitBalloon(b) {
  if (b === null || b === undefined) return [null, ""];
  const at = b.indexOf(BRANCH_SEP);
  return at < 0 ? [b, ""] : [b.slice(0, at), b.slice(at + 1)];
}

function joinBalloon(balloon, branch) {
  return balloon && branch ? `${balloon}${BRANCH_SEP}${branch}` : balloon;
}

// 風船番号の列とサブ番号の列のセル（どの表も同じ見せ方）
const balloonText = (b) => splitBalloon(b)[0] ?? NO_BALLOON;
const branchText = (b) => splitBalloon(b)[1];

function balloonOrder(a, b) {
  if (a === null || b === null) return (a === null) - (b === null);
  const [ha, ba] = splitBalloon(a);
  const [hb, bb] = splitBalloon(b);
  return ha.localeCompare(hb, "ja", { numeric: true }) || ba.localeCompare(bb, "ja", { numeric: true });
}

// 「表示」の欄に並べる、出し方の切り替えのチェック
function toggle(text, on, change) {
  const input = el("input", { type: "checkbox" });
  input.checked = on;
  input.addEventListener("change", () => change(input.checked));
  return el("label", { class: "tg" }, input, text);
}

function showGroup(...toggles) {
  return el("div", { class: "f" }, "表示", el("div", { class: "tgs" }, toggles));
}

// いくつも選べる一覧。押すとチェックの一覧が開く。何も選んでいない時と全部選んだ時は、全部（set([])）
// options() → [[値, 見せる文字]]・get() → 選んだ値（空は全部）・set(選んだ値)。draw() で見せ方を今の値に合わせる
function checkList(options, get, set) {
  const label = el("span");
  const panel = el("div", { class: "checks" });
  panel.hidden = true;
  let checked = new Set();
  const values = () => options().map(([v]) => v);
  const drawLabel = () => {
    const on = get();
    label.textContent = !on.length ? `全部（${values().length}）` : on.length === 1 ? on[0] : `${on[0]} ほか ${on.length - 1}`;
  };
  const apply = () => {
    set(checked.size === 0 || checked.size === values().length ? [] : values().filter((v) => checked.has(v)));
    drawLabel();
    drawPanel();
  };
  const box = (on, change) => {
    const input = el("input", { type: "checkbox" });
    input.checked = on;
    input.addEventListener("change", () => change(input.checked));
    return input;
  };
  const drawPanel = () => {
    const all = values().every((v) => checked.has(v));
    fill(panel, [
      el("label", { class: "all" }, box(all, (on) => { checked = new Set(on ? values() : []); apply(); }), "全部"),
      options().map(([v, text]) => el("label", {}, box(checked.has(v), (on) => { on ? checked.add(v) : checked.delete(v); apply(); }), text))]);
  };
  const button = el("button", { class: "in pick", type: "button", onclick: () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      checked = new Set(get().length ? get() : values());
      drawPanel();
    }
  } }, label, " ▾");
  const wrap = el("span", { class: "checkwrap" }, button, panel);
  // 外を押すと閉じる。画面を描き直して一覧が外れたら、見張りも外す
  const outside = (e) => {
    if (!wrap.isConnected) document.removeEventListener("mousedown", outside);
    else if (!wrap.contains(e.target)) panel.hidden = true;
  };
  document.addEventListener("mousedown", outside);
  wrap.draw = () => {
    drawLabel();
    if (!panel.hidden) { // 開いている間は、外した途中の状態を残す（読み直しで無くなった値だけ落とす）
      checked = new Set(values().filter((v) => checked.has(v)));
      drawPanel();
    }
  };
  return wrap;
}

// 組立品の中を変えた時に取る新しい品番を聞く窓。needs は { code（元品番）, name, suggested（自動の品番） }。
// 決めたら { 元品番: 新しい品番 }、やめたら null
function askNumbers(needs) {
  return new Promise((resolve) => {
    const inputs = needs.map((n) => el("input", { class: "in", value: n.suggested }));
    let answered = false;
    const finish = (value) => {
      if (answered) return;
      answered = true;
      box.close();
      resolve(value);
    };
    const ok = () => {
      const typed = inputs.map((x) => clean(x.value));
      if (typed.some((t) => !t)) return alert("新しい品番を入れてください");
      if (new Set(typed).size !== typed.length) return alert("同じ品番が2つあります");
      finish(Object.fromEntries(needs.map((n, i) => [n.code, typed[i]])));
    };
    for (const x of inputs) x.addEventListener("keydown", (e) => { if (e.key === "Enter") ok(); });
    const box = dialog("新しい品番", el("table", { class: "list" },
      el("tr", {}, ["元品番", "名称", "新しい品番"].map((t) => el("th", {}, t))),
      needs.map((n, i) => el("tr", {}, el("td", {}, n.code), el("td", {}, n.name), el("td", {}, inputs[i])))),
      [el("button", { class: "btn pri", onclick: ok }, "決定"), el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => finish(null) }, "やめる")]);
    // ×で閉じた時も、やめたのと同じ
    const cross = box.overlay.querySelector(".close");
    if (cross) cross.replaceWith(el("button", { class: "close", onclick: () => finish(null) }, "×"));
    inputs[0].focus();
    inputs[0].select();
  });
}

function movedSlot(s) {
  return s.now !== s.part || !same(s.before, s.quantity);
}

// 組立品の中の窓は、親（流用元の品番）と、開いた時に出していた製品の範囲（図面・製品）を URL に持つ
async function structureView(key, redraw) {
  const child = PARAMS.has("parents") ? { bases: PARAMS.get("parents").split(","), range: JSON.parse(PARAMS.get("range")) } : null;
  // 選んでいる図面・親は、ほかの窓の保存で描き直しても残す
  // picked は選んだ製品（組立品の窓では組立品）。空は全部
  const view = (S.structure ??= {})[key] ??= { drawing: child ? child.range[0] : null, picked: [] };
  view.picked ??= [];
  const rangePicked = child ? [child.range[1] || []].flat() : null; // 前の形の窓の URL（製品1つか空）も読む
  let data = { drawing: null, drawings: [], products: [], main_product: null, direct: {}, inside: {}, bases: {}, items: {} };
  let parents = [];
  const base = (code) => data.bases[code] ?? code;
  const me = {
    child, view, base, only: false, note: el("span", { class: "mini" }),
    get data() { return data; },
    get parents() { return parents; },
    // 打ち替えが効く製品
    range: () => data.products.filter((p) => { const set = child ? rangePicked : view.picked; return !set.length || set.includes(p); }),
    columns: () => parents.filter((p) => !view.picked.length || view.picked.includes(p)),
    all: (parent) => (child ? data.inside[parent] : data.direct[parent]) || [],
    slots: (parent) => me.all(parent).filter((s) => !s.lost), // 現行の品番が無くなった箇所を除く
    name: (code) => (data.items[code] ? data.items[code].name : ""),
    nameCell: (code) => (!code ? { text: "" } : data.items[code] ? { text: me.name(code), cls: "auto" } : { text: "未登録", cls: "bad" }),
    spec: (code) => ({ text: data.items[code] ? data.items[code].specification : "", cls: "auto" }),
    // 仮品番は、名称に印を足さず、状態の列に「仮」と出す
    state: (code) => ({ text: !data.items[code] ? "" : data.items[code].temp ? "仮" : data.items[code].lifecycle, cls: "auto c q" }),
    // busy が真なら、読んだ物を使わずに false を返す（打っている間に行と列を入れ替えない）
    async load(busy = () => false) {
      if (!S.number) return true;
      const got = await api("GET", "/api/v2/structure", q({ plan: PLAN, ...(view.drawing === null ? {} : view.drawing === "*" ? { all: "1" } : view.drawing === "" ? { blank: "1" } : { drawing: view.drawing }) }));
      if (busy()) return false;
      data = got;
      if (!data.products.length && view.drawing !== null && !child) { // 対象品番から外れた図面
        view.drawing = null;
        return me.load(busy);
      }
      if (view.drawing === null) view.drawing = data.drawing ?? "";
      parents = child ? inRange() : data.products;
      // 中を変えた組立品は品番が替わるので、流用元が同じ物を選び直す
      view.picked = [...new Set(view.picked.map((x) => (parents.includes(x) ? x : parents.find((p) => base(p) === base(x)))).filter(Boolean))];
      if (view.picked.length === parents.length) view.picked = [];
      fill(drawingPick, data.drawings.map((d) => el("option", { value: d.drawing, selected: d.drawing === view.drawing }, `${d.drawing || "（なし）"}（${d.count} 製品）`)),
        data.drawings.length > 1 ? el("option", { value: "*", selected: view.drawing === "*" }, `全部（${S.targets.length} 製品）`) : null);
      parentPick.draw();
      if (child) users.textContent = `使用製品 ${usedBy()}`;
      return true;
    },
    // 行 { balloon, first, part, now, cells: 親 → 箇所 }。compare なら現行の品番も行の組に入れ、balloons が偽なら風船番号をまとめる。
    // compare でなければ、1つの親の同じ風船番号に同じ品番が2つある時は行を分け（n＝何番目か・変えていない箇所が先）、無くなった箇所（lost）も出す
    rows({ compare = false, balloons = true, sort = null } = {}) {
      const lead = child ? parents : [...new Set([...(data.main_product ? [data.main_product] : []), ...parents])];
      // 風船番号の順。風船番号の無い行は最後に、現行の品番の順（打ち替えても動かないように）
      const code = (r) => r.now ?? r.part;
      const byCode = (a, b) => code(a).localeCompare(code(b), "ja", { numeric: true });
      const order = sort || ((a, b) => (!balloons ? a.rank - b.rank || balloonOrder(a.first, b.first) || byCode(a, b)
        : balloonOrder(a.balloon, b.balloon) || (a.balloon === null ? byCode(a, b) : a.rank - b.rank) || a.part.localeCompare(b.part)));
      const add = (s, p) => (p ? { ...s, before: s.before === null ? p.before : String(Number(p.before ?? 0) + Number(s.before)), quantity: String(Number(p.quantity) + Number(s.quantity)), problem: s.problem || p.problem } : s);
      const found = new Map();
      for (const p of me.columns()) {
        const seen = new Map();
        const slots = compare ? me.slots(p) : [...me.all(p)].sort((a, b) => (a.now !== a.part) - (b.now !== b.part));
        for (const s of slots) {
          const place = JSON.stringify([s.balloon, s.part, !!s.lost]);
          const n = compare ? 0 : seen.get(place) ?? 0;
          seen.set(place, n + 1);
          const k = JSON.stringify([balloons ? s.balloon : "", compare ? s.now : "", s.part, n, !!s.lost]);
          if (!found.has(k)) found.set(k, { balloon: balloons ? s.balloon : null, first: s.balloon, part: s.part, now: s.now, n, lost: !!s.lost, rank: Infinity, cells: new Map() });
          const row = found.get(k);
          row.rank = Math.min(row.rank, lead.indexOf(p));
          row.cells.set(p, add(s, row.cells.get(p)));
        }
      }
      return [...found.values()].sort(order);
    },
    // 絞り込みと「変更行のみ」
    shown(rows) {
      const c = norm(codeBox.value);
      const n = norm(nameBox.value);
      const hit = (code) => code !== null && (!c || norm(code).includes(c)) && (!n || norm(me.name(code)).includes(n));
      const found = rows.filter((r) => (hit(r.part) || hit(r.now)) && (!me.only || [...r.cells.values()].some(movedSlot)));
      me.note.textContent = `${found.length === rows.length ? `行 ${rows.length}` : `行 ${found.length} / ${rows.length}`}${found.length > MAX_STRUCTURE_ROWS ? `・先頭の ${MAX_STRUCTURE_ROWS} 行を出しています` : ""}`;
      return found.slice(0, MAX_STRUCTURE_ROWS);
    },
    head(code) {
      const star = !child && code === data.main_product ? "★ " : "";
      return { text: `${star}${code}\n${me.name(code) || "未登録"}`, cls: "vh" + (data.items[code] ? "" : " bad"), title: `${star}${code} ${me.name(code)}` };
    },
    opens: (row) => !row.draft && !row.lost && !!data.inside[row.part] && [...row.cells.values()].some((s) => Number(s.quantity) !== 0),
    openCell: (row) => (row && me.opens(row) ? { text: "開く", cls: "link", title: "同じ風船番号の組立品を並べて開く" } : { text: "" }),
    // 同じ風船番号の組立品を、まとめて開く（風船番号の無い行は、その組立品だけ）
    open(row) {
      const bases = new Set([base(row.part)]);
      if (row.balloon !== null) {
        for (const p of me.columns()) for (const s of me.slots(p)) if (s.balloon === row.balloon && data.inside[s.part] && Number(s.quantity) !== 0) bases.add(base(s.part));
      }
      const list = [...bases].sort();
      const range = child ? child.range : [view.drawing, view.picked];
      if (!openWindow(key, { parents: list.join(","), range: JSON.stringify(range) }, `-${list.join("-").replace(/[^A-Za-z0-9-]/g, "_")}`)) alert("窓を開けませんでした（ブラウザの設定で、別の窓を許してください）");
    },
  };
  // 範囲の製品の下にある組立品のうち、流用元が親の品番の物
  const reach = (products) => {
    const seen = new Set();
    const stack = products.flatMap((p) => data.direct[p] || []);
    while (stack.length) {
      const s = stack.pop();
      if (s.lost || Number(s.quantity) === 0 || seen.has(s.part) || !data.inside[s.part]) continue;
      seen.add(s.part);
      stack.push(...data.inside[s.part]);
    }
    return seen;
  };
  const inRange = () => [...reach(me.range())].filter((c) => child.bases.includes(base(c)))
    .sort((a, b) => child.bases.indexOf(base(a)) - child.bases.indexOf(base(b)) || a.localeCompare(b, "ja", { numeric: true }));
  const usedBy = () => me.range().filter((p) => {
    const found = reach([p]);
    return parents.some((c) => found.has(c));
  }).length;
  const norm = (s) => clean(s).toLowerCase();
  const drawingPick = el("select", { class: "in", onchange: () => {
    view.drawing = drawingPick.value;
    view.picked = [];
    work("読み込んでいます", async () => {
      await me.load();
      redraw();
    }).catch(fail);
  } });
  const parentPick = checkList(() => parents.map((p) => [p, `${p} ${me.name(p)}`]), () => view.picked, (list) => {
    view.picked = list;
    redraw();
  });
  const users = el("span", { class: "mini" });
  const codeBox = el("input", { class: "in" });
  const nameBox = el("input", { class: "in" });
  for (const box of [codeBox, nameBox]) box.addEventListener("input", () => redraw());
  const only = toggle("変更行のみ", false, (on) => {
    me.only = on;
    redraw();
  });
  me.picks = [child ? null : el("label", { class: "f" }, "図面", drawingPick), el("div", { class: "f" }, child ? "組立品" : "製品", parentPick)]; // label で包むと、中のチェックを押した時に一覧のボタンも押される
  me.filters = [el("label", { class: "f" }, "品番", codeBox), el("label", { class: "f" }, "名称", nameBox)];
  me.onlyToggle = only;
  me.users = child ? users : null;
  await me.load();
  me.title = (text) => (child ? `${text}　${child.bases.slice(0, 3).join("・")}${child.bases.length > 3 ? ` ほか ${child.bases.length - 3}` : ""}` : text);
  return me;
}

async function listScreen(main, cur) {
  let rows = [];
  let balloons = true;
  let sort = "balloon";
  let side = "after";
  const body = el("div", { class: "listview" });
  const draw = () => {
    const text = { part: (r) => r.now ?? r.part, name: (r) => me.name(r.now ?? r.part) }[sort];
    const order = text && ((a, b) => text(a).localeCompare(text(b), "ja", { numeric: true }) || a.rank - b.rank || balloonOrder(a.first, b.first));
    rows = me.shown(me.rows({ compare: true, balloons, sort: order || null }));
    grid.render();
  };
  const me = await structureView(5, () => draw());
  cur.reload = async () => {
    await me.load();
    draw();
  };
  const parents = () => me.columns();
  // 列は1組。［現行］［変更後］で出す品番と数量を切り替え、変わる所はどちらでも黄色にする
  const moved = (r) => r.part !== r.now;
  const code = (r) => (side === "now" ? r.now : r.part);
  const mark = (r, cell) => (moved(r) ? { ...cell, cls: `${cell.cls ?? ""} own` } : cell);
  const columns = () => [
    ...(balloons ? [[{ text: "風船", cls: "c" }, (r) => ({ text: balloonText(r.balloon), cls: "c q" })],
      [{ text: "サブ番号", cls: "c" }, (r) => ({ text: branchText(r.balloon), cls: "c q" })]] : []),
    ["品番", (r) => mark(r, { text: code(r) ?? "" })], ["名称", (r) => mark(r, me.nameCell(code(r)))],
    ["仕様", (r) => me.spec(code(r))], ["状態", (r) => me.state(code(r))], ["下位", (r) => me.openCell(r)]];
  const model = {
    head: () => ["", ...columns().map((c) => c[0]), ...parents().map(me.head)],
    nRows: () => rows.length,
    nCols: () => 1 + columns().length + parents().length,
    cell(r, c) {
      if (c === 0) return { text: String(r + 1), cls: "n" };
      const cols = columns();
      if (c <= cols.length) return cols[c - 1][1](rows[r]);
      const s = rows[r].cells.get(parents()[c - 1 - cols.length]);
      const edge = c === cols.length + 1 ? " split" : "";
      if (!s) return { text: "", cls: "r q" + edge };
      const shown = side === "now" ? s.before : s.quantity;
      return { text: shown === null ? "" : fmt(shown), cls: "r q" + edge + (s.problem ? " bad" : !same(s.before, s.quantity) ? " own" : ""),
        title: s.problem ? "要確認（変更一覧）" : null };
    },
    set() {},
    action(r, c) {
      if (rows[r] && c === columns().length && me.opens(rows[r])) me.open(rows[r]);
      return false;
    },
  };
  const grid = new Grid(body, model);
  const balloonToggle = toggle("風船", true, (on) => {
    balloons = on;
    draw();
  });
  const sides = el("div", { class: "chips" });
  const drawSides = () => fill(sides, [["now", "現行"], ["after", "変更後"]].map(([k, text]) =>
    el("button", { class: "chip" + (side === k ? " on" : ""), onclick: () => {
      side = k;
      drawSides();
      draw();
    } }, text)));
  drawSides();
  const pick = (options, set) => {
    const select = el("select", { class: "in", style: "min-width:90px", onchange: () => {
      set(select.value);
      draw();
    } }, options.map(([v, t]) => el("option", { value: v }, t)));
    return select;
  };
  const whereUsed = () => {
    const at = rows[grid.sel.r];
    if (!at) return alert("品番の行を選んでください");
    whereUsedDialog(at.part);
  };
  // 主対象は対象品番の中から1つ。替えると並びと★だけが変わる
  const mainPick = el("select", { class: "in", disabled: !canWrite(), onchange: async () => {
    try {
      await api("PUT", "/api/v2/main-product", { number: S.number }, { main_product: mainPick.value || null });
      await me.load();
      draw();
    } catch (e) {
      fail(e);
    }
  } }, el("option", { value: "" }, "（なし）"), S.targets.map((code) => el("option", { value: code, selected: code === me.data.main_product }, code)));
  main.append(card(me.title("構成一覧"), [
    me.child ? null : el("label", { class: "inline" }, "主対象 ", mainPick),
    el("button", { class: "btn", onclick: whereUsed, disabled: !S.number }, "使用先"),
    me.child ? null : el("button", { class: "btn", onclick: conditionsDialog, disabled: !S.number }, "計算条件"),
  ], [
    el("div", { class: "row" }, me.picks, el("label", { class: "f" }, "並び", pick([["balloon", "風船番号"], ["part", "品番"], ["name", "名称"]], (v) => { sort = v; })),
      me.filters, sides, showGroup(me.onlyToggle, balloonToggle), me.users, me.note),
    body,
  ]));
  draw();
  grid.focus();
}

// ⑧ 品番と数量を、その欄で打ち替える。品番は行に出ている親の全部に、数量は親ごとに効く。空にすると現行に戻る。
// 親が組立品なら、中を変えた組立品に新しい品番が付き（サーバが決める）、開いた時に出していた製品の範囲に効く
async function rewriteScreen(main, cur) {
  const ro = !canWrite();
  let rows = [];
  let drawing = null;
  let drafts = []; // 足しかけの行 { draft, balloon, part, cells: 空, amounts: 親 → 数量, after: 上の行の鍵 }
  let place = new Map(); // 行の鍵 → 並びの位置（打った行が動かないように、開いた時・並べ直した時の位置を持つ）
  let zero = true; // 数量 0（消した箇所）を出すか
  let moved = false; // 現行と違う箇所があるか
  const body = el("div", { class: "listview" });
  const nowLine = el("div", { class: "nowline" }); // 選んだ行の現行
  const keyOf = (r) => JSON.stringify([r.balloon, me.base(r.part), r.n ?? 0, !!r.lost]);
  let late = false; // 打っている間に来て、描き直しを待っている物があるか
  const draw = () => {
    if (grid.editing) {
      late = true;
      return;
    }
    // 前からある行は前の位置のまま、新しい行は直前の行のすぐ後ろ
    const all = me.rows();
    moved = all.some((r) => [...r.cells.values()].some(movedSlot));
    const sorted = zero ? all : all.filter((r) => [...r.cells.values()].some((s) => Number(s.quantity) !== 0));
    let last = 0;
    for (const r of sorted) {
      const k = keyOf(r);
      if (!place.has(k)) place.set(k, last + 1e-6);
      last = place.get(k);
    }
    rows = me.shown(sorted.map((r, i) => [r, i]).sort(([a, i], [b, j]) => place.get(keyOf(a)) - place.get(keyOf(b)) || i - j).map(([r]) => r));
    if (sorted.length < all.length) me.note.textContent += `（0 の行 ${all.length - sorted.length} 非表示）`;
    for (const d of drafts) {
      // 足しかけの行は、選んでいた行の後ろに出す
      let at = rows.findIndex((r) => !r.draft && keyOf(r) === d.after);
      if (at >= 0) for (at += 1; at < rows.length && rows[at].draft && rows[at].after === d.after; at += 1);
      rows.splice(at < 0 ? rows.length : at, 0, d);
    }
    grid.render();
  };
  const restart = () => {
    drawing = me.view.drawing;
    place = new Map();
    me.rows().forEach((r, i) => { if (!place.has(keyOf(r))) place.set(keyOf(r), i); });
  };
  const me = await structureView(8, () => {
    if (drawing !== me.view.drawing) {
      drafts = [];
      restart();
    }
    draw();
  });
  restart();
  const parents = () => me.columns();
  // 列: 番号・風船・サブ番号・品番・名称・仕様・状態・下位・一律、その右に親ごとの数量
  const [BALLOON, BRANCH, PART, NAME, SPEC, STATE, LOWER, UNIFORM] = [1, 2, 3, 4, 5, 6, 7, 8]; // 一律は足しかけの行だけ打てる。打つと出ている親の全部に同じ数量が入る
  const left = 9;
  // head・branch は足しかけの行に打った風船番号とサブ番号（balloon はその2つを合わせた箇所の印）。
  // uniform は一律に打った数量。spread は一律で入れたか（もう同じ物がある親には足さない）
  const newDraft = (near) => ({ draft: true, balloon: "", head: "", branch: "", part: "", now: null, cells: new Map(), amounts: new Map(), uniform: "", spread: false, after: near ? (near.draft ? near.after : keyOf(near)) : null });
  // 打ち替えは溜めて、changed() で1回に送る
  let parts = new Map(); // 行 → 品番（空は現行に戻す）
  let amounts = new Map(); // 行 → 親 → 数量（空は現行に戻す）
  const model = {
    head: () => [{ text: "", cls: "slim" }, { text: "風船", cls: "c slim" }, { text: "サブ番号", cls: "c" }, "品番", "名称", "仕様", { text: "状態", cls: "slim" }, { text: "下位", cls: "slim" },
      { text: "一律", cls: "r slim" }, ...parents().map(me.head)],
    nRows: () => rows.length + (ro || !parents().length ? 0 : 1),
    nCols: () => left + parents().length,
    grow: ro ? undefined : () => {
      const d = newDraft(null);
      drafts.push(d);
      rows.push(d);
      return true;
    },
    cell(r, c) {
      const row = rows[r];
      if (c === 0) return { text: String(r + 1), cls: "n" };
      const part = row ? row.part : "";
      if (c === BALLOON || c === BRANCH) {
        const text = !row ? "" : row.draft ? row[c === BALLOON ? "head" : "branch"] : c === BALLOON ? balloonText(row.balloon) : branchText(row.balloon);
        return { text, editable: !ro && (!row || !!row.draft), lock: !!row && !row.draft, cls: c === BALLOON ? "c q slim" : "c q wide" };
      }
      if (c === PART) {
        const own = row && (row.draft ? part : [...row.cells.values()].some((s) => s.now !== s.part));
        return { text: part, editable: !ro && !(row && row.lost), lock: !!row && row.lost, cls: row && row.lost ? "bad" : own ? "own" : "",
          title: row && row.lost ? "現行の品番が部品表に無くなった（［戻す］で消す）" : null };
      }
      // 細い列と詰める列の印を足す（詰めた文字は、押さえた時の札で全部見える）
      const fit = (cell, cls) => ({ ...cell, cls: `${cell.cls ?? ""} ${cls}`, title: cell.title ?? (cls.startsWith("clip") ? cell.text : null) });
      if (c === NAME) return fit(row && row.draft ? nameCell(part) : me.nameCell(part), "clip");
      if (c === SPEC) return fit(me.spec(part), "clip narrow");
      if (c === STATE) return fit(me.state(part), "slim");
      if (c === LOWER) return fit(me.openCell(row), "slim");
      if (c === UNIFORM) return !row || row.draft ? { text: row ? row.uniform : "", editable: !ro, cls: "r q slim" } : { text: "", lock: true, cls: "off slim" };
      const parent = parents()[c - left];
      const edge = c === left ? " split" : "";
      if (!row) return { text: "", editable: !ro, cls: "r q" + edge };
      if (row.draft) return { text: row.amounts.get(parent) ?? "", editable: !ro, cls: "r q" + edge };
      const s = row.cells.get(parent);
      if (row.lost) return { text: s ? fmt(s.quantity) : "", lock: true, cls: "r q" + (s ? " bad" : "") + edge };
      if (!s) return { text: "", editable: !ro, cls: "r q" + edge };
      const changed = !same(s.before, s.quantity);
      const shown = zero || Number(s.quantity) !== 0;
      return { text: shown ? fmt(s.quantity) : "", edit: shown ? s.quantity : "", editable: !ro, cls: "r q" + edge + (s.problem ? " bad" : changed ? " own" : ""),
        title: s.problem ? "要確認（変更一覧）" : changed && s.before !== null ? `現行 ${fmt(s.before)}` : null };
    },
    set(r, c, text) {
      if (c < BALLOON || (c > PART && c < left && c !== UNIFORM)) return;
      if (c === UNIFORM && rows[r] && !rows[r].draft) return;
      if (!rows[r]) {
        if (!text) return;
        model.grow();
      }
      const row = rows[r];
      const parent = parents()[c - left];
      if ((c >= left || c === UNIFORM) && text !== "" && !(isNumber(text) && Number(text) >= 0)) return alert(`数量は0以上の数で入力してください（${row.part || "品番なし"}「${text}」）`);
      if (row.draft) {
        row.refused = false;
        if (c === BALLOON || c === BRANCH) {
          if (text.includes(BRANCH_SEP)) return alert(`風船番号とサブ番号に「${BRANCH_SEP}」は使えません（「${text}」）`);
          row[c === BALLOON ? "head" : "branch"] = c === BALLOON && text === NO_BALLOON ? "" : text;
          row.balloon = joinBalloon(row.head, row.branch) ?? "";
        } else if (c === PART) row.part = text;
        else if (c === UNIFORM) {
          row.uniform = text;
          if (text !== "") {
            row.spread = true;
            for (const p of parents()) row.amounts.set(p, text);
          }
        } else if (text === "") row.amounts.delete(parent);
        else row.amounts.set(parent, text);
        return;
      }
      if (c === BALLOON || c === BRANCH || row.lost) return;
      if (c === PART) {
        if (text !== row.part) parts.set(row, text);
        return;
      }
      const s = row.cells.get(parent);
      if (s ? same(text, s.quantity) || (!zero && text === "" && Number(s.quantity) === 0) : text === "") return; // 隠した 0 は、空のままなら変えない
      if (!amounts.has(row)) amounts.set(row, new Map());
      amounts.get(row).set(parent, text);
    },
    changed() { apply(); },
    idle() {
      if (!late) return;
      late = false;
      later(refresh);
    },
    action(r, c) {
      if (c === LOWER && rows[r] && me.opens(rows[r])) me.open(rows[r]);
      return false;
    },
    selected(r) {
      const row = rows[r];
      const cells = row && !row.draft ? [...row.cells.values()] : [];
      const found = new Map(); // 現行の品番 → 数量 → 親の数
      for (const s of cells) {
        if (!found.has(s.now)) found.set(s.now, new Map());
        const counts = found.get(s.now);
        const n = s.before === null ? "" : fmt(s.before);
        counts.set(n, (counts.get(n) || 0) + 1);
      }
      const unit = me.child ? "組立品" : "製品";
      const text = [...found].map(([code, counts]) => (code === null ? `現行には無い（${[...counts.values()].reduce((a, b) => a + b, 0)} ${unit}）`
        : `${code} ${me.name(code)}　数量 ${[...counts].map(([n, k]) => `${n}（${k} ${unit}）`).join("・")}`)).join("　／　");
      fill(nowLine, el("b", {}, "選択行の現行　"), text ? `風船 ${balloonText(row.balloon)}${branchText(row.balloon) ? `　サブ番号 ${branchText(row.balloon)}` : ""}　${text}` : "");
      rowBack.disabled = ro || !cells.some(movedSlot);
      allBack.disabled = ro || !moved;
    },
  };
  // 箇所を現行の品番と数量に戻す。list は [行, 親, 箇所]
  const back = async (list) => {
    const body = { back: [], inside: [] };
    for (const [row, parent, s] of list) {
      const was = keyOf({ ...row, part: s.now, n: 0, lost: false });
      if (s.now !== null && !place.has(was)) place.set(was, place.get(keyOf(row)) ?? 0);
      const at = { balloon: s.balloon };
      if (!me.child) body.back.push({ ...at, product: parent, now: s.now, part: s.part });
      else if (s.now === null) body.inside.push({ ...at, parent, now: null, old: s.part, quantity: "0" });
      else if (Number(s.quantity) === 0) body.inside.push({ ...at, parent, old: null, new: s.now, quantity: s.before });
      else body.inside.push({ ...at, parent, now: s.now, old: s.part, new: s.now, quantity: s.before });
    }
    for (const e of body.inside) e.products = me.range();
    try {
      await send("/api/v2/structure", body, `${list.length} 箇所を現行に戻しました`);
    } catch (e) {
      fail(e);
    }
    await refresh();
  };
  const movedIn = (list) => list.flatMap((row) => [...row.cells].filter(([, s]) => movedSlot(s)).map(([parent, s]) => [row, parent, s]));
  const backRow = async () => {
    const row = selected();
    const list = row ? movedIn([row]) : [];
    if (!list.length) return;
    const was = keyOf({ ...row, part: list[0][2].now ?? row.part, n: 0, lost: false });
    const c = grid.sel.c;
    await back(list);
    const r = rows.findIndex((x) => !x.draft && keyOf(x) === was);
    if (r >= 0) grid.select(r, c);
    grid.focus();
  };
  const backAll = async () => {
    const list = movedIn(me.rows());
    if (!list.length) return;
    const where = `${me.child ? "組立品" : "製品"} ${new Set(list.map(([, parent]) => parent)).size} つ`;
    if (confirm(`表示中の${where}の ${list.length} 箇所を、全部現行に戻します（非表示の行も戻します）。よろしいですか？`)) await back(list);
    grid.focus();
  };
  const button = (text, run) => el("button", { class: "btn", disabled: ro || !S.number, onclick: () => {
    if (Grid.active) Grid.active.commit();
    run();
  } }, text);
  const rowBack = button("戻す", () => later(backRow));
  const allBack = button("全て戻す", () => later(backAll));
  rowBack.title = "選択行の品番と数量を現行に戻す";
  allBack.title = "表示中の全部を現行に戻す";
  const zeroToggle = toggle("数量0の行", true, (on) => {
    zero = on;
    const row = selected();
    draw();
    const r = row ? rows.findIndex((x) => x.draft ? x === row : !row.draft && keyOf(x) === keyOf(row)) : -1; // 選んでいた行のまま
    if (r >= 0) grid.select(r, grid.sel.c);
  });
  const grid = new Grid(body, model);
  // 足しかけの行がある間は、閉じる時に引き止める
  const later = sendQueue(cur, () => drafts.some((d) => d.part || d.balloon || d.amounts.size));
  const renamed = new Map(); // 品番の替わった組立品 → 新しい品番（送っている間に打った物を、新しい品番の親に入れる）
  const newest = (parent) => (renamed.has(parent) ? newest(renamed.get(parent)) : parent);
  const carry = (from, to) => {
    for (const d of drafts) {
      if (!d.amounts.has(from)) continue;
      if (!d.amounts.has(to)) d.amounts.set(to, d.amounts.get(from));
      d.amounts.delete(from);
    }
  };
  const follow = (from, to) => {
    if (newest(to) === from) renamed.delete(to);
    if (from !== to) renamed.set(from, to);
    carry(from, to);
  };
  const origins = new Map(); // 今までに出た仮品番 → 流用元
  // 出ている親の品番が替わっていたら（中を現行に戻して流用元に戻った時など）、足しかけの数量を移す
  const settle = () => {
    for (const [code, from] of Object.entries(me.data.bases)) origins.set(code, from);
    const origin = (code) => origins.get(code) ?? code;
    for (const lost of new Set(drafts.flatMap((d) => [...d.amounts.keys()]).filter((code) => !me.parents.includes(code)))) {
      const found = me.parents.filter((code) => origin(code) === origin(lost));
      if (found.length === 1) carry(lost, found[0]);
    }
  };
  // 組立品の中を変えると新しい品番が要る時は、入れる前に聞く（自動の品番を入れておく・中身の同じ仮品番がある時は聞かない）。
  // やめたら入れずに、打った所を戻す（false を返す）
  const send = async (path, payload, text) => {
    if (payload.inside && payload.inside.length) {
      const fresh = (await api("POST", "/api/v2/structure/needs", { number: S.number, plan: PLAN }, { inside: payload.inside })).needs.filter((n) => n.fresh);
      if (fresh.length) {
        const numbers = await askNumbers(fresh);
        if (!numbers) {
          await refresh();
          done("やめました");
          return false;
        }
        payload = { ...payload, numbers };
      }
    }
    const d = await api("POST", path, { number: S.number, plan: PLAN }, payload);
    tell("saved");
    if (me.child) for (const m of d.made || []) follow(m.code, m.new);
    done(text + madeText(d));
  };
  // 保存した物を読み直して描く。打っている間は待ち、閉じた時に読み直す
  const refresh = async () => {
    items.clear(); // 仮品番の品番・名称が変わる
    await lookup(drafts.map((d) => d.part));
    if (await me.load(() => !!grid.editing)) {
      renamed.clear();
      settle();
      draw();
    } else late = true;
  };
  cur.reload = () => later(refresh);
  const apply = () => {
    const typed = parts;
    const counts = amounts;
    parts = new Map();
    amounts = new Map();
    const shown = new Set(parents());
    const ready = [];
    let kept = 0; // 全部に入れる時に、もうあるので足さない所
    for (const d of drafts) {
      if (!d.part || d.refused || d.sending) continue;
      for (const parent of d.spread ? [...d.amounts.keys()] : []) {
        if (!me.slots(parent).some((s) => s.balloon === (d.balloon || null) && s.part === d.part && Number(s.quantity) !== 0)) continue;
        d.amounts.delete(parent);
        kept += 1;
      }
      const list = new Map([...d.amounts].filter(([parent]) => shown.has(parent))); // 出していない親に入れた数量は送らない
      if (!list.size) continue;
      d.sending = true;
      ready.push({ d, part: d.part, balloon: d.balloon || null, list });
    }
    if (typed.size || counts.size || ready.length) return later(() => run(typed, counts, ready, kept));
    if (kept) alert("同じ風船番号と品番が、もう全部にあります");
    lookup(drafts.map((d) => d.part)).then((got) => got && grid.render()).catch(fail);
  };
  const run = async (typed, counts, ready, kept) => {
    await lookup([...typed.values(), ...ready.map((x) => x.part)]);
    const body = { back: [], quantity: [], replace: [], add: [], inside: [] };
    const renames = [];
    // 親が製品なら直下の箇所、組立品なら組立品の中の箇所として送る。1つの箇所の品番と数量は1つにまとめる
    const edits = new Map();
    const inner = (parent, s) => {
      const k = JSON.stringify([parent, s.balloon, s.now, s.part]);
      if (!edits.has(k)) edits.set(k, { parent: newest(parent), balloon: s.balloon, now: s.now, old: s.part });
      return edits.get(k);
    };
    const fresh = (parent, balloon, part, quantity) => (me.child ? body.inside.push({ parent: newest(parent), balloon, old: null, new: part, quantity })
      : body.add.push({ product: parent, balloon, part, quantity }));
    let count = 0;
    for (const [row, text] of typed) {
      // 流用した仮品番に、登録の無い品番を打った時は、その仮品番の品番を替える
      if (text && me.data.bases[row.part] && items.get(text) && !items.get(text).registered) {
        renames.push([row.part, text]);
        continue;
      }
      for (const [parent, s] of row.cells) {
        const to = text || s.now;
        const given = counts.has(row) ? counts.get(row).get(parent) : undefined;
        if (Number(s.quantity) === 0 && !Number(given)) continue; // 消した箇所は、数量を空にして戻してから替える
        if (given !== undefined && given !== "" && Number(given) === 0) continue; // 一緒に消した箇所
        const at = { product: parent, balloon: s.balloon, now: s.now };
        if (to === null) me.child ? (inner(parent, s).quantity = "0") : body.back.push({ ...at, part: s.part }); // 足した箇所の取り消し
        else if (to !== s.part) me.child ? (inner(parent, s).new = to) : body.replace.push({ ...at, old: s.part, new: to });
        count += 1;
      }
      const to = keyOf({ ...row, part: text, n: 0 });
      if (text && !place.has(to)) place.set(to, place.get(keyOf(row))); // 打った行が動かないように、新しい鍵に位置を渡す
    }
    for (const [row, values] of counts) {
      const part = typed.get(row) || row.part; // 品番と一緒に打った（貼った）数量は、新しい品番に入れる
      for (const [parent, text] of values) {
        const s = row.cells.get(parent);
        if (s && s.now === null && typed.get(row) === "") continue; // 一緒に取り消した箇所
        count += 1;
        if (!s) {
          if (text !== "" && Number(text) !== 0) fresh(parent, row.balloon, part, text);
          continue;
        }
        if (!me.child) {
          body.quantity.push({ product: parent, balloon: s.balloon, now: s.now, part: s.part, quantity: text });
          continue;
        }
        const value = text === "" ? s.before ?? "0" : text;
        if (Number(s.quantity) !== 0) inner(parent, s).quantity = value;
        else if (Number(value) !== 0) fresh(parent, s.balloon, part, value);
      }
    }
    body.inside.push(...edits.values());
    for (const e of body.inside) e.products = me.range();
    try {
      if (Object.values(body).some((list) => list.length)) await send("/api/v2/structure", body, `${count} 箇所を変えました`);
      for (const [code, to] of renames) {
        await send("/api/v2/temp-rename", { code, new: to }, `${code} を ${to} にしました`);
        follow(code, to);
      }
    } catch (e) {
      fail(e);
    }
    // 足しかけの行は1行ずつ送る（1行が断られても、ほかの打ち替えは止めない。断られた行は、打ち直すまで送らない）
    for (const { d, part, balloon, list } of ready) {
      const adding = [...list].map(([parent, quantity]) => (me.child ? { parent: newest(parent), products: me.range(), balloon, old: null, new: part, quantity }
        : { product: parent, balloon, part, quantity }));
      const k = keyOf({ balloon, part });
      if (!place.has(k)) place.set(k, place.has(d.after) ? place.get(d.after) + 1e-7 : Math.max(0, ...place.values()) + 1);
      try {
        await send("/api/v2/structure", me.child ? { inside: adding } : { add: adding }, `${adding.length} 箇所に足しました${kept ? `（もうある ${kept} 箇所は足していません）` : ""}`);
        const shown = new Set(parents());
        for (const parent of [...d.amounts.keys()]) if (list.has(parent) || !shown.has(parent)) d.amounts.delete(parent);
        for (const [parent] of list) d.amounts.delete(newest(parent));
        if (!d.amounts.size) drafts = drafts.filter((x) => x !== d);
      } catch (e) {
        d.refused = true;
        fail(e);
      }
      d.sending = false;
    }
    await refresh();
    if (drafts.length) apply(); // 送っている間に打った足しかけ
  };
  const selected = () => rows[grid.sel.r];
  // 一括置換: 今の構成（変えた後）の品番を、行（風船番号と元品番、全階層なら親品番も）ごとに選んで1回だけ置き換える。決まりとしては残さない。
  // 範囲の「全階層」（本体の窓だけ）は、対象品番の全部の、組立品の中まで探す。中を替えると上の組立品に新しい品番が要るので、入れる前に聞く
  const bulkDialog = async () => {
    const at = selected();
    const old = el("input", { class: "in", value: at && !at.draft ? at.part : "" });
    const oldWord = el("input", { class: "in" }); // 元の部品を名称でも探す（品番と両方入れたら両方に合う物）
    const next = el("input", { class: "in" });
    const oldName = el("span", { class: "mini" });
    const nextName = el("span", { class: "mini" });
    const list = el("div");
    const off = new Set(); // 替えない行
    let every = null; // 対象品番の全部の直下（範囲で選んだ時に読む）
    let everyItems = {};
    let deep = null; // 全階層（範囲で選んだ時に読む・⑪ と同じ口）
    const nameOf = (code) => (me.data.items[code] || everyItems[code] || {}).name ?? (items.get(code) || {}).name ?? "";
    const unit = me.child ? "組立品" : "製品";
    const range = el("select", { class: "in" }, el("option", { value: "here" }, `表示中の${unit}（${parents().length}）`),
      me.child ? null : el("option", { value: "all" }, `全対象品番（${S.targets.length}）`),
      me.child ? null : el("option", { value: "deep" }, `全階層（${S.targets.length}）`));
    const same = (a, b) => a === b.trim() || clean(a) === clean(b);
    const fold = (s) => clean(s).toLowerCase();
    const hit = (part) => (!clean(old.value) || same(part, old.value)) && (!clean(oldWord.value) || fold(nameOf(part)).includes(fold(oldWord.value)));
    // 行: { key, level, parent（組立品の中の時の親）, balloon, part, codes（製品か組立品）, top（直下の箇所）, inner（組立品の中の箇所） }
    const groups = () => {
      if (!clean(old.value) && !clean(oldWord.value)) return [];
      const found = new Map();
      const group = (key, init) => {
        if (!found.has(key)) found.set(key, { key, codes: new Set(), top: [], inner: [], ...init });
        return found.get(key);
      };
      if (range.value === "deep") {
        for (const p of deep.places) {
          if (Number(p.quantity) === 0 || !hit(p.part)) continue;
          const g = group(JSON.stringify([1, "", p.balloon, p.part]), { level: 1, parent: "", balloon: p.balloon, part: p.part });
          g.codes.add(p.product);
          g.top.push({ product: p.product, balloon: p.balloon, ...(p.now === undefined ? {} : { now: p.now }), old: p.part });
        }
        for (const r of deep.inside) {
          if (!hit(r.part)) continue;
          for (const line of r.lines) {
            if (Number(line.quantity) === 0) continue;
            const parent = r.path[r.path.length - 1];
            // 同じ親品番の中の同じ箇所は、上の組立品が違っても1行にまとめる（どの上かは見せない）
            const g = group(JSON.stringify([r.path.length + 1, parent, line.balloon, r.part]), { level: r.path.length + 1, parent, balloon: line.balloon, part: r.part });
            for (const place of deep.tops[r.path[0]] || []) g.codes.add(place.product);
            g.inner.push({ path: r.path, old: r.part, balloon: line.balloon });
          }
        }
      } else {
        const slots = range.value === "all" ? Object.entries(every) : parents().map((p) => [p, me.slots(p)]);
        for (const [parent, found2] of slots) {
          for (const s of found2) {
            if (s.lost || !hit(s.part) || Number(s.quantity) === 0) continue;
            const g = group(JSON.stringify([1, "", s.balloon, s.part]), { level: 1, parent: "", balloon: s.balloon, part: s.part });
            g.codes.add(parent);
            g.top.push(me.child ? { parent, products: me.range(), balloon: s.balloon, now: s.now, old: s.part }
              : { product: parent, balloon: s.balloon, now: s.now, old: s.part });
          }
        }
      }
      return [...found.values()].sort((a, b) => a.level - b.level || a.parent.localeCompare(b.parent, "ja", { numeric: true })
        || balloonOrder(a.balloon, b.balloon) || a.part.localeCompare(b.part, "ja", { numeric: true }));
    };
    // 新品番と同じ品番の行は、替えても変わらないので外す
    const chosen = () => groups().filter((g) => !off.has(g.key) && !same(g.part, next.value));
    const count = (gs) => gs.reduce((n, g) => n + g.top.length + g.inner.length, 0);
    const run = el("button", { class: "btn pri", onclick: () => replace().catch(fail) });
    const drawList = () => {
      const shown = groups();
      const levels = range.value === "deep";
      const head = ["対象", ...(levels ? ["レベル", "親品番"] : []), "風船", "サブ番号", "元品番", "名称", "箇所", unit];
      fill(list, shown.length ? el("div", { style: "max-height:360px;overflow:auto" }, el("table", { class: "list" }, el("tr", {}, head.map((t) => el("th", {}, t))),
        shown.map((g) => {
          const codes = [...g.codes];
          return el("tr", {},
            el("td", {}, el("input", { type: "checkbox", checked: !off.has(g.key), onchange: (e) => {
              e.target.checked ? off.delete(g.key) : off.add(g.key);
              drawList();
            } })),
            ...(levels ? [el("td", { class: "r" }, String(g.level)), el("td", {}, g.parent)] : []),
            el("td", {}, balloonText(g.balloon)), el("td", {}, branchText(g.balloon)), el("td", {}, g.part), el("td", {}, nameOf(g.part)),
            el("td", { class: "r" }, g.top.length + g.inner.length),
            el("td", { title: codes.join("・") }, codes.slice(0, 4).join("・") + (codes.length > 4 ? ` ほか ${codes.length - 4}` : "")));
        }))) : el("div", { class: "mini" }, clean(old.value) || clean(oldWord.value) ? "今の構成にありません" : ""));
      run.textContent = `置換（${count(chosen())} 箇所）`;
      run.disabled = !chosen().length || !clean(next.value);
    };
    const names = async () => {
      const codes = [old.value.trim(), clean(next.value)];
      await lookup(codes);
      oldName.textContent = nameCell(codes[0]).text;
      nextName.textContent = nameCell(codes[1]).text;
    };
    for (const input of [old, oldWord]) input.addEventListener("input", () => {
      off.clear();
      drawList();
    });
    next.addEventListener("input", drawList);
    for (const input of [old, next]) input.addEventListener("change", () => names().catch(fail));
    range.addEventListener("change", async () => {
      try {
        if (range.value === "all" && !every) {
          const got = await api("GET", "/api/v2/structure", q({ plan: PLAN, all: "1" }));
          every = got.direct;
          everyItems = got.items;
        }
        if (range.value === "deep" && !deep) {
          deep = await work("読み込んでいます", () => api("GET", "/api/v2/after-parts", q({ plan: PLAN })));
          await lookup(deep.places.map((p) => p.part)); // 直下の部品の名称（組立品の中の部品は name が付いて来る）
          for (const r of deep.inside) if (r.name !== null && !everyItems[r.part]) everyItems[r.part] = { name: r.name };
        }
      } catch (e) {
        range.value = "here";
        fail(e);
      }
      off.clear();
      drawList();
    });
    const replace = async () => {
      const picked = chosen();
      const to = clean(next.value);
      const top = picked.flatMap((g) => g.top.map((t) => ({ ...t, new: to })));
      const inner = picked.flatMap((g) => g.inner.map((x) => ({ ...x, new: to })));
      await later(async () => {
        if (range.value !== "deep") {
          const body = me.child ? { inside: top } : { replace: top };
          if (await work("変更しています", () => send("/api/v2/structure", body, `${top.length} 箇所を ${to} に替えました`)) === false) return;
        } else {
          // 組立品の中を替えると上の組立品に新しい品番が要るので、何も入れる前に聞く（やめたら何も入れない）。入れるのは直下が先、中は後
          const fresh = inner.length ? (await api("POST", "/api/v2/structure/needs", { number: S.number, plan: PLAN }, { rows: inner })).needs.filter((n) => n.fresh) : [];
          const numbers = fresh.length ? await askNumbers(fresh) : {};
          if (numbers === null) return done("やめました");
          if (top.length) await work("変更しています", () => api("POST", "/api/v2/replace/apply", { number: S.number, plan: PLAN }, { rows: top }));
          if (inner.length) {
            const d = await work("変更しています", () => api("POST", "/api/v2/inside/replace", { number: S.number, plan: PLAN }, { rows: inner, numbers }));
            done(`${top.length + inner.length} 箇所を ${to} に替えました${madeText(d)}`);
          } else done(`${top.length} 箇所を ${to} に替えました`);
          tell("saved");
        }
        for (const g of picked) {
          if (g.level !== 1) continue;
          // 替えた行が動かないように、新しい鍵に位置を渡す
          const [from, key] = [g.part, to].map((part) => keyOf({ balloon: g.balloon, part }));
          if (place.has(from) && !place.has(key)) place.set(key, place.get(from));
        }
        box.close();
        await refresh();
        grid.focus();
      });
    };
    const box = dialog("一括置換", [
      el("div", { class: "row" }, el("label", { class: "f" }, "元品番", old), el("label", { class: "f" }, "元の名称", oldWord), oldName, "→",
        el("label", { class: "f" }, "新品番", next), nextName,
        el("label", { class: "f" }, "範囲", range)),
      list,
    ], [run, el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
    drawList();
    next.focus();
    await names();
  };
  const addRow = () => {
    const d = newDraft(selected() || null);
    drafts.push(d);
    draw();
    grid.select(rows.indexOf(d), 1);
    grid.focus();
  };
  const dropRow = () => {
    const row = selected();
    if (!row) return alert("行を選んでください");
    if (row.draft) {
      drafts = drafts.filter((d) => d !== row);
      return draw();
    }
    const all = new Map([...row.cells].filter(([, s]) => Number(s.quantity) !== 0).map(([p]) => [p, "0"]));
    if (!all.size) return;
    if (row.lost) return;
    amounts.set(row, all);
    apply();
  };
  main.append(card(me.title("構成変更"), [button("行追加", addRow), button("行削除", dropRow), button("一括置換", () => bulkDialog().catch(fail)), rowBack, allBack, button("風船順", () => {
    restart();
    draw();
  })], [
    el("div", { class: "row" }, me.picks, me.filters, showGroup(me.onlyToggle, zeroToggle), me.users, me.note),
    body, nowLine,
  ]));
  draw();
  grid.firstEditable();
  grid.render();
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

// ⑥ 変更一覧 -------------------------------------------------------------

// 構成の変更を、左に現行・右に変更後で見る（左が空＝追加・右が空＝削除）。組立品の中の変更は、レベルを付けて下に続ける。
// 製品から（製品×変更ごとに1行・風船番号の順）か、部品から（同じ変更は1行・製品の数を押すと製品ごとの箇所）で見る。
// 金額の変更（⑩ 製品の費目の書き換え）は下に分けて出す
async function changesScreen(main) {
  const plan = PLAN;
  let product = "";
  let view = "product"; // product＝製品から・part＝部品から
  let data = { summary: [], by_product: [] };
  let rows = []; // 出している行 { level, now: {code, name, quantity} か null, after, products, balloon, problems, product, details }
  let temps = {}; // 流用した仮品番 → { base, name, changes }
  const ownBox = el("div");
  const deepBox = el("div");
  const noteBox = el("span", { class: "mini" });
  const ro = !canWrite();
  // した事1つを、現行と変更後の数量の組ごとの行にする
  const lines = (s, level) => {
    const found = new Map();
    for (const d of s.details) {
      const key = `${d.before} ${d.after} ${level > 1 || view === "product" ? d.balloon : ""}`;
      if (!found.has(key)) found.set(key, { ...d, details: [], products: [] });
      const x = found.get(key);
      x.details.push(d);
      if (!x.products.includes(d.product)) x.products.push(d.product);
    }
    const old = { code: s.old, name: s.old_name };
    const fresh = s.kind === "数量" ? old : { code: s.new, name: s.new_name };
    return [...found.values()].flatMap((x) => [{
      level, problems: s.problems, balloon: x.balloon, product: x.product, products: x.products, details: x.details,
      now: s.kind === "追加" ? null : { ...old, quantity: x.before }, after: s.kind === "削除" ? null : { ...fresh, quantity: x.after },
    }, ...inner(s.kind === "数量" ? null : s.new, level + 1)]);
  };
  const inner = (code, level) => (code && temps[code] && level < 20 ? temps[code].changes.flatMap((s) => lines(s, level)) : []);
  // 並べ替えは1段目の行ごと（中の行は付いて動く）
  const sort = sortable(() => arrange());
  const arrange = () => {
    const tops = view === "part" ? data.summary : data.by_product.filter((r) => r.kind !== "変更なし").map((r) => ({ ...r, details: [r] }));
    const blocks = [];
    for (const row of tops.flatMap((s) => lines(s, 1))) {
      if (row.level === 1) blocks.push([row]);
      else blocks[blocks.length - 1].push(row);
    }
    rows = sort.apply(blocks, (b) => b[0]).flat();
    const none = data.by_product.filter((r) => r.kind === "変更なし").map((r) => r.product);
    noteBox.textContent = none.length ? `変更の無い製品 ${none.length}` : "";
    noteBox.title = none.join("・");
  };
  const load = async () => {
    data = await api("GET", "/api/v2/changes", q({ plan, product }));
    temps = data.temps;
    fill(deepBox, data.deep ? el("div", { class: "row" },
      el("span", { class: "pill warn" }, `2階層目の変更 ${data.deep} 件は計算に入りません`),
      el("span", { class: "mini" }, "組立品の中は、構成変更の［開く］で替えます"),
      ro ? null : el("button", { class: "btn", onclick: dropDeep }, "2階層目の変更削除")) : null);
    const owns = data.rows.filter((r) => r.source === "own");
    await lookup([...data.by_product.map((r) => r.product), ...owns.map((r) => r.product)]);
    fill(ownBox, owns.length ? [el("h3", {}, "製品の費目の書き換え"), el("table", { class: "list" },
      el("tr", {}, ["製品", "名称", "費目", "現行", "変更後", ...(owns.some((r) => r.problems.length) ? ["要確認"] : [])].map((t) => el("th", {}, t))),
      owns.map((r) => el("tr", {}, el("td", {}, r.product), el("td", {}, nameCell(r.product).text), el("td", {}, r.part),
        el("td", { class: "r" }, r.before === null ? "—" : fmt(r.before)), el("td", { class: "r" }, fmt(r.after)),
        owns.some((x) => x.problems.length) ? el("td", {}, r.problems.join("・")) : null)))] : []);
    arrange();
  };
  const detailDialog = (row) => {
    const box = dialog([row.now && row.now.code, row.after && row.after.code].filter(Boolean).join(" → "),
      el("table", { class: "list" }, el("tr", {}, ["製品", "名称", "親", "風船", "サブ番号", "現行", "変更後"].map((t) => el("th", {}, t))),
        row.details.map((d) => el("tr", {}, el("td", {}, d.product), el("td", {}, nameCell(d.product).text), el("td", {}, d.parent),
          el("td", {}, balloonText(d.balloon)), el("td", {}, branchText(d.balloon)), el("td", { class: "r" }, d.before === null ? "—" : fmt(d.before)), el("td", { class: "r" }, fmt(d.after))))),
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
  const body = el("div");
  const named = (x) => (!x ? { text: "" } : x.name === null ? { text: "未登録", cls: "bad" } : { text: x.name, cls: "auto" });
  const count = (x) => (!x ? "" : x.quantity === null ? "—" : fmt(x.quantity));
  const code = (side) => (x) => (x[side] ? x[side].code : "");
  // 左の列（見出し, 中身）と、現行・変更後の列
  const columns = () => [
    ...(view === "product" ? [
      [sort.head("製品", (x) => x.product), (x, first) => ({ text: first ? x.product : "" })],
      ["名称", (x, first) => (first ? nameCell(x.product) : { text: "" })]] : []),
    ["レベル", (x) => ({ text: ".".repeat(x.level - 1) + x.level })],
    ...(view === "product" ? [[{ text: "風船", cls: "c" }, (x) => ({ text: balloonText(x.balloon), cls: "c" })],
      [{ text: "サブ番号", cls: "c" }, (x) => ({ text: branchText(x.balloon), cls: "c" })]] : []),
    [{ ...sort.head("品番", code("now"), false, "now"), cls: "split" }, (x) => ({ text: code("now")(x), cls: "split" })],
    ["名称", (x) => named(x.now)],
    [{ text: "数量", cls: "r" }, (x) => ({ text: count(x.now), cls: "r" })],
    [{ ...sort.head("品番", code("after"), false, "after"), cls: "after split" }, (x) => ({ text: code("after")(x), cls: "split" + (code("after")(x) && code("after")(x) !== code("now")(x) ? " own" : "") })],
    [{ text: "名称", cls: "after" }, (x) => named(x.after)],
    [{ text: "数量", cls: "after r" }, (x) => ({ text: count(x.after), cls: "r" + (x.after && x.now && !same(x.after.quantity, x.now.quantity) ? " own" : "") })],
    ...(view === "part" ? [[{ ...sort.head("製品", (x) => String(x.products.length), true), cls: "r split" }, (x) => (x.level > 1 ? { text: "", cls: "split" } : { text: String(x.products.length), cls: "r link split", title: "押すと製品ごとの箇所" })]] : []),
    ["要確認", (x) => ({ text: x.problems.join("・"), cls: x.problems.length ? "bad" : "" })],
  ];
  const before = () => (view === "part" ? 1 : 5); // 現行より左の列の数（行番号を除く）
  const model = {
    groups: () => [{ text: "", span: 1 + before() }, { text: "現行", span: 3, cls: "split" }, { text: "変更後", span: 3, cls: "after split" }, { text: "", span: view === "part" ? 2 : 1, cls: "split" }],
    head: () => ["", ...columns().map((x) => x[0])],
    nRows: () => rows.length,
    nCols: () => 1 + columns().length,
    cell(r, c) {
      if (c === 0) return { text: String(r + 1), cls: "n" };
      const first = r === 0 || rows[r].level === 1 && rows.slice(0, r).findLast((x) => x.level === 1).product !== rows[r].product;
      return columns()[c - 1][1](rows[r], first);
    },
    set() {},
    action(r, c) {
      if (view === "part" && c === 8 && rows[r] && rows[r].level === 1) detailDialog(rows[r]);
      return false;
    },
  };
  await load();
  const grid = new Grid(body, model);
  const views = el("div", { class: "chips" });
  const drawViews = () => fill(views, [["product", "製品別"], ["part", "部品別"]].map(([k, text]) =>
    el("button", { class: "chip" + (view === k ? " on" : ""), onclick: () => {
      view = k;
      sort.reset();
      arrange();
      drawViews();
      grid.render();
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
  ], [deepBox, el("div", { class: "row" }, views, noteBox), body, ownBox]));
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

// 製品別＝製品ごとの費目全部（現行・変更後・差）、変更別＝変更ごとの影響（製造原価への効き方）
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
  // した事ごとの効き方。足すと製品別の表の製造原価の年間の影響になる
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
  // 費目全部: 製品ごとに 現行・変更後・差 の3行、列＝費目。見出しを押すと並べ替える（費目は差の順）
  const sort = sortable(() => draw());
  const drawProducts = () => {
    const heads = data.heads;
    const rows = sort.apply(heads.length ? heads[0].rows.map((r, i) => ({ r, i })) : []);
    const head = (text, value, number, cls) => {
      const s = sort.head(text, value, number);
      return el("th", { class: cls, title: s.title, style: "cursor:pointer", onclick: s.action }, s.text);
    };
    const diffOf = (r) => (r.plans[plan] === null || r.now === null ? null : String(Number(r.plans[plan]) - Number(r.now)));
    const line = (title, cells) => [el("td", { class: "mini" + (title === "差" ? "" : " low") }, title), cells];
    return el("table", { class: "res" },
      el("tr", {}, head("対象品番", (x) => x.r.product), head("名称", (x) => x.r.name), head("年間台数", (x) => x.r.annual, true, "r"), el("th", {}),
        heads.map((h) => head(h.label, (x) => diffOf(h.rows[x.i]), true, "r"))),
      rows.flatMap(({ r: r0, i }) => [
        el("tr", {}, el("td", { rowspan: "3" }, r0.product), el("td", { rowspan: "3" }, r0.name ?? "未登録"), el("td", { class: "r", rowspan: "3" }, fmt(r0.annual)),
          line("現行", heads.map((h) => el("td", { ...incomplete(h.rows[i].now_incomplete), class: "r low" + (h.rows[i].now_incomplete ? " inc" : "") }, fmt(h.rows[i].now))))),
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
  // 製品別（費目全部）と変更別（製造原価への効き方）を切り替えて出す。開いた時は製品別
  let view = "product";
  const views = () => el("div", { class: "chips" }, [["product", "製品別"], ["change", "変更別"]].map(([k, text]) =>
    el("button", { class: "chip" + (view === k ? " on" : ""), onclick: () => {
      view = k;
      draw();
    } }, text)));
  const draw = () => fill(body, data ? [
    el("div", { class: "row" }, views(), view === "change" ? el("span", { class: "mini" }, "製造原価") : null),
    el("div", { style: "overflow:auto" }, view === "change" ? drawEffects() : drawProducts())] : null);
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
    head: () => ["", "対象品番", "名称", ...heads.flatMap((h) => [{ text: `${h.label} 現行`, cls: "r" }, { text: `${h.label} 変更後`, cls: "r" }])],
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
// 品目の台帳に無い品番の名称・仕様・材料費（1個あたり）。この検討の計算にだけ使う（部品の原価は製品の材料費にだけ入るので材料費だけ）。
// 部品の新しい品番は、ここで先に取る。組立品の中を変えた品番は ⑧ の［開く］でできる（元品番は見るだけ・材料費は空なら中の変更から計算する）

async function tempScreen(main, cur) {
  const ro = !canWrite();
  const blank = () => ({ key: "", code: "", name: "", specification: "", base: "", material: "", derived: null, master: false, mark: false });
  let rows = [];
  let known = new Set(); // 前に受け取った仮品番（ここに無い物は、ほかの窓でできた物）
  // サーバーで決まる値（元品番・中の変更から計算した材料費・台帳にあるか）を行に移す
  const take = (row, got) => Object.assign(row, { key: got.code, base: got.base ?? "", derived: got.derived, master: got.master });
  const row = (r) => take({ ...blank(), code: r.code, name: r.name, specification: r.specification ?? "", material: r.material ?? "" }, r);
  const load = async () => {
    const d = await api("GET", "/api/v2/temp-items", q());
    rows = d.rows.map(row);
    known = new Set(d.rows.map((r) => r.code));
  };
  // 受け取った物を今の行に移し、ほかの窓でできた仮品番の行を足す
  const merge = (d) => {
    const got = new Map(d.rows.map((x) => [x.code, x]));
    for (const r of rows) if (got.has(r.code)) take(r, got.get(r.code));
    for (const x of d.rows) if (!known.has(x.code) && !rows.some((r) => r.key === x.code)) rows.push(row(x));
    known = new Set(got.keys());
  };
  await load();
  const markAll = () => {
    const all = !(rows.length && rows.every((r) => r.mark));
    for (const r of rows) r.mark = all;
  };
  const sort = sortable(() => {
    rows = sort.apply(rows);
    if (!ro) saver.schedule(); // 並びも一時保存する
  });
  // 材料費を打っていない、中を変えた組立品は、計算した値を灰色で出す（打てば打った値を使う）
  const derivedShown = (row) => row.base && row.material === "" && !row.master;
  const body = el("div");
  const model = {
    head: () => [
      { text: rows.length && rows.every((r) => r.mark) ? "☑" : "☐", cls: "c", title: "全部の行に印", action: ro ? null : markAll },
      "", sort.head("品番", (r) => r.code), sort.head("名称", (r) => r.name), sort.head("仕様", (r) => r.specification),
      { ...sort.head("材料費", (r) => (r.material !== "" ? r.material : r.derived), true), cls: "r" }, sort.head("元品番", (r) => r.base), "",
    ],
    nRows: () => rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push(blank()); return true; },
    nCols: () => 8,
    cell(r, c) {
      const row = rows[r];
      if (c === 0) return { text: row ? (row.mark ? "☑" : "☐") : "", cls: "act" };
      if (c === 1) return { text: String(r + 1), cls: "n" };
      if (c >= 2 && c <= 4) return { text: row ? row[["code", "name", "specification"][c - 2]] : "", editable: !ro };
      if (c === 5) {
        if (row && derivedShown(row)) {
          return row.derived === null ? { text: "不明", edit: "", editable: !ro, cls: "r bad", title: "元品番か、中の部品の材料費が分かりません" }
            : { text: fmt(row.derived), edit: "", editable: !ro, cls: "r auto" };
        }
        return { text: row ? row.material : "", editable: !ro, cls: "r" };
      }
      if (c === 6) return { text: row ? row.base : "", cls: "auto" };
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
      rows[r][["code", "name", "specification", "material"][c - 2]] = text;
      saver.schedule();
    },
    action(r, c) {
      const row = rows[r];
      if (!row || ro || c !== 0) return false;
      row.mark = !row.mark;
      return true;
    },
  };
  const grid = new Grid(body, model);
  // 品番を打った行だけを送る（名称だけ打った行は画面に残す）。元品番は送らない（⑧ で決まり、ここでは変えない）
  const saver = autosaver(cur, async () => {
    const d = await api("PUT", "/api/v2/temp-items", { number: S.number },
      { known: [...known], rows: rows.filter((r) => r.code).map((r) => ({ key: r.key || undefined, code: r.code, name: r.name, specification: r.specification, material: r.material })) });
    merge(d);
    items.clear();
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
  main.append(card("仮品番", [del], [body]));
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

// 新規作成の ③: 下書きの中身は ② の窓が持つ
function openDraft(id) {
  if (!channel) return done("この画面は開けません");
  S.mode = "new";
  S.draft = { id, header: {}, targets: [], take(targets) {
    S.draft.take = () => {}; // 最初の返事だけを使う
    S.draft.targets = targets;
    document.title = `${NUM[3]} ${label(NAMES[3])}`;
    openScreen(3);
  } };
  channel.postMessage({ type: "draft-ask", id });
}

// 別の窓は URL の検討を読んでその画面を開く
async function openFromUrl() {
  const key = Number(PARAMS.get("screen"));
  if (!NAMES[key]) return done("開く画面が違います");
  S.mode = ["edit", "view"].includes(PARAMS.get("mode")) ? PARAMS.get("mode") : "edit";
  if (key === 3 && PARAMS.get("draft") && !PARAMS.get("number")) return openDraft(PARAMS.get("draft"));
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
