#!/bin/bash

# Root directory where your wallpapers (and wallpaper subfolders) live
WALLPAPER_DIR="$HOME/Pictures/Wallpaper"

# Cache files
CACHE_FILE="$HOME/.cache/current_wallpaper_index"      # last wallpaper index
FOLDER_FILE="$HOME/.cache/current_wallpaper_folder"    # active subfolder (empty = all)
THUMB_DIR="$HOME/.cache/wallpaper_thumbs"              # video thumbnails for rofi
mkdir -p "$THUMB_DIR"

# One log for everything that can go wrong (awww, mpvpaper, the colour script). A keybind has
# no terminal, so without this a failure is invisible. Read it with:
#   cat ~/.cache/wallpaper_script.log
LOG_FILE="$HOME/.cache/wallpaper_script.log"
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/cycle_wallpaper.lock"

# Your own colour choices. The colour script finds the colours in each wallpaper and picks the
# strongest one by itself, which is not always the one you would choose (for a picture of pink
# flowers among leaves it can pick the leaves). Fix it for a wallpaper with:
#   cycle_wallpaper.sh color            a menu of the colours found, pick one (best for a keybind)
#   cycle_wallpaper.sh pick 1           the same, by number (0 = first colour in the list)
#   cycle_wallpaper.sh pick "#cb5056"   or any colour you like
#   cycle_wallpaper.sh pick auto        go back to the automatic choice
# Choices are saved in PICKS_FILE as "<choice> <file name>", one per line, and used from then on.
PICKS_FILE="$HOME/.config/wallpaper_colors"
CURRENT_FILE="$HOME/.cache/current_wallpaper_path"    # the wallpaper that is on screen now

log() { printf '%s  %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"; }

# Only one wallpaper change at a time. Two overlapping runs (a double press, or a press while
# the last one is still working) can finish in a different order for the wallpaper and for the
# theme, which leaves the bar and the wallpaper from different pictures.
# File descriptor 9 holds the lock until the script exits, so anything started in the
# background below has to close it (9>&-), or it would keep the lock forever.
take_lock() {
    exec 9>"$LOCK_FILE"
    if ! flock -w 20 9; then
        log "gave up waiting for the previous run to finish"
        exit 1
    fi
    # keep the log short: only the last 300 lines
    if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 300 ]; then
        tail -n 300 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
    fi
}

report_failure() {
    log "FAILED: wallpaper not changed, theme left as it was"
    command -v notify-send &>/dev/null \
        && notify-send -u low "Wallpaper" "Could not set it, press again. Details: $LOG_FILE"
}

# Which subfolder is active? (relative to WALLPAPER_DIR, empty = everything)
folder=""
[ -f "$FOLDER_FILE" ] && folder=$(<"$FOLDER_FILE")

# Build the list of wallpapers from the active folder (searches subfolders too)
# null-delimited so spaces in filenames don't break the array; -iname so
# .JPG/.PNG etc match too
load_files() {
    local search_dir="$WALLPAPER_DIR"
    if [ -n "$folder" ] && [ -d "$WALLPAPER_DIR/$folder" ]; then
        search_dir="$WALLPAPER_DIR/$folder"
    fi
    mapfile -d '' -t files < <(
        find "$search_dir" -type f \( \
            -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.webp" \
            -o -iname "*.gif" \
            -o -iname "*.mp4" -o -iname "*.webm" -o -iname "*.mkv" -o -iname "*.mov" \
        \) -print0 | sort -z
    )
    num_files=${#files[@]}
}

load_files
if [ "$num_files" -eq 0 ]; then
    exit 1
fi

is_video() {
    case "${1,,}" in
        *.mp4|*.webm|*.mkv|*.mov) return 0 ;;
        *) return 1 ;;
    esac
}

# Make sure the awww daemon is up (only needed for images and gifs)
# (swww was renamed to awww — the old swww/swww-daemon binaries no longer exist)
ensure_awww() {
    if ! awww query &>/dev/null; then
        awww-daemon &>/dev/null 9>&- & disown
        # poll until the socket is ready instead of guessing a sleep time
        for _ in {1..10}; do
            awww query &>/dev/null && break
            sleep 0.3
        done
    fi
}

# ---- Wallpaper colours ----
# wallpaper_palette.py (keep it in the same folder as this script) does the whole job:
#   Python reads the picture and picks the seed colours -> Matugen builds the Material You
#   palettes from them -> a correction layer fixes contrast, saturation and brightness ->
#   colour.css, which style.css imports. Waybar is then told to reload.
# Matugen is used when it is installed (it works without it, using a built-in generator).
# Your own matugen config is not touched. Needs python and Pillow:  sudo pacman -S python-pillow
PALETTE_SCRIPT="$(dirname "$(readlink -f "$0")")/wallpaper_palette.py"
COLOUR_FILE="$HOME/.config/waybar/colour.css"

