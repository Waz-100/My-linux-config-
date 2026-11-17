
# ---- prompt colours that follow the wallpaper ----
# cycle_wallpaper.sh writes ~/.cache/wallpaper_prompt.sh at every wallpaper change; the prompt reads it
# before it is drawn, so new colours show up as soon as you press Enter. The values below are only the
# fallback for before that file exists.
WP_BRACKET=$'\e[0;90m'; WP_NAME=$'\e[1;31m'; WP_AT=$'\e[0;37m'; WP_HOST=$'\e[1;33m'
WP_PATH=$'\e[0;36m'; WP_SIGIL=$'\e[1;31m'; WP_RESET=$'\e[0m'
__wp_colours() { [ -r "$HOME/.cache/wallpaper_prompt.sh" ] && . "$HOME/.cache/wallpaper_prompt.sh"; return 0; }
case ";$PROMPT_COMMAND;" in
    *";__wp_colours;"*) ;;
    *) PROMPT_COMMAND="__wp_colours${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
PS1='\[${WP_BRACKET}\][\[${WP_NAME}\]ALUCARD\[${WP_AT}\]@\[${WP_HOST}\]DRAGON \[${WP_PATH}\]\w\[${WP_BRACKET}\]]\[${WP_SIGIL}\]\$\[${WP_RESET}\] '
