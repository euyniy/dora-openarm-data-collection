# データ収集ランチャー

ターミナルを使わずにデータ収集を開始するためのランチャーです。作業者は
デスクトップのショートカットをダブルクリックするだけで、

1. CAN インタフェースの設定（`sudo openarm-can-configure-socketcan-4-arms -fd`）
2. 必要なら `uv run dora build <dataflow> --uv`
3. `uv run dora run <dataflow> --uv`
4. タスク画面（`dora-openarm-data-collection-ui`）へ自動で切り替え

までが実行されます。途中で失敗した場合は、ターミナルではなく画面に赤字で
エラー内容が表示されます。

## 構成

| ファイル | 用途 |
| --- | --- |
| `launcher.yaml` | 起動する dataflow / metadata の組（entry）の定義 |
| `openarm_launcher.py` | ランチャー本体（標準ライブラリ + PyYAML のみ） |
| `install.sh` | ショートカット作成と sudo 設定（管理者が一度だけ実行） |
| （生成）`データ収集を強制停止` | `--kill` を呼ぶショートカット |
| `openarm-can.sudoers` | `/etc/sudoers.d/openarm-can` の雛形 |

## 管理者向け: 初期セットアップ

```console
$ ./launcher/install.sh
```

行われること:

- `launcher.yaml` の entry ごとにデスクトップショートカットを作成
  （entry が複数あるときは「メニュー」ショートカットも作成）
- `/etc/sudoers.d/openarm-can` を導入し、CAN 設定コマンドだけを
  パスワードなしで実行できるようにする（作業者がパスワードを打たずに
  済むようにするため。許可するのは
  `/usr/bin/openarm-can-configure-socketcan-4-arms -fd` の 1 つだけ）

`dora` は `uv run dora ... --uv` で実行します。`uv` が見つからない場合は
`launcher.yaml` の `uv` に実行ファイルのパスを書いてください
（例: `uv: /home/openarm/.local/bin/uv`）。省略した場合は
`<venv>/bin/uv` → リポジトリ直下の `.venv/bin/uv` → `~/.local/bin/uv` →
`PATH` 上の `uv` の順に探します。

### メタデータが複数ある場合

`launcher.yaml` の `entries` に追加して `install.sh` を再実行すると、
入口（ショートカット）が増えます。

```yaml
entries:
  - id: ker
    name: "KER データ収集"
    dataflow: "dataflow-ker.yaml"
    metadata: "metadata.yaml"
  - id: ker-task-b
    name: "KER データ収集 (タスクB)"
    dataflow: "dataflow-ker.yaml"
    metadata: "metadata_task_b.yaml"
```

dataflow 側の `METADATA_FILE` と entry の `metadata` が違うときは、
`METADATA_FILE` を差し替えた `.launcher-<id>.yaml` を自動生成して実行します
（このファイルは `.gitignore` 済み）。

### 実機がない PC での動作確認

`launcher.yaml` には動作確認用の `dummy` entry を入れてあります
（`dataflow_dummy.yaml`、CAN 設定なし）。ショートカット
「ダミー収集（動作確認用）」から、実機がなくても画面と操作を確認できます。

## 作業者向け: 使い方

1. デスクトップのショートカット（例: 「KER データ収集」）をダブルクリック
2. 「起動中…」の画面が出るので待つ（初回は数分かかることがあります）
3. 自動でタスク画面に切り替わったら「スタート」を押して収録開始
4. 赤い画面が出たら、書かれている内容を担当者に伝える（「再試行」で再起動）

ターミナルの操作は不要です。PC の再起動後や USB を挿し直した後の CAN 設定も
ランチャーが自動で行います。

止まらなくなったとき（画面が固まる、起動し直しても始まらない）は、
デスクトップの「データ収集を強制停止」をダブルクリックしてください。
収集に関するプロセスをすべて止めてから（ランチャー自身も終了します）、
結果をダイアログで知らせます。もう一度収集のショートカットを
ダブルクリックすれば、まっさらな状態で始められます。

起動画面の「すべて強制停止」ボタンでも収集を止められます。こちらは
画面を残したまま「停止中」に戻るので、エラーが出て先に進めないときは
このボタン → 「再開」または「メニューに戻る」で復帰できます。

ショートカットをもう一度ダブルクリックすると:

- 収集中: 画面を開くだけ（実行中の収集はそのまま）
- 停止中・エラー: もう一度起動し直す（「再試行」と同じ）

起動そのものに失敗した場合（設定ファイルが壊れている等、画面を出す前の失敗）は
デスクトップにエラーダイアログを表示し、`~/.local/state/openarm-launcher/launcher-crash.log`
に記録します。

## 動作確認（開発者向け）

ランチャーだけを手で起動する場合:

```console
$ python3 launcher/openarm_launcher.py --entry ker
```

| オプション | 意味 |
| --- | --- |
| `--entry <id>` | 起動する entry。省略時は entry が 1 つならそれを起動、複数ならメニュー表示 |
| `--config <path>` | `launcher.yaml` のパス |
| `--port <port>` | 待ち受けポート（既定は `launcher.yaml` の `port`） |
| `--no-browser` | ブラウザを自動で開かない |
| `--kill` | 収集に関するプロセスをすべて停止して終了する |

エンドポイント:

| パス | 内容 |
| --- | --- |
| `GET /` | 起動状態の画面（実行中はタスク画面へ転送） |
| `GET /status` | 状態の JSON（タスク画面が接続断を検知したときに参照する） |
| `GET /log` | `dora` の出力ログ全文（`~/.local/state/openarm-launcher/logs/`） |
| `POST /start` | entry の起動（フォーム `entry=<id>`） |
| `POST /stop` | 実行中の dataflow の停止 |
| `POST /cleanup` | 停止 + 残っているプロセスの強制終了 |

実行中に `dora` の出力からエラー行を検出すると、タスク画面の
`POST /api/error` に転送して画面上に赤字で表示します。

起動時（`cleanup_before_start: true`）と `--kill` / `POST /cleanup` では、
このリポジトリを作業ディレクトリにしている dora のプロセス
（`dora`、`dora-*`、`opencv-video-capture`）だけを選んで停止します。
プログラム名で判定しているので、パスに `dora-openarm` を含むだけの
シェルやランチャー自身は対象になりません。

停止時は `dora run` のプロセスグループに SIGINT → SIGTERM → SIGKILL を送り、
さらに同じセッションに残ったノードプロセスも終了させます（残ると次回起動時に
ポート 8000 を掴んだままになるため）。`pkill -f openarm_launcher.py` でも
ランチャー本体は確実に終了します。