# The terminal follows the wallpaper too. The same run also writes kitty's colours and the colours
# of the shell prompt, made from the same palette as the bar. Two lines of setup, once:
#   ~/.config/kitty/kitty.conf   (last line)   include wallpaper-colors.conf
#   ~/.bashrc                                  the prompt lines from the setup note (they read PROMPT_FILE)
# kitty is told to reload its colours after every change; a prompt picks up the new colours the
# next time it is drawn (after you press Enter).
KITTY_FILE="$HOME/.config/kitty/wallpaper-colors.conf"
PROMPT_FILE="$HOME/.cache/wallpaper_prompt.sh"

# What the colour script should look at for a wallpaper: the file itself. For a video it reads
# several frames (needs ffmpeg); without ffmpeg it uses the cached thumbnail instead.
colour_source() {
    local f="$1"
    if is_video "$f" && ! command -v ffmpeg &>/dev/null; then
        icon_for "$f"
    else
        echo "$f"
    fi
}

# Everything the colour script prints goes into the log (LOG_FILE, at the top).
apply_theme() {
    local src pick thumb
    if [ ! -f "$PALETTE_SCRIPT" ]; then
        log "colour script not found: $PALETTE_SCRIPT (keep it next to this script)"
        return 0
    fi
    src=$(colour_source "$1")
    [ -f "$src" ] || return 0
    pick=$(pick_for "$1")
    log "colours: $src${pick:+  (your choice: $pick)}"
    {
        # your choice first; if the script refuses it, fall back to the automatic choice;
        # a video that cannot be read frame by frame falls back to its thumbnail
        if { [ -n "$pick" ] && run_palette "$src" "$pick"; } || run_palette "$src" auto \
            || { is_video "$1" && thumb=$(icon_for "$1") && [ -f "$thumb" ] && [ "$thumb" != "$src" ] \
                 && run_palette "$thumb" auto; }; then
            pkill -SIGUSR2 -x waybar
            pkill -SIGUSR1 -x kitty    # SIGUSR1 makes kitty reload kitty.conf, and so the file it includes
        else
            echo "colour script failed, bar colours left as they were"
            command -v notify-send &>/dev/null \
                && notify-send -u low "Wallpaper" "Bar colours not updated (is python-pillow installed?). Details: $LOG_FILE"
        fi
    } >> "$LOG_FILE" 2>&1
}

run_palette() {
    timeout 30 python3 -I "$PALETTE_SCRIPT" "$1" --out "$COLOUR_FILE" --kitty "$KITTY_FILE" --prompt "$PROMPT_FILE" \
        --pick "$2" </dev/null 9>&-
}

# The choice you saved for this wallpaper (empty if you never chose one).
pick_for() {
    [ -f "$PICKS_FILE" ] || return 0
    local want n name
    want=$(basename "$1")
    while read -r n name; do
        if [ "$name" = "$want" ]; then
            echo "$n"
            return 0
        fi
    done < "$PICKS_FILE"
}

# cycle_wallpaper.sh pick <number | #rrggbb | auto>: remember a colour for the wallpaper that
# is on screen and re-theme the bar right now.
pick_colour() {
    local n="$1" f name old_n old_name
    case "$n" in
        auto|[0-9]|[0-9][0-9]) ;;
        \#[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]) ;;
        *)
            echo "usage: $0 pick <number | #rrggbb | auto>"
            echo "see the numbers with:  python3 \"$PALETTE_SCRIPT\" \"\$(cat $CURRENT_FILE)\" --list"
            exit 1 ;;
    esac
    if [ ! -f "$CURRENT_FILE" ]; then
        echo "no wallpaper has been set yet, change it once first"
        exit 1
    fi
    f=$(<"$CURRENT_FILE")
    name=$(basename "$f")
    mkdir -p "$(dirname "$PICKS_FILE")"
    take_lock
    # drop any old line for this wallpaper, then add the new one (unless it is "auto")
    : > "$PICKS_FILE.tmp"
    if [ -f "$PICKS_FILE" ]; then
        while read -r old_n old_name; do
            [ -z "$old_n" ] && continue
            [ "$old_name" = "$name" ] && continue
            printf '%s %s\n' "$old_n" "$old_name" >> "$PICKS_FILE.tmp"
        done < "$PICKS_FILE"
    fi
    [ "$n" != "auto" ] && printf '%s %s\n' "$n" "$name" >> "$PICKS_FILE.tmp"
    mv "$PICKS_FILE.tmp" "$PICKS_FILE"
    log "colour choice for $name: $n"
    apply_theme "$f"
    echo "colour choice '$n' saved for $name and applied"
}

# cycle_wallpaper.sh color: a rofi menu of the colours found in the wallpaper on screen.
# Pick one and it is saved for that wallpaper and applied straight away. Bind this to a key.
choose_colour() {
    if [ ! -f "$CURRENT_FILE" ]; then
        command -v notify-send &>/dev/null && notify-send -u low "Wallpaper" "Change the wallpaper once first"
        exit 1
    fi
    local f src idx hex pct menu choice typed count=0
    f=$(<"$CURRENT_FILE")
    src=$(colour_source "$f")
    menu="Automatic (let the script choose)"
    while IFS=$'\t' read -r idx hex pct; do
        menu+=$'\n'"<span foreground=\"$hex\">██████</span>  $hex   ${pct}%"
        count=$((count + 1))
    done < <(python3 -I "$PALETTE_SCRIPT" "$src" --plain 2>>"$LOG_FILE")
    menu+=$'\n'"Type a colour..."

    choice=$(printf '%s' "$menu" | rofi -dmenu -i -markup-rows -p "Colour for this wallpaper" -format i)
    [ -z "$choice" ] && exit 0
    if [ "$choice" -eq 0 ]; then
        pick_colour auto
    elif [ "$choice" -le "$count" ]; then
        pick_colour $((choice - 1))
    else
        typed=$(rofi -dmenu -p "Colour (like #cb5056)" < /dev/null)
        [ -z "$typed" ] && exit 0
        pick_colour "$typed"
    fi
}

