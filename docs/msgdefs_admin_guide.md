# 定義パック作成手順（管理者向け）

pilot-auto.x1.eve のバージョンが変わったら、定義パック（`msgdefs_<version>.json`）を作り直して配布します。
基本は **Claude Code に依頼** して作成します。手作業でも同じ手順で作れます。

## 0. 必要なもの

- tier4/pilot-auto.x1.eve と、`autoware.repos` に含まれる private リポジトリ（`eve-autonomy/*_msgs`、
  `tier4/*_msgs`、`tier4/g30esli_interface` など）を読める GitHub アカウント
- Python 3.10 以上、git
- このリポジトリ（bag2mcap）

## 1. Claude Code に依頼する場合（推奨）

Claude Code（ローカル、または bag2mcap と pilot-auto.x1.eve を選んだクラウドセッション）で、次のように依頼します。
`<...>` は書き換えてください。

```
bag2mcap の docs/msgdefs_admin_guide.md の手順に従って、定義パックを作成してください。
- 対象: tier4/pilot-auto.x1.eve の <タグ or コミット>（例: v4.5.2）
- 作成者: <氏名>
- 出力: msgdefs/msgdefs_<バージョン>.json
作成レポートのエラー（解析失敗・依存型不足）は、原因を調べて解消してください。
解消できないものは一覧で報告してください。
完了したら、前回の定義パックとの差分（追加・削除・変更された型）も報告してください。
```

> クラウドセッションでは private リポジトリを個別に追加する必要があります。取得に失敗したリポジトリは
> Claude からの報告に従って追加してください。

## 2. 手作業で行う場合

### 2.1 準備

```bat
git clone git@github.com:<owner>/bag2mcap.git
cd bag2mcap
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

### 2.2 ソースの取得

```bat
git clone git@github.com:tier4/pilot-auto.x1.eve.git work\pilot-auto
git -C work\pilot-auto checkout <タグ or コミット>
python tools\fetch_sources.py --repos work\pilot-auto\autoware.repos --out work\src
```

- 続けて、apt で入るため autoware.repos に無いメッセージパッケージ（geographic_msgs, can_msgs, ublox_msgs,
  map_msgs, grid_map_msgs）を取得します。

  ```bat
  python tools\fetch_sources.py --repos tools\extra_sources.repos --out work\src
  ```

- SSH 鍵を使わない場合は `--https` を付けます（https でのアクセス権が必要です）。
- launch / パラメータ / 車両 description 系のリポジトリ（autoware_launch.x1.eve, individual_params, *_params.eve,
  ymc_golfcart_*_description など）はメッセージを含まないため、取得に失敗しても問題ありません。
- 時間を短くしたい場合は、`--only msgs universe autoware/common g30esli` のようにメッセージを含むリポジトリに絞れます。

### 2.3 定義パックの作成

```bat
python tools\build_msgdefs.py build --src work\src --out msgdefs\msgdefs_<バージョン>.json ^
    --repository tier4/pilot-auto.x1.eve --ref <タグ or コミット> --generated-by <氏名>
```

`msgdefs\msgdefs_<バージョン>_report.txt` にレポートが出ます。

| レポートの表示 | 対応 |
|---|---|
| `解析失敗: ...idl` | rosbags が解釈できない IDL 構文。`tools/build_msgdefs.py` の前処理（`strip_idl_annotations` / `array_members_to_typedef`）を追加・修正する |
| `定義の生成に失敗 ... Type 'xxx_msgs/msg/Yyy' is unknown` | 依存先のパッケージが不足している。そのパッケージのソース（例：`geographic_msgs` なら ros-geographic-info の humble ブランチ）を取得し、`--src` を追加して再実行する |
| `型の重複（内容が異なる）` | 同じ型名が複数のリポジトリにある。どちらが正しいか確認する（先に見つかった方を採用している） |

### 2.4 確認

確認は 2 段階で行います。

**(1) ソースとの突き合わせ（audit）**：ソースが参照しているメッセージパッケージ（C++ の include、Python の import、
package.xml の `*_msgs` / `*_interfaces` 依存）が定義パックにあるか確認します。srv / action しか持たないパッケージは自動で除外されます。

```bat
python tools\build_msgdefs.py audit --pack msgdefs\msgdefs_<バージョン>.json --src work\src ^
    --ignore test_msgs tf2_geometry_msgs tf2_sensor_msgs
```

不足が出たら、そのパッケージのソースを `tools\extra_sources.repos` に追加して作り直します。
`autoware_adapi_v1_msgs` は driving_log_replayer（シミュレータ）だけが参照しており、実車の bag には現れないため対象外で構いません。

**(2) 実際の bag との突き合わせ（check）** ← 最終確認
apt で入るノードが内部で使う型（例：rosbridge_server の `/connected_clients` = `rosbridge_msgs/msg/ConnectedClients`）は
ソースからは見つけられません。実車の bag に記録されている型と突き合わせて確認します。
bag 本体が大きくて手元に無い場合は、**bag フォルダ内の `metadata.yaml`（数十 KB）だけ** で確認できます。

```bat
python tools\build_msgdefs.py check --pack msgdefs\msgdefs_<バージョン>.json --bag <bag フォルダ or metadata.yaml> [--bag ...]
```

その後、bag2mcap で変換し、Lichtblick で主要トピック（点群、車速、診断、自己位置など）が表示されること、
Alerts に「Message encoding cdr with no encoding is not supported」が出ないことを確認します
（このエラーは、そのトピックの型が定義パックに無いことを示します）。

### 2.5 配布

1. GitHub Releases の該当リリース（または新しいリリース）に `msgdefs_<バージョン>.json` を添付する
2. 利用者は `bag2mcap.exe` と同じフォルダ、または `msgdefs\` フォルダに置く（自動で選ばれる）
3. 社内の連絡チャンネルで、対応する pilot-auto のバージョンを周知する

> 定義パックは private リポジトリの内容を含むため **社外秘** です。Git にはコミットしないでください（`.gitignore` 済み）。

## 3. 定義パックの命名規則

`msgdefs_<pilot-auto のバージョン>[_<連番>].json`（例：`msgdefs_v4.5.2.json`、`msgdefs_v4.5.2_2.json`）

アプリは名前順で最後のファイルを自動選択します。複数のバージョンを置く場合は、利用者が「参照...」で選び直してください。
