# bag2mcap 仕様書 v1.0

## 1. 目的

評価試験・市場不具合解析で取得した rosbag2（sqlite3 / `.db3`）を、ROS 2 をインストールしていない Windows PC で
MCAP に変換し、Lichtblick で再生・解析できるようにする。

## 2. 前提・決定事項

| 項目 | 決定内容 |
|---|---|
| 対象ソフトウェア | pilot-auto.x1.eve（ROS 2 Humble / rosbag2 0.15.5）。将来アップストリーム追従予定 |
| 利用者 | eve autonomy 全社（評価試験チーム、開発部門ほか） |
| 利用ツール | Lichtblick |
| 入力の規模 | 1 bag あたり 2GB 程度。点群トピックを含む（カメラ画像は含まない） |
| UI | 簡素な GUI（1 画面）。補助としてコマンドライン版あり |
| メッセージ定義 | 外部ファイル（定義パック）から読み込む。pilot-auto の更新ごとに管理者が Claude を使って作成する |
| 記録時の圧縮 | 使用状況は不明のため、非圧縮・ファイル単位 zstd・メッセージ単位 zstd の全てに対応する |
| 配布 | 社内ルール特になし。zip（exe 一式）で配布 |

## 3. 全体構成

```
管理者 + Claude                                利用者（全社）
autoware.repos の各リポジトリを指定版で取得
  → tools/build_msgdefs.py                     bag2mcap.exe（変換アプリ）
  → msgdefs_<version>.json（定義パック） ──配布──▶   + msgdefs_<version>.json
```

- 変換アプリはメッセージ定義を内蔵しない。pilot-auto のバージョンアップでは定義パックの差し替えのみで対応する。
- 定義パックは依存型まで展開済みの完全なスキーマを持つため、アプリ側で依存解決はしない。

## 4. 定義パック仕様（format_version = 1）

```json
{
  "format_version": 1,
  "ros_distro": "humble",
  "source": {
    "repository": "tier4/pilot-auto.x1.eve",
    "ref": "<タグまたはコミット>",
    "generated_at": "2026-10-09T10:00:00+09:00",
    "generated_by": "<作成者>",
    "note": "<任意>",
    "custom_type_count": 0,
    "standard_type_count": 0
  },
  "types": {
    "<pkg>/msg/<Type>": { "encoding": "ros2msg", "definition": "<依存型を展開した定義全文>" }
  }
}
```

- `definition` は rosbag2_storage_mcap と同じ形式（本体の後に `====...====` 区切りで `MSG: pkg/Type` の依存型を連結）。
- `encoding` は `ros2msg`（標準）。将来のために `ros2idl` も受け付ける。
- `.idl` しかない型（例：autoware_auto_msgs）は、作成時に `ros2msg` 形式へ変換する。
- ROS 2 標準メッセージ（std_msgs, sensor_msgs, geometry_msgs, diagnostic_msgs など 150 型）も同梱する。
- 型名は `pkg/msg/Type` に正規化する（`pkg/Type` で検索しても見つかる）。
- 定義パックは private リポジトリの内容を含むため社外秘とする。Git では管理せず、Releases で配布する。

## 5. 変換仕様

### 5.1 入力

| 入力 | 動作 |
|---|---|
| bag フォルダ（metadata.yaml あり） | `relative_file_paths` の順に全分割ファイルを変換。圧縮設定も metadata.yaml に従う |
| bag フォルダ（metadata.yaml なし） | 記録中断などを想定。フォルダ内の `*.db3` / `*.db3.zstd` を自然順で変換し、警告を出す |
| `.db3` 単体 / `metadata.yaml` | `.db3` はそのファイルのみ、`metadata.yaml` はそのフォルダを変換 |

- db3 は読み取り専用（`mode=ro&immutable=1`）で開き、**入力は一切変更しない**（`-wal` が残っている場合のみ `mode=ro`）。
- ファイル単位圧縮（`.db3.zstd`）は出力先の一時フォルダに展開し、終了後に削除する。
- メッセージ単位圧縮は、zstd のマジックナンバーで判定して解凍する（metadata.yaml が無くても対応）。
- 分割ファイルごとに topic の id が異なっても、(トピック名, 型) で同じチャンネルにまとめる。
- 読めない（破損した）分割ファイルはスキップし、警告を出して残りを変換する。読み込み途中で破損を検出した場合は、読めた分までを出力する。

