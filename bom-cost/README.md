# bom-cost

型: 部品（計算部品）

BOM（部品構成）と品番ごとの原価から製品の原価を計算し、差し替えの試算・型費の載せ方・版の比較をブラウザの画面で見る道具です。

## 起動

- zip の場合（Windows）: `bom-cost-win64.zip` を展開し、`bom-cost\起動.bat` をダブルクリック。ブラウザで画面が開く。止めるにはウィンドウを閉じる。DB は `bom-cost\data\bom-cost.sqlite`
- Python の場合（3.12）: このフォルダで `python -m bomcost.app --db bom-cost.sqlite`。.xlsx を読むときは openpyxl が要る（`python -m pip install -r requirements.txt`）
- zip を作る: `python3 build_dist.py --cache <embeddable 版の zip と wheel を置いたフォルダ>` → `dist/bom-cost-win64.zip`
