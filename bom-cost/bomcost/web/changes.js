"use strict";
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
