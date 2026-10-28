# bom-cost（BOM 原価の計算の中心）

型: 部品（計算部品）

BOM（部品構成）と品番ごとの原価から、製品の原価を計算する Python の層です。CSV／Excel（.xlsx）の取り込みと SQLite への保存、手元で開く画面（製品から見る画面）も持ちます。Python 3.12 の標準ライブラリと openpyxl（.xlsx の読み書きのときだけ）で動きます。

## 計算するもの

- 原価の積み上げ: 品目の原価 ＝ 自分の原価 ＋ Σ（子の数量 × 子の原価）。品番ごとに1回だけ計算する。費目ごとに「下位込み／自分だけ」を持ち、費目単位・品目単位で上書きできる（既定は下位込み）。償却費は計算から外す
- 未登録の原価（空欄）は 0 円とせず、未登録の品目の一覧として結果に出す
- 構成行の重複（親品番＋風船番号＋子品番）を畳み、数量の食い違いは結果に出す。風船番号を付けずに見るときは同じ子品番の数量を合算する
- 差分の試算: 土台の製品に「足す・抜く・替える・数量」を重ね、変更ごとに前後の原価と前提（版・費目）を履歴に残す。同じ試算を別の版の原価で計算し直して品目ごとの差を出す
- 二重計上の判定: 親の登録値と子の足し上げを比べた推定（下位込みらしい／自分だけらしい／製造原価は合計らしい／判定できない）。しきい値は引数で変えられる
- 型費: 1個あたり ＝ 型費 ÷ 償却年数（既定 2年）÷ 部品の年間の数。載せ方（新製品だけ／使う数で分ける／載せない）の切り替えと、流用先の既存品の差額
- モデルチェンジ: 元の製品と新しい製品の構成を、同じ品番・部品の対応表・足した／抜いた部品の3段で対応させる

入力の型の揺れ（int の品番、"1,600" や float の金額、1/0/"1"/"0"/True/False のフラグ）は、データのクラスを作る時に1回だけそろえます。金額と数量は Decimal で持ちます（float は文字列を経由して Decimal にする）。

## 品番と版

- 品番は8桁、バリエーションは後ろに2桁を足した10桁。原価の表の MBOM の品番は後ろを 0 で埋めた11桁で、取り込むときに先頭10桁を取り、後ろ2桁が 00 なら8桁に戻す（`mbom_to_ebom`）。11桁でない品番は変換せずに取り込み、警告に出す
- 品目の区分（製品／部品）・状態（量産／廃止）は元の記号のまま文字列で持つ
- 取り込み1回分が1つの版。手で入れる品目・原価・構成・年間台数は消えない版 `manual` に置き、どの版を読むときも上に重ねる（同じ品番は `manual` を採り、重なりは `manual_overlaps` に出る）
- 年間台数の種別は出どころで決まる: 取り込んだ台数は量産の実績、`manual` の台数は新製品の計画（型費の「新製品だけに載せる」が載せる先）
- 版は自動では消えない。試算が使っている版と `manual` は消せない

## 使い方の例

```python
from bomcost import BomLine, CostHead, CostRow, Version, calculate_cost

v = Version("2026-10")
v.add_bom_lines([BomLine("AB123456", "1", "CD234567", 2)])
v.add_cost(CostRow.from_mbom("CD234567000", {CostHead.MATERIAL: "1,600"}))
print(calculate_cost(v, "AB123456").total)  # 3200
```

取り込みと保存（列の対応づけは見出しの並びごとに保存し、同じ並びのファイルには次から自動で当たる）:

```python
from bomcost import ColumnMap, Store, Version, VersionImport, read_table

store = Store("bom-cost.sqlite")
table = read_table("構成.csv")  # .csv（UTF-8・Shift_JIS）か .xlsx
column_map = store.find_column_map(table.headers, "bom_line")
if column_map is None:
    column_map = ColumnMap("bom_line", table.headers,
                           {"parent": "親品番", "child": "子品番", "quantity": "数量", "balloon": "風船番号"})
    store.save_column_map(column_map)
imp = VersionImport(Version("2026-10"))
imp.add_table(table, column_map)  # 製品ごとの表を何本でも。重複は畳み、数量の食い違いは残す
print(imp.warnings)
store.save_version(imp.version)
v = store.load_version("2026-10")  # manual を重ねて読む
```

## 画面の起動

このフォルダで次を打つと、手元にサーバーが立ち（127.0.0.1 だけで待つ）、既定のブラウザで画面が開きます。既定のポートは 8765 で、使われていれば空いているポートを使います（開く URL は起動時に表示）。止めるには Ctrl+C。

```bash
python -m bomcost.app --db bom-cost.sqlite              # 無ければ空の DB を作る
python -m bomcost.app --db bom-cost.sqlite --port 8800 --no-browser
python -m bomcost.app --demo --db demo.sqlite           # 架空の例を入れた DB を作って終わる（あれば止める）
```

架空の例は版 2026-10・2026-12、手入力の試作品 X-610、年間台数、型費、流用先、試算「V-110 検討」（A-100 を土台に F-600 を足し C-300 を抜く・材料費 4,650 円）です。

画面でできること:

- 上の帯: 差分／一括（一括は土台の製品をそのまま見る）・品番でまとめる／風船番号で分ける・計算に使う版・費目・試算の選択と新規作成・ツリー／テーブル・版の比較
- 左: 構成のツリー（開いた所だけ取りに行く）。選んだ費目の原価を出し、未登録は「未登録」と出す
- 右上: 選んだ品目の費目5つ（登録値と計算値）・出どころ・二重計上の判定と、替える・抜く・足す・数量の操作
- 右下: 変更の履歴（差額と前提＝版・費目・台数）と最後の取り消し
- 下: 変更の結果（変更前・変更後・差額と、費目・参考値・償却費除外・未登録の有無）
- テーブル: 1行1構成行の表。列の並べ替え・絞り込み・表示する列の選択と、表示中の行の Excel 書き出し（見るだけ・打ち替えはツリーで）

画面の静的ファイルは `bomcost/web/`、API は `bomcost/api.py`（`Api.handle` をソケット無しで呼べる）、サーバーは `bomcost/app.py` です。外部の CDN・フォントは読みません。

## .xlsx を読む準備

`.xlsx` の読み込みは openpyxl を使います。`requirements.txt` の版をこのフォルダの `vendor/` に入れておけば、それを優先して読みます（例: `python -m pip install -r requirements.txt --target vendor`）。`vendor/` が無ければ手元の Python の openpyxl を使います。CSV だけなら要りません。

## 検証

```bash
cd bom-cost && python3 -m unittest discover -s tests
```
