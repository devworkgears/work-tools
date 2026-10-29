# bom-cost

Type: component

Roll up product cost from a bill of materials and per-part costs; try part swaps, tooling cost allocation and cost-version comparison in a browser UI.

## Run

- Windows zip: extract `bom-cost-win64.zip` and double-click `bom-cost\起動.bat`. The UI opens in the browser; close the window to stop. Data is stored in `bom-cost\data\bom-cost.sqlite`.
- Python 3.12: `python -m bomcost.app --db bom-cost.sqlite` in this folder. Reading .xlsx needs openpyxl (`python -m pip install -r requirements.txt`).
- Build the zip: `python3 build_dist.py --cache <folder with the embeddable Python zip and wheels>` → `dist/bom-cost-win64.zip`
