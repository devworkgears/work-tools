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
const NAMES = { 5: "グリッド画面", 6: "変更一覧", 7: "結果", 8: "書き換え", 9: "仮品番", 10: "製品の費目", 11: "置換" };
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
      applyStudy(await api("POST", "/api/v2/studies", {}, { ...header(), products: draft.targets.filter(Boolean) }));
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
      el("fieldset", { class: "grp" }, big(11, "置換"), big(5, "グリッド画面"), big(8, "書き換え"), big(9, "仮品番"), big(10, "製品の費目")),
      el("fieldset", { class: "grp" }, big(6, "変更一覧", S.problems ? [" ", el("span", { class: "pill warn" }, S.problems)] : null), big(7, "結果")),
      release),
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
  if (S.number) {
    const d = await api("GET", "/api/v2/targets", q());
    rows = d.targets.slice();
  }
  await lookup(rows);
  const body = el("div");
  const model = {
    head: () => ["", "品番", "名称", "代表", "規格", "組立図", { text: "年間台数", cls: "r" }, { text: "材料費", cls: "r" }, { text: "製造原価", cls: "r" }],
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

// ⑪ 置換 -----------------------------------------------------------------
// 対象品番の変えた後の構成にある部品を、品番か名称で絞って品番ごとに1行で出す。品番を打ち替えると、
// 対象品番の全製品のその箇所を置き換える（1回きりの操作で、決まりとしては残さない）。
// 戻す時は「置き換える前」を押す（打ち替えて戻すと、もともとその品番だった製品まで変わる）。
// 一部の製品・風船番号だけ替える時は ⑤ か ⑧ で直す

async function searchReplaceScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let parts = []; // { code, name, used, products, was: 置き換える前の品番 }
  let shown = [];
  if (S.number) parts = (await api("GET", "/api/v2/after-parts", q({ plan }))).parts;
  const codeBox = el("input", { class: "in" });
  const nameBox = el("input", { class: "in" });
  const note = el("span", { class: "mini" });
  const norm = (s) => clean(s).toLowerCase();
  // 置き換えた品番は検索に関係なく上に出す。その下に、検索に当たる部品
  const refilter = async () => {
    const c = norm(codeBox.value);
    const n = norm(nameBox.value);
    const replaced = parts.filter((p) => p.was.length);
    const hits = c || n ? parts.filter((p) => !p.was.length && (!c || norm(p.code).includes(c)) && (!n || norm(p.name || "").includes(n))) : [];
    shown = [...replaced, ...hits];
    note.textContent = c || n ? `置き換えた品番 ${replaced.length}・検索に当たる部品 ${hits.length}`
      : `置き換えた品番 ${replaced.length}（品番か名称で絞ると、対象品番の全製品の部品が出ます）`;
    await lookup(shown.flatMap((p) => [p.code, ...p.was]));
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
  const apply = async (p, code) => {
    try {
      const d = await api("POST", "/api/v2/replace/apply", { number: S.number, plan }, { old: p.code, new: code });
      parts = d.parts;
      tell("saved");
      done(`${p.code} を ${code} に置き換えました（${p.used} 製品）`);
    } catch (e) {
      fail(e);
    }
    await refilter();
  };
  const revert = async (p) => {
    if (!confirm(`${p.code} に置き換えた箇所を、今の部品表（${p.was.join("・")}）に戻します。よろしいですか？`)) return;
    try {
      parts = (await api("POST", "/api/v2/replace/revert", { number: S.number, plan }, { part: p.code })).parts;
      tell("saved");
      done(`${p.code} に置き換えた箇所を今の部品表に戻しました`);
    } catch (e) {
      fail(e);
    }
    await refilter();
  };
  const body = el("div");
  const model = {
    head: () => ["", "品番", "名称", { text: "使う製品", cls: "r" }, { text: "材料費", cls: "r" }, "置き換える前"],
    nRows: () => shown.length,
    nCols: () => 6,
    cell(r, c) {
      const p = shown[r];
      if (c === 0) return { text: String(r + 1), cls: "n" };
      if (c === 1) return { text: p.code, editable: !ro, title: ro ? null : "打ち替えると、使う製品の全部で置き換える" };
      if (c === 2) return nameCell(p.code);
      if (c === 3) return { text: String(p.used), cls: "r link", title: "押すと製品の一覧" };
      if (c === 4) {
        const info = items.get(p.code);
        return { text: info && info.material != null ? fmt(info.material) : "", cls: "r" };
      }
      return { text: p.was.join("・"), cls: !ro && p.was.length ? "auto link" : "auto", title: !ro && p.was.length ? "押すと今の部品表に戻す" : null };
    },
    set(r, c, value) {
      const p = shown[r];
      if (c !== 1 || !p || !value || value === p.code) return;
      apply(p, value);
    },
    action(r, c) {
      if (c === 3 && shown[r]) usersDialog(shown[r]);
      if (c === 5 && !ro && shown[r] && shown[r].was.length) revert(shown[r]);
      return false;
    },
  };
  const grid = new Grid(body, model);
  codeBox.addEventListener("input", () => refilter().catch(fail));
  nameBox.addEventListener("input", () => refilter().catch(fail));
  main.append(card("置換", [], [
    el("div", { class: "row" }, el("label", { class: "f" }, "品番", codeBox), el("label", { class: "f" }, "名称", nameBox), note),
    body,
  ]));
  await refilter();
  grid.focus();
}

// ⑤ グリッド画面 ---------------------------------------------------------------

async function gridScreen(main, cur) {
  const ro = !canWrite();
  const plan = PLAN;
  let cols = []; // 列の製品（左で印を付けた対象品番＋使用先で足した製品）
  let rows = []; // { part, problems, locked, balloons: 製品 → 風船番号 }
  let mainProduct = null; // 並びの基準（主対象の風船番号の順。主対象に無い部品は後ろ）
  // どれも「親品番\t品番」 → 数量。saved＝保存済みの変えた後・base＝今の部品表・edits＝打ってまだ保存していない数量（空は今に戻す）
  const saved = new Map();
  const base = new Map();
  const edits = new Map();
  const key = (product, part) => `${product}\t${part}`;
  const load = async () => {
    const d = S.number ? await api("GET", "/api/v2/grid", q({ plan })) : { columns: [], rows: [] };
    cols = d.columns.slice();
    rows = d.rows.map((r) => ({ part: r.part, problems: r.problems || {}, locked: r.locked || {}, balloons: r.balloons || {} }));
    edits.clear();
    take(d.rows);
    mainProduct = d.main_product ?? null;
    sortRows();
    await lookup([...cols, ...rows.map((r) => r.part)]);
  };
  function take(got) {
    saved.clear();
    base.clear();
    for (const r of got) {
      for (const [c, v] of Object.entries(r.cells)) saved.set(key(c, r.part), v);
      for (const [c, v] of Object.entries(r.now)) base.set(key(c, r.part), v);
    }
  }
  const valueOf = (product, part) => {
    const k = key(product, part);
    return edits.has(k) ? edits.get(k) : saved.get(k) ?? "";
  };
  // 主対象の風船番号の順に並べる（数として比べる）。主対象に無い・主対象が無い部品は、今の順のまま後ろ
  function sortRows() {
    if (!mainProduct) return;
    const at = (row) => row.balloons && row.balloons[mainProduct];
    const placed = rows.filter((row) => row.part && at(row));
    const rest = rows.filter((row) => !(row.part && at(row)));
    placed.sort((a, b) => at(a).localeCompare(at(b), "ja", { numeric: true }));
    rows = [...placed, ...rest];
  }
  await load();
  const body = el("div");
  // 表の行 → 部品の行。部品1つにつき、上に風船の行・下に数量の行。最後の1行は品番を打つ空の行
  const place = (r) => {
    const d = r - 1;
    const i = Math.floor(d / 2);
    if (i >= rows.length) return { i: rows.length, kind: "new" };
    return { i, kind: d % 2 === 0 ? "balloon" : "qty" };
  };
  const P = 4; // 製品の列が始まる位置
  const model = {
    // 見出しは1段（表の1行目）。親品番の列は品番と名称を縦書きにして、列の幅を数量の桁だけにする
    nRows: () => 1 + 2 * rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push({ part: "", problems: {}, locked: {}, balloons: {} }); return true; },
    nCols: () => P + cols.length,
    cell(r, c) {
      if (r === 0) {
        if (c === 0) return { text: "", cls: "n" };
        if (c === 1) return { text: "品番 ↓　親品番 →", th: true, cls: "foot" };
        if (c === 2) return { text: "名称", th: true, cls: "foot" };
        if (c === 3) return { text: "", th: true };
        const code = cols[c - P] || "";
        const outside = code && !S.targets.includes(code);
        const info = code ? items.get(code) : null;
        const name = info ? (info.registered ? info.name : "未登録") : "";
        const star = code && code === mainProduct ? "★ " : "";
        return { text: code ? `${star}${code}\n${name}` : "", th: true, cls: "vh" + (outside || (info && !info.registered) ? " bad" : ""),
          title: outside ? "要確認（対象品番に無いので計算に入らない）" : code ? `${star}${code} ${name}` : null };
      }
      const { i, kind } = place(r);
      const row = rows[i];
      if (kind === "new") {
        if (c === 0) return { text: String(i + 1), cls: "n" };
        if (c === 1) return { text: "", editable: !ro };
        return { text: "", cls: c >= P ? "q" : "" };
      }
      if (kind === "balloon") {
        if (c === 0) return { text: String(i + 1), cls: "n" };
        if (c === 1) return { text: row.part, editable: !ro };
        if (c === 2) return nameCell(row.part);
        if (c === 3) return { text: "風船", cls: "auto" };
        const code = cols[c - P];
        return { text: (code && row.balloons[code]) || "", cls: "auto c q" };
      }
      if (c === 0) return { text: "", cls: "n" };
      if (c === 3) return { text: "数量", cls: "foot" };
      if (c < P) return { text: "" };
      const code = cols[c - P];
      const value = code && row.part ? valueOf(code, row.part) : "";
      const locked = !!(code && row.locked[code]);
      const bad = locked || (code && (row.problems[code] || (value !== "" && !isNumber(value))));
      return {
        text: value, editable: !ro && !!row.part && !!code && !locked, cls: "r q" + (bad ? " bad" : ""),
        title: locked ? "要確認（複数の箇所にある・書き換えで直す）" : null,
      };
    },
    set(r, c, text) {
      if (r < 1) return;
      const { i, kind } = place(r);
      if (c === 1 && kind !== "qty") {
        if (kind === "new") {
          if (!text) return;
          rows.push({ part: text, problems: {}, locked: {}, balloons: {} });
        } else {
          Object.assign(rows[i], { part: text, problems: {}, locked: {}, balloons: {} });
        }
      } else if (kind === "qty" && c >= P) {
        const row = rows[i];
        if (!cols[c - P]) return;
        // 数でない数量はその場で弾く（入れずに元の値のまま）
        if (text !== "" && !isNumber(text)) return alert(`数量は数で入力してください（${row.part} × ${cols[c - P]}「${text}」）`);
        edits.set(key(cols[c - P], row.part), text);
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
    for (const row of rows) {
      row.locked = d.locked[row.part] || {};
      row.balloons = d.balloons[row.part] || {};
    }
    grid.render();
  };
  const grid = new Grid(body, model);

  // 打ったセルだけを送る。送っている間に打ち直したセルは、次の保存で送る
  const saver = autosaver(cur, async () => {
    const sent = [...edits].filter(([k, v]) => !same(v, saved.get(k)));
    const cells = sent.map(([k, v]) => {
      const [product, part] = k.split("\t");
      return { product, part, value: v === "" ? null : v, row: Math.max(0, rows.findIndex((r) => r.part === part)) };
    });
    const before = new Map(edits);
    const d = await api("PUT", "/api/v2/grid", { number: S.number, plan },
      { columns: cols.filter(Boolean), rows: rows.map((r) => r.part).filter(Boolean), cells });
    for (const [k, v] of before) if (edits.get(k) === v) edits.delete(k);
    take(d.rows);
    const byPart = new Map(d.rows.map((r) => [r.part, r]));
    for (const row of rows) {
      const got = byPart.get(row.part);
      row.problems = got ? got.problems || {} : {};
      row.locked = got ? got.locked || {} : {};
      row.balloons = got ? got.balloons || {} : {};
    }
    grid.render();
  }, async () => {
    await load();
    grid.render();
  });
  const whereUsed = () => {
    const row = grid.sel.r >= 1 ? rows[place(grid.sel.r).i] : null;
    if (!row || !row.part) return alert("品番の行を選んでください");
    whereUsedDialog(row.part, ro ? null : addColumns);
  };
  // 使用先で選んだ製品を、まだ無ければ列の右端に足す。足した数を返す
  const addColumns = (codes) => {
    const fresh = codes.filter((c) => !cols.includes(c));
    if (!fresh.length) return 0;
    // 見出しを消した右端の空き列は詰めてから足す
    while (cols.length && !cols[cols.length - 1]) cols.pop();
    cols.push(...fresh);
    saver.schedule();
    model.changed();
    grid.render();
    return fresh.length;
  };
  // 列は左で印を付けた対象品番の順。使用先で足した対象品番の外の製品は右に残す。
  // 今の部品表から変えた数量のある列の印を外す時は、今の部品表に戻すかを聞く（隠したまま計算に入るのを防ぐ）
  const setColumns = (products) => {
    const extra = cols.filter((c) => c && !S.targets.includes(c));
    const next = [...products, ...extra];
    const dropped = cols.filter((c) => c && !next.includes(c));
    const moved = rows.filter((r) => r.part).flatMap((r) => dropped.filter((code) => !same(valueOf(code, r.part), base.get(key(code, r.part))))
      .map((code) => ({ code, row: r })));
    const stuck = moved.filter((m) => m.row.locked[m.code]);
    if (stuck.length) {
      alert(`${[...new Set(stuck.map((m) => m.code))].join("・")} に、複数の箇所の変更があります。先に書き換えで直してください`);
      return false;
    }
    const names = [...new Set(moved.map((m) => m.code))].join("・");
    if (moved.length && !confirm(`${names} の列に、変えた数量があります。列を外すと、その数量を今の部品表に戻します。よろしいですか？`)) return false;
    for (const m of moved) edits.set(key(m.code, m.row.part), "");
    cols = next;
    lockAsked = ""; // 列が変われば風船番号も取り直す
    saver.schedule();
    model.changed();
    grid.render();
    return true;
  };
  const first = cols.some((c) => S.targets.includes(c)) ? cols : S.targets;
  // 部品の行はグリッドに直に打つか貼る（部品を一覧から選んで入れるのは ④ の役目）
  const pick = chooser({ chosen: first, ro, onProducts: ro ? () => false : setColumns });
  await pick.ready;
  if (!ro && !cols.length && S.targets.length) setColumns(pick.ordered());
  // 主対象は対象品番の中から1つ。替えると並びと★だけが変わり、打った数量は変わらない
  const mainPick = el("select", { class: "in", disabled: ro || !S.number, onchange: async () => {
    try {
      const d = await api("PUT", "/api/v2/main-product", { number: S.number }, { main_product: mainPick.value || null });
      mainProduct = d.main_product;
      sortRows();
      grid.render();
    } catch (e) {
      fail(e);
    }
  } }, el("option", { value: "" }, "（なし）"), S.targets.map((code) => el("option", { value: code, selected: code === mainProduct }, code)));
  main.append(card("グリッド画面", [
    el("label", { class: "inline" }, "主対象 ", mainPick),
    el("button", { class: "btn", onclick: whereUsed, disabled: !S.number }, "使用先"),
    el("button", { class: "btn", onclick: conditionsDialog, disabled: !S.number }, "計算条件"),
  ], el("div", { class: "chooser two" }, pick.left, el("div", { class: "work" }, body))));
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
  const ownBox = el("div");
  const load = async () => {
    const d = await api("GET", "/api/v2/changes", q({ plan, product }));
    rows = d.summary;
    byProduct = d.by_product;
    owns = d.rows.filter((r) => r.source === "own");
    await lookup([...byProduct.map((r) => r.product), ...owns.map((r) => r.product)]);
    fill(ownBox, owns.length ? [el("h3", {}, "製品の費目の書き換え"), el("table", { class: "list" },
      el("tr", {}, ["製品", "名称", "費目", "台帳の値", "書き換えた値"].map((t) => el("th", {}, t))),
      owns.map((r) => el("tr", {}, el("td", {}, r.product), el("td", {}, nameCell(r.product).text), el("td", {}, r.part),
        el("td", { class: "r" }, r.before === null ? "—" : fmt(r.before)), el("td", { class: "r" }, fmt(r.after)))))] : []);
  };
  await load();
  const detailDialog = (row) => {
    const box = dialog(`${row.kind} ${[row.old, row.new].filter(Boolean).join(" → ")}`,
      el("table", { class: "list" }, el("tr", {}, ["製品", "名称", "親", "風船", row.new && row.old ? `元 ${row.old}` : "変更前", row.new && row.old ? `新 ${row.new}` : "変更後"].map((t) => el("th", {}, t))),
        row.details.map((d) => el("tr", {}, el("td", {}, d.product), el("td", {}, nameCell(d.product).text), el("td", {}, d.parent),
          el("td", {}, d.balloon ?? "—"), el("td", { class: "r" }, d.before === null ? "—" : fmt(d.before)), el("td", { class: "r" }, fmt(d.after))))),
      [el("span", { class: "sp" }), el("button", { class: "btn", onclick: () => box.close() }, "閉じる")]);
  };
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
        case 4: return { text: row.new ?? "" };
        case 5: return named(row.new, row.new_name);
        case 6: return { text: row.quantity, cls: "r" };
        case 7: return { text: String(row.products.length), cls: "r link", title: "押すと製品ごとの箇所" };
        default: return problemCell(row);
      }
    },
    set() {},
    action(r, c) {
      if (c === 7 && rows[r]) detailDialog(rows[r]);
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
        case 6: return { text: row.new ?? "" };
        case 7: return named(row.new, row.new_name);
        case 8: return { text: row.quantity, cls: "r" };
        case 9: return { text: row.parent ?? "" };
        case 10: return { text: row.kind === "変更なし" ? "" : row.balloon ?? "—", cls: "c" };
        default: return problemCell(row);
      }
    },
    set() {},
  };
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
  ], [el("div", { class: "row" }, views), body, ownBox]));
  grid.focus();
}

