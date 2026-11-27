// 設計検討の画面の共通の道具と画面の枠（画面ごとの中身は hub.js〜temp.js・起動は boot.js）。② から ③〜⑪ を別の窓で開く（② は開いたまま）。入れた値はその場で一時保存する（⑦の［実行］は点検と計算だけで、書かない）。
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
  variations: {}, // 対象品番のバリエーション → 元の製品
  draft: null, // 新規作成で番号が付く前に入れた物 { header, targets, main }
};
const items = new Map(); // 品番 → 右の列の値（/api/v2/lookup）
let current = null; // 開いている画面 { key, saver }

// 別の窓: ② から ③〜⑪ を開く。窓の URL に画面・検討番号・版・処理（新規作成の ③ は下書きの印 draft）を載せる
const NAMES = { 3: "対象品番", 5: "構成一覧", 6: "変更一覧", 7: "結果", 8: "構成変更", 9: "仮品番", 10: "費目変更", 11: "バリエーション" };
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
  S.variations = data.variations || {};
}
