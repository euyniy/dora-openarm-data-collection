#!/bin/bash
# Copyright 2026 Enactic, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# 管理者が最初に一度だけ実行するセットアップ。
#
#   ./launcher/install.sh
#
# 行うこと:
#   1. launcher.yaml の entry ごとにデスクトップショートカットを作成
#      (shortcut: false の entry はメニューからのみ選ぶ。収録するタスクは
#       アイコンをダブルクリックしたあとの画面で選ぶ)
#   2. WebXR 構成があれば、その TLS 証明書を作る
#      (WebXR は HTTPS でないと動かない。ヘッドセットが名前を引けるホスト名で
#       自己署名証明書を作り、ランチャーが画面に出す URL と揃える)
#   3. CAN 設定コマンドをパスワードなし sudo で実行できるようにする
#      (アルバイトがターミナルでパスワードを入力しないで済むようにするため)
#
# ホスト名は WEBXR_HOSTNAME で上書きできる (既定は <ホスト名>.local)。

set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${LAUNCHER_DIR}/.." && pwd)"
CONFIG="${LAUNCHER_DIR}/launcher.yaml"
PYTHON="${PYTHON:-/usr/bin/python3}"
APPLICATIONS_DIR="${HOME}/.local/share/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "${HOME}/Desktop")"
SUDOERS_FILE="/etc/sudoers.d/openarm-can"
TARGET_USER="${SUDO_USER:-$USER}"

if [ ! -f "${CONFIG}" ]; then
  echo "エラー: ${CONFIG} がありません" >&2
  exit 1
fi

if ! "${PYTHON}" -c "import yaml" 2>/dev/null; then
  echo "エラー: ${PYTHON} に PyYAML がありません。" >&2
  echo "  sudo apt install python3-yaml   を実行してください。" >&2
  exit 1
fi

mkdir -p "${APPLICATIONS_DIR}" "${DESKTOP_DIR}"

echo "== ショートカットを作成します =="

# launcher.yaml から "id<TAB>name<TAB>description" を取り出す。
entries="$("${PYTHON}" - "${CONFIG}" <<'PY'
import sys
import yaml

config = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
for entry in config.get("entries") or []:
    # shortcut: false の entry はアイコンを作らない (メニューからのみ)。
    if not entry.get("shortcut", True):
        continue
    print("\t".join([
        str(entry.get("id", "")),
        str(entry.get("name", entry.get("id", ""))),
        str(entry.get("description", "")),
    ]))
PY
)"

entry_count=0
keep_files=("openarm-data-collection-stop.desktop")
while IFS=$'\t' read -r id name description; do
  [ -z "${id}" ] && continue
  entry_count=$((entry_count + 1))
  desktop_file="${APPLICATIONS_DIR}/openarm-data-collection-${id}.desktop"
  keep_files+=("$(basename "${desktop_file}")")
  cat > "${desktop_file}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=${name}
Comment=${description}
Exec=${PYTHON} ${LAUNCHER_DIR}/openarm_launcher.py --entry ${id}
Icon=applications-science
Terminal=false
Categories=Science;Utility;
StartupNotify=true
EOF
  chmod +x "${desktop_file}"
  cp -f "${desktop_file}" "${DESKTOP_DIR}/"
  chmod +x "${DESKTOP_DIR}/$(basename "${desktop_file}")"
  # GNOME はこのフラグがないとダブルクリックを拒否する。
  if command -v gio >/dev/null 2>&1; then
    gio set "${DESKTOP_DIR}/$(basename "${desktop_file}")" \
      metadata::trusted true 2>/dev/null || true
  fi
  echo "  作成: ${name} (${id})"
done <<< "${entries}"

# entry が複数あるときは「どれを実施するか選ぶ」入口も置く。
if [ "${entry_count}" -gt 1 ]; then
  menu_file="${APPLICATIONS_DIR}/openarm-data-collection-menu.desktop"
  keep_files+=("$(basename "${menu_file}")")
  cat > "${menu_file}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=OpenArm データ収集（メニュー）
