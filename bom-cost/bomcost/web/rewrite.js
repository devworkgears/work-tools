"use strict";
// ⑧ 構成変更（⑤⑧ の共通の部品は structure.js）

// ⑧ 品番と数量を、その欄で打ち替える。品番は行に出ている親の全部に、数量は親ごとに効く。空にすると現行に戻る。
// 親が組立品なら、中を変えた組立品に新しい品番が付き（サーバが決める）、開いた時に出していた製品の範囲に効く
// key 11 は⑪バリエーション: 同じ表で、元の製品を除いた足し引きの行を打つ（元の製品の部品を引く行はマイナスの数）
async function rewriteScreen(main, cur, key = 8) {
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
  const me = await structureView(key, () => {
    if (drawing !== me.view.drawing) {
      drafts = [];
      restart();
    }
    draw();
  });
  restart();
  const minus = key === 11 && !me.child; // バリエーションの直下は、元の製品の部品をマイナスの数で引く
  const parents = () => me.columns();
  // 列: 番号・風船・サブ番号・品番・名称・仕様・状態・子・一律、その右に親ごとの数量
  const [BALLOON, BRANCH, PART, NAME, SPEC, STATE, LOWER, UNIFORM] = [1, 2, 3, 4, 5, 6, 7, 8]; // 一律は足しかけの行だけ打てる。打つと出ている親の全部に同じ数量が入る
  const left = 9;
  // head・branch は足しかけの行に打った風船番号とサブ番号（balloon はその2つを合わせた箇所の印）。
  // uniform は一律に打った数量。spread は一律で入れたか（もう同じ物がある親には足さない）
  const newDraft = (near) => ({ draft: true, balloon: "", head: "", branch: "", part: "", now: null, cells: new Map(), amounts: new Map(), uniform: "", spread: false, after: near ? (near.draft ? near.after : keyOf(near)) : null });
  // 打ち替えは溜めて、changed() で1回に送る
  let parts = new Map(); // 行 → 品番（空は現行に戻す）
  let amounts = new Map(); // 行 → 親 → 数量（空は現行に戻す）
  const model = {
    head: () => [{ text: "", cls: "slim" }, { text: "風船", cls: "c slim" }, { text: "サブ番号", cls: "c" }, "品番", "名称", "仕様", { text: "状態", cls: "slim" }, { text: "子", cls: "slim" },
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
        title: s.short ? "元の製品の変更で、この部品の数がマイナスになる（ここで直す）" : s.problem ? "要確認（変更一覧）" : changed && s.before !== null ? `現行 ${fmt(s.before)}` : null };
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
      if ((c >= left || c === UNIFORM) && text !== "" && !(isNumber(text) && (minus || Number(text) >= 0))) {
        return alert(`数量は${minus ? "" : "0以上の"}数で入力してください（${row.part || "品番なし"}「${text}」）`);
      }
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
    let deep = null; // 全階層（範囲で選んだ時に読む）
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
          // 置き換える組立品と、その中の部品が両方当たっていると、先に組立品が替わって中が見つからなくなるので、何も入れずに断る
          const swapped = new Set(picked.map((g) => g.part));
          const clash = inner.find((x) => x.path.some((c) => swapped.has(c)));
          if (clash) return alert(`組立品 ${clash.path.find((c) => swapped.has(c))} と、その中の部品が両方当たっています。どちらかの印を外してください`);
          // 組立品の中を替えると上の組立品に新しい品番が要るので、何も入れる前に聞く（やめたら何も入れない）。
          // 中の分が断られないことを書かずに確かめてから、直下を先、中を後に入れる（直下だけが残らないように）
          const fresh = inner.length ? (await api("POST", "/api/v2/structure/needs", { number: S.number, plan: PLAN }, { rows: inner })).needs.filter((n) => n.fresh) : [];
          const numbers = fresh.length ? await askNumbers(fresh) : {};
          if (numbers === null) return done("やめました");
          if (inner.length) await api("POST", "/api/v2/inside/replace", { number: S.number, plan: PLAN }, { rows: inner, numbers, check: true });
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
  main.append(card(me.title(key === 11 ? label("バリエーション") : "構成変更"), [button("行追加", addRow), button("行削除", dropRow),
    key === 11 ? null : button("一括置換", () => bulkDialog().catch(fail)), rowBack, allBack, button("風船順", () => {
      restart();
      draw();
    })], [
    el("div", { class: "row" }, me.picks, me.filters, showGroup(me.onlyToggle, zeroToggle), me.users, me.note),
    me.baseLine, body, nowLine,
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
