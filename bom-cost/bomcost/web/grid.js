// Excel 風の格子。セルを選び、Enter・矢印で移り、打てば書き換え、タブ区切りの貼り付けで行が足される。
// 中身は model が持つ: nRows()・nCols()・cell(r, c) → {text, edit: 入力欄と写しに使う値（無ければ text）, cls, editable, th,
// lock: 書けず、打った文字も貼った値もほかの列へ回さない}・set(r, c, text)。
// 任意で groups()（見出しの上の段・{ text, span, cls }）・head()（見出しの行・{ text, cls, title, action } で押せる見出し）・action(r, c)（クリックで動くセル）・changed()（書き換えの後に1回）・
// grow()（空の行を1行足す。足せなければ false）・idle()（打っていたセルを閉じた後）。
"use strict";

// 語の読み替え（画面の語 → 使う語）。起動の時にサーバーから受け取る。語がまるごと同じか、後ろに「（…）」が付いた物だけ替える
const LABELS = {};
function label(text) {
  if (Object.hasOwn(LABELS, text)) return LABELS[text];
  const at = text.indexOf("（");
  const head = at < 0 ? text.trimEnd() : text.slice(0, at);
  return Object.hasOwn(LABELS, head) ? LABELS[head] + text.slice(head.length) : text;
}

// 打った・貼った値の全角の英数字・記号を半角にそろえる（マイナス記号の − も - に）
function clean(text) {
  return text.normalize("NFKC").replace(/\u2212/g, "-").trim();
}

class Grid {
  constructor(container, model) {
    this.container = container;
    this.model = model;
    this.sel = { r: 0, c: 0 };
    this.editing = null; // { r, c, input }
    this.heads = 0; // 見出しの段数
    this.drawn = []; // 出している行の中身（変わった行だけ作り直すため）
    this.wrap = document.createElement("div");
    this.wrap.className = "gridwrap";
    this.sink = document.createElement("textarea");
    this.sink.className = "sink";
    this.sink.setAttribute("aria-hidden", "true");
    this.table = document.createElement("table");
    this.table.className = "grid";
    this.wrap.append(this.sink, this.table);
    container.replaceChildren(this.wrap);
    Grid.active = this; // 画面を閉じる・案を替える前に、打ちかけのセルを書き換えるため
    this.table.addEventListener("mousedown", (e) => this.onMouseDown(e));
    this.table.addEventListener("dblclick", (e) => {
      const td = e.target.closest("td[data-r]");
      if (td) this.startEdit(true);
    });
    this.sink.addEventListener("keydown", (e) => this.onKey(e));
    // 日本語入力の確定や insertText のように keydown を経ずに入った文字も、編集を始めて引き継ぐ
    this.sink.addEventListener("input", (e) => {
      if (!e.isComposing) this.takeSink();
    });
    this.sink.addEventListener("compositionend", () => this.takeSink());
    this.sink.addEventListener("paste", (e) => this.onPaste(e));
    this.sink.addEventListener("copy", (e) => this.onCopy(e));
    this.firstEditable();
    this.render();
  }

  firstEditable() {
    for (let r = 0; r < this.model.nRows(); r++)
      for (let c = 0; c < this.model.nCols(); c++)
        if (this.model.cell(r, c).editable) {
          this.sel = { r, c };
          return;
        }
  }