Comment=実施する収集を選んで開始します
Exec=${PYTHON} ${LAUNCHER_DIR}/openarm_launcher.py
Icon=applications-science
Terminal=false
Categories=Science;Utility;
StartupNotify=true
EOF
  chmod +x "${menu_file}"
  cp -f "${menu_file}" "${DESKTOP_DIR}/"
  chmod +x "${DESKTOP_DIR}/$(basename "${menu_file}")"
  if command -v gio >/dev/null 2>&1; then
    gio set "${DESKTOP_DIR}/$(basename "${menu_file}")" \
      metadata::trusted true 2>/dev/null || true
  fi
  echo "  作成: メニュー"
fi

# 固まったときに作業者が自分で全部止められる入口。
stop_file="${APPLICATIONS_DIR}/openarm-data-collection-stop.desktop"
cat > "${stop_file}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=データ収集を強制停止
Comment=収集に関するプロセスをすべて停止します
Exec=${PYTHON} ${LAUNCHER_DIR}/openarm_launcher.py --kill
Icon=process-stop
Terminal=false
Categories=Science;Utility;
StartupNotify=true
EOF
chmod +x "${stop_file}"
cp -f "${stop_file}" "${DESKTOP_DIR}/"
chmod +x "${DESKTOP_DIR}/$(basename "${stop_file}")"
if command -v gio >/dev/null 2>&1; then
  gio set "${DESKTOP_DIR}/$(basename "${stop_file}")" \
    metadata::trusted true 2>/dev/null || true
fi
echo "  作成: データ収集を強制停止"

# 設定から消えた entry のショートカットが残ると、押しても起動しない
# アイコンになるので消す。
for dir in "${APPLICATIONS_DIR}" "${DESKTOP_DIR}"; do
  for file in "${dir}"/openarm-data-collection-*.desktop; do
    [ -e "${file}" ] || continue
    base="$(basename "${file}")"
    keep=false
    for valid in "${keep_files[@]}"; do
      if [ "${base}" = "${valid}" ]; then
        keep=true
        break
      fi
    done
    if [ "${keep}" = false ]; then
      rm -f "${file}"
      echo "  削除: ${base}（設定にない古いショートカット）"
    fi
  done
done

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${APPLICATIONS_DIR}" 2>/dev/null || true
fi

# launcher.yaml の webxr 構成から "証明書<TAB>鍵" を取り出す (重複は除く)。
webxr_files="$("${PYTHON}" - "${CONFIG}" <<'PY'
import sys
import yaml

config = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
seen = []
for entry in config.get("entries") or []:
    webxr = entry.get("webxr")
    if not webxr:
        continue
    pair = (
        str(webxr.get("tls_certificate",
                      "nodes/dora-openarm-webxr/example/server.crt")),
        str(webxr.get("tls_key", "nodes/dora-openarm-webxr/example/server.key")),
    )
    if pair not in seen:
        seen.append(pair)
for pair in seen:
    print("\t".join(pair))
PY
)"

