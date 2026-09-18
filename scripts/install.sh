#!/usr/bin/env bash
# Install this Hermes skill and its optional MCP adapter into Hermes skill directories.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
SKILL_NAME="linux-whole-system-file-search"
USER_HOME="${HOME:?HOME is not set}"
HERMES_HOME_DIR="${HERMES_ROOT:-${HERMES_HOME:-$USER_HOME/.hermes}}"

declare -a TARGETS=()
declare -A SEEN_TARGETS=()
DRY_RUN=0
TARGETS_EXPLICIT=0
MODE="default"
ACTION="install"
INSTALL_PLOCATE=0
REMOVE_PLOCATE=0
INSTALL_WATCHER=0
WATCHER_SERVICE_NAME="linux-whole-system-file-search-watcher.service"
USER_CONFIG_HOME="${XDG_CONFIG_HOME:-$USER_HOME/.config}"

interactive_menu() {
    cat <<'EOF'
Linux Whole-System File Search installer

  1) Install/update the skill and MCP server
  2) Install/update the skill and install/setup plocate
  3) Install/update and enable the optional live index watcher
  4) Install/update, plocate, and the optional live index watcher
  5) Uninstall the skill, MCP server, and live index watcher
  6) Uninstall the skill, live watcher, and remove plocate
  7) Exit
EOF
    while true; do
        read -r -p "Choose an option [1-7]: " choice || exit 1
        case "$choice" in
            1) return 0 ;;
            2) INSTALL_PLOCATE=1; return 0 ;;
            3) INSTALL_WATCHER=1; return 0 ;;
            4) INSTALL_PLOCATE=1; INSTALL_WATCHER=1; return 0 ;;
            5) ACTION="uninstall"; return 0 ;;
            6) ACTION="uninstall"; REMOVE_PLOCATE=1; return 0 ;;
            7) exit 0 ;;
            *) echo "Please choose a number from 1 to 7." ;;
        esac
    done
}

usage() {
    cat <<'EOF'
Usage: scripts/install.sh [OPTIONS]

Install the Linux whole-system file-search skill and MCP adapter into Hermes.

When run without options in an interactive terminal, a menu offers normal
install, plocate setup, the optional live index watcher, uninstall, and exit
choices. Normal installation preserves the existing Hermes trust setting and
does not enable the watcher.

By default, installs into ~/.hermes/skills and the existing
~/.hermes/profiles/coder/skills directory.

Options:
  --normal-only       install only into the normal Hermes profile
  --coder-only        install only into the coder profile
  --all-profiles      install into normal Hermes plus every existing profile
  --hermes-root DIR   use DIR as the shared Hermes root
  --target DIR        install into DIR; may be supplied more than once
  --install-plocate   install plocate, build its initial index, and set up its timer
  --setup-plocate     alias for --install-plocate
  --install-watcher   install and enable a user-level inotify index watcher
  --setup-watcher     alias for --install-watcher
  --uninstall         remove this skill's managed files from the targets
  --remove-plocate    with --uninstall, remove the optional plocate package too
  --dry-run           show targets without changing files
  -h, --help          show this help
EOF
}

die() {
    echo "install.sh: $*" >&2
    exit 2
}

command -v realpath >/dev/null 2>&1 || die "realpath is required"

