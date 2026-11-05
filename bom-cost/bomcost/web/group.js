// ①グループを選ぶ: グループの製品を絞って並べ、表示中の製品を②に入れる。直すのは②部品・費目を変えるの画面（trial.js）。
// app.js・screens.js の関数を使う。
"use strict";

const TRIAL_HEAD = "MANUFACTURING"; // ②の原価は製造原価で出す（②の画面も使う）

// 最後に並べた一覧の条件。入れるボタンは入力欄の今の値でなく、表示中の一覧と同じ条件で入れる
let groupShown = null; // { group, q, count }

async function enterGroup() {
  const group = document.getElementById("group-pick");
  groupOptions(group, { value: group.value || state.summary?.scenario.group_id });
  if (group.value) await drawGroup();
}

function countText(n, shown, truncated) {
  return truncated ? `${shown} / ${n} 件（上限）` : `${n} 件`;
}

function setCreateLabel() {
  const button = document.getElementById("group-create");
  button.textContent = groupShown ? `表示中の ${groupShown.count} 製品を②に入れる` : "②に入れる";
  button.disabled = !groupShown || !groupShown.count;
}

async function drawGroup() {
  const group = document.getElementById("group-pick").value;
  if (!group) throw new Error("グループが空");
  const q = document.getElementById("group-q").value.trim();
  const p = await api("GET", `/api/group/products?${qs({ group, q })}`);
  groupShown = { group, q, count: p.products.length };
  setCreateLabel();
  const inTrial = new Set(state.scenarios.filter((s) => s.group_id === group).map((s) => s.base));
  const productRows = p.products.map((x) => el("tr", {}, el("td", {}, x.code), el("td", {}, x.name),
    el("td", {}, inTrial.has(x.code) ? el("span", { class: "tag" }, "②") : "")));
  const none = (n) => [el("tr", {}, el("td", { colspan: n, class: "muted" }, "なし"))];
  document.getElementById("group-body").replaceChildren(
    el("h3", {}, `製品（${countText(p.count, p.products.length, p.truncated)}・区分 ${p.kind ?? "—"}）`),
    table(["品番", "名称", "②"], productRows.length ? productRows : none(3)),
  );
  document.getElementById("group-trials").replaceChildren(
    el("span", {}, `②の製品 ${inTrial.size} 件`),
    el("button", { type: "button", disabled: !inTrial.size, onclick: () => guarded(() => openTrial(group)) }, "②で開く"),
  );
}

async function createTrials() {
  if (!groupShown) throw new Error("先に絞り込む");
  const r = await api("POST", "/api/group/scenarios", { group_id: groupShown.group, head: TRIAL_HEAD, q: groupShown.q });
  await loadLists();
  await drawGroup();
  showMessage(`②に ${r.created} 製品を入れた・既存 ${r.existing} 製品${r.truncated ? "（上限）" : ""}`, "info");
}

function wireGroup() {
  document.getElementById("group-show").addEventListener("click", () => guarded(drawGroup));
  document.getElementById("group-create").addEventListener("click", () => guarded(createTrials));
  document.getElementById("group-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawGroup));
  document.getElementById("group-pick").addEventListener("change", () => guarded(drawGroup));
  setCreateLabel();
}

wireGroup();
