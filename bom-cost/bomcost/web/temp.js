"use strict";
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
    // 断られた時（使っている仮品番を消した・品番が重なった など）は、保存してある中身に戻す
    await load();
    grid.render();
    done("保存できなかったので、元に戻しました");
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