  render() {
    // 打っている途中は描き直さない（一時保存の返事で入力欄が消えないように）。確定・取り消しの時に描き直す
    if (this.editing) return;
    const rows = [];
    const nr = this.model.nRows();
    const nc = this.model.nCols();
    if (this.sel.r >= nr) this.sel.r = Math.max(0, nr - 1);
    if (this.sel.c >= nc) this.sel.c = Math.max(0, nc - 1);
    if (this.model.groups) {
      const tr = document.createElement("tr");
      for (const g of this.model.groups()) {
        const th = document.createElement("th");
        th.textContent = label(g.text);
        th.colSpan = g.span;
        if (g.cls) th.className = g.cls;
        tr.append(th);
      }
      rows.push(tr);
    }
    if (this.model.head) {
      const tr = document.createElement("tr");
      for (const h of this.model.head()) {
        const th = document.createElement("th");
        th.textContent = label(h.text ?? h);
        if (h.cls) th.className = h.cls;
        if (h.title) th.title = h.title;
        // 押せる見出し（全部の行に印を付ける など）
        if (h.action) {
          th.style.cursor = "pointer";
          th.addEventListener("mousedown", (e) => {
            e.preventDefault();
            this.commit();
            h.action();
            this.changed();
          });
        }
        tr.append(th);
      }
      rows.push(tr);
    }
    // 見出しは毎回作り直し、中身の行は変わった物だけ作り直す（列の多い表で、1つ打つたびに全部を描くと遅い）
    const old = [...this.table.children].slice(this.heads);
    const drawn = [];
    for (let r = 0; r < nr; r++) {
      const cells = [];
      const marks = [];
      for (let c = 0; c < nc; c++) {
        const cell = this.model.cell(r, c);
        cells.push(cell);
        marks.push(cell.text ?? "", cell.cls || "", cell.editable ? 1 : 0, cell.th ? 1 : 0, cell.title || "");
      }
      drawn.push(marks.join("\u0001"));
      if (old[r] && drawn[r] === this.drawn[r]) continue;
      const tr = document.createElement("tr");
      cells.forEach((cell, c) => {
        const td = document.createElement(cell.th ? "th" : "td");
        td.dataset.r = r;
        td.dataset.c = c;
        td.textContent = cell.text ?? "";
        td.className = `${cell.cls || ""}${cell.editable ? " in" : ""}`.trim();
        if (cell.title) td.title = cell.title;
        tr.append(td);
      });
      if (old[r]) old[r].replaceWith(tr);
      else this.table.append(tr);
    }
    for (const tr of old.slice(nr)) tr.remove();
    for (const tr of [...this.table.children].slice(0, this.heads)) tr.remove();
    this.table.prepend(...rows);
    this.heads = rows.length;
    this.drawn = drawn;
    for (const td of this.table.querySelectorAll(".cur")) td.classList.remove("cur");
    const at = this.td(this.sel.r, this.sel.c);
    if (at) at.classList.add("cur");
    if (this.model.selected) this.model.selected(this.sel.r);
  }

  td(r, c) {
    const tr = this.table.children[this.heads + r];
    return (tr && tr.children[c]) || null;
  }

  focus() {
    this.sink.focus({ preventScroll: true });
  }

