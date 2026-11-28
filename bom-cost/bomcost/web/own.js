"use strict";
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
    head: () => ["", "対象品番", "名称", ...heads.flatMap((h) => [{ text: `${label(h.label)} 現行`, cls: "r" }, { text: `${label(h.label)} 変更後`, cls: "r" }])],
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
      if (text !== "" && !isNumber(text)) return alert(`${label(h.label)}は数で入力してください（${rows[r].product}「${text}」）`);
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
