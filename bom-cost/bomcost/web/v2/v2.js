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
  if (info.temp) return { text: `${info.name}（仮）`, cls: "auto", title: "仮品番（⑨ で登録した品番）" };
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
  const url = location.pathname + query({ screen: key, number: S.number, version: S.version ?? "", mode: S.mode, draft: S.draft ? S.draft.id : "" });
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
      el("tr", {}, ["親品番", "風船番号", "子品番", "取り込む数量", "ほかの数量"].map((t) => el("th", {}, t))),
      conflicts.slice(0, MAX_CONFLICTS).map((c) => el("tr", {}, el("td", {}, raw(c.parent)), el("td", {}, raw(c.balloon ?? "—")), el("td", {}, raw(c.child)),
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
// 対象品番の部品（全階層）を、品番か名称で絞って、左に現行・右に変更後で出す。変更後の空欄＝現行のまま。打つと置き換え、空にすると現行に戻す
// （1回きりの操作で、決まりとしては残さない）。行の細かさは3つで、打った行に入っている箇所が全部替わる:
// 一括＝現行の部品ごと・親品番ごと＝部品×入っている組立品ごと・製品ごと＝部品×製品×風船番号ごと（直下の行は数量も変えられる。0 は消す）。
// 組立品の中を替えると、上の組立品に新しい品番（流用した仮品番）が自動で付き、変更後の親品番に出る（その場で打ち直せる）。
// 「部品を足す」は、選んだ製品の直下にまとめて足す（風船番号は無し）
const MAX_PART_ROWS = 1000;

async function searchReplaceScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let grain = "part"; // part＝一括・parent＝親品番ごと・product＝製品ごと
  let places = []; // 直下の箇所 { product, balloon, part, quantity（0 は消した箇所）, was, before: 現行の数量（足した箇所は null） }
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
    shown = hits.slice(0, MAX_PART_ROWS);
    note.textContent = `${searched ? `検索に当たる行 ${hits.length}・` : ""}変えた行 ${hits.filter((g) => g.moved).length}${hits.length > shown.length ? `・先頭の ${MAX_PART_ROWS} 行を出しています` : ""}`;
    await lookup(shown.flatMap((g) => [...g.codes, g.parent, g.parentAfter]));
    grid.render();
  };
  const usersDialog = async (g) => {
    const products = [...g.products];
    try {
      await lookup(products);
      const box = dialog(`使う製品 ${g.now || [...g.afters][0]}`,
        el("table", { class: "list" }, el("tr", {}, el("th", {}, "品番"), el("th", {}, "名称")),
          products.map((code) => el("tr", {}, el("td", {}, code), el("td", {}, nameCell(code).text)))),
        [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
    } catch (e) {
      fail(e);
    }
  };
  // 送って、返ってきた物で描き直す。うまくいけば true
  const send = async (path, body, text) => {
    try {
      const d = await work("置き換えています", () => api("POST", path, { number: S.number, plan }, body));
      items.clear(); // 仮品番の品番・材料費が変わる
      take(d);
      tell("saved");
      const made = d.made && d.made.length ? `。組立品 ${d.made.length} つに新しい品番を付けました（${d.made.map((m) => `${m.code} → ${m.new}`).join("・")}）` : "";
      const typedPrice = d.typed && d.typed.length ? `。${d.typed.join("・")} は材料費を入力してあるので、金額は変わりません` : "";
      done(text + made + typedPrice);
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
  const apply = async () => {
    const work = [...typed];
    const amounts = [...counts];
    const names = [...renames];
    typed = new Map();
    counts = new Map();
    renames = new Map();
    if (!work.length && !amounts.length && !names.length) return;
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
  const slot = (p) => ({ product: p.product, balloon: p.balloon });
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
      [{ text: "風船", cls: "c" }, (g) => ({ text: (only(g) ? only(g).balloon : line(g).balloon) ?? "—", cls: "c" })],
      [{ text: "数量", cls: "r" }, (g) => ({ text: fmt(only(g) ? only(g).before : line(g).quantity), cls: "r" })]]
      : [[{ text: "製品", cls: "r" }, (g) => ({ text: String(g.products.size), cls: "r link", title: "押すと製品の一覧" })]]),
    [{ text: "部品品番", cls: "after split" }, afterCell, "code"],
    [{ text: "部品名称", cls: "after" }, afterName],
    ...(grain === "part" ? [[{ text: "材料費", cls: "after r" }, (g) => (afterOf(g) ? price(afterOf(g)) : text(""))],
      [{ text: "組立品の新しい品番", cls: "after" }, (g) => ({ text: [...g.made].slice(0, 2).join("・") + (g.made.size > 2 ? ` ほか ${g.made.size - 2}` : ""), cls: "auto", title: [...g.made].join("・") })]]
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
        // 置き換えた行の空は、現行の数量に戻す（現行に無い品番なので、そのまま送ると消える）
        if (value === "" && p.was.length && p.before === null) return alert("現行が1つに決まらないので、数量を入力してください");
        counts.set(g, value === "" && p.was.length ? p.before : value);
      }
    },
    changed: () => { apply().catch(fail); },
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
    fill(modes, [["part", "一括"], ["parent", "親品番ごと"], ["product", "製品ごと"]].map(([k, name]) => el("button", { class: "chip" + (grain === k ? " on" : ""), onclick: async () => {
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
      fill(list, el("table", { class: "list" }, el("tr", {}, el("th", {}, all), el("th", {}, "製品"), el("th", {}, "名称"), el("th", {}, "使っている数量")),
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
      el("div", { class: "row" }, el("label", { class: "f" }, "製品品番", code), el("label", { class: "f" }, "製品名称", title),
        el("label", { class: "f" }, "使っている品番", uses), count),
      list,
    ], [el("span", { class: "sp" }), el("button", { class: "btn pri", onclick: run }, "足す"), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
    drawList();
    part.focus();
  };
  main.append(card("部品変更", [el("button", { class: "btn", disabled: ro || !S.number, onclick: addDialog }, "部品を足す")], [
    el("div", { class: "row" }, modes, el("label", { class: "f" }, "部品品番", codeBox), el("label", { class: "f" }, "部品名称", nameBox), productFilter, note),
    body,
  ]));
  await refilter();
  grid.firstEditable();
  grid.render();
  grid.focus();
}

// ⑤ 構成一覧・⑧ 構成変更 ---------------------------------------------------
// どちらも、図面が同じ製品を横に並べ、組立品の中はレベルを付けて下に続ける。⑧ は変えた後の構成をそのまま打ち替える画面、
// ⑤ は左に現行・右に変更後を並べて見るだけの画面

const MAX_STRUCTURE_ROWS = 1000;
const MAX_STRUCTURE_CELLS = 20000; // 最初から全部のレベルを出すのは、このマス数まで

function balloonOrder(a, b) {
  return (a === null) - (b === null) || (a ?? "").localeCompare(b ?? "", "ja", { numeric: true });
}

// 選んでいる図面・製品・レベルは、ほかの窓の保存で描き直しても残す
async function structureView(key, redraw) {
  const view = (S.structure ??= {})[key] ??= { drawing: null, product: "", level: null };
  let data = { drawing: null, drawings: [], products: [], main_product: null, direct: {}, inside: {}, bases: {}, items: {} };
  const me = {
    view, only: false, note: el("span", { class: "mini" }),
    get data() { return data; },
    products: () => data.products.filter((p) => !view.product || p === view.product),
    info: (code) => data.items[code],
    name: (code) => (data.items[code] ? data.items[code].name + (data.items[code].temp ? "（仮）" : "") : ""),
    nameCell: (code) => (!code ? { text: "" } : data.items[code] ? { text: me.name(code), cls: "auto" } : { text: "未登録", cls: "bad" }),
    spec: (code) => ({ text: data.items[code] ? data.items[code].specification : "", cls: "auto" }),
    state: (code) => ({ text: data.items[code] ? data.items[code].lifecycle : "", cls: "auto c q" }),
    async load() {
      if (!S.number) return;
      data = await api("GET", "/api/v2/structure", q({ plan: PLAN, ...(view.drawing === null ? {} : view.drawing === "*" ? { all: "1" } : view.drawing === "" ? { blank: "1" } : { drawing: view.drawing }) }));
      if (!data.products.length && view.drawing !== null) { // 対象品番から外れた図面
        view.drawing = null;
        return me.load();
      }
      if (view.drawing === null) view.drawing = data.drawing ?? "";
      if (view.product && !data.products.includes(view.product)) view.product = "";
      const deepest = me.depth();
      if (view.level === null) view.level = me.rows({ level: Infinity }).length * me.products().length > MAX_STRUCTURE_CELLS ? 1 : Infinity;
      fill(drawingPick, data.drawings.map((d) => el("option", { value: d.drawing, selected: d.drawing === view.drawing }, `${d.drawing || "（なし）"}（${d.count} 製品）`)),
        data.drawings.length > 1 ? el("option", { value: "*", selected: view.drawing === "*" }, `全部（${S.targets.length} 製品）`) : null);
      fill(productPick, el("option", { value: "" }, "全部"), data.products.map((p) => el("option", { value: p, selected: p === view.product }, `${p} ${me.name(p)}`)));
      fill(levelPick, Array.from({ length: Math.max(0, deepest - 1) }, (_, i) => el("option", { value: String(i + 1), selected: view.level === i + 1 }, `${i + 1} まで`)),
        el("option", { value: "", selected: view.level === Infinity || view.level >= deepest }, "全部"));
    },
    depth() {
      const known = {};
      const at = (code) => {
        if (code in known) return known[code];
        known[code] = 1;
        for (const s of data.inside[code] || []) known[code] = Math.max(known[code], 1 + at(s.part));
        return known[code];
      };
      return Math.max(1, ...Object.values(data.direct).flatMap((lines) => lines.map((s) => at(s.part))));
    },
    // 出す製品の構成を行にする。compare なら現行の品番も行の組に入れ、balloons が偽なら風船番号をまとめる。
    // 行 { level, balloon, part, now, cells: 製品 → 箇所, path: 上の組立品, tops: 直下の（製品, 風船番号）, derived: 現行と比べられるか, children }
    rows({ level = view.level, compare = false, balloons = true, sort = null } = {}) {
      const lead = [...new Set([...(data.main_product ? [data.main_product] : []), ...data.products])];
      const order = sort || ((a, b) => (balloons ? balloonOrder(a.balloon, b.balloon) || a.rank - b.rank : a.rank - b.rank || balloonOrder(a.first, b.first)) || a.part.localeCompare(b.part));
      const key = (s) => JSON.stringify([balloons ? s.balloon : "", compare ? s.now : "", s.part]);
      const add = (s, p) => (p ? { ...s, before: s.before === null ? p.before : String(Number(p.before ?? 0) + Number(s.before)), quantity: String(Number(p.quantity) + Number(s.quantity)), problem: s.problem || p.problem } : s);
      const open = (row) => {
        const live = [...row.cells].filter(([, s]) => Number(s.quantity) !== 0);
        const path = [...row.path, row.part];
        if (row.level >= level || !live.length || row.path.includes(row.part) || !data.inside[row.part]) return;
        const derived = row.derived && row.now !== null && (row.part === row.now || data.bases[row.part] === row.now);
        const tops = row.level === 1 ? live.map(([product]) => ({ product, balloon: row.balloon })) : row.tops;
        const found = new Map();
        for (const s of data.inside[row.part]) {
          const k = key(s);
          const line = add(s, found.get(k) && found.get(k).line);
          if (!found.has(k)) found.set(k, { level: row.level + 1, balloon: balloons ? s.balloon : null, first: s.balloon, part: s.part, now: s.now, rank: 0, path, tops, derived });
          found.get(k).line = line;
        }
        for (const child of found.values()) {
          child.cells = new Map(live.map(([p]) => [p, child.line]));
          open(child);
        }
        row.children = [...found.values()].sort(order);
      };
      const tops = new Map();
      for (const p of me.products()) {
        for (const s of data.direct[p] || []) {
          const k = key(s);
          if (!tops.has(k)) tops.set(k, { level: 1, balloon: balloons ? s.balloon : null, first: s.balloon, part: s.part, now: s.now, rank: lead.indexOf(p), cells: new Map(), path: [], tops: null, derived: true });
          const row = tops.get(k);
          row.rank = Math.min(row.rank, lead.indexOf(p));
          row.cells.set(p, add(s, row.cells.get(p)));
        }
      }
      for (const row of tops.values()) open(row);
      const flat = (list) => list.flatMap((row) => [row, ...flat(row.children || [])]);
      return flat([...tops.values()].sort(order));
    },
    // 絞り込みと「変えた所だけ」
    shown(rows) {
      const c = norm(codeBox.value);
      const n = norm(nameBox.value);
      const hit = (code) => code !== null && (!c || norm(code).includes(c)) && (!n || norm(me.name(code)).includes(n));
      const found = rows.filter((r) => (hit(r.part) || hit(r.now)) && (!me.only || [...r.cells.values()].some(movedSlot)));
      me.note.textContent = `${found.length === rows.length ? `行 ${rows.length}` : `行 ${found.length} / ${rows.length}`}${found.length > MAX_STRUCTURE_ROWS ? `・先頭の ${MAX_STRUCTURE_ROWS} 行を出しています` : ""}`;
      return found.slice(0, MAX_STRUCTURE_ROWS);
    },
    productHead(code) {
      const star = code === data.main_product ? "★ " : "";
      return { text: `${star}${code}\n${me.name(code) || "未登録"}`, cls: "vh" + (data.items[code] ? "" : " bad"), title: `${star}${code} ${me.name(code)}` };
    },
  };
  const norm = (s) => clean(s).toLowerCase();
  const reload = () => work("読み込んでいます", async () => {
    await me.load();
    redraw();
  }).catch(fail);
  const drawingPick = el("select", { class: "in", onchange: () => {
    view.drawing = drawingPick.value;
    view.product = "";
    reload();
  } });
  const productPick = el("select", { class: "in", onchange: () => {
    view.product = productPick.value;
    redraw();
  } });
  const levelPick = el("select", { class: "in", style: "min-width:80px", onchange: () => {
    view.level = levelPick.value ? Number(levelPick.value) : Infinity;
    redraw();
  } });
  const codeBox = el("input", { class: "in" });
  const nameBox = el("input", { class: "in" });
  for (const box of [codeBox, nameBox]) box.addEventListener("input", () => redraw());
  const only = el("button", { class: "chip", onclick: () => {
    me.only = !me.only;
    only.classList.toggle("on", me.only);
    redraw();
  } }, "変えた所だけ");
  me.picks = [el("label", { class: "f" }, "図面", drawingPick), el("label", { class: "f" }, "製品", productPick), el("label", { class: "f" }, "レベル", levelPick)];
  me.filters = [el("label", { class: "f" }, "品番", codeBox), el("label", { class: "f" }, "名称", nameBox)];
  me.onlyButton = only;
  await me.load();
  return me;
}

function movedSlot(s) {
  return s.now !== s.part || !same(s.before, s.quantity);
}

function levelText(level) {
  return ".".repeat(level - 1) + level;
}

async function listScreen(main) {
  let rows = [];
  let balloons = true;
  let sort = "balloon";
  let side = "after";
  const body = el("div", { class: "listview" });
  const draw = () => {
    const text = { part: (r) => r.now ?? r.part, name: (r) => me.name(r.now ?? r.part) }[sort];
    const tops = text && ((a, b) => (a.level > 1 ? balloonOrder(a.first, b.first) : text(a).localeCompare(text(b), "ja", { numeric: true })) || a.rank - b.rank || balloonOrder(a.first, b.first));
    rows = me.shown(me.rows({ compare: true, balloons, sort: tops || null }));
    grid.render();
  };
  const me = await structureView(5, () => draw());
  const products = () => me.products();
  const nowOf = (r) => (r.derived ? r.now : null);
  const afterOf = (r) => (r.part === nowOf(r) ? null : r.part);
  const gone = (r) => [...r.cells.values()].every((s) => Number(s.quantity) === 0);
  const left = () => [
    ["レベル", (r) => ({ text: levelText(r.level) })],
    ...(balloons ? [[{ text: "風船", cls: "c" }, (r) => ({ text: r.balloon ?? "—", cls: "c" })]] : []),
    ["品番", (r) => ({ text: nowOf(r) ?? "" })], ["名称", (r) => me.nameCell(nowOf(r))], ["仕様", (r) => me.spec(nowOf(r))], ["状態", (r) => me.state(nowOf(r))]];
  const right = [
    [{ text: "品番", cls: "after split" }, (r) => ({ text: afterOf(r) ?? "", cls: "split" + (afterOf(r) ? " own" : "") })],
    [{ text: "名称", cls: "after" }, (r) => (gone(r) ? { text: "削除", cls: "auto" } : me.nameCell(afterOf(r)))],
    [{ text: "仕様", cls: "after" }, (r) => me.spec(afterOf(r))], [{ text: "状態", cls: "after" }, (r) => me.state(afterOf(r))]];
  const columns = () => [...left(), ...right];
  const model = {
    groups: () => [{ text: "", span: 1 }, { text: "現行", span: left().length }, { text: "変更後", span: right.length, cls: "after split" },
      ...(products().length ? [{ text: side === "now" ? "現行の数量" : "変更後の数量", span: products().length, cls: "split" }] : [])],
    head: () => ["", ...columns().map((c) => c[0]), ...products().map(me.productHead)],
    nRows: () => rows.length,
    nCols: () => 1 + columns().length + products().length,
    cell(r, c) {
      if (c === 0) return { text: String(r + 1), cls: "n" };
      const cols = columns();
      if (c <= cols.length) return cols[c - 1][1](rows[r]);
      const s = rows[r].cells.get(products()[c - 1 - cols.length]);
      const edge = c === cols.length + 1 ? " split" : "";
      if (!s) return { text: "", cls: "r q" + edge };
      const shown = side === "now" ? (rows[r].derived ? s.before : null) : s.quantity;
      return { text: shown === null ? "" : fmt(shown), cls: "r q" + edge + (s.problem ? " bad" : side === "after" && !same(s.before, s.quantity) ? " own" : ""),
        title: s.problem ? "要確認（変更一覧）" : null };
    },
    set() {},
  };
  const grid = new Grid(body, model);
  const balloonButton = el("button", { class: "chip on", onclick: () => {
    balloons = !balloons;
    balloonButton.classList.toggle("on", balloons);
    draw();
  } }, "風船");
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
  main.append(card("構成一覧", [
    el("label", { class: "inline" }, "主対象 ", mainPick),
    el("button", { class: "btn", onclick: whereUsed, disabled: !S.number }, "使用先"),
    el("button", { class: "btn", onclick: conditionsDialog, disabled: !S.number }, "計算条件"),
  ], [
    el("div", { class: "row" }, me.picks, el("label", { class: "f" }, "並び", pick([["balloon", "風船番号"], ["part", "品番"], ["name", "名称"]], (v) => { sort = v; })),
      el("label", { class: "f" }, "数量", pick([["after", "変更後"], ["now", "現行"]], (v) => { side = v; })),
      me.filters, el("div", { class: "chips" }, me.onlyButton, balloonButton), me.note),
    body,
  ]));
  draw();
  grid.focus();
}

// ⑧ 品番と数量を、その欄で打ち替える。直下の数量は製品ごと、ほかは行に出ている製品の全部に効く。空にすると現行に戻る。
// 組立品の中を変えると、上の組立品に新しい品番が付く（サーバが決める）
async function rewriteScreen(main, cur) {
  const ro = !canWrite();
  let rows = [];
  let drawing = null;
  let drafts = []; // 足しかけの行 { draft, level, balloon, part, path, tops, cells: 空, amounts: 製品 → 数量, after・afterKey: 上の行 }
  let place = new Map(); // 行の鍵 → 並びの位置（打った行が動かないように、開いた時・並べ直した時の位置を持つ）
  const body = el("div", { class: "listview" });
  const base = (code) => me.data.bases[code] ?? code;
  const idOf = (r) => JSON.stringify([r.path, r.level > 1 ? r.tops : null, r.balloon, r.part]);
  const keyOf = (r) => JSON.stringify([r.path.map(base), r.level > 1 ? r.tops.map((t) => t.balloon)[0] : null, r.balloon, base(r.part)]);
  const draw = () => {
    // 1段目ごとのまとまりの中で並べる。前からある行は前の位置のまま、新しい行は直前の行のすぐ後ろ
    const sorted = me.rows();
    const groups = new Map();
    for (const r of sorted) {
      const k = JSON.stringify(r.path);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    let last = 0;
    for (const r of sorted) {
      const k = keyOf(r);
      if (!place.has(k)) place.set(k, last + 1e-6);
      last = place.get(k);
    }
    const nest = (list) => list.map((r, i) => [r, i]).sort(([a, i], [b, j]) => place.get(keyOf(a)) - place.get(keyOf(b)) || i - j).flatMap(([r]) => [r, ...nest(r.children || [])]);
    const all = nest(sorted.filter((r) => r.level === 1));
    rows = me.shown(all);
    for (const d of drafts) {
      // 足しかけの行は、選んでいた行（とその中）の後ろに出す
      let at = rows.findIndex((r) => !r.draft && idOf(r) === d.after);
      if (at >= 0) for (at += 1; at < rows.length && (rows[at].level > d.level || (rows[at].draft && rows[at].after === d.after)); at += 1);
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
  const products = () => me.products();
  const left = 7; // 番号・レベル・風船・品番・名称・仕様・状態
  const newDraft = (near) => ({ draft: true, level: near ? near.level : 1, balloon: "", part: "", now: null, path: near ? near.path : [], tops: near && near.level > 1 ? near.tops : null,
    cells: new Map(), amounts: new Map(), after: near ? (near.draft ? near.after : idOf(near)) : null, afterKey: near ? (near.draft ? near.afterKey : keyOf(near)) : null });
  const holds = (r, product) => (r.tops ? r.tops.some((t) => t.product === product) : true);
  // 打ち替えは溜めて、changed() で1回に送る
  let parts = new Map(); // 行 → 品番（空は現行に戻す）
  let amounts = new Map(); // 行 → 製品 → 数量（空は現行に戻す）
  const model = {
    head: () => ["", "レベル", { text: "風船", cls: "c" }, "品番", "名称", "仕様", "状態", ...products().map(me.productHead)],
    nRows: () => rows.length + (ro || !products().length ? 0 : 1),
    nCols: () => left + products().length,
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
      if (c === 1) return { text: row ? levelText(row.level) : "", cls: "q" };
      if (c === 2) return { text: row ? (row.draft ? row.balloon : row.balloon ?? "—") : "", editable: !ro && (!row || !!row.draft), cls: "c q" };
      if (c === 3) {
        const own = row && (row.draft ? part : [...row.cells.values()].some((s) => s.now !== s.part));
        return { text: part, editable: !ro, cls: own ? "own" : "" };
      }
      if (c === 4) return row && row.draft ? nameCell(part) : me.nameCell(part);
      if (c === 5) return me.spec(part);
      if (c === 6) return me.state(part);
      const product = products()[c - left];
      const edge = c === left ? " split" : "";
      if (!row) return { text: "", editable: !ro, cls: "r q" + edge };
      if (row.draft) return { text: row.amounts.get(product) ?? "", editable: !ro && holds(row, product), cls: "r q" + edge };
      const s = row.cells.get(product);
      if (!s) return { text: "", editable: !ro && row.level === 1, cls: "r q" + edge };
      const changed = !same(s.before, s.quantity);
      return { text: fmt(s.quantity), editable: !ro, cls: "r q" + edge + (s.problem ? " bad" : changed ? " own" : ""),
        title: s.problem ? "要確認（変更一覧）" : changed && s.before !== null ? `現行 ${fmt(s.before)}` : null };
    },
    set(r, c, text) {
      if (c < 2 || (c > 3 && c < left)) return;
      if (!rows[r]) {
        if (!text) return;
        model.grow();
      }
      const row = rows[r];
      const product = products()[c - left];
      if (c >= left && text !== "" && !(isNumber(text) && Number(text) >= 0)) return alert(`数量は0以上の数で入力してください（${row.part || "品番なし"}「${text}」）`);
      if (row.draft) {
        if (c === 2) row.balloon = text;
        else if (c === 3) row.part = text;
        // 組立品の中の数量は、その組立品を使う製品の全部に効く
        else for (const p of row.tops ? row.tops.map((t) => t.product) : [product]) text === "" ? row.amounts.delete(p) : row.amounts.set(p, text);
        return;
      }
      if (c === 2) return;
      if (c === 3) {
        if (text !== row.part) parts.set(row, text);
        return;
      }
      const s = row.cells.get(product);
      if (s ? same(text, s.quantity) : text === "") return;
      if (!amounts.has(row)) amounts.set(row, new Map());
      amounts.get(row).set(product, text);
    },
    changed() { apply().catch(fail); },
  };
  const grid = new Grid(body, model);
  const send = async (path, payload, text) => {
    const d = await work("変更しています", () => api("POST", path, { number: S.number, plan: PLAN }, payload));
    tell("saved");
    const made = d.made && d.made.length ? `。組立品 ${d.made.length} つに新しい品番を付けました（${d.made.map((m) => `${m.code} → ${m.new}`).join("・")}）` : "";
    const typed = d.typed && d.typed.length ? `。${d.typed.join("・")} は材料費を入力してあるので、金額は変わりません` : "";
    done(text + made + typed);
  };
  const apply = async () => {
    const typed = [...parts];
    const counts = [...amounts];
    parts = new Map();
    amounts = new Map();
    const ready = drafts.filter((d) => d.part && d.amounts.size);
    const codes = [...typed.map(([, v]) => v), ...drafts.map((d) => d.part)];
    if (await lookup(codes) && drafts.length) grid.render();
    if (!typed.length && !counts.length && !ready.length) return;
    const body = { quantity: [], replace: [], add: [], inside: [] };
    const renames = [];
    const deep = new Map(); // 行 → 中の打ち替え（品番と数量を1つにまとめる）
    const inner = (row) => {
      if (!deep.has(row)) deep.set(row, { path: row.path, tops: row.tops, balloon: row.balloon, old: row.part });
      return deep.get(row);
    };
    const line = (row) => row.cells.values().next().value;
    let count = 0;
    const move = (from, to) => !place.has(to) && place.has(from) && place.set(to, place.get(from)); // 打った行が動かないように、新しい鍵に位置を渡す
    for (const [row, text] of typed) {
      // 流用した仮品番に、登録の無い品番を打った時は、その仮品番の品番を替える
      if (text && me.data.bases[row.part] && items.get(text) && !items.get(text).registered) {
        renames.push([row.part, text]);
        continue;
      }
      if (row.level === 1) {
        for (const [product, s] of row.cells) {
          const at = { product, balloon: row.balloon };
          const to = text || s.now;
          if (!text && s.now === null) body.quantity.push({ ...at, part: s.part, quantity: "" }); // 足した箇所の取り消し
          else if (Number(s.quantity) === 0) continue; // 消した箇所は、数量を空にして戻してから替える
          else if (to !== s.part) body.replace.push({ ...at, old: s.part, new: to });
          count += 1;
        }
      } else {
        const s = line(row);
        if (Number(s.quantity) === 0) continue;
        if (!text && s.now === null) inner(row).quantity = "0";
        else if ((text || s.now) !== s.part) inner(row).new = text || s.now;
        count += 1;
      }
      if (text) move(keyOf(row), keyOf({ ...row, part: text }));
    }
    for (const [row, values] of counts) {
      if (row.level === 1) {
        for (const [product, text] of values) {
          const s = row.cells.get(product);
          const at = { product, balloon: row.balloon, part: row.part };
          if (!s) body.add.push({ ...at, quantity: text });
          // 置き換えた箇所の空は、現行の数量に戻す（現行に無い品番なので、そのまま送ると消える）
          else body.quantity.push({ ...at, quantity: text === "" && s.now !== s.part ? s.before ?? "" : text });
          count += 1;
        }
      } else {
        const kinds = new Set(values.values());
        if (kinds.size > 1) {
          alert(`${row.part} の数量が製品で違います。組立品の中の数量は、その組立品を使う製品の全部に効くので、1つだけ変える時は上で製品を選んでください`);
          continue;
        }
        const s = line(row);
        const text = [...kinds][0] === "" ? s.before ?? "0" : [...kinds][0];
        if (Number(s.quantity) === 0) {
          if (Number(text) !== 0) Object.assign(inner(row), { old: null, new: row.part, quantity: text }); // 消した箇所を戻す
        } else inner(row).quantity = text;
        count += 1;
      }
    }
    for (const d of ready) {
      const balloon = d.balloon || null;
      if (d.level === 1) for (const [product, quantity] of d.amounts) body.add.push({ product, balloon, part: d.part, quantity });
      else body.inside.push({ path: d.path, tops: d.tops, balloon, old: null, new: d.part, quantity: [...d.amounts.values()][0] });
      const key = keyOf({ ...d, balloon });
      place.set(key, place.has(d.afterKey) ? place.get(d.afterKey) + 1e-7 : Math.max(0, ...place.values()) + 1);
      count += 1;
    }
    // 組立品ごと替わる行が混ざると、中の行の上の品番が変わってしまうので、数量・直下・中の順に送る
    body.inside.push(...[...deep.values()].filter((e) => e.new !== undefined || e.quantity !== undefined));
    try {
      if (Object.values(body).some((list) => list.length)) await send("/api/v2/structure", body, `${count} 箇所を変えました`);
      for (const [code, to] of renames) await send("/api/v2/temp-rename", { code, new: to }, `${code} を ${to} にしました`);
      drafts = drafts.filter((d) => !ready.includes(d));
    } catch (e) {
      fail(e);
    }
    items.clear(); // 仮品番の品番・名称が変わる
    await me.load();
    await lookup(drafts.map((d) => d.part));
    draw();
  };
  const selected = () => rows[grid.sel.r];
  const addRow = () => {
    const near = selected();
    const d = newDraft(near || null);
    drafts.push(d);
    draw();
    grid.select(rows.indexOf(d), 2);
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
    amounts.set(row, all);
    apply().catch(fail);
  };
  const button = (text, run) => el("button", { class: "btn", disabled: ro || !S.number, onclick: () => {
    if (Grid.active) Grid.active.commit();
    run();
  } }, text);
  // 足しかけの行がある間は、ほかの窓の保存で描き直さない（打ちかけが消えるため）
  cur.saver = { flush: async () => {}, get pending() { return drafts.some((d) => d.part || d.balloon || d.amounts.size); } };
  main.append(card("構成変更", [button("行を追加", addRow), button("行を削除", dropRow), button("風船番号で並べる", () => {
    restart();
    draw();
  })], [
    el("div", { class: "row" }, me.picks, me.filters, el("div", { class: "chips" }, me.onlyButton), me.note),
    body,
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
// 部品から（同じ変更は1行・製品の数を押すと製品ごとの箇所）か、製品から（製品×変更ごとに1行）で見る。
// 金額の変更（⑩ 製品の費目の書き換え）は下に分けて出す
async function changesScreen(main) {
  const plan = PLAN;
  let product = "";
  let view = "part"; // part＝部品から・product＝製品から
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
      el("span", { class: "mini" }, "組立品の中は、部品変更で替えます"),
      ro ? null : el("button", { class: "btn", onclick: dropDeep }, "2階層目の変更を消す")) : null);
    const owns = data.rows.filter((r) => r.source === "own");
    await lookup([...data.by_product.map((r) => r.product), ...owns.map((r) => r.product)]);
    fill(ownBox, owns.length ? [el("h3", {}, "製品の費目の書き換え"), el("table", { class: "list" },
      el("tr", {}, ["製品", "名称", "費目", "現行", "変更後"].map((t) => el("th", {}, t))),
      owns.map((r) => el("tr", {}, el("td", {}, r.product), el("td", {}, nameCell(r.product).text), el("td", {}, r.part),
        el("td", { class: "r" }, r.before === null ? "—" : fmt(r.before)), el("td", { class: "r" }, fmt(r.after)))))] : []);
    arrange();
  };
  const detailDialog = (row) => {
    const box = dialog([row.now && row.now.code, row.after && row.after.code].filter(Boolean).join(" → "),
      el("table", { class: "list" }, el("tr", {}, ["製品", "名称", "親", "風船", "現行", "変更後"].map((t) => el("th", {}, t))),
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
    ...(view === "product" ? [[{ text: "風船", cls: "c" }, (x) => ({ text: x.balloon ?? "—", cls: "c" })]] : []),
    [{ ...sort.head("品番", code("now"), false, "now"), cls: "split" }, (x) => ({ text: code("now")(x), cls: "split" })],
    ["名称", (x) => named(x.now)],
    [{ text: "数量", cls: "r" }, (x) => ({ text: count(x.now), cls: "r" })],
    [{ ...sort.head("品番", code("after"), false, "after"), cls: "after split" }, (x) => ({ text: code("after")(x), cls: "split" + (code("after")(x) && code("after")(x) !== code("now")(x) ? " own" : "") })],
    [{ text: "名称", cls: "after" }, (x) => named(x.after)],
    [{ text: "数量", cls: "after r" }, (x) => ({ text: count(x.after), cls: "r" + (x.after && x.now && !same(x.after.quantity, x.now.quantity) ? " own" : "") })],
    ...(view === "part" ? [[{ ...sort.head("製品", (x) => String(x.products.length), true), cls: "r split" }, (x) => (x.level > 1 ? { text: "", cls: "split" } : { text: String(x.products.length), cls: "r link split", title: "押すと製品ごとの箇所" })]] : []),
    ["要確認", (x) => ({ text: x.problems.join("・"), cls: x.problems.length ? "bad" : "" })],
  ];
  const before = () => (view === "part" ? 1 : 4); // 現行より左の列の数（行番号を除く）
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
  const drawViews = () => fill(views, [["part", "部品から"], ["product", "製品から"]].map(([k, text]) =>
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

// 上に「変更ごとの影響」（製造原価への効き方）、下に製品ごとの費目全部（現行・変更後・差）
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
  const sort = sortable(() => {
    rows = sort.apply(rows);
    if (!ro) saver.schedule(); // 並びも一時保存する
  });
  const body = el("div");
  const model = {
    head: () => [
      { text: rows.length && rows.every((r) => r.mark) ? "☑" : "☐", cls: "c", title: "全部の行に印", action: ro ? null : markAll },
      "", sort.head("品番", (r) => r.code), sort.head("名称", (r) => r.name), sort.head("流用元", (r) => r.base), { ...sort.head("材料費", (r) => (r.material !== "" ? r.material : r.derived), true), cls: "r" }, { text: "流用元から計算", cls: "r" }, "中", "",
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
