#!/bin/sh
set -eu

package="nanobot-ai"
install_target="$package"
install_source="PyPI"
dry_run="0"
nanobot_python=""

info() {
  printf '%s\n' "$*"
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

install_failure_hint() {
  printf '%s\n' "Error: could not install nanobot from $install_source." >&2
  printf '%s\n' "If pip mentioned externally-managed-environment, use uv, pipx, or a virtual environment instead of system pip." >&2
  printf '%s\n' "You can also run manually:" >&2
  printf '  %s\n' "uv tool install --force --upgrade $install_target" >&2
  printf '  %s\n' "$python_bin -m venv ~/.nanobot/venv" >&2
  printf '  %s\n' "~/.nanobot/venv/bin/python -m pip install --upgrade $install_target" >&2
  printf '%s\n' "Then start setup with:" >&2
  printf '  %s\n' "nanobot onboard --wizard" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: install.sh [--dry-run]

By default this installs or upgrades nanobot-ai from PyPI.
Use --dry-run to print what would happen without installing or starting setup.

For current main, clone the repository and run `python -m pip install -e .`.
EOF
}

find_python() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
      then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

python_is_virtual_env() {
  "$python_bin" - <<'PY'
import sys
raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)
PY
}

select_install_method() {
  if python_is_virtual_env; then
    active_prefix="$("$python_bin" -I -c 'import sys; print(sys.prefix)')"
    if [ -f "$active_prefix/uv-receipt.toml" ] || [ -f "$active_prefix/pipx_metadata.json" ]; then
      fail "The active Python belongs to uv or pipx. Deactivate it and rerun this installer so its manager owns the update."
    fi
    install_method="python"
    return
  fi

  install_method=""
  existing_count=0
  if command -v uv >/dev/null 2>&1; then
    uv_dir="$(uv tool dir)" || fail "Could not inspect the uv tools directory"
    [ -n "$uv_dir" ] || fail "uv returned an empty tools directory"
    if [ -e "$uv_dir/$package" ]; then
      install_method="uv"
      existing_count=$((existing_count + 1))
    fi
  fi
  if command -v pipx >/dev/null 2>&1; then
    pipx_dir="$(pipx environment --value PIPX_LOCAL_VENVS)" || fail "Could not inspect the pipx environments directory"
    [ -n "$pipx_dir" ] || fail "pipx returned an empty environments directory"
    if [ -e "$pipx_dir/$package" ]; then
      install_method="pipx"
      existing_count=$((existing_count + 1))
    fi
  fi
  if [ -n "${NANOBOT_VENV:-}${HOME:-}" ] && [ -f "${NANOBOT_VENV:-${HOME:-}/.nanobot/venv}/pyvenv.cfg" ]; then
    install_method="managed"
    existing_count=$((existing_count + 1))
  fi
  [ "$existing_count" -le 1 ] || fail "Multiple nanobot installations were found. Use the intended environment's package manager; this installer changed none of them."
  if [ -z "$install_method" ]; then
    if command -v uv >/dev/null 2>&1; then
      install_method="uv"
    elif command -v pipx >/dev/null 2>&1; then
      install_method="pipx"
    else
      install_method="managed"
    fi
  fi
}

ensure_pip() {
  target_python="$1"
  if "$target_python" -m pip --version >/dev/null 2>&1; then
    return 0
  fi

  info "pip was not found for $target_python. Trying ensurepip..."
  "$target_python" -m ensurepip --upgrade >/dev/null 2>&1
}

select_installed_python() {
  # Keep the venv interpreter path (resolving its symlink would select base Python).
  # Isolated mode also prevents a checkout/PYTHONPATH from shadowing the new package.
  nanobot_python="$("$1" -I - "$2" <<'PY'
import sys
from importlib.metadata import distribution
from pathlib import Path

import nanobot

prefix = Path(sys.prefix).resolve()
package = Path(nanobot.__file__).resolve()
expected = sys.argv[1]
if sys.prefix == sys.base_prefix or (expected and prefix != Path(expected).resolve()):
    raise SystemExit("The installed interpreter does not belong to the expected virtual environment")
if not package.is_relative_to(prefix) or package != Path(distribution("nanobot-ai").locate_file("nanobot/__init__.py")).resolve():
    raise SystemExit("nanobot imports from outside the installed package")
print(Path(sys.executable).absolute())
PY
  )" || fail "nanobot was installed, but its environment could not be verified. No other installation was attempted."
}

