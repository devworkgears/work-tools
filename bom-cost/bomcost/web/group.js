// ①対象製品: 版の製品を絞って並べ、検討案の名前を付けて一覧の製品で試算を作る。直すのは②構成変更の画面（trial.js）。
// app.js・screens.js の関数を使う。
"use strict";

const TRIAL_HEAD = "MANUFACTURING"; // 検討案の原価は製造原価で出す（構成変更の画面も使う）

// 最後に並べた一覧の条件。作るボタンは入力欄の今の値でなく、表示中の一覧と同じ条件で作る
let groupShown = null; // { version, q, count }

async function enterGroup() {
  const version = document.getElementById("group-version");
  versionOptions(version, { value: version.value || state.summary?.scenario.version_id });
  if (version.value) await drawGroup();
}

function countText(n, shown, truncated) {
  return truncated ? `${shown} / ${n} 件（上限）` : `${n} 件`;
}

function setCreateLabel() {
  const button = document.getElementById("group-create");
  button.textContent = groupShown ? `表示中の ${groupShown.count} 製品で検討案を作る` : "検討案を作る";
  button.disabled = !groupShown || !groupShown.count;
}

async function drawGroup() {
  const version = document.getElementById("group-version").value;
  if (!version) throw new Error("版が空");
  const q = document.getElementById("group-q").value.trim();
  const [p, trials] = await Promise.all([
    api("GET", `/api/group/products?${qs({ version, q })}`),
    api("GET", `/api/group/trials?${qs({ version })}`),
  ]);
  groupShown = { version, q, count: p.products.length };
  setCreateLabel();
  document.getElementById("group-names").replaceChildren(...trials.map((t) => el("option", { value: t.name })));
  const productRows = p.products.map((x) => el("tr", {}, el("td", {}, x.code), el("td", {}, x.name)));
  const trialRows = trials.map((t) => el(
    "tr",
    {},
    el("td", {}, t.name),
    el("td", { class: "num" }, String(t.count)),
    el("td", {}, versionLabel(t.version_id)),
    el("td", {}, el("button", { type: "button", onclick: () => guarded(() => openTrial(t.version_id, t.name)) }, "構成変更で開く")),
  ));
  const none = (n) => [el("tr", {}, el("td", { colspan: n, class: "muted" }, "なし"))];
  document.getElementById("group-body").replaceChildren(
    el("h3", {}, `製品（${countText(p.count, p.products.length, p.truncated)}・区分 ${p.kind ?? "—"}）`),
    table(["品番", "名称"], productRows.length ? productRows : none(2)),
  );
  document.getElementById("group-trials").replaceChildren(
    el("h3", {}, `作った検討案（${trials.length} 件）`),
    table(["検討案", numTh("製品"), "版", ""], trialRows.length ? trialRows : none(4)),
  );
}

async function createTrials() {
  const name = document.getElementById("group-name").value.trim();
  if (!name) throw new Error("検討案が空");
  if (!groupShown) throw new Error("先に絞り込む");
  const r = await api("POST", "/api/group/scenarios", {
    version_id: groupShown.version, name, head: TRIAL_HEAD, q: groupShown.q,
  });
  await loadLists();
  await drawGroup();
  showMessage(`「${name}」${r.created} 製品に作成・既存 ${r.existing} 製品${r.truncated ? "（上限）" : ""}`, "info");
}

function wireGroup() {
  document.getElementById("group-show").addEventListener("click", () => guarded(drawGroup));
  document.getElementById("group-create").addEventListener("click", () => guarded(createTrials));
  document.getElementById("group-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawGroup));
  document.getElementById("group-version").addEventListener("change", () => guarded(drawGroup));
  setCreateLabel();
}

wireGroup();
