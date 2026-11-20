#!/usr/bin/env bash
# Remove Omameter: disable the bar widget, delete the installed plugin folder
# and the `omameter` CLI wrapper, then reload omarchy-shell.
#
#   ./uninstall.sh          keep your accounts, settings and cached usage
#   ./uninstall.sh --purge  also delete Omameter's data and stored credentials
set -euo pipefail

PLUGIN_ID="paulimfavarato.omameter"
TARGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/${PLUGIN_ID}"
CLI_TARGET="${HOME}/.local/bin/omameter"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omameter"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/omameter"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omameter"

PURGE=0
case "${1:-}" in
  "") ;;
  --purge) PURGE=1 ;;
  -h | --help)
    sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "Unknown option: $1 (use --purge or --help)" >&2
    exit 2
    ;;
esac

if command -v omarchy >/dev/null 2>&1; then
  echo "==> Disabling plugin..."
  omarchy plugin disable "$PLUGIN_ID" 2>/dev/null || true
fi

if [[ -d "$TARGET_DIR" ]]; then
  echo "==> Removing ${TARGET_DIR}..."
  rm -rf "$TARGET_DIR"
fi

# Only remove the CLI wrapper install.sh wrote (it execs the plugin's bin/omameter).
if [[ -f "$CLI_TARGET" ]] && grep -q "plugins/${PLUGIN_ID}/bin/omameter" "$CLI_TARGET" 2>/dev/null; then
  echo "==> Removing ${CLI_TARGET}..."
  rm -f "$CLI_TARGET"
fi

if [[ "$PURGE" == "1" ]]; then
  echo "==> Purging Omameter data..."
  rm -rf "$CONFIG_DIR" "$CACHE_DIR" "$DATA_DIR"
  if command -v secret-tool >/dev/null 2>&1; then
    secret-tool clear service com.omarchy.omameter.accounts 2>/dev/null || true
    secret-tool clear service com.omarchy.omameter.cliproxy-hubs 2>/dev/null || true
  fi
fi

if command -v omarchy-restart-shell >/dev/null 2>&1; then
  echo "==> Restarting omarchy-shell..."
  omarchy-restart-shell || true
fi

echo ""
echo "Omameter removed."
if [[ "$PURGE" != "1" ]]; then
  echo "  Your data is still in ${CONFIG_DIR}, ${CACHE_DIR} and ${DATA_DIR}"
  echo "  (credentials in the keyring). Run ./uninstall.sh --purge to delete it."
fi