// 検討1つを1つの Excel に出す（⑥ と ⑦ で同じファイル）
function exportStudy() {
  return download("/api/v2/export/study", q({ plan: PLAN }), `検討_${S.number}.xlsx`);
}

// ⑦ 結果 -----------------------------------------------------------------

// 費目は式の並び（サーバの HEADS と同じ名前）。最初は全部を並べる
const RESULT_HEADS = [["all", "全部"], ["material", "材料費小計"], ["processing", "社内加工費"], ["amortization", "償却費"],
  ["adjustment", "調整費"], ["material_total", "材料費合計"], ["indirect", "間接費"], ["direct", "直接費"], ["total", "製造原価"]];

function signed(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return (n < 0 ? "−" : n > 0 ? "+" : "") + fmt(String(Math.abs(n)));
}

function tone(v) {
  const n = Number(v);
  return v === null || v === undefined ? "" : n < 0 ? " minus" : n > 0 ? " plus" : "";
}

async function resultScreen(main) {
  let head = "all";
  let data = null;
  const plan = PLAN;
  const stamp = el("span", { class: "mini", style: "font-weight:400" });
  const table = el("div", { style: "overflow:auto" });
  const chips = el("div", { class: "chips" });
  const drawChips = () => fill(chips, RESULT_HEADS.map(([k, label]) =>
    el("button", { class: "chip" + (head === k ? " on" : ""), onclick: () => { head = k; drawChips(); if (data) compute(); } }, label)));
  // 1つの費目: 行＝対象品番、列＝今と案
  const drawOne = () => {
    const plans = data.plans;
    return el("table", { class: "res" },
      el("tr", {}, el("th", {}, "対象品番"), el("th", { class: "r" }, "年間台数"), el("th", { class: "r" }, "今"),
        plans.map((p) => el("th", { class: "r" }, `案${p}`))),
      data.rows.map((r) => el("tr", {},
        el("td", {}, r.product),
        el("td", { class: "r" }, fmt(r.annual)),
        el("td", { class: "r" + (r.now_incomplete ? " inc" : ""), title: r.now_incomplete ? "不完全" : null }, fmt(r.now)),
        plans.map((p) => el("td", { class: "r" + (r.incomplete[p] ? " inc" : ""), title: r.incomplete[p] ? "不完全" : null }, fmt(r.plans[p]))))),
      el("tr", {}, el("th", {}, "年間の影響"), el("th", {}), el("th", {}),
        plans.map((p) => el("th", { class: "r" + tone(data.impact[p]) + (data.impact_incomplete[p] ? " inc" : ""),
          title: data.impact_incomplete[p] ? "不完全" : null }, man(data.impact[p])))),
      // 年間の影響 ÷ 年間台数の合計（1台あたりの増減）
      el("tr", {}, el("th", {}, "台数の加重平均"), el("th", {}), el("th", { class: "mini" }, "1台あたり"),
        plans.map((p) => el("th", { class: "r" + tone(data.weighted[p]) + (data.impact_incomplete[p] ? " inc" : ""),
          title: data.impact_incomplete[p] ? "不完全" : null }, signed(data.weighted[p])))));
  };
  // 費目全部: 行＝対象品番、列＝費目。セルは案の値と、その下に今との差（今の値は押さえると出る）
  const drawAll = () => {
    const heads = data.heads;
    const rows = heads.length ? heads[0].rows : [];
    return el("table", { class: "res" },
      el("tr", {}, el("th", {}, "対象品番"), el("th", { class: "r" }, "年間台数"), heads.map((h) => el("th", { class: "r" }, h.label))),
      rows.map((r0, i) => el("tr", {},
        el("td", {}, r0.product),
        el("td", { class: "r" }, fmt(r0.annual)),
        heads.map((h) => {
          const r = h.rows[i];
          const after = r.plans[plan];
          const diff = after === null || r.now === null ? null : Number(after) - Number(r.now);
          const inc = r.incomplete[plan] || r.now_incomplete;
          return el("td", { class: "r" + (inc ? " inc" : ""), title: `今 ${fmt(r.now) || "—"}${inc ? "・不完全" : ""}` },
            fmt(after), el("div", { class: "d" + tone(diff) }, diff === null ? "" : signed(String(diff))));
        }))),
      el("tr", {}, el("th", {}, "年間の影響"), el("th", {}),
        heads.map((h) => el("th", { class: "r" + tone(h.impact[plan]) + (h.impact_incomplete[plan] ? " inc" : ""),
          title: h.impact_incomplete[plan] ? "不完全" : null }, man(h.impact[plan])))),
      el("tr", {}, el("th", {}, "台数の加重平均"), el("th", { class: "mini" }, "1台あたり"),
        heads.map((h) => el("th", { class: "r" + tone(h.weighted[plan]) + (h.impact_incomplete[plan] ? " inc" : ""),
          title: h.impact_incomplete[plan] ? "不完全" : null }, signed(h.weighted[plan])))));
  };
  const draw = () => fill(table, data ? (data.head === "all" ? drawAll() : drawOne()) : null);
  const compute = async () => {
    try {
      data = await api("GET", "/api/v2/calc", q({ head }));
      stamp.textContent = `計算日時 ${data.computed_at.replace("T", " ").slice(0, 16).replaceAll("-", "/")}　調整費 ${data.rates.adjustment}%・間接費 ${data.rates.indirect}%`;
      draw();
      done("計算しました");
    } catch (e) {
      fail(e);
    }
  };
  drawChips();
  main.append(card(["結果", stamp], [
    el("button", { class: "btn", onclick: conditionsDialog }, "計算条件"),
    el("button", { class: "btn", onclick: () => exportStudy().catch(fail) }, "Excel"),
    el("button", { class: "btn pri", onclick: compute }, "実行"),
  ], [el("div", { class: "row" }, el("span", { class: "mini" }, "費目"), chips), table]));
}