  select(r, c) {
    const nr = this.model.nRows();
    const nc = this.model.nCols();
    r = Math.max(0, Math.min(nr - 1, r));
    c = Math.max(0, Math.min(nc - 1, c));
    const old = this.td(this.sel.r, this.sel.c);
    if (old) old.classList.remove("cur");
    this.sel = { r, c };
    const td = this.td(r, c);
    if (td) {
      td.classList.add("cur");
      td.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    if (this.model.selected) this.model.selected(r);
  }

  onMouseDown(e) {
    const td = e.target.closest("[data-r]");
    if (!td) return;
    if (this.editing && td.contains(this.editing.input)) return;
    e.preventDefault();
    this.commit();
    const r = +td.dataset.r;
    const c = +td.dataset.c;
    this.select(r, c);
    this.focus();
    if (this.model.action && this.model.action(r, c)) this.changed();
  }

  // (r, c) から (dr, dc) の向きに進んで最初の書けるセル。無ければ null
  nextEditable(r, c, dr, dc) {
    const nr = this.model.nRows();
    const nc = this.model.nCols();
    for (let rr = r + dr, cc = c + dc; rr >= 0 && rr < nr && cc >= 0 && cc < nc; rr += dr, cc += dc) {
      if (this.model.cell(rr, cc).editable) return { r: rr, c: cc };
    }
    return null;
  }

  // Tab・Enter・左右の矢印は書けるセルへ飛ぶ。その向きに書けるセルが無ければ（読むだけの表など）1つ動く
  move(dr, dc, skip) {
    const { r, c } = this.sel;
    const to = skip ? this.nextEditable(r, c, dr, dc) : null;
    if (to) this.select(to.r, to.c);
    else this.select(r + dr, c + dc);
  }

  startEdit(keep, initial = "") {
    let { r, c } = this.sel;
    if (!this.model.cell(r, c).editable) {
      // 書けないセル（名称など）で打った文字は捨てず、右の書ける列に入れる
      const to = keep || this.model.cell(r, c).lock ? null : this.nextEditable(r, c, 0, 1);
      if (!to) return false;
      this.select(to.r, to.c);
      ({ r, c } = to);
    }
    const td = this.td(r, c);
    const input = document.createElement("input");
    input.className = "cellin";
    const cell = this.model.cell(r, c);
    input.value = keep ? cell.edit ?? cell.text ?? "" : initial;
    td.textContent = "";
    td.append(input);
    this.drawn[r] = null; // 入力欄を出した行は、次に必ず作り直す
    this.editing = { r, c, input };
    input.addEventListener("keydown", (e) => this.onEditKey(e));
    // 後回しにすると、打ったまま押したボタン・×が書き換えより先に動き、打った物が落ちる
    input.addEventListener("blur", () => this.commit());
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    return true;
  }

  takeSink() {
    const text = this.sink.value.replace(/[\r\n]/g, "");
    this.sink.value = "";
    if (!text || this.editing) return;
    this.startEdit(false, text);
  }

  commit() {
    if (!this.editing) return;
    const { r, c, input } = this.editing;
    this.editing = null;
    this.model.set(r, c, clean(input.value));
    this.changed();
    if (this.model.idle) this.model.idle();
  }

  cancel() {
    this.editing = null;
    this.render();
    this.focus();
    if (this.model.idle) this.model.idle();
  }

  changed() {
    this.render();
    if (this.model.changed) this.model.changed();
  }

  onEditKey(e) {
    const moves = { Enter: [1, 0], Tab: [0, 1], ArrowDown: [1, 0], ArrowUp: [-1, 0] };
    if (e.isComposing) return;
    if (e.key === "Escape") {
      e.preventDefault();
      this.cancel();
      return;
    }
    if (e.key in moves) {
      e.preventDefault();
      let [dr, dc] = moves[e.key];
      if (e.shiftKey && (e.key === "Enter" || e.key === "Tab")) [dr, dc] = [-dr, -dc];
      this.commit();
      this.move(dr, dc, e.key !== "ArrowUp" && e.key !== "ArrowDown");
      this.focus();
    }
  }

  onKey(e) {
    if (e.isComposing) return;
    const moves = { ArrowDown: [1, 0], ArrowUp: [-1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1], Enter: [1, 0], Tab: [0, 1] };
    const { r, c } = this.sel;
    if (e.key in moves && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      let [dr, dc] = moves[e.key];
      if (e.shiftKey && (e.key === "Enter" || e.key === "Tab")) [dr, dc] = [-dr, -dc];
      this.move(dr, dc, e.key !== "ArrowUp" && e.key !== "ArrowDown");
      return;
    }
    if (e.key === "F2") {
      e.preventDefault();
      this.startEdit(true);
      return;
    }
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      if (this.model.cell(r, c).editable) {
        this.model.set(r, c, "");
        this.changed();
      }
      return;
    }
    if (e.key === " " && this.model.action && !this.model.cell(r, c).editable) {
      e.preventDefault();
      if (this.model.action(r, c)) this.changed();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    // 変換中は textarea に組ませ、確定（compositionend）で takeSink が編集を始める。途中で入れ物を替えると文字が落ちる
    if (e.key === "Process") return;
    if (e.key.length === 1) {
      e.preventDefault();
      this.startEdit(false, e.key);
    }
  }

  onPaste(e) {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (!text) return;
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
    const { r, c } = this.sel;
    // 途中の空の行も1行に数える。空の行では行が増えないので、貼る行数ぶんを先に足す
    if (this.model.grow) while (this.model.nRows() < r + lines.length && this.model.grow());
    lines.forEach((line, i) => {
      if (r + i >= this.model.nRows()) return;
      let cc = c;
      for (const value of line.split("\t")) {
        // 書き換えられない列（名称など）は飛ばし、次の書き換えられる列に入れる。列は貼りながら増えるので毎回たずねる
        while (cc < this.model.nCols() && !this.model.cell(r + i, cc).editable && !this.model.cell(r + i, cc).lock) cc++;
        if (cc >= this.model.nCols()) break;
        if (this.model.cell(r + i, cc).editable) this.model.set(r + i, cc, clean(value));
        cc++;
      }
    });
    this.changed();
  }

  onCopy(e) {
    const cell = this.model.cell(this.sel.r, this.sel.c);
    e.clipboardData.setData("text/plain", cell.edit ?? cell.text ?? "");
    e.preventDefault();
  }
}