run_nanobot() {
  "$nanobot_python" -I -m nanobot "$@"
}

nanobot_try_command() {
  "$python_bin" -I -c 'import shlex, sys; print(shlex.quote(sys.argv[1]) + " -I -m nanobot")' "$nanobot_python"
}

is_fresh_nanobot_install() {
  [ -n "${HOME:-}" ] || return 1
  [ ! -e "$HOME/.nanobot/config.json" ]
}

has_browser_session() {
  if [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ]; then
    return 1
  fi
  if ! : 2>/dev/null < /dev/tty; then
    return 1
  fi

  case "$(uname -s)" in
    Darwin)
      command -v launchctl >/dev/null 2>&1 &&
        launchctl print "gui/$(id -u)" >/dev/null 2>&1
      ;;
    *)
      [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]
      ;;
  esac
}

install_with_active_python() {
  info "Detected an active virtual environment. Installing into it..."
  ensure_pip "$python_bin" || return 1
  "$python_bin" -m pip install --upgrade "$install_target" || return 1
  select_installed_python "$python_bin" ""
}

install_with_uv() {
  info "Installing or upgrading nanobot from $install_source with uv tool..."
  tool_dir="$(uv tool dir)" || fail "Could not locate the uv tools directory"
  [ -n "$tool_dir" ] || fail "uv returned an empty tools directory"
  if [ -f "$tool_dir/$package/uv-receipt.toml" ]; then
    uv tool upgrade "$package" || fail "uv could not upgrade nanobot. Resolve the error above and retry; no other installation was attempted."
  elif [ -e "$tool_dir/$package" ]; then
    fail "The uv environment exists without its installation record. Repair it with uv before rerunning this installer."
  else
    uv tool install --python "$python_bin" "$install_target" ||
      fail "uv could not install nanobot. Resolve the error above and retry; no other installation was attempted."
  fi
  select_installed_python "$tool_dir/$package/bin/python" "$tool_dir/$package"
}

install_with_pipx() {
  info "Installing or upgrading nanobot from $install_source with pipx..."
  tool_dir="$(pipx environment --value PIPX_LOCAL_VENVS)" || fail "Could not locate the pipx environments directory"
  [ -n "$tool_dir" ] || fail "pipx returned an empty environments directory"
  if [ -f "$tool_dir/$package/pipx_metadata.json" ]; then
    pipx upgrade "$package" || fail "pipx could not upgrade nanobot. Resolve the error above and retry; no other installation was attempted."
  elif [ -e "$tool_dir/$package" ]; then
    fail "The pipx environment exists without its installation record. Repair it with pipx before rerunning this installer."
  else
    pipx install --python "$python_bin" "$install_target" ||
      fail "pipx could not install nanobot. Resolve the error above and retry; no other installation was attempted."
  fi
  select_installed_python "$tool_dir/$package/bin/python" "$tool_dir/$package"
}

write_managed_wrapper() {
  bin_dir="${NANOBOT_BIN_DIR:-$HOME/.local/bin}"
  wrapper="$bin_dir/nanobot"
  mkdir -p "$bin_dir" || return 0

  if [ -e "$wrapper" ] && ! grep -q "Generated by nanobot installer" "$wrapper" 2>/dev/null; then
    info "Not updating $wrapper because it already exists."
    return 0
  fi

  nanobot_command="$(nanobot_try_command)"
  cat > "$wrapper" <<EOF
#!/bin/sh
# Generated by nanobot installer.
exec $nanobot_command "\$@"
EOF
  chmod +x "$wrapper" || return 0

  if ! command -v nanobot >/dev/null 2>&1; then
    info "Installed a nanobot launcher at $wrapper."
    info "Add $bin_dir to PATH to run nanobot directly."
  fi
}

install_with_managed_venv() {
  [ -n "${HOME:-}" ] || fail "HOME is not set; cannot create a managed virtual environment"

  venv_dir="${NANOBOT_VENV:-$HOME/.nanobot/venv}"
  venv_python="$venv_dir/bin/python"

  if [ ! -x "$venv_python" ]; then
    info "Creating a dedicated virtual environment at $venv_dir..."
    mkdir -p "$(dirname "$venv_dir")"
    "$python_bin" -m venv "$venv_dir" || return 1
  fi

  "$venv_python" - <<'PY' >/dev/null 2>&1 || fail "The managed venv uses Python older than 3.11. Remove it or set NANOBOT_VENV to a new path."
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

  info "Installing or upgrading nanobot from $install_source in $venv_dir..."
  ensure_pip "$venv_python" || return 1
  "$venv_python" -m pip install --upgrade "$install_target" || return 1

  select_installed_python "$venv_python" "$venv_dir"
  write_managed_wrapper
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dev)
      fail "--dev installed an untracked main snapshot and is no longer supported; clone the repository and run 'python -m pip install -e .' instead"
      ;;
    --dry-run)
      dry_run="1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
  shift
done

python_bin="${PYTHON:-}"

if [ -n "$python_bin" ]; then
  command -v "$python_bin" >/dev/null 2>&1 || fail "PYTHON=$python_bin was not found"
  "$python_bin" - <<'PY' >/dev/null 2>&1 || fail "nanobot requires Python 3.11 or newer"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
else
  python_bin="$(find_python)" || fail "Python 3.11 or newer was not found. Install Python first, then rerun this command."
fi

info "Using Python: $("$python_bin" --version 2>&1)"
select_install_method

if [ "$dry_run" = "1" ]; then
  info "Dry run: would install or upgrade nanobot from $install_source."
  if [ "$install_method" = "python" ]; then
    info "Dry run: active virtual environment detected; would run: $python_bin -m pip install --upgrade $install_target"
    info "Dry run: would start nanobot with this environment's Python in isolated mode."
  elif [ "$install_method" = "uv" ]; then
    info "Dry run: would install with uv tool install, or use uv tool upgrade for an existing installation."
    info "Dry run: would start nanobot from its persistent uv tool environment."
  elif [ "$install_method" = "pipx" ]; then
    info "Dry run: would install with pipx install, or use pipx upgrade for an existing installation."
    info "Dry run: would start nanobot from its persistent pipx environment."
  else
    venv_dir="${NANOBOT_VENV:-$HOME/.nanobot/venv}"
    info "Dry run: would create or reuse a dedicated virtual environment: $venv_dir"
    info "Dry run: would run: $venv_dir/bin/python -m pip install --upgrade $install_target"
    info "Dry run: would start nanobot with $venv_dir/bin/python in isolated mode."
  fi
  if [ "${NANOBOT_SKIP_WIZARD:-}" = "1" ]; then
    info "Dry run: would skip automatic setup because NANOBOT_SKIP_WIZARD=1."
  elif is_fresh_nanobot_install && has_browser_session; then
    info "Dry run: would start the WebUI for this fresh desktop install."
    info "Dry run: would fall back to the setup wizard for older releases."
  else
    info "Dry run: would run the setup wizard."
  fi
  info "Dry run: no changes made."
  exit 0
fi

if [ "$install_method" = "python" ]; then
  install_with_active_python || install_failure_hint
elif [ "$install_method" = "uv" ]; then
  install_with_uv
elif [ "$install_method" = "pipx" ]; then
  install_with_pipx
else
  info "Using a dedicated virtual environment to avoid system pip."
  install_with_managed_venv || install_failure_hint
fi

info "Installed nanobot:"
run_nanobot --version

if [ "${NANOBOT_SKIP_WIZARD:-}" = "1" ]; then
  info "Skipping automatic setup because NANOBOT_SKIP_WIZARD=1."
  info "Run this later: $(nanobot_try_command) webui"
  exit 0
fi

if is_fresh_nanobot_install && has_browser_session; then
  if run_nanobot webui --help >/dev/null 2>&1; then
    info "Starting nanobot WebUI..."
    info "Configure your first provider and model in Settings > Models."
    info "Run this later: $(nanobot_try_command) webui"
    run_nanobot webui --yes
    exit 0
  fi
  info "The installed release does not support nanobot webui yet."
  info "Falling back to the setup wizard..."
fi

if [ -t 0 ]; then
  info "Starting setup wizard..."
  run_nanobot onboard --wizard
elif : 2>/dev/null < /dev/tty; then
  info "Starting setup wizard..."
  run_nanobot onboard --wizard < /dev/tty
else
  info "Skipping setup wizard because no interactive terminal is available."
  info "Run this later: $(nanobot_try_command) onboard --wizard"
fi

info "Done. Try: $(nanobot_try_command) agent -m \"Hello!\""