# Sets the wallpaper and, only if that worked, re-themes the bar to match.
# Returns 0 on success and 1 if the wallpaper could not be set. The theme is then left alone,
# so the bar can never show colours from a picture that is not on screen.
set_wallpaper() {
    local file="$1"
    log "set: $file"

    if is_video "$file"; then
        # awww can't play video, so hand it to mpvpaper
        pkill -x mpvpaper
        mpvpaper -o "no-audio loop hwdec=auto panscan=1.0" '*' "$file" &>/dev/null 9>&- & disown
        # give it a moment, then check it is really running (it exits at once when it
        # cannot open the file or the hardware decoder)
        sleep 1
        if ! pgrep -x mpvpaper &>/dev/null; then
            log "mpvpaper did not start for: $file"
            return 1
        fi
    else
        # images and gifs: awww handles both (gifs animate on their own)
        pkill -x mpvpaper
        local try
        for try in 1 2 3; do
            # last attempt: restart the daemon first, in case it is stuck
            if [ "$try" -eq 3 ]; then
                log "restarting awww-daemon"
                pkill -x awww-daemon
                sleep 0.5
            fi
            ensure_awww
            if awww img "$file" --transition-type random --transition-fps 60 --transition-pos top-right >>"$LOG_FILE" 2>&1; then
                break
            fi
            log "awww img failed (attempt $try of 3)"
            [ "$try" -eq 3 ] && return 1
            sleep 0.5
        done
    fi

    printf '%s\n' "$file" > "$CURRENT_FILE"

    # every path (next, select, folder) goes through here, so all of them re-theme
    apply_theme "$file"
    return 0
}

# Icon for the rofi menu: videos get a cached thumbnail, everything else
# just uses the file itself. Thumbnails are keyed by a hash of the full path
# so same-named files in different folders don't clash.
icon_for() {
    local f="$1"
    if is_video "$f"; then
        local key
        key=$(printf '%s' "$f" | md5sum | cut -d' ' -f1)
        local thumb="$THUMB_DIR/$key.jpg"
        if [ ! -f "$thumb" ] && command -v ffmpeg &>/dev/null; then
            ffmpeg -loglevel quiet -y -ss 1 -i "$f" -frames:v 1 -vf scale=256:-1 "$thumb" </dev/null
        fi
        [ -f "$thumb" ] && echo "$thumb" || echo "video-x-generic"
    else
        echo "$f"
    fi
}

save_index() {
    echo "$1" > "$CACHE_FILE"
}

cycle_next() {
    take_lock
    local index=0
    [ -f "$CACHE_FILE" ] && index=$(cat "$CACHE_FILE")
    local next_index=$(( (index + 1) % num_files ))
    # only move on if it worked, so pressing again retries the same wallpaper
    if set_wallpaper "${files[$next_index]}"; then
        save_index "$next_index"
    else
        report_failure
        exit 1
    fi
}

# Pick which folder the cycling (and the wallpaper picker) uses
select_folder() {
    local choice
    choice=$(
        {
            echo "All folders"
            find "$WALLPAPER_DIR" -mindepth 1 -type d -printf '%P\n' | sort
        } | rofi -dmenu -i -p "Folder"
    )
    [ -z "$choice" ] && exit 0

    if [ "$choice" == "All folders" ]; then
        folder=""
        rm -f "$FOLDER_FILE"
    else
        folder="$choice"
        echo "$folder" > "$FOLDER_FILE"
    fi

    load_files
    if [ "$num_files" -eq 0 ]; then
        # nothing usable in there, go back to using everything
        rm -f "$FOLDER_FILE"
        exit 1
    fi

    # start from the first wallpaper of the new folder
    save_index -1
    cycle_next
}

# Pick a specific wallpaper from the active folder
select_wallpaper() {
    local choice
    choice=$(
        for f in "${files[@]}"; do
            printf '%s\0icon\x1f%s\n' "${f#"$WALLPAPER_DIR"/}" "$(icon_for "$f")"
        done | rofi -dmenu -i -p "Wallpaper${folder:+ ($folder)}" -show-icons -format i
    )
    [ -z "$choice" ] && exit 0

    take_lock
    if set_wallpaper "${files[$choice]}"; then
        save_index "$choice"
    else
        report_failure
        exit 1
    fi
}

case "$1" in
    pick)   pick_colour "$2" ;;
    color)  choose_colour ;;
    select) select_wallpaper ;;
    folder) select_folder ;;
    *)      cycle_next ;;
esac