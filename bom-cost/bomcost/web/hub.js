"use strict";
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
  // バリエーションは、元の製品を直した後にたまに使うので、費目変更の後ろに小さく置く。対象品番に無ければ押せない
  const variationButton = big(11, "バリエーション");
  variationButton.classList.add("minor");
  if (!Object.keys(S.variations || {}).length) {
    variationButton.disabled = true;
    variationButton.title = `対象品番に${label("バリエーション")}がありません`;
  }
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
        el("label", { class: "f" }, "最終更新日", el("input", { class: "in ro", value: updated, readonly: true, style: "min-width:100px;width:110px" })),
        run))),
    el("div", { class: "groups" },
      // 左が準備、まん中（青）が打つ画面、右が見る画面。行き来して使うので、順の矢印は置かない
      el("fieldset", { class: "grp" }, big(3, "対象品番"), big(9, "仮品番")),
      el("fieldset", { class: "grp edit" }, big(8, "構成変更"), big(10, "費目変更"), variationButton),
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
