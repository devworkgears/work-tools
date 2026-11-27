"use strict";
// 起動: 画面の表と、最初の画面を開く。ほかのファイルの後に読む

const SCREENS = {
  2: hubScreen,
  3: targetsScreen,
  5: listScreen,
  6: changesScreen,
  7: resultScreen,
  8: rewriteScreen,
  9: tempScreen,
  10: ownScreen,
};

// 新規作成の ③: 下書きの中身は ② の窓が持つ
function openDraft(id) {
  if (!channel) return done("この画面は開けません");
  S.mode = "new";
  S.draft = { id, header: {}, targets: [], take(targets) {
    S.draft.take = () => {}; // 最初の返事だけを使う
    S.draft.targets = targets;
    document.title = `${NUM[3]} ${label(NAMES[3])}`;
    openScreen(3);
  } };
  channel.postMessage({ type: "draft-ask", id });
}

// 別の窓は URL の検討を読んでその画面を開く
async function openFromUrl() {
  const key = Number(PARAMS.get("screen"));
  if (!NAMES[key]) return done("開く画面が違います");
  S.mode = ["edit", "view"].includes(PARAMS.get("mode")) ? PARAMS.get("mode") : "edit";
  if (key === 3 && PARAMS.get("draft") && !PARAMS.get("number")) return openDraft(PARAMS.get("draft"));
  try {
    await loadStudy(PARAMS.get("number") || "", PARAMS.get("version") || null, false);
  } catch (e) {
    return done(`読込に失敗しました（${e && e.message ? e.message : e}）`);
  }
  document.title = `${NUM[key]} ${label(NAMES[key])} ${S.number}`;
  openScreen(key);
}

// 語の読み替えを受け取ってから最初の画面を描く（受け取れなくても元の語で開く）
api("GET", "/api/v2/labels")
  .then((d) => Object.assign(LABELS, d.labels), fail)
  .finally(() => (POPUP ? openFromUrl() : openScreen(2)));
