#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
launcher=$repo/scripts/desktop-launch.sh
riot_icon_file=$repo/assets/icons/darling-lol-riot-client.png
league_icon_file=$repo/assets/icons/darling-lol-league-of-legends.png
if [ ! -x "$launcher" ]; then
  printf 'Launcher is missing or not executable: %s\n' "$launcher" >&2
  exit 1
fi
if [ ! -r "$riot_icon_file" ] || [ ! -r "$league_icon_file" ]; then
  printf 'Bundled Riot or League icon is missing from %s/assets/icons\n' "$repo" >&2
  exit 1
fi

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
applications_dir=$data_home/applications
icon_dir=$data_home/icons/hicolor/256x256/apps
mkdir -p "$applications_dir" "$icon_dir"
cp "$riot_icon_file" "$icon_dir/darling-lol-riot-client.png"
cp "$league_icon_file" "$icon_dir/darling-lol-league-of-legends.png"

# A quoted Exec path also handles checkout paths containing spaces or shell
# metacharacters that Desktop Entry treats specially.
exec_path=$(printf '%s' "$launcher" | sed 's/\\/\\\\/g; s/"/\\"/g; s/`/\\`/g; s/\$/\\$/g')

write_entry() {
  entry_path=$1
  app_name=$2
  comment=$3
  icon=$4
  wm_class=$5
  entry_tmp=$(dirname -- "$entry_path")/.$(basename -- "$entry_path").tmp.$$
  cat >"$entry_tmp" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$app_name
Comment=$comment
Exec="$exec_path"
Icon=$icon
Terminal=false
Categories=Game;
StartupNotify=false
StartupWMClass=$wm_class
EOF
  chmod 644 "$entry_tmp"
  mv -f "$entry_tmp" "$entry_path"
}

write_entry "$applications_dir/darling-lol-riot.desktop" \
  'Riot Client' 'Launch Riot Client through darling-lol' \
  darling-lol-riot-client RiotClientServices
write_entry "$applications_dir/darling-lol-league.desktop" \
  'League of Legends' 'Launch League through darling-lol' \
  darling-lol-league-of-legends LeagueofLegends
write_entry "$applications_dir/darling-lol-league-client.desktop" \
  'League Client' 'Launch League through darling-lol' \
  darling-lol-league-of-legends LeagueClientUx

# Prefer the conventional Desktop directory when it exists. Some systems set
# XDG_DESKTOP_DIR to the home directory even when ~/Desktop is present.
if [ -d "$HOME/Desktop" ]; then
  desktop_dir=$HOME/Desktop
elif command -v xdg-user-dir >/dev/null 2>&1; then
  desktop_dir=$(xdg-user-dir DESKTOP)
else
  desktop_dir=$HOME
fi
[ -n "$desktop_dir" ] || desktop_dir=$HOME
mkdir -p "$desktop_dir"
write_entry "$desktop_dir/League of Legends.desktop" \
  'League of Legends' 'Launch League through darling-lol' \
  darling-lol-league-of-legends LeagueofLegends
chmod 755 "$desktop_dir/League of Legends.desktop"

# On KDE, keep the game window fullscreen whenever it opens. Match both parts
# of WM_CLASS so the League client UI window is left alone.
if command -v kwriteconfig6 >/dev/null 2>&1 &&
   command -v kreadconfig6 >/dev/null 2>&1; then
  rule_id=leagueoflegends-fullscreen
  rules_file=kwinrulesrc
  current_rules=$(kreadconfig6 --file "$rules_file" --group General --key rules --default '')
  case ",$current_rules," in
    *,$rule_id,*) ;;
    *)
      if [ -n "$current_rules" ]; then
        current_rules=$current_rules,$rule_id
      else
        current_rules=$rule_id
      fi
      rule_count=$(printf '%s\n' "$current_rules" | awk -F, '{ print NF }')
      kwriteconfig6 --file "$rules_file" --group General --key count "$rule_count"
      kwriteconfig6 --file "$rules_file" --group General --key rules "$current_rules"
      ;;
  esac
  kwriteconfig6 --file "$rules_file" --group "$rule_id" --key Description \
    'League of Legends always fullscreen'
  kwriteconfig6 --file "$rules_file" --group "$rule_id" --key wmclass \
    'LeagueofLegends LeagueofLegends'
  kwriteconfig6 --file "$rules_file" --group "$rule_id" --key wmclasscomplete true
  kwriteconfig6 --file "$rules_file" --group "$rule_id" --key wmclassmatch 1
  kwriteconfig6 --file "$rules_file" --group "$rule_id" --key fullscreen true
  kwriteconfig6 --file "$rules_file" --group "$rule_id" --key fullscreenrule 3
  if command -v qdbus6 >/dev/null 2>&1; then
    qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>&1 || true
  fi
fi

printf 'Installed Riot Client and League launchers in %s\n' "$applications_dir"
printf 'Installed bundled Riot and League icons in %s\n' "$icon_dir"
printf 'Installed League desktop icon in %s\n' "$desktop_dir"
if command -v kwriteconfig6 >/dev/null 2>&1 &&
   command -v kreadconfig6 >/dev/null 2>&1; then
  printf 'Configured League game windows to open fullscreen in KDE\n'
fi
printf 'Launcher output is saved to %s\n' "${XDG_STATE_HOME:-$HOME/.local/state}/darling-lol/launcher.log"
