# US Money Flow Monitor — 米国株 資金フロー モニター

米国株の「市場全体」「業種別（GICS 11セクター）」「個別株（S&P 500 構成銘柄）」の3つの視点で、資金の流入・流出を毎日自動更新して表示する単一HTMLアプリです。
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
### 市場全体タブ
- **方向一致メーター**：RSP/SPY・IWM/SPY・HYG/LQD・HY OAS・VIX・A/D・Up/Down Volume の7指標について、1日/5日/20日の変化方向が Risk-on か Risk-off かを数えます。SPY の方向と食い違う場合はダイバージェンス注意を表示します
- **参考スコア**：添付資料の Risk Flow Score（Zスコア合成、±100）。仮置きの尺度で未検証
- **主要 ETF・指数**、**市場内部の強弱**（比率・スプレッド・Breadth の60日推移）、**公表統計**（ICI 週次フロー、MMF、FINRA Margin Debt、CFTC COT、AAII、FRB ネット流動性、Put/Call、ETF フロー推定）
- 各指標の右の「取得OK／前回値／未取得」バッジで、その日の取得状況が分かります

### 業種別タブ
- **業種別 推定ネットフロー**：1日 / 5日 / 20日の累計と、金額 / 時価総額比（bp）を切り替え。棒をクリックで下の詳細が切り替わります
- **資金ローテーション**：横軸 20日累計、縦軸 5日累計。右上＝流入継続、左上＝流入に転換、左下＝流出継続、右下＝流出に転換
- **日次ヒートマップ**：直近20営業日の日次フロー（時価総額比）。セルにマウスを載せると数値が出ます
- **業種詳細**：累積フローの推移、日次サマリー（空売り比率を含む）、流入/流出上位銘柄

### 個別株タブ
- ティッカー・社名で検索。株価（50日・200日線）、推定ネットフロー、対 SPY 相対パフォーマンス、空売り比率（FINRA 取引所外）の60日推移
- ランキング：推定ネットフロー（1日/5日/20日）、出来高比、対 SPY 相対強度、空売り比率、騰落率で上位・下位15銘柄。業種で絞り込み可。行クリックで詳細表示

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
| 業種区分 | **GICS**（S&P Dow Jones Indices / MSCI）。構成銘柄リストの GICS Sector / Sub-Industry 列をそのまま使用（既定）。`SECTOR_SCHEME=yahoo` を環境変数で指定すると Yahoo Finance の11業種（Morningstar 準拠）に切替可能（取得できない銘柄は GICS を対応表で変換） |
| 株価・出来高 | Yahoo Finance 日足（yfinance 経由、調整前終値） |
| 発行済株式数 | Yahoo Finance `sharesOutstanding`（時価総額計算用、45日ごとに更新） |
| 主要 ETF・指数・セクター ETF | Yahoo Finance 日足（SPY QQQ IWM RSP HYG LQD TLT GLD UUP ^VIX ^VIX3M、XLK 等11本） |
| HY/IG OAS、FRB 総資産、RRP、TGA | FRED（`BAMLH0A0HYM2` `BAMLC0A0CM` `WALCL` `RRPONTSYD` `WTREGEN`）CSV |
| ファンドフロー（週次） | ICI「Combined Estimated Long-Term Flows」ページの表（直近5週を毎回取得して蓄積） |
| MMF 残高（週次） | ICI「Money Market Fund Assets」ページの表（同上） |
| Margin Debt（月次） | FINRA `margin-statistics.xlsx` |
| 先物ポジション（週次） | CFTC Public Reporting API（Disaggregated COT、E-mini S&P 500） |
| 個人投資家センチメント（週次） | AAII `sentiment.xls` |
| Put/Call（日次） | Cboe Daily Market Statistics ページ（スクレイピング、蓄積型） |
| 空売り比率（日次） | FINRA Reg SHO `CNMSshvol{YYYYMMDD}.txt`（取引所外分のみ、蓄積型） |
| ETF フロー推定（日次・実験的） | Yahoo Finance の発行済口数スナップショット差分 × 終値（蓄積型） |

出力ファイル：`data/latest.json`（業種・銘柄サマリー）、`data/stocks.json`（銘柄別60日時系列、約1.4MB、個別株タブを開いたときのみ読み込み）、`data/market.json`（市場全体）。蓄積用：`data/short_history.json` `data/putcall_history.json` `data/etf_shares.json`。

各データ源は独立して取得し、失敗した場合は前回値を保持して「前回値」と表示します（1つのサイトの障害で全体が止まらない設計）。

## 業種分類の切り替え
既定は GICS です。Yahoo Finance の分類に切り替えたい場合は `.github/workflows/update.yml` の「Fetch prices and compute sector flows」ステップに次を追加します。
```yaml
        env:
          SECTOR_SCHEME: yahoo
```
Yahoo 分類は銘柄ごとに Yahoo への問い合わせが必要なため、初回は約500件の取得に10分前後かかります（`MAX_INFO_PER_RUN` 既定 600）。

## 更新履歴
- v1.2: 市場全体タブ（方向一致メーター、参考スコア、主要 ETF、市場内部の強弱、公表統計、セクター ETF 照合）と個別株タブ（検索・詳細・ランキング）を追加。FINRA 空売り比率、FRED、ICI、FINRA、CFTC、AAII、Cboe の取得を追加（`scripts/market.py`、`scripts/shortvol.py`）。株価取得期間を1年に延長（200日線）
- v1.1: 業種分類を GICS に統一（旧版は `MAX_INFO_PER_RUN=120` の制限により初回実行で 383 銘柄が GICS フォールバックになり、Yahoo 分類と混在していた）。指数から外れた銘柄のキャッシュ自動削除、Wikipedia 取得時の User-Agent 明示、発行済株式数の取得上限撤廃。

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