if (($# == 0)) && [[ -t 0 && -t 1 ]]; then
    interactive_menu
fi

normalize_hermes_root() {
    local normalized
    normalized="$(realpath -m -- "$1")"
    # Hermes may set HERMES_HOME to a named profile directory in a subprocess;
    # this installer targets the shared root so it can install both profiles.
    case "$normalized" in
        */profiles/*) normalized="${normalized%%/profiles/*}" ;;
    esac
    printf '%s\n' "$normalized"
}

HERMES_HOME_DIR="$(normalize_hermes_root "$HERMES_HOME_DIR")"
NORMAL_SKILLS_DIR="$HERMES_HOME_DIR/skills"
CODER_SKILLS_DIR="$HERMES_HOME_DIR/profiles/coder/skills"

add_target() {
    local target="$1"
    local absolute

    if [[ -z "$target" ]]; then
        die "target directory cannot be empty"
    fi
    absolute="$(realpath -m -- "$target")"
    if [[ -z "${SEEN_TARGETS[$absolute]+x}" ]]; then
        SEEN_TARGETS["$absolute"]=1
        TARGETS+=("$absolute")
    fi
}

while (($#)); do
    case "$1" in
        --normal-only)
            [[ "$TARGETS_EXPLICIT" == 0 ]] || die "profile mode cannot be combined with --target"
            MODE="normal"
            ;;
        --coder-only)
            [[ "$TARGETS_EXPLICIT" == 0 ]] || die "profile mode cannot be combined with --target"
            MODE="coder"
            ;;
        --all-profiles)
            [[ "$TARGETS_EXPLICIT" == 0 ]] || die "profile mode cannot be combined with --target"
            MODE="all"
            ;;
        --hermes-root)
            (($# >= 2)) || die "--hermes-root requires a directory"
            HERMES_HOME_DIR="$(normalize_hermes_root "$2")"
            NORMAL_SKILLS_DIR="$HERMES_HOME_DIR/skills"
            CODER_SKILLS_DIR="$HERMES_HOME_DIR/profiles/coder/skills"
            shift
            ;;
        --target)
            (($# >= 2)) || die "--target requires a directory"
            [[ "$MODE" == default ]] || die "--target cannot be combined with a profile mode"
            TARGETS_EXPLICIT=1
            add_target "$2"
            shift
            ;;
        --install-plocate|--setup-plocate)
            INSTALL_PLOCATE=1
            ;;
        --install-watcher|--setup-watcher)
            INSTALL_WATCHER=1
            ;;
        --uninstall)
            ACTION="uninstall"
            ;;
        --remove-plocate)
            REMOVE_PLOCATE=1
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
    shift
done

if ((INSTALL_PLOCATE && REMOVE_PLOCATE)); then
    die "--install-plocate and --remove-plocate cannot be combined"
fi
if ((INSTALL_PLOCATE)) && [[ "$ACTION" == uninstall ]]; then
    die "--install-plocate cannot be combined with --uninstall"
fi
if ((REMOVE_PLOCATE)) && [[ "$ACTION" != uninstall ]]; then
    die "--remove-plocate requires --uninstall"
fi
if ((INSTALL_WATCHER)) && [[ "$ACTION" == uninstall ]]; then
    die "--install-watcher cannot be combined with --uninstall"
fi
if ((INSTALL_WATCHER)) && [[ "$(id -u)" == 0 ]]; then
    die "install the watcher as a normal user, not root"
fi
if ((INSTALL_WATCHER && TARGETS_EXPLICIT)); then
    die "--install-watcher cannot be combined with --target; use a profile mode"
fi

if ((TARGETS_EXPLICIT == 0)); then
    case "$MODE" in
        normal)
            add_target "$NORMAL_SKILLS_DIR"
            ;;
        coder)
            if [[ "$ACTION" == install && ! -d "$CODER_SKILLS_DIR" ]]; then
                die "coder profile skill directory does not exist: $CODER_SKILLS_DIR"
            fi
            add_target "$CODER_SKILLS_DIR"
            ;;
        all)
            add_target "$NORMAL_SKILLS_DIR"
            shopt -s nullglob
            for profile_skills in "$HERMES_HOME_DIR"/profiles/*/skills; do
                [[ -d "$profile_skills" ]] && add_target "$profile_skills"
            done
            ;;
        default)
            add_target "$NORMAL_SKILLS_DIR"
            if [[ -d "$CODER_SKILLS_DIR" ]]; then
                add_target "$CODER_SKILLS_DIR"
            else
                echo "Skipping missing coder profile: $CODER_SKILLS_DIR" >&2
            fi
            ;;
    esac
fi

if [[ "$ACTION" == install ]]; then
    [[ -f "$PROJECT_ROOT/SKILL.md" ]] || die "SKILL.md is missing from $PROJECT_ROOT"
    [[ -f "$PROJECT_ROOT/scripts/search.py" ]] || die "scripts/search.py is missing from $PROJECT_ROOT"
    [[ -f "$PROJECT_ROOT/scripts/mcp_server.py" ]] || die "scripts/mcp_server.py is missing from $PROJECT_ROOT"
    [[ -f "$PROJECT_ROOT/scripts/index_watcher.py" ]] || die "scripts/index_watcher.py is missing from $PROJECT_ROOT"
fi

package_manager() {
    local candidate
    for candidate in apt-get dnf yum pacman zypper apk; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

run_as_root() {
    if [[ "$(id -u)" == 0 ]]; then
        "$@"
        return
    fi
    if command -v sudo >/dev/null 2>&1; then
        sudo "$@"
        return
    fi
    die "root privileges are required for this operation; rerun with sudo"
}

find_updatedb() {
    local candidate resolved
    for candidate in updatedb.plocate updatedb /usr/sbin/updatedb.plocate /usr/bin/updatedb; do
        if [[ "$candidate" == */* ]]; then
            [[ -x "$candidate" ]] || continue
            printf '%s\n' "$candidate"
            return 0
        fi
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        if [[ -n "$resolved" ]]; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done
    return 1
}

user_index_database_path() {
    local cache_home="${XDG_CACHE_HOME:-$USER_HOME/.cache}"
    printf '%s\n' "$cache_home/plocate/plocate.db"
}

watcher_script_path() {
    case "$MODE" in
        coder)
            printf '%s\n' "$CODER_SKILLS_DIR/$SKILL_NAME/scripts/index_watcher.py"
            ;;
        *)
            # The normal profile is always one of the targets for default/all
            # installs, and is the most stable path for a user service.
            printf '%s\n' "$NORMAL_SKILLS_DIR/$SKILL_NAME/scripts/index_watcher.py"
            ;;
    esac
}

watcher_unit_path() {
    printf '%s\n' "$USER_CONFIG_HOME/systemd/user/$WATCHER_SERVICE_NAME"
}

install_watcher_service() {
    local systemctl_bin unit_dir unit_path watcher_script python_bin log_path temporary

    watcher_script="$(watcher_script_path)"
    unit_path="$(watcher_unit_path)"
    unit_dir="$(dirname -- "$unit_path")"

    if ((DRY_RUN)); then
        echo "Would install: $unit_path"
        echo "Would enable/start the user-level inotify watcher for /"
        return 0
    fi

    [[ -f "$watcher_script" ]] || die "watcher script was not installed: $watcher_script"
    if [[ -e "$unit_path" ]] && ! grep -Fq "Managed by linux-whole-system-file-search installer" "$unit_path"; then
        die "refusing to overwrite an unrelated user service: $unit_path"
    fi
    command -v python3 >/dev/null 2>&1 || die "python3 is required for the live index watcher"
    python_bin="$(command -v python3)"
    log_path="$USER_HOME/.cache/plocate/linux-file-search-watcher.log"
    install -d -m 0700 -- "$(dirname -- "$log_path")"
    install -d -m 0700 -- "$unit_dir"
    temporary="$(mktemp "$unit_dir/.${WATCHER_SERVICE_NAME}.XXXXXX")"
    {
        printf '%s\n' \
            "# Managed by linux-whole-system-file-search installer" \
            "[Unit]" \
            "Description=Debounced live plocate index watcher" \
            "After=default.target" \
            "" \
            "[Service]" \
            "Type=simple"
        printf 'ExecStart=%s "%s" --root / --quiet-period 30 --max-delay 300 --refresh-timeout 900\n' \
            "$python_bin" "$watcher_script"
        printf 'StandardOutput=append:%s\nStandardError=append:%s\n' "$log_path" "$log_path"
        printf '%s\n' \
            "Restart=on-failure" \
            "RestartSec=10" \
            "NoNewPrivileges=true" \
            "" \
            "[Install]" \
            "WantedBy=default.target"
    } >"$temporary"
    chmod 0644 -- "$temporary"
    mv -f -- "$temporary" "$unit_path"
    echo "Installed user watcher service: $unit_path"

    systemctl_bin="$(command -v systemctl 2>/dev/null || true)"
    if [[ -z "$systemctl_bin" ]]; then
        echo "No systemctl found; the watcher unit is installed but not enabled."
        return 0
    fi
    if ! "$systemctl_bin" --user daemon-reload; then
        echo "Warning: could not contact the user systemd manager; enable later with:"
        echo "  systemctl --user enable --now $WATCHER_SERVICE_NAME"
        return 0
    fi
    if "$systemctl_bin" --user enable --now "$WATCHER_SERVICE_NAME"; then
        echo "Enabled/started: $WATCHER_SERVICE_NAME"
    else
        echo "Warning: could not enable/start the user watcher; enable later with:"
        echo "  systemctl --user enable --now $WATCHER_SERVICE_NAME"
    fi
}

remove_watcher_service() {
    local systemctl_bin unit_path

    unit_path="$(watcher_unit_path)"
    if ((DRY_RUN)); then
        echo "Would disable/stop: $WATCHER_SERVICE_NAME"
        echo "Would remove: $unit_path"
        return 0
    fi
    if [[ ! -e "$unit_path" && ! -L "$unit_path" ]]; then
        echo "Live watcher service is not installed."
        return 0
    fi
    [[ -f "$unit_path" ]] || die "refusing to remove non-regular watcher unit: $unit_path"
    grep -Fq "Managed by linux-whole-system-file-search installer" "$unit_path" || \
        die "refusing to remove an unrelated user service: $unit_path"

    systemctl_bin="$(command -v systemctl 2>/dev/null || true)"
    if [[ -n "$systemctl_bin" ]]; then
        if ! "$systemctl_bin" --user disable --now "$WATCHER_SERVICE_NAME" >/dev/null 2>&1; then
            echo "Warning: could not stop the user watcher; leaving its unit in place: $unit_path"
            die "cannot safely uninstall while the watcher may still be running"
        fi
        "$systemctl_bin" --user daemon-reload >/dev/null 2>&1 || true
    fi
    rm -f -- "$unit_path"
    if [[ -n "$systemctl_bin" ]]; then
        "$systemctl_bin" --user daemon-reload >/dev/null 2>&1 || true
    fi
    echo "Removed live watcher service: $unit_path"
}

systemd_timer_available() {
    local systemctl_bin
    systemctl_bin="$(command -v systemctl 2>/dev/null || true)"
    [[ -n "$systemctl_bin" ]] || return 1
    "$systemctl_bin" cat plocate-updatedb.timer >/dev/null 2>&1
}

setup_plocate_timer() {
    local systemctl_bin system_state
    systemctl_bin="$(command -v systemctl 2>/dev/null || true)"
    if [[ -z "$systemctl_bin" ]]; then
        echo "No systemctl found; the package's cron integration, if provided, will handle future updates."
        return 0
    fi
    system_state="$("$systemctl_bin" is-system-running 2>/dev/null || true)"
    case "$system_state" in
        running|degraded|starting|maintenance) ;;
        *)
            echo "systemd is not running; leaving the package's default scheduler unchanged."
            return 0
            ;;
    esac
    if ! systemd_timer_available; then
        echo "plocate-updatedb.timer is not available; leaving the package's default scheduler unchanged."
        return 0
    fi

    if ! run_as_root "$systemctl_bin" daemon-reload; then
        echo "Warning: could not reload systemd; the initial index was still built."
        return 0
    fi
    if run_as_root "$systemctl_bin" enable plocate-updatedb.timer; then
        echo "Enabled: plocate-updatedb.timer"
    else
        echo "Note: plocate-updatedb.timer is package-managed or static; enable was not required."
    fi
    if run_as_root "$systemctl_bin" start plocate-updatedb.timer; then
        echo "Started: plocate-updatedb.timer"
    else
        echo "Warning: could not start plocate-updatedb.timer; the initial index was still built."
    fi
}

install_plocate() {
    local manager updatedb_bin user_database

    if ((DRY_RUN)); then
        manager="$(package_manager 2>/dev/null || true)"
        if [[ -z "$manager" ]]; then
            manager="a supported package manager"
        fi
        echo "Would install/setup plocate using: $manager"
        echo "Would build a user-owned initial database with updatedb (this can take a while)."
        echo "Would enable/start plocate-updatedb.timer when available."
        return 0
    fi

    if ! command -v plocate >/dev/null 2>&1; then
        manager="$(package_manager 2>/dev/null || true)"
        [[ -n "$manager" ]] || die "plocate is not installed and no supported package manager was found"
        echo "Installing plocate with $manager..."
        case "$manager" in
            apt-get) run_as_root apt-get install -y plocate ;;
            dnf) run_as_root dnf install -y plocate ;;
            yum) run_as_root yum install -y plocate ;;
            pacman) run_as_root pacman -S --needed --noconfirm plocate ;;
            zypper) run_as_root zypper --non-interactive install plocate ;;
            apk) run_as_root apk add plocate ;;
            *) die "unsupported package manager: $manager" ;;
        esac
    else
        echo "plocate is already installed."
    fi

    command -v plocate >/dev/null 2>&1 || die "plocate installation did not provide the plocate command"
    updatedb_bin="$(find_updatedb 2>/dev/null || true)"
    [[ -n "$updatedb_bin" ]] || die "plocate is installed but no updatedb command was found"

    user_database="$(user_index_database_path)"
    install -d -m 0700 -- "$(dirname -- "$user_database")"
    echo "Building the user-owned initial plocate index with $updatedb_bin..."
    echo "This may take a while; later updates reuse unchanged directory data."
    "$updatedb_bin" --require-visibility no --output "$user_database"
    chmod 0600 -- "$user_database"
    echo "User-owned initial plocate index completed: $user_database"
    setup_plocate_timer
}

remove_plocate() {
    local manager systemctl_bin

    if ((DRY_RUN)); then
        manager="$(package_manager 2>/dev/null || true)"
        if [[ -z "$manager" ]]; then
            manager="a supported package manager"
        fi
        echo "Would stop/disable plocate-updatedb.timer when available."
        echo "Would remove plocate using: $manager"
        return 0
    fi

    systemctl_bin="$(command -v systemctl 2>/dev/null || true)"
    if [[ -n "$systemctl_bin" ]] && systemd_timer_available; then
        run_as_root "$systemctl_bin" disable --now plocate-updatedb.timer >/dev/null 2>&1 || \
            run_as_root "$systemctl_bin" stop plocate-updatedb.timer >/dev/null 2>&1 || true
    fi

    if ! command -v plocate >/dev/null 2>&1; then
        echo "plocate is not installed; nothing to remove."
        return 0
    fi
    manager="$(package_manager 2>/dev/null || true)"
    [[ -n "$manager" ]] || die "cannot remove plocate: no supported package manager was found"
    echo "Removing plocate with $manager..."
    case "$manager" in
        apt-get) run_as_root apt-get remove -y plocate ;;
        dnf) run_as_root dnf remove -y plocate ;;
        yum) run_as_root yum remove -y plocate ;;
        pacman) run_as_root pacman -R --noconfirm plocate ;;
        zypper) run_as_root zypper --non-interactive remove plocate ;;
        apk) run_as_root apk del plocate ;;
        *) die "unsupported package manager: $manager" ;;
    esac
    echo "Removed plocate."
}

uninstall_target() {
    local destination="$1/$SKILL_NAME"
    local managed_file
    local remaining

    if [[ ! -e "$destination" && ! -L "$destination" ]]; then
        echo "Not installed: $destination"
        return 0
    fi
    if [[ -L "$destination" ]]; then
        die "refusing to uninstall symlinked destination: $destination"
    fi
    if [[ ! -d "$destination" ]]; then
        die "refusing to uninstall non-directory destination: $destination"
    fi
    if [[ ! -f "$destination/SKILL.md" ]] || ! grep -Fq "name: $SKILL_NAME" "$destination/SKILL.md"; then
        die "refusing to uninstall a directory that is not this skill: $destination"
    fi
    if [[ "$destination" == "$PROJECT_ROOT" && ( -f "$PROJECT_ROOT/README.md" || -f "$PROJECT_ROOT/LICENSE" ) ]]; then
        die "refusing to uninstall the project checkout: $destination"
    fi

    [[ ! -L "$destination/scripts" ]] || die "refusing symlinked scripts directory: $destination/scripts"

    for managed_file in \
        "$destination/SKILL.md" \
        "$destination/scripts/search.py" \
        "$destination/scripts/mcp_server.py" \
        "$destination/scripts/index_watcher.py" \
        "$destination/scripts/install.sh"; do
        if [[ -e "$managed_file" || -L "$managed_file" ]]; then
            if ((DRY_RUN)); then
                echo "Would remove: $managed_file"
            else
                rm -f -- "$managed_file"
                echo "Removed: $managed_file"
            fi
        fi
    done

    if ((DRY_RUN)); then
        return 0
    fi
    if [[ -d "$destination/scripts" ]]; then
        rmdir -- "$destination/scripts" 2>/dev/null || true
    fi
    if [[ -d "$destination" ]]; then
        remaining="$(find "$destination" -mindepth 1 -maxdepth 1 -print -quit)"
        if [[ -n "$remaining" ]]; then
            echo "Preserved extra files in: $destination"
        else
            rmdir -- "$destination" 2>/dev/null || true
            echo "Removed empty skill directory: $destination"
        fi
    fi
}

if [[ "$ACTION" == uninstall && "$TARGETS_EXPLICIT" == 0 ]]; then
    # Stop the service before removing the script it executes.
    remove_watcher_service
fi

for target in "${TARGETS[@]}"; do
    if [[ "$ACTION" == uninstall ]]; then
        uninstall_target "$target"
    else
        destination="$target/$SKILL_NAME"
        if [[ "$destination" == "$PROJECT_ROOT" ]]; then
            echo "Already installed: $destination"
            continue
        fi
        if ((DRY_RUN)); then
            echo "Would install: $destination"
            continue
        fi

        [[ ! -L "$destination" && ! -L "$destination/scripts" ]] || die "refusing symlinked installation directory: $destination"
        install -d -m 0755 -- "$destination/scripts"
        install -m 0644 -- "$PROJECT_ROOT/SKILL.md" "$destination/SKILL.md"
        install -m 0755 -- "$PROJECT_ROOT/scripts/search.py" "$destination/scripts/search.py"
        install -m 0755 -- "$PROJECT_ROOT/scripts/mcp_server.py" "$destination/scripts/mcp_server.py"
        install -m 0755 -- "$PROJECT_ROOT/scripts/index_watcher.py" "$destination/scripts/index_watcher.py"
        install -m 0755 -- "$PROJECT_ROOT/scripts/install.sh" "$destination/scripts/install.sh"
        echo "Installed: $destination"
    fi
done

if ((INSTALL_PLOCATE)); then
    install_plocate
fi
if ((INSTALL_WATCHER)); then
    install_watcher_service
fi
if ((REMOVE_PLOCATE)); then
    remove_plocate
fi
