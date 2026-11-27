"use strict";
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
// 年間台数（計算条件）: 対象品番ごとの年間台数を、表で打つ・Excel から列で貼る。空は部品表の値で計算する
async function annualDialog() {
  try {
    const d = await api("GET", "/api/v2/conditions", q());
    const ro = !canWrite();
    const rows = d.rows.map((r) => ({ product: r.product, bom: r.bom_annual, annual: r.annual ?? "" }));
    await lookup(rows.map((r) => r.product));
    const saver = autosaver({}, async () => {
      await api("PUT", "/api/v2/conditions", { number: S.number }, { annual: Object.fromEntries(rows.map((r) => [r.product, r.annual.trim() || null])) });
    }, async () => {
      const again = await api("GET", "/api/v2/conditions", q());
      for (const x of again.rows) {
        const row = rows.find((r) => r.product === x.product);
        if (row) row.annual = x.annual ?? "";
      }
      grid.render();
    });
    const body = el("div");
    const model = {
      head: () => ["", "対象品番", "名称", { text: "部品表", cls: "r" }, { text: "年間台数", cls: "r" }],
      nRows: () => rows.length,
      nCols: () => 5,
      cell(r, c) {
        const row = rows[r];
        if (c === 0) return { text: String(r + 1), cls: "n" };
        if (c === 1) return { text: row.product };
        if (c === 2) return { text: nameCell(row.product).text, cls: "auto" };
        if (c === 3) return { text: fmt(row.bom), cls: "r auto" };
        return { text: row.annual, editable: !ro, cls: "r" + (row.annual !== "" && !same(row.annual, row.bom) ? " own" : "") };
      },
      set(r, c, text) {
        if (c !== 4) return;
        // 数でない台数はその場で弾く（入れずに元の値のまま）
        if (text !== "" && !isNumber(text)) return alert(`年間台数は数で入力してください（${rows[r].product}「${text}」）`);
        rows[r].annual = text;
        saver.schedule();
      },
    };
    const before = Grid.active;
    const grid = new Grid(body, model);
    const finish = async () => {
      grid.commit();
      Grid.active = before;
      await saver.flush();
    };
    const box = dialog("年間台数", body, [
      el("span", { class: "sp" }),
      el("button", { class: "btn", onclick: async () => { await finish(); box.close(); } }, "閉じる"),
    ]);
    box.overlay.querySelector(".close").addEventListener("click", () => finish().catch(fail));
    grid.render();
    grid.focus();
  } catch (e) {
    fail(e);
  }
}

// 検討1つを1つの Excel に出す
function exportStudy() {
  return work("Excel を作っています", () => download("/api/v2/export/study", q({ plan: PLAN }), `検討_${S.number}.xlsx`));
}

async function resultScreen(main) {
  let data = null;
  const plan = PLAN;
  const stamp = el("span", { class: "mini", style: "font-weight:400" });
  const body = el("div");
  const incomplete = (on) => (on ? { class: "inc", title: "不完全" } : {});
  const productsDialog = (e) => {
    const box = dialog(`${e.kind} ${[e.old, e.new, e.head && label(e.head)].filter(Boolean).join(" → ")}`,
      el("table", { class: "list" }, el("tr", {}, ["製品", "名称"].map((x) => el("th", {}, x)), ["1台あたりの増減", "年間台数", "年間の影響"].map((x) => el("th", { class: "r" }, x))),
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
        ["製品", "1台あたりの増減", "年間の影響", "現行比"].map((x) => el("th", { class: "r" }, x)), el("th", {}, "要確認")),
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
    const line = (title, cells) => [el("td", { class: "mini" + (title === "増減" ? "" : " low") }, title), cells];
    return el("table", { class: "res" },
      el("tr", {}, head("対象品番", (x) => x.r.product), head("名称", (x) => x.r.name), head("年間台数", (x) => x.r.annual, true, "r"), el("th", {}),
        heads.map((h) => head(h.label, (x) => diffOf(h.rows[x.i]), true, "r"))),
      rows.flatMap(({ r: r0, i }) => [
        el("tr", {}, el("td", { rowspan: "3" }, r0.product), el("td", { rowspan: "3" }, r0.name ?? "未登録"), el("td", { class: "r", rowspan: "3" }, fmt(r0.annual)),
          line("現行", heads.map((h) => el("td", { ...incomplete(h.rows[i].now_incomplete), class: "r low" + (h.rows[i].now_incomplete ? " inc" : "") }, fmt(h.rows[i].now))))),
        el("tr", {}, line("変更後", heads.map((h) => el("td", { ...incomplete(h.rows[i].incomplete[plan]), class: "r low" + (h.rows[i].incomplete[plan] ? " inc" : "") }, fmt(h.rows[i].plans[plan]))))),
        el("tr", {}, line("増減", heads.map((h) => {
          const diff = diffOf(h.rows[i]);
          const rate = diff === null || Number(diff) === 0 ? "" : percent(diff, h.rows[i].now);
          const inc = h.rows[i].incomplete[plan];
          return el("td", { class: "r" + tone(diff) + (inc ? " inc" : ""), title: inc ? "不完全" : null }, diff === null ? "—" : signed(diff), rate ? `（${rate}）` : "");
        }))),
      ]),
      el("tr", {}, el("th", { colspan: "3" }, "年間の影響"), el("th", {}),
        heads.map((h) => el("th", { class: "r" + tone(h.impact[plan]) + (h.impact_incomplete[plan] ? " inc" : ""), title: h.impact_incomplete[plan] ? "不完全" : null },
          man(h.impact[plan]), Number(h.impact[plan]) ? `（${percent(h.impact[plan], h.base[plan])}）` : ""))),
      el("tr", {}, el("th", { colspan: "3" }, "1台あたりの平均の増減"), el("th", { class: "mini" }, "年間台数で重み付け"),
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
    el("button", { class: "btn", onclick: annualDialog }, "年間台数"),
    el("button", { class: "btn", onclick: () => exportStudy().catch(fail) }, "Excel"),
    el("button", { class: "btn pri", onclick: compute }, "実行"),
  ], body));
}
