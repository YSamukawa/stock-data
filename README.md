# Sector Flow Monitor — 米国株 業種別 資金フロー モニター

S&P 500 構成銘柄を Yahoo Finance の11業種に分け、株価×出来高から推定した「資金の流入・流出」を毎日自動更新して表示する単一HTMLアプリです。
データ取得は GitHub Actions（GitHub のサーバー）が毎日自動で行い、HTML は GitHub Pages で公開します。**開くだけで最新データが表示されます。**

## セットアップ手順（GitHub の Web 画面だけで完結、約5分）

### 1. リポジトリを作る
1. https://github.com/new を開く
2. Repository name に `sector-flow`（任意）を入力
3. **Public** を選択（Private でも Pages は使えますが、無料プランでは Public が必要です）
4. **Create repository** をクリック

### 2. ファイルをアップロードする
1. 作成したリポジトリの画面で **「uploading an existing file」** リンク（または `Add file` → `Upload files`）をクリック
2. この ZIP を解凍してできたフォルダの中身を **すべて** ドラッグ＆ドロップ
   - `index.html`
   - `requirements.txt`
   - `README.md`
   - `scripts/fetch.py`
   - `.github/workflows/update.yml` ← **`.github` フォルダごと**ドロップしてください（隠しフォルダなので表示設定に注意）
   - `data/.gitkeep`
3. 画面下の **Commit changes** をクリック

> `.github/workflows/update.yml` が正しい場所に入っていれば、リポジトリの **Actions** タブに「Update sector flow data」が表示されます。表示されない場合はフォルダ階層を確認してください。

### 3. Actions に書き込み権限を与える
1. リポジトリの **Settings** → 左メニュー **Actions** → **General**
2. 一番下の **Workflow permissions** で **Read and write permissions** を選択 → **Save**

### 4. 初回のデータ取得を手動実行する
1. **Actions** タブ → 左の **Update sector flow data** → 右上の **Run workflow** → 緑の **Run workflow**
2. 5〜15分ほどで完了します（初回は業種情報を約500銘柄分取得するため時間がかかります）
3. 完了すると `data/latest.json` と `data/sector_map.json` がコミットされます

### 5. GitHub Pages を有効にする
1. **Settings** → 左メニュー **Pages**
2. **Source** を `Deploy from a branch`、Branch を `main` / `/ (root)` にして **Save**
3. 1〜2分後に `https://<あなたのユーザー名>.github.io/sector-flow/` でアプリが開きます

以降は毎日 **日本時間 火〜土 07:30 頃**（米国市場の終了後）に自動でデータが更新され、ページを開けば最新の状態が見られます。

## 使い方
- **業種別 推定ネットフロー**：1日 / 5日 / 20日の累計と、金額 / 時価総額比（bp）を切り替え。棒をクリックで下の詳細が切り替わります
- **資金ローテーション**：横軸 20日累計、縦軸 5日累計。右上＝流入継続、左上＝流入に転換、左下＝流出継続、右下＝流出に転換
- **日次ヒートマップ**：直近20営業日の日次フロー（時価総額比）。セルにマウスを載せると数値が出ます
- **業種詳細**：累積フローの推移、日次サマリー、流入/流出上位銘柄

## 指標の定義（重要：推定値です）
無料データでは実際のファンド資金流出入は取得できません。本アプリの「推定ネットフロー」は次の近似です。

```
銘柄ごとの推定ネットフロー = sign(終値 − 前日終値) × 終値 × 出来高
業種の推定ネットフロー   = 業種内の銘柄の合計
時価総額比 (bp)          = 業種の推定ネットフロー ÷ 業種時価総額 × 10,000
```
上昇日は売買代金の全額を流入、下落日は全額を流出とみなす単純化です。

## データの出所
| 項目 | 出所 |
|---|---|
| 構成銘柄 | Wikipedia「List of S&P 500 companies」（失敗時は GitHub `datasets/s-and-p-500-companies`） |
| 業種区分 | Yahoo Finance quoteSummary の `sector`（yfinance 経由）。取得できない銘柄は GICS セクターを対応表で変換 |
| 株価・出来高 | Yahoo Finance 日足（yfinance 経由、調整前終値） |
| 発行済株式数 | Yahoo Finance `sharesOutstanding`（時価総額計算用、45日ごとに更新） |

## 注意
- Yahoo Finance は非公式API（yfinance）経由の取得です。Yahoo 側の仕様変更で取得に失敗することがあります。その場合は Actions のログを確認し、`requirements.txt` の yfinance を最新版に更新してください
- 米国の休場日は前営業日のデータのまま更新されます（変更がなければコミットされません）
- ファイルサイズ：`data/latest.json` は約150KB です

## ローカルで実行する場合
```
pip install -r requirements.txt
python scripts/fetch.py
python -m http.server 8000   # http://localhost:8000/ を開く
```
