#!/usr/bin/env bash
# Install (or upgrade) Omameter from this checkout into the Omarchy plugins
# folder, add the `omameter` CLI to ~/.local/bin, and enable the bar widget.
# Re-running it upgrades in place and restarts omarchy-shell so the new QML and
# daemon load. User data in ~/.config/omameter, ~/.cache/omameter and
# ~/.local/share/omameter is never touched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="paulimfavarato.omameter"
TARGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/${PLUGIN_ID}"
CLI_BIN_DIR="${HOME}/.local/bin"
CLI_TARGET="${CLI_BIN_DIR}/omameter"

echo "==> Validating plugin manifest and layout..."
if command -v omarchy >/dev/null 2>&1; then
  omarchy plugin validate "$SCRIPT_DIR"
fi

WAS_INSTALLED=0
if [[ -d "$TARGET_DIR" ]]; then
  WAS_INSTALLED=1
fi

echo "==> Installing plugin to ${TARGET_DIR}..."
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"

# Copy plugin files without symlinks (Omarchy rejects symlinks inside plugin dirs).
# Development-only files (tests, CI, scripts) stay in the checkout.
tar -C "$SCRIPT_DIR" \
  --exclude='./.git' \
  --exclude='./.github' \
  --exclude='./.gitignore' \
  --exclude='./tests' \
  --exclude='./scripts' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  -cf - . | tar -C "$TARGET_DIR" -xf -

chmod +x "$TARGET_DIR/bin/omameter" "$TARGET_DIR/bin/omameter-analytics" "$TARGET_DIR/backend/daemon.py" "$TARGET_DIR/backend/analytics/cli.py" "$TARGET_DIR/install.sh" "$TARGET_DIR/uninstall.sh"

# Write a CLI wrapper only where it is ours to write: a missing path, a wrapper
# an earlier install.sh wrote, or a symlink into this plugin's bin/ (the
# `omarchy plugin add` setup in the README). Anything else is the user's own
# command and is left alone. The wrapper is written to a temp file and moved
# into place, so an existing symlink is replaced, never written through.
install_cli_wrapper() {
  local name="$1" runner="$2"
  local target="${CLI_BIN_DIR}/${name}"
  local marker="plugins/${PLUGIN_ID}/bin/${name}"
  if [[ -L "$target" ]]; then
    if [[ "$(readlink "$target")" != *"$marker" ]]; then
      echo "    Skipping ${target}: it is a symlink to something else. Remove it to install the ${name} command."
      return 0
    fi
  elif [[ -e "$target" ]] && ! grep -qF "$marker" "$target" 2>/dev/null; then
    echo "    Skipping ${target}: it already exists and was not written by Omameter."
    return 0
  fi
  local tmp
  tmp="$(mktemp "${CLI_BIN_DIR}/.${name}.XXXXXX")"
  printf '#!/usr/bin/env bash\nexec %s %q "$@"\n' "$runner" "${TARGET_DIR}/bin/${name}" > "$tmp"
  chmod 755 "$tmp"
  mv -f "$tmp" "$target"
}

echo "==> Installing CLI helpers to ${CLI_BIN_DIR}..."
mkdir -p "$CLI_BIN_DIR"
install_cli_wrapper omameter python3
install_cli_wrapper omameter-analytics bash

if command -v omarchy >/dev/null 2>&1; then
  echo "==> Validating installed copy..."
  omarchy plugin validate "$TARGET_DIR"
fi

if [[ "$WAS_INSTALLED" == "1" ]] && command -v omarchy-restart-shell >/dev/null 2>&1; then
  echo "==> Restarting omarchy-shell to reload updated QML and daemon..."
  omarchy-restart-shell || true
elif command -v omarchy-shell >/dev/null 2>&1; then
  echo "==> Rescanning plugins in omarchy-shell..."
  omarchy-shell shell rescanPlugins 2>/dev/null || true
fi

if command -v omarchy >/dev/null 2>&1; then
  echo "==> Enabling plugin in Omarchy bar..."
  omarchy plugin enable "$PLUGIN_ID" right || true
fi

if command -v omarchy-shell >/dev/null 2>&1; then
  omarchy-shell shell rescanPlugins 2>/dev/null || true
fi

echo ""
echo "Omameter installed successfully!"
echo "  Plugin dir : ${TARGET_DIR}"
echo "  CLI helper : ${CLI_TARGET}"
echo "  Toggle IPC : omarchy-shell ${PLUGIN_ID} toggle"
