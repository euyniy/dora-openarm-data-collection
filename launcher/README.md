# データ収集ランチャー

ターミナルを使わずにデータ収集を開始するためのランチャーです。作業者は
デスクトップのショートカットをダブルクリックするだけで、

1. 画面に出た一覧から、収録するタスク（metadata）を選ぶ
2. WebXR 構成なら TLS 証明書の確認と、ヘッドセットで開く URL の表示
3. CAN インタフェースの設定（`sudo openarm-can-configure-socketcan-4-arms -fd`）
4. 必要なら `uv run dora build <dataflow> --uv`
5. `uv run dora run <dataflow> --uv`
6. タスク画面（`dora-openarm-data-collection-ui`）へ自動で切り替え

までが実行されます。途中で失敗した場合は、ターミナルではなく画面に赤字で
エラー内容が表示されます。

## 構成

| ファイル | 用途 |
| --- | --- |
| `launcher.yaml` | 構成（entry）と、その中で選べるタスク（`tasks`）の定義 |
| `openarm_launcher.py` | ランチャー本体（標準ライブラリ + PyYAML のみ） |
| `install.sh` | ショートカット作成、WebXR の TLS 証明書作成、sudo 設定（管理者が一度だけ実行） |
| （生成）`データ収集を強制停止` | `--kill` を呼ぶショートカット |
| `openarm-can.sudoers` | `/etc/sudoers.d/openarm-can` の雛形 |
| [`../view-webxr.yaml`](../view-webxr.yaml) | ヘッドセットに頭部カメラをどう出すか（WebXR 構成） |
| （生成）`nodes/dora-openarm-webxr/example/server.{crt,key,host}` | WebXR の TLS 証明書と、発行したホスト名 |

## 管理者向け: 初期セットアップ

```console
$ ./launcher/install.sh
```

行われること:

- `launcher.yaml` の entry ごとにデスクトップショートカットを作成
  （entry が複数あるときは「メニュー」ショートカットも作成。
  `shortcut: false` の entry はアイコンを作らず、メニューからのみ選べます。
  設定から消えた entry の古いショートカットは削除されます）
- WebXR 構成（entry に `webxr:` があるもの）の TLS 証明書を作成
  （下の「WebXR 構成」を参照。既に同じホスト名の有効な証明書があるときは
  作り直しません）
- `/etc/sudoers.d/openarm-can` を導入し、CAN 設定コマンドだけを
  パスワードなしで実行できるようにする（作業者がパスワードを打たずに
  済むようにするため。許可するのは
  `/usr/bin/openarm-can-configure-socketcan-4-arms -fd` の 1 つだけ）

`dora` は `uv run dora ... --uv` で実行します。`uv` が見つからない場合は
`launcher.yaml` の `uv` に実行ファイルのパスを書いてください
（例: `uv: /home/openarm/.local/bin/uv`）。省略した場合は
`<venv>/bin/uv` → リポジトリ直下の `.venv/bin/uv` → `~/.local/bin/uv` →
`PATH` 上の `uv` の順に探します。

### 収録データの保存先

`launcher.yaml` の `dataset.root` に書きます。1 セッション 1 フォルダで
`<root>/<日付>/<日時>/` に保存されます。

```yaml
dataset:
  root: "/hdd_data"          # 例: /hdd_data/2026-08-06/2026-08-06_09-12-30/
  date_format: "%Y-%m-%d"
  session_format: "%Y-%m-%d_%H-%M-%S"
```

ランチャーが起動時にこのディレクトリを作り、dataflow の recorder ノードの
`DIRECTORY` / `NAME` を差し替えます（dataflow を直接編集する必要はありません）。
作成・書き込みができないとき（ディスクが未マウント等）は収集を始めずに
画面へ日本語でエラーを出します。

優先順位は `entry の dataset_root` > 環境変数 `DATASET_ROOT` > `dataset.root` です。
ダミー収集の entry は `dataset_root: dummy_data` にしてあるので、動作確認の
データが本番の保存先に混ざりません。

### 構成（entry）とタスク（metadata）

**entry = 何で操作するか（どの dataflow）**、**task = 何を収録するか（どの
metadata）** です。ショートカットは entry ごとに 1 つだけ作られ、タスクは
起動後の画面で作業者が選びます。タスクを増やしてもアイコンは増えません。

```yaml
entries:
  - id: ker
    name: "KER データ収集"
    dataflow: "dataflow-ker.yaml"
    can_setup: true
    tasks:
      - id: standard
        name: "標準タスク"
        description: "metadata.yaml のタスク一式"
        metadata: "metadata.yaml"
      - id: task-b
        name: "タスクB"
        metadata: "metadata_task_b.yaml"
```

- タスクを増やす: `tasks` に 1 項目足すだけ（`install.sh` の再実行は不要）
- 構成を増やす: `entries` に足して `install.sh` を再実行（アイコンが増える）
- WebXR の構成には `webxr:` を書く（下の「WebXR 構成」を参照）
- `tasks` を書かなければ、従来どおり entry 直下の `metadata` で起動します
- タスク側には `metadata` のほか `dataset_root` などの entry と同じ項目も
  書けます（書いた項目が entry の値を上書きします）

