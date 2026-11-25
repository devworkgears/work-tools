// 設定の画面。どの画面からもリンクしない。［保存］で DB と同じフォルダの settings.json に書き、すぐ効く。
"use strict";

const $ = (id) => document.getElementById(id);
const FIELDS = [
  ["adjustment_percent", "諸経費の率", "%"],
  ["indirect_percent", "間接費の率", "%"],
  ["code_total", "原価の品番の桁数", "基本＋枝番＋後ろの 0"],
  ["code_base", "基本の品番の桁数", ""],
  ["code_branch", "枝番の桁数", "全部 0 なら基本の品番"],
  ["product_kind", "製品の区分の記号", ""],
  ["part_kind", "部品の区分の記号", ""],
  ["mold_years", "型の償却年数", "年"],
];
let defaults = {};
let groups = []; // 読み替えられる語の組 { name, words, imports }

function cell(tag, ...children) {
  const node = document.createElement(tag);
  node.append(...children);
  return node;
}

function input(value, name) {
  const node = document.createElement("input");
  node.className = "in";
  node.value = value ?? "";
  node.setAttribute("aria-label", name);
  return node;
}

function say(text, bad = false) {
  $("msg").textContent = text;
  $("msg").className = bad ? "note bad" : "note";
}

function labelRow(from = "", to = "") {
  const a = input(from, "元の語");
  const b = input(to, "使う語");
  const row = cell("tr", cell("td", a), cell("td", "→"), cell("td", b));
  // 最後の行に打ったら、次の空の行を足す
  row.addEventListener("input", () => {
    if (row === $("labels").lastElementChild) $("labels").append(labelRow());
  });
  return row;
}

function draw(s) {
  $("values").replaceChildren(...FIELDS.map(([key, name, note]) => {
    const node = input(s[key], name);
    node.dataset.key = key;
    if (defaults[key]) node.placeholder = defaults[key];
    const mini = cell("td", note);
    mini.className = "mini";
    return cell("tr", cell("th", name), cell("td", node), mini);
  }));
  const labels = s.labels ?? {};
  const listed = new Set(groups.flatMap((g) => g.words));
  $("words").replaceChildren(...groups.flatMap((g) => {
    const title = cell("th", g.name);
    title.colSpan = 2;
    title.className = "group";
    return [cell("tr", title), ...g.words.map((w) => {
      const node = input(labels[w], `${w} の代わりに使う語`);
      node.dataset.word = w;
      node.placeholder = w;
      return cell("tr", cell("th", w), cell("td", node));
    })];
  }));
  $("labels").replaceChildren(...Object.entries(labels).filter(([a]) => !listed.has(a)).map(([a, b]) => labelRow(a, b)), labelRow());
}

// 画面の中身。読み替えの元の語が空・重なりなら止める
function read() {
  const s = {};
  for (const node of $("values").querySelectorAll("input")) s[node.dataset.key] = node.value.normalize("NFKC").trim();
  s.labels = {};
  for (const node of $("words").querySelectorAll("input")) {
    if (node.value.trim()) s.labels[node.dataset.word] = node.value.trim();
  }
  for (const row of $("labels").children) {
    const [a, b] = [...row.querySelectorAll("input")].map((x) => x.value.trim());
    if (!a && !b) continue;
    if (!a || !b) throw new Error("語の読み替えは、元の語と使う語の両方を入れてください");
    if (Object.hasOwn(s.labels, a)) throw new Error(`語の読み替えの「${a}」が重なっています`);
    s.labels[a] = b;
  }
  return s;
}

async function api(method, body) {
  const res = await fetch("/api/settings", {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error);
  return data;
}

function show(data) {
  defaults = data.defaults;
  groups = data.words;
  $("file").textContent = data.file ? `${data.file}${data.exists ? "" : "（まだ無い）"}` : "";
  draw(data.settings);
}

const act = (work) => async () => {
  try {
    await work();
  } catch (e) {
    say(e.message, true);
  }
};

$("save").addEventListener("click", act(async () => {
  show(await api("PUT", { settings: read() }));
  say("保存しました");
}));

$("export").addEventListener("click", act(async () => {
  const { settings } = await api("PUT", { settings: read(), check: true });
  const url = URL.createObjectURL(new Blob([JSON.stringify(settings, null, 2) + "\n"], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "settings.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  say("今の画面の値を書き出しました（保存はしていません）");
}));

$("load").addEventListener("click", () => $("pick").click());
$("pick").addEventListener("change", act(async () => {
  const [file] = $("pick").files;
  $("pick").value = "";
  if (!file) return;
  let data;
  try {
    data = JSON.parse((await file.text()).replace(/^﻿/, ""));
  } catch (_) {
    throw new Error(`${file.name} を JSON として読めません`);
  }
  draw((await api("PUT", { settings: data, check: true })).settings);
  say("読み込みました（［保存］で効きます）");
}));

act(async () => show(await api("GET")))();
