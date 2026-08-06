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
#   2. CAN 設定コマンドをパスワードなし sudo で実行できるようにする
#      (アルバイトがターミナルでパスワードを入力しないで済むようにするため)

set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
    print("\t".join([
        str(entry.get("id", "")),
        str(entry.get("name", entry.get("id", ""))),
        str(entry.get("description", "")),
    ]))
PY
)"

entry_count=0
while IFS=$'\t' read -r id name description; do
  [ -z "${id}" ] && continue
  entry_count=$((entry_count + 1))
  desktop_file="${APPLICATIONS_DIR}/openarm-data-collection-${id}.desktop"
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

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${APPLICATIONS_DIR}" 2>/dev/null || true
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
