#!/bin/bash
# 琴房监控 / Piano Room Monitor  -  double-click this file to start.
# It first CHECKS what is already on this Mac and installs ONLY what is missing.

cd "$(dirname "$0")" || exit 1
ENV_DIR="$HOME/.piano-monitor"
VENV="$ENV_DIR/venv"
export PATH="$HOME/.local/bin:$PATH"
export UV_NO_MODIFY_PATH=1            # never touch the user's shell settings

# places where a real Python / Chrome may already be
SYS_PYTHONS=(/opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3)
CHROME_APPS=("/Applications/Google Chrome.app" "$HOME/Applications/Google Chrome.app")

clear 2>/dev/null || true
echo "=============================================="
echo "   琴房监控  Piano Room Monitor"
echo "=============================================="

ok()   { echo "  ✓ $1"; }
miss() { echo "  ✗ $1"; }

wait_close() {
  echo
  read -r -p "按回车键关闭窗口 / Press Enter to close this window... " _
}

fail() {
  echo
  echo "!!! $1"
  echo "    请检查网络后重新双击本文件 / Check your internet, then double-click this file again."
  wait_close
  exit 1
}

# a working Python 3.9 or newer?
py_ok() {
  [ -n "$1" ] && [ -x "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

# Apple's /usr/bin/python3 only works after the Command Line Tools are installed;
# without them it would pop up an installer window, so it is only used when they exist.
if xcode-select -p >/dev/null 2>&1; then SYS_PYTHONS+=(/usr/bin/python3); fi

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then ok "uv 已有 / uv already installed"; return 0; fi
  miss "uv 没有，正在安装 / uv missing, installing ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || return 1
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1
}

echo
echo "正在检查电脑上已有的软件 / Checking what is already installed ..."

# ---------------------------------------------------------------- Python + Playwright
RUN_PY=""

# 1) a Python that already has Playwright -> use it, install nothing
for c in "$VENV/bin/python" "${SYS_PYTHONS[@]}"; do
  if py_ok "$c" && "$c" -c "import playwright" >/dev/null 2>&1; then RUN_PY="$c"; break; fi
done

if [ -n "$RUN_PY" ]; then
  ok "Python + Playwright 已有 / already installed ($RUN_PY)"
else
  # 2) own environment from an earlier run?
  if py_ok "$VENV/bin/python"; then
    ok "Python 已有 / Python already installed (own copy)"
    RUN_PY="$VENV/bin/python"
  else
    # 3) a Python somewhere on this Mac? -> make a small private environment from it
    BASE_PY=""
    for c in "${SYS_PYTHONS[@]}"; do
      if py_ok "$c"; then BASE_PY="$c"; break; fi
    done
    mkdir -p "$ENV_DIR"
    rm -rf "$VENV"
    if [ -n "$BASE_PY" ] && "$BASE_PY" -m venv "$VENV" >/dev/null 2>&1; then
      ok "Python 已有，无需安装 / Python found, nothing to install ($BASE_PY)"
    else
      # 4) no Python at all -> install a private copy (no password needed)
      miss "没有找到 Python，正在安装 / Python not found, installing ..."
      rm -rf "$VENV"
      ensure_uv || fail "uv 安装失败 / Could not install uv."
      uv venv --seed --python 3.12 "$VENV" || fail "Python 安装失败 / Could not install Python."
    fi
    RUN_PY="$VENV/bin/python"
  fi

  if "$RUN_PY" -c "import playwright" >/dev/null 2>&1; then
    ok "Playwright 已有 / already installed"
  else
    miss "没有 Playwright，正在安装 / Playwright missing, installing ..."
    if ! "$RUN_PY" -m pip install --disable-pip-version-check playwright; then
      echo "官方源失败，改用镜像重试 / Trying a mirror ..."
      "$RUN_PY" -m pip install --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple playwright \
        || fail "Playwright 安装失败 / Could not install Playwright."
    fi
  fi
fi

# ---------------------------------------------------------------- browser
CHROME_FOUND=""
for a in "${CHROME_APPS[@]}"; do
  if [ -d "$a" ]; then CHROME_FOUND="$a"; break; fi
done

if [ -n "$CHROME_FOUND" ]; then
  ok "浏览器：Google Chrome 已有 / Browser: Google Chrome already installed"
else
  HAS_CHROMIUM=$("$RUN_PY" - 2>/dev/null << 'PY'
import os
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print("yes" if os.path.exists(p.chromium.executable_path) else "no")
PY
)
  if [ "$HAS_CHROMIUM" = "yes" ]; then
    ok "浏览器：Chromium 已下载 / Browser: Chromium already downloaded"
  else
    miss "没有浏览器，正在下载（约 150 MB）/ No browser, downloading (about 150 MB) ..."
    if ! "$RUN_PY" -m playwright install chromium; then
      echo "官方源失败，改用镜像重试 / Trying a mirror ..."
      PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
        "$RUN_PY" -m playwright install chromium \
        || fail "浏览器下载失败 / Could not download the browser."
    fi
  fi
fi

echo
echo "全部就绪，启动中 / All set, starting ..."
echo
"$RUN_PY" piano_monitor.py
echo
echo "程序已结束 / The program has finished."
wait_close