if [ -n "${webxr_files}" ]; then
  echo
  echo "== WebXR の TLS 証明書を用意します =="

  # ヘッドセットが名前を引けるホスト名。証明書の CN と、ランチャーが画面に
  # 出す URL のホスト名は同じでなければならない (違うと接続を拒否される)。
  # ドメイン付きのホスト名なら .local は足さない (ランチャー側と同じ判定)。
  default_host="$(hostname)"
  case "${default_host}" in
    *.*) ;;
    *) default_host="${default_host}.local" ;;
  esac
  WEBXR_HOSTNAME="${WEBXR_HOSTNAME:-${default_host}}"
  echo "  ホスト名: ${WEBXR_HOSTNAME}"

  # avahi-resolve は引けなくても終了コード 0 なので、出力で判定する
  # (引けたときだけ "<名前><TAB><アドレス>" を出す)。
  if command -v avahi-resolve >/dev/null 2>&1; then
    resolved="$(avahi-resolve --name "${WEBXR_HOSTNAME}" 2>/dev/null | cut -f2)"
    if [ -n "${resolved}" ]; then
      echo "  確認: ${WEBXR_HOSTNAME} は ${resolved} に解決されます"
      echo "        (ヘッドセットと同じネットワークのアドレスか確認してください)"
    else
      echo "  注意: ${WEBXR_HOSTNAME} を名前解決できません。ヘッドセットから" >&2
      echo "        つながらない場合は、WEBXR_HOSTNAME にヘッドセットから届く" >&2
      echo "        名前 (IP アドレスなど) を指定して実行し直してください。" >&2
    fi
  fi

  if ! command -v openssl >/dev/null 2>&1; then
    echo "エラー: openssl がありません。sudo apt install openssl を実行してください。" >&2
    exit 1
  fi

  while IFS=$'\t' read -r certificate key; do
    [ -z "${certificate}" ] && continue
    cert_path="${REPO_DIR}/${certificate}"
    key_path="${REPO_DIR}/${key}"
    cert_dir="$(dirname "${cert_path}")"
    host_file="${cert_path%.*}.host"
    prepare="${cert_dir}/prepare_tls.sh"

    if [ ! -x "${prepare}" ]; then
      echo "エラー: ${prepare} がありません。" >&2
      echo "  git submodule update --init nodes/dora-openarm-webxr を実行してください。" >&2
      exit 1
    fi

    # 作り直しが要るのは、無いとき・別のホスト名のとき・期限が近いとき。
    if [ -f "${cert_path}" ] && [ -f "${key_path}" ] &&
       [ "$(cat "${host_file}" 2>/dev/null)" = "${WEBXR_HOSTNAME}" ] &&
       openssl x509 -checkend 2592000 -noout -in "${cert_path}" >/dev/null 2>&1; then
      echo "  作成済みです（変更しません）: ${certificate}"
      continue
    fi

    echo "  作成: ${certificate}"
    if ! "${prepare}" "${WEBXR_HOSTNAME}" >/dev/null 2>&1; then
      echo "エラー: TLS 証明書を作成できませんでした" >&2
      exit 1
    fi
    # prepare_tls.sh は自分のディレクトリに server.crt / server.key を作る。
    # launcher.yaml が別の名前を指しているときはそこへ置き直す。
    if [ "${cert_path}" != "${cert_dir}/server.crt" ]; then
      mv -f "${cert_dir}/server.crt" "${cert_path}"
      mv -f "${cert_dir}/server.key" "${key_path}"
    fi
    # ランチャーはこのファイルを読んで、画面に出す URL のホスト名にする。
    echo "${WEBXR_HOSTNAME}" > "${host_file}"
    echo "  ヘッドセットで開く URL: https://${WEBXR_HOSTNAME}:8443/"
  done <<< "${webxr_files}"
fi

echo
echo "== CAN 設定のパスワードなし sudo を設定します =="
echo "  対象ユーザー: ${TARGET_USER}"

# entry を足して再実行するだけのときに、パスワードを聞かれないようにする。
if sudo -n -l /usr/bin/openarm-can-configure-socketcan-4-arms -fd >/dev/null 2>&1; then
  echo "  設定済みです（変更しません）"
  echo
  echo "完了しました。デスクトップのアイコンをダブルクリックして起動できます。"
  exit 0
fi

tmp_sudoers="$(mktemp)"
trap 'rm -f "${tmp_sudoers}"' EXIT
sed "s/@TARGET_USER@/${TARGET_USER}/" "${LAUNCHER_DIR}/openarm-can.sudoers" \
  > "${tmp_sudoers}"

if ! visudo -c -q -f "${tmp_sudoers}"; then
  echo "エラー: sudoers ファイルの検証に失敗しました" >&2
  exit 1
fi

if sudo install -m 0440 -o root -g root "${tmp_sudoers}" "${SUDOERS_FILE}"; then
  echo "  導入: ${SUDOERS_FILE}"
else
  echo "エラー: ${SUDOERS_FILE} を導入できませんでした" >&2
  exit 1
fi

if sudo -n -l /usr/bin/openarm-can-configure-socketcan-4-arms -fd >/dev/null 2>&1; then
  echo "  確認: パスワードなしで CAN 設定コマンドを実行できます"
else
  echo "  注意: まだパスワードなしで実行できません。設定内容を確認してください。" >&2
fi

echo
echo "完了しました。デスクトップのアイコンをダブルクリックして起動できます。"
