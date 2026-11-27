"use strict";
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
    // 対象品番の全部の構成（全階層）。量が多いので、⑦の検討の Excel とは別のファイル
    me.child ? null : el("button", { class: "btn", disabled: !S.number, onclick: () => work("Excel を作っています",
      () => download("/api/v2/export/structure", q({ plan: PLAN }), `構成_${S.number}.xlsx`)).catch(fail) }, "構成Excel"),
  ], [
    el("div", { class: "row" }, me.picks, el("label", { class: "f" }, "並び", pick([["balloon", "風船番号"], ["part", "品番"], ["name", "名称"]], (v) => { sort = v; })),
      me.filters, sides, showGroup(me.onlyToggle, balloonToggle), me.users, me.note),
    body,
  ]));
  draw();
  grid.focus();
}
