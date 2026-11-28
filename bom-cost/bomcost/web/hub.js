"use strict";
// ② 設計検討 -------------------------------------------------------------

// 版（作業中の版の番号・［版確定］・［過去の版］）を画面に出すか。今は隠す（仕組みは残す）
const SHOW_VERSIONS = false;
// ⑤構成一覧・⑥変更一覧のボタンを出すか。今は隠す（画面は残す）
const SHOW_LISTS = false;

async function hubScreen(main, cur) {
  if (S.version !== null) return pastHub(main);
  const ro = S.mode === "view";
  const draft = S.mode === "new" ? S.draft : null;
  const modes = [["new", "新規作成"], ["edit", "修正"], ["view", "照会"]];
  const numberInput = el("input", { class: "in", style: "min-width:110px", value: S.number, disabled: S.mode === "new" });
  // ② はいつも作業中の版を開く。確定した版は［過去の版］から別の窓で見る
  const read = async () => {
    const number = numberInput.value.trim();
    if (!number) return;
    await leave(cur);
    work("読み込んでいます", () => loadStudy(number, null)).catch(fail);
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
  const working = S.number ? el("span", { class: "ver" }, `版${S.versions.length ? Math.max(...S.versions) + 1 : 1} 作業中`) : null;
  const past = el("select", { class: "in", disabled: !!draft || !S.number || !S.releases.length },
    el("option", { value: "" }, "過去の版"), S.releases.slice().reverse().map((r) => el("option", { value: String(r.version) }, `版${r.version}　${day(r.taken_at)}`)));
  past.addEventListener("change", async () => {
    const version = past.value;
    past.value = "";
    if (!version) return;
    await leave(cur);
    if (!openWindow(2, { version, mode: "view" })) alert("窓を開けませんでした（ブラウザの設定で、別の窓を許してください）");
  });
  const updated = day(h.updated_at);
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

  // 部品表は全部の検討で共通なので、検討の操作と離して置く。件数は出さず、空の時だけ知らせる
  const bomNote = el("span", { class: "pill warn", style: "display:none" }, "部品表なし");
  const showBom = async () => {
    const { counts } = await api("GET", "/api/v2/bom");
    bomNote.style.display = counts.item || counts.bom_line ? "none" : "";
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
      el("button", { class: "btn", onclick: read, disabled: S.mode === "new" }, "読込"),
      el("button", { class: "btn", onclick: studyListDialog, disabled: S.mode === "new" }, "一覧検索"),
      el("span", { class: "sp" }), bomNote,
      el("button", { class: "btn quiet", onclick: () => importDialog(() => imported().catch(fail)) }, "⋯")),
    el("div", { class: "card" },
      el("div", { class: "h" }, el("span", { class: "no" }, S.number || "　"), SHOW_VERSIONS ? [working, release, past] : null),
      el("div", { class: "b" }, el("div", { class: "row", style: "margin:0" }, fields,
        el("label", { class: "f" }, "最終更新日", el("input", { class: "in ro", value: updated, readonly: true, style: "min-width:100px;width:110px" })),
        run))),
    el("div", { class: "groups" },
      // 左が準備、まん中（青）が打つ画面、右が見る画面。行き来して使うので、順の矢印は置かない
      el("fieldset", { class: "grp" }, big(3, "対象品番"), big(9, "仮品番")),
      el("fieldset", { class: "grp edit" }, big(8, "構成変更"), big(10, "費目変更"), variationButton),
      el("fieldset", { class: "grp" }, SHOW_LISTS ? [big(5, "構成一覧"), big(6, "変更一覧", S.problems ? [" ", el("span", { class: "pill warn" }, S.problems)] : null)] : null, big(7, "結果"))),
  );
}

function day(at) {
  return at ? at.slice(0, 10).replaceAll("-", "/") : "";
}

// 確定した版の ②（別の窓・色は灰色）。見る画面だけを置く
function pastHub(main) {
  const h = S.header || {};
  const at = (S.releases.find((r) => r.version === S.version) || {}).taken_at;
  const field = (text, key, width) => el("label", { class: "f" }, text,
    el("input", { class: "in ro", value: h[key] || "", readonly: true, tabindex: "-1", style: width ? `min-width:${width}px` : null }));
  const open = (key) => el("button", { class: "btn big", onclick: () => openWindow(key) || openScreen(key) }, NAMES[key]);
  main.append(
    el("div", { class: "card" },
      el("div", { class: "h" }, el("span", { class: "no" }, S.number), el("span", { class: "ver" }, `版${S.version}　${day(at)}`)),
      el("div", { class: "b" }, el("div", { class: "row", style: "margin:0" },
        field("題目", "title", 260), field("担当", "owner"), field("承認者", "approver"), field("案件番号", "dev_number")))),
    el("div", { class: "groups" }, el("fieldset", { class: "grp" }, [5, 6, 7].map(open)),
      el("fieldset", { class: "grp" }, el("button", { class: "btn", onclick: () => compareBom().catch(fail) }, "今の部品表と比べる"))),
  );
}

// 版を確定した時と今の部品表で、原価・構成・年間台数が違う所を Excel で出す
async function compareBom() {
  const got = await work("今の部品表と比べています", () =>
    download("/api/v2/export/release-drift", q(), `部品表の違い_${S.number}_版${S.version}.xlsx`));
  if (got && got.same) done(`版${S.version}の時から変わっていません`);
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