### 5.2 出力（MCAP）

| 項目 | 内容 |
|---|---|
| ファイル名 | `<bag フォルダ名>.mcap`（db3 単体の場合は `<db3 名>.mcap`） |
| 出力先 | 入力と同じ場所（既定）または指定フォルダ |
| profile | `ros2` |
| メッセージ | CDR をデコードせずにそのまま格納（`message_encoding=cdr`）。log_time / publish_time = 記録時刻 |
| スキーマ | db3 内の定義（Iron 以降）→ 定義パック → なし（schema_id=0）の順に採用 |
| チャンネルメタデータ | `offered_qos_profiles`（rosbag2_storage_mcap と同じキー） |
| メタデータレコード | `rosbag2`（元の metadata.yaml）、`bag2mcap`（バージョン、入力、定義パック、変換日時） |
| 圧縮 | zstd（既定）/ lz4 / なし。チャンクサイズ 4MB、インデックス・サマリーあり |
| 書き込み | `<名前>.mcap.part` に書き込み、検証に成功したらリネームする。失敗・中止時は削除する |
| 上書き | 既定では既存ファイルを上書きしない（「既存ファイルを上書き」をチェックした場合のみ上書き） |

### 5.3 検証とログ

- 変換後に MCAP を読み直し、チャンネルごとのメッセージ数を書き込み数と照合する（不一致ならエラー）。
- 入力（db3 の件数）と出力の件数を比較し、不一致なら警告する。
- `<名前>_bag2mcap_log.txt`（UTF-8 BOM 付き）を出力先に保存する。内容は、結果、警告、定義が見つからない型、トピックごとの件数。

## 6. GUI 仕様

1 画面の構成：定義パック選択、入力リスト（フォルダ追加・db3 追加・ドラッグ&ドロップ・削除）、
出力先（入力と同じ / 指定フォルダ）、MCAP 圧縮、上書き、変換開始 / 中止 / 出力先を開く、進捗バー、ログ表示。

- 定義パックは「前回使用したもの」→「exe と同じ場所または `msgdefs\` にある `msgdefs*.json`（名前順で最後）」の順に自動選択する。
- 定義パック未選択で変換しようとすると確認ダイアログを表示する。
- 複数 bag を順番に変換する。1 件が失敗しても残りは続行する。
- 設定は `%APPDATA%\bag2mcap\settings.json` に保存する。

## 7. 非機能要件

| 項目 | 内容 |
|---|---|
| 動作環境 | Windows 10 / 11（64bit）。管理者権限・ROS 2・Python のインストール不要 |
| 性能 | ストリーム処理（メモリはファイルサイズに比例しない）。参考値：474MB・30 万メッセージを 3.4 秒（Linux 開発機） |
| パス | 日本語・空白を含むパス、ネットワークドライブに対応 |
| 配布形態 | PyInstaller（onedir）で作成した exe 一式の zip。GitHub Actions（windows-latest）でテストとビルドを行う |
| 依存 OSS | mcap (MIT), zstandard (BSD), lz4 (BSD), PyYAML (MIT), tkinterdnd2 (MIT), Python (PSF) |

## 8. 将来のアップストリーム追従

- Iron 以降の db3 は `message_definitions` テーブルに定義を持つため、定義パックなしでも変換できる（実装済み）。
- Jazzy 以降は rosbag2 の既定の保存形式が MCAP になるため、本ツールは過去の db3 の変換用途になる見込み。
- 型名の変更（例：`autoware_auto_*_msgs` → `autoware_*_msgs`）は定義パックの再作成で対応する。
- 定義パック作成スクリプトは `--ros-distro jazzy` で Jazzy の標準メッセージに切り替えられる。

## 9. 制約・既知の事項

- 定義パックに無い型は、スキーマなしで出力する（データは保持するが Lichtblick ではデコードできない）。
- 定義パックの作成には、autoware.repos に含まれる private リポジトリへのアクセス権が必要。
- rosbags 内蔵の標準メッセージに含まれないパッケージ（例：`geographic_msgs`）を参照する型は、そのパッケージのソースも
  定義パック作成時に追加する必要がある（作成レポートに表示される）。