dataflow 側の `METADATA_FILE` と選んだタスクの `metadata` が違うときは、
`METADATA_FILE` を差し替えた `.launcher-<entry>-<task>.yaml` を自動生成して
実行します（このファイルは `.gitignore` 済み）。`dora build` の結果は
dataflow 単位で覚えるので、同じ構成でタスクを変えても再ビルドは走りません。

### WebXR 構成

`webxr` entry は VR entry と同じ実機構成ですが、Quest 専用アプリと UDP で
つなぐ代わりに、ヘッドセットのブラウザから WebXR でつなぎます
（`dataflow-webxr.yaml`）。作業者側の手順が 1 つ増えます。

- 作業者は **ヘッドセットのブラウザ** で `https://<ホスト名>.local:8443/` を
  開き、「Start」を押す。この URL は起動画面（タスク選択画面と「起動中…」の
  画面）に大きく表示されます
- 自己署名証明書なので、初回はブラウザが警告を出します。「詳細設定」から
  続行してください

WebXR は HTTPS でないと動かないので、証明書が要ります。`install.sh` が
`nodes/dora-openarm-webxr/example/` に自己署名証明書を作り、使ったホスト名を
`server.host` に記録します。ランチャーはこれを読んで、証明書と食い違わない
URL を画面に出します。

```console
$ ./launcher/install.sh                              # <ホスト名>.local で作成
$ WEBXR_HOSTNAME=192.168.1.20 ./launcher/install.sh  # 名前が引けないとき
```

`.local` がヘッドセットから引けるかは、次のコマンドで確認できます。

```console
$ avahi-resolve --name $(hostname).local
```

証明書が無いまま起動しようとすると、`dora` のスタックトレースではなく
「WebXR の TLS 証明書がありません」と画面に出て止まります。

`entry` に書ける項目は次のとおりです（すべて省略できます）。

```yaml
  - id: webxr
    dataflow: "dataflow-webxr.yaml"
    webxr:
      # 画面に出す URL のホスト名。省略すると server.host →
      # <この PC のホスト名>.local の順に決める。
      hostname: null
      port: 8443
      tls_certificate: "nodes/dora-openarm-webxr/example/server.crt"
      tls_key: "nodes/dora-openarm-webxr/example/server.key"
```

ヘッドセットに頭部カメラをどう出すかは、リポジトリ直下の
[`view-webxr.yaml`](../view-webxr.yaml) で決めます。既定は「右目の画像を
1 枚、部屋に固定」で、カメラの校正値がいらないためどの頭部カメラでも
そのまま動きます。左右 2 枚の立体視にするには、頭部ステレオカメラの
校正値が必要です（同ファイルのコメント参照）。

ヘッドセットの接続確認だけをしたいときは、実機を使わない
`webxr-mujoco` entry（`dataflow-webxr-mujoco.yaml`、CAN 設定なし、保存先は
`dummy_data/`）をメニューから選んでください。証明書とネットワークと
ヘッドセットの設定を、セルを止めずに確認できます。

### 実機がない PC での動作確認

`launcher.yaml` には動作確認用の `dummy` entry を入れてあります
（`dataflow_dummy.yaml`、CAN 設定なし、保存先は `dummy_data/`）。
デスクトップにアイコンは作らない設定（`shortcut: false`）なので、
「OpenArm データ収集（メニュー）」から選んでください。実機がなくても
タスクの選択から画面表示までを確認できます。

## 作業者向け: 使い方

1. デスクトップのショートカット（例: 「KER データ収集」「VR データ収集」
   「WebXR データ収集」）をダブルクリック
2. 出てきた一覧から、これから収録するタスクの「開始」を押す
3. 「起動中…」の画面が出るので待つ（初回は数分かかることがあります）
4. WebXR のときは、画面に出ている URL をヘッドセットのブラウザで開き、
   「Start」を押す（証明書の警告は「詳細設定」から続行）
5. 自動でタスク画面に切り替わったら「スタート」を押して収録開始
6. 赤い画面が出たら、書かれている内容を担当者に伝える（「再試行」で再起動）

「再試行」「再開」は、そのとき選んでいたタスクのまま起動し直します。
別のタスクに変えるときは「メニューに戻る」から選び直してください。

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
- 停止中・エラー: タスクの一覧を開く（選び直して開始できる）

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
| `--entry <id>` | 対象の entry。タスク選択画面を開く（複数 entry で省略するとメニュー表示） |
| `--task <id>` | 収録するタスク。`--entry` と併せて指定すると画面を出さずに起動する |
| `--config <path>` | `launcher.yaml` のパス |
| `--port <port>` | 待ち受けポート（既定は `launcher.yaml` の `port`） |
| `--no-browser` | ブラウザを自動で開かない |
| `--kill` | 収集に関するプロセスをすべて停止して終了する |

エンドポイント:

| パス | 内容 |
| --- | --- |
| `GET /` | 起動状態の画面（待機中はメニュー、実行中はタスク画面へ転送） |
| `GET /entry?id=<id>` | その構成のタスク選択画面 |
| `GET /menu` | すべての構成とタスクの一覧 |
| `GET /status` | 状態の JSON（タスク画面が接続断を検知したときに参照する） |
| `GET /log` | `dora` の出力ログ全文（`~/.local/state/openarm-launcher/logs/`） |
| `POST /start` | 起動（フォーム `entry=<id>&task=<id>`。タスク未選択なら選択画面へ戻す） |
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