// ⑩ 製品の費目 -----------------------------------------------------------
// 対象品番ごとに、製品自身の費目（社内加工費・償却費・直接費）を書き換える。空は台帳の値のまま

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
  main.append(card("製品の費目", [], [
    el("div", { class: "row" }, el("span", { class: "mini" }, "空の欄は台帳の値で計算します")),
    body,
  ]));
  grid.render();
  grid.focus();
}

// ⑨ 仮品番 -------------------------------------------------------------
// 品目の台帳に無い品番の名称と材料費（1個あたり）。この検討の計算にだけ使う（部品の原価は製品の材料費にだけ入るので材料費だけ）

async function tempScreen(main, cur) {
  const ro = !canWrite();
  const blank = () => ({ code: "", name: "", material: "", master: false, mark: false });
  let rows = [];
  let unregistered = [];
  const load = async () => {
    const d = await api("GET", "/api/v2/temp-items", q());
    rows = d.rows.map((r) => ({ ...blank(), code: r.code, name: r.name, material: r.material ?? "", master: r.master }));
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
      "", "品番", "名称", { text: "材料費", cls: "r" }, "",
    ],
    nRows: () => rows.length + (ro ? 0 : 1),
    grow: ro ? undefined : () => { rows.push(blank()); return true; },
    nCols: () => 6,
    cell(r, c) {
      const row = rows[r];
      if (c === 0) return { text: row ? (row.mark ? "☑" : "☐") : "", cls: "act" };
      if (c === 1) return { text: String(r + 1), cls: "n" };
      if (c === 2) return { text: row ? row.code : "", editable: !ro };
      if (c === 3) return { text: row ? row.name : "", editable: !ro };
      if (c === 4) return { text: row ? row.material : "", editable: !ro, cls: "r" };
      return row && row.master ? { text: "台帳にあり（台帳の値を使う）", cls: "bad" } : { text: "" };
    },
    set(r, c, text) {
      if (c < 2 || c > 4) return;
      // 数でない材料費はその場で弾く（入れずに元の値のまま）
      if (c === 4 && text !== "" && !isNumber(text)) return alert(`材料費は数で入力してください（「${text}」）`);
      if (r >= rows.length) {
        if (!text) return;
        rows.push(blank());
      }
      rows[r][["code", "name", "material"][c - 2]] = text;
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
  // 品番を打った行だけを送る（名称だけ打った行は画面に残す）。台帳にあるかの印と未登録の一覧は返ってきた物で直す
  const saver = autosaver(cur, async () => {
    const d = await api("PUT", "/api/v2/temp-items", { number: S.number },
      { rows: rows.filter((r) => r.code).map((r) => ({ code: r.code, name: r.name, material: r.material })) });
    const master = new Map(d.rows.map((x) => [x.code, x.master]));
    for (const r of rows) r.master = master.get(r.code) ?? false;
    unregistered = d.unregistered;
    items.clear();
    drawNote();
    grid.render();
  }, async () => {
    await load();
    drawNote();
    grid.render();
  });
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
  5: gridScreen,
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
