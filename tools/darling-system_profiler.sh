#!/bin/sh
# system_profiler for the Darling prefix: reports the selected GPU presentation
# and host-backed screens/network for the data types queried at startup. The hardware
# and software overviews come from the guest's own IOKit, sysctl and sw_vers, so they
# match what other APIs report (including a configure-darling-platform.py override).
# Installed as /usr/sbin/system_profiler by tools/install-darling-system-profiler.py.
host=${RCLOL_SYSTEM_ROOT:-/Volumes/SystemRoot}
xml=0
types=
while [ $# -gt 0 ]; do
 case "$1" in
  -xml) xml=1;;
  -detailLevel|-timeout) shift;;
  -listDataTypes) printf 'Available Datatypes:\nSPAirPortDataType\nSPDisplaysDataType\nSPEthernetDataType\nSPHardwareDataType\nSPSoftwareDataType\n'; exit 0;;
  SP*DataType) types="$types $1";;
 esac
 shift
done
[ -n "$types" ] || types=" SPHardwareDataType SPSoftwareDataType SPDisplaysDataType SPEthernetDataType SPAirPortDataType"

escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

pci_name() { # vendor device (hex without 0x) -> "Vendor Model"
 for ids in "$host/usr/share/hwdata/pci.ids" "$host/usr/share/misc/pci.ids"; do
  [ -r "$ids" ] || continue
  awk -v v="$1" -v d="$2" '
   /^[0-9a-f][0-9a-f][0-9a-f][0-9a-f]  / { found = ($1 == v); if (found) { vendor = $0; sub(/^[0-9a-f]+  /, "", vendor) } next }
   found && /^\t[0-9a-f][0-9a-f][0-9a-f][0-9a-f]  / && $1 == d { model = $0; sub(/^\t[0-9a-f]+  /, "", model); exit }
   END {
    if (v == "1002") vendor = "AMD"; else if (v == "10de") vendor = "NVIDIA"; else if (v == "8086") vendor = "Intel"
    if (match(model, /\[[^]]*\]/)) model = substr(model, RSTART + 1, RLENGTH - 2)
    if (vendor != "" && model != "") print vendor " " model; else if (vendor != "") print vendor " Graphics"
   }' "$ids"
  return
 done
 case "$1" in
  1002) echo 'AMD Graphics';;
  10de) echo 'NVIDIA Graphics';;
  8086) echo 'Intel Graphics';;
  *) echo 'Display';;
 esac
}

valid_vram() { # positive decimal MB, bounded so the shell test cannot overflow
 case "$1" in ''|*[!0-9]*) return 1;; esac
 [ "${#1}" -le 7 ] && [ "$1" -gt 0 ] && [ "$1" -le 1048576 ]
}

card_vram_bytes() { # amdgpu's mem_info_vram_total in bytes, 0 when absent or not a number
 v=$(cat "$1/device/mem_info_vram_total" 2>/dev/null) || v=
 case "$v" in ''|*[!0-9]*) echo 0;; *) echo "$v";; esac
}

# True when the AMD card $1 sits beside the host's discrete GPU: an NVIDIA card, or an
# AMD card with more VRAM (an APU's carve-out is far smaller than a dGPU's memory).
# The mac name then presents it as the MacBookPro16,1's integrated UHD Graphics 630, so
# an APU + dGPU host shows the Mac's pair instead of two Radeon Pro 5500Ms. Same rule as
# presentedAsIntegrated in patches/iokitd.patch and patches/metal.patch.
presented_as_integrated() {
 [ "$(sed 's/^0x//' "$1/device/vendor" 2>/dev/null)" = 1002 ] || return 1
 own_device=$(sed 's/^0x//' "$1/device/device" 2>/dev/null)
 own_vram=$(card_vram_bytes "$1")
 for other in "$host"/sys/class/drm/card[0-9] "$host"/sys/class/drm/card[0-9][0-9]; do
  [ -r "$other/device/vendor" ] && [ -r "$other/device/device" ] || continue
  other_vendor=$(sed 's/^0x//' "$other/device/vendor"); other_device=$(sed 's/^0x//' "$other/device/device")
  [ "$other_vendor" = 1002 ] && [ "$other_device" = "$own_device" ] && continue
  [ "$other_vendor" = 10de ] && return 0
  if [ "$other_vendor" = 1002 ] &&
     awk -v a="$(card_vram_bytes "$other")" -v b="$own_vram" 'BEGIN { exit !(a + 0 > b + 0) }'; then
   return 0
  fi
 done
 return 1
}

displays() { # vendor|device|vram MB or empty|name|bus|mac identity|display owner
 # HOST reports the host card. COMPAT is the default MacBookPro16,1 presentation,
 # selected by tools/system.prop; it only changes the reported identity, never
 # the host, the driver, or the GPU that actually presents frames. The mac name
 # presents Intel, and an AMD card beside the discrete GPU, as the
 # MacBookPro16,1's UHD Graphics 630 and the discrete
 # vendors (AMD, NVIDIA) as its Radeon Pro 5500M, like iokitd and Metal do: no
 # Mac that runs macOS 15 has an NVIDIA GPU.
 # Run outside the launcher, nothing is handed over: read the installed profile.
 prop=${RCLOL_SYSTEM_PROP:-/private/etc/darling-system.prop}
 if [ -z "${RCLOL_PLATFORM_PROFILE:-}" ] && [ -r "$prop" ]; then
  RCLOL_PLATFORM_PROFILE=$(sed -n 's/^profile=//p' "$prop" | tail -n 1)
  case "$RCLOL_PLATFORM_PROFILE" in host|compat) ;; *) RCLOL_PLATFORM_PROFILE=compat;; esac
  [ -n "${RCLOL_GPU_NAME:-}" ] || RCLOL_GPU_NAME=$(sed -n "s/^$RCLOL_PLATFORM_PROFILE\.gpu\.name=//p" "$prop" | tail -n 1)
  [ -n "${RCLOL_GPU_VRAM_MB:-}" ] || RCLOL_GPU_VRAM_MB=$(sed -n "s/^$RCLOL_PLATFORM_PROFILE\.gpu\.vram=//p" "$prop" | tail -n 1)
 fi
 profile=${RCLOL_PLATFORM_PROFILE:-compat}
 gpu_mode=${RCLOL_GPU_NAME:-}
 if [ -z "$gpu_mode" ]; then
  case "${DARLING_METAL_MAC_GPU_NAME:-}" in '') ;; 0) ;; *) gpu_mode=mac;; esac
 fi
 [ -n "$gpu_mode" ] || gpu_mode=host
 mac_gpu_profile=0
 [ "$gpu_mode" = mac ] && mac_gpu_profile=1
 profile_vram=${RCLOL_GPU_VRAM_MB:-}
 [ -n "$profile_vram" ] || profile_vram=${DARLING_METAL_VRAM_MB:-}
 case "$profile_vram" in host) profile_vram=;; esac
 valid_vram "$profile_vram" || profile_vram=
 host_vram=${RCLOL_HOST_GPU_VRAM_MB:-}
 valid_vram "$host_vram" || host_vram=
 if [ "$profile" = compat ] && [ "$mac_gpu_profile" = 1 ]; then
  load_platform
  if [ "$(platform model)" = MacBookPro16,1 ]; then
   # The overview describes the complete model, unlike Metal/IOKit's list of
   # available rendering devices. Do not add this pair to host-name overrides
   # or to other models. The discrete GPU owns the compatibility display tree;
   # this is a presentation relationship, not a claim about host connector wiring.
   pair_vram=$profile_vram
   if [ -z "$pair_vram" ]; then
    # Preserve gpu.vram=host / DARLING_METAL_VRAM_MB behaviour: use the largest
    # dedicated host heap, not a small APU carve-out, when no size is configured.
    pair_vram=0
    for card in "$host"/sys/class/drm/card[0-9] "$host"/sys/class/drm/card[0-9][0-9]; do
     card_vendor=$(sed 's/^0x//' "$card/device/vendor" 2>/dev/null)
     case "$card_vendor" in 1002|10de) ;; *) continue;; esac
     candidate=$(card_vram_bytes "$card" | awk '{ printf "%d", $1 / 1048576 }')
     if [ "$card_vendor" = 10de ] && ! valid_vram "$candidate"; then candidate=$host_vram; fi
     if valid_vram "$candidate" && [ "$candidate" -gt "$pair_vram" ]; then pair_vram=$candidate; fi
    done
    valid_vram "$pair_vram" || pair_vram=4096
   fi
   echo '8086|3e9b||Intel UHD Graphics 630|builtin|1|none'
   echo "1002|7340|$pair_vram|AMD Radeon Pro 5500M|pcie|1|all"
   return
  fi
 fi
 first_card=
 for card in "$host"/sys/class/drm/card[0-9] "$host"/sys/class/drm/card[0-9][0-9]; do
  [ -r "$card/device/vendor" ] || continue
  vendor=$(sed 's/^0x//' "$card/device/vendor"); device=$(sed 's/^0x//' "$card/device/device")
  vram=
  if [ -r "$card/device/mem_info_vram_total" ]; then
   vram=$(awk '{ printf "%d", $1 / 1048576 }' "$card/device/mem_info_vram_total")
  fi
  # The host value comes from nvidia-smi for a single NVIDIA GPU; other cards must not inherit it.
  if [ -z "$vram" ] && [ "$vendor" = 10de ]; then vram=$host_vram; fi
  [ -n "$profile_vram" ] && vram=$profile_vram
  name=; bus=builtin
  if [ "$vendor" = 10de ] && [ -r "$card/device/uevent" ]; then
   slot=$(sed -n 's/^PCI_SLOT_NAME=//p' "$card/device/uevent" | head -n 1)
   if [ -n "$slot" ] && [ -r "$host/proc/driver/nvidia/gpus/$slot/information" ]; then
    name=$(sed -n 's/^Model:[ \t]*//p' "$host/proc/driver/nvidia/gpus/$slot/information" | head -n 1)
    nv_bus=$(sed -n 's/^Bus Type:[ \t]*//p' "$host/proc/driver/nvidia/gpus/$slot/information" | head -n 1)
    [ "$nv_bus" = PCIe ] && bus=pcie
   fi
  fi
  [ -n "$name" ] || name=$(pci_name "$vendor" "$device"); [ -n "$name" ] || name="Display"
  mac_identity=0
  if [ "$mac_gpu_profile" = 1 ]; then
   [ "$vendor" = 1002 ] && presented_as_integrated "$card" && vendor=8086
   case "$vendor" in
    1002|10de) vendor=1002; device=7340; name='AMD Radeon Pro 5500M'
          valid_vram "$vram" || vram=4096
          bus=pcie; mac_identity=1;;
    8086) vendor=8086; device=3e9b; name='Intel UHD Graphics 630'
          vram=; bus=builtin; mac_identity=1;;
   esac
  elif [ "$gpu_mode" != host ]; then
   name=$gpu_mode
  fi
  owner=${card##*/}
  if [ -z "$first_card" ]; then first_card=$card; owner=first:$owner; fi
  echo "$vendor|$device|$vram|$name|$bus|$mac_identity|$owner"
 done
}

vram_text() {
 # macOS prints dedicated 4096 MB as "4 GB", but shared 1536 MB stays in MB.
 awk -v mb="$1" 'BEGIN { if (mb >= 1024 && mb % 1024 == 0) printf "%d GB", mb / 1024; else printf "%d MB", mb }'
}

gpu_fields() { # label|XML key|value; the enclosing loop supplies the GPU row
 echo "Chipset Model|sppci_model|$name"
 echo 'Type|sppci_device_type|spdisplays_gpu'
 echo "Bus|sppci_bus|spdisplays_$bus"
 if [ "$mac_identity" = 1 ] && [ "$vendor" = 1002 ]; then
  echo 'PCIe Lane Width|spdisplays_pcie_width|x16'
 fi
 if [ "$mac_identity" = 1 ] && [ "$vendor" = 8086 ]; then
  echo 'VRAM (Dynamic, Max)|spdisplays_vram_shared|1536 MB'
 elif [ -n "$vram" ]; then
  echo "VRAM (Total)|spdisplays_vram|$(vram_text "$vram")"
 fi
 echo "Vendor|spdisplays_vendor|${name%% *} (0x$vendor)"
 echo "Device ID|spdisplays_device-id|0x$device"
 if [ "$mac_identity" = 1 ]; then
  [ "$vendor" = 8086 ] && revision=0002 || revision=0040
  echo "Revision ID|spdisplays_revision-id|0x$revision"
  echo 'Automatic Graphics Switching|spdisplays_automatic_graphics_switching|spdisplays_supported'
  echo 'gMux Version|spdisplays_gmux-version|5.0.0'
 fi
 echo 'Metal Support|spdisplays_mtlgpufamilysupport|spdisplays_metal3'
}

screens() { # display ID|connector|width|height|main|mirror|internal|name
 # X11Display.m builds NSScreen from active XRandR CRTC geometry, primary first;
 # CGDisplayPixelsWide/High use that frame and backingScaleFactor is 1.0. Query
 # the same server, not the MacBook's nominal 3072x1920 Retina panel or sysfs
 # "modes" (supported, not current sizes). Match NSScreen's output-index IDs,
 # including gaps for inactive outputs. The separate
 # process cannot observe an application's private emulated fullscreen mode.
 # Darling execve dispatches host ELF tools directly; no guest probe is needed.
 # Missing xrandr / an inaccessible X server means unknown geometry: omit it,
 # rather than invent a panel, a Retina scale or automatic brightness support.
 # macOS names a display after the EDID monitor name descriptor (0xFC), e.g.
 # "PL3466WQ"; --verbose carries each output's EDID, also for connectors whose
 # X name differs from the DRM one. Without a name, keep "Display N".
 randr="$host/usr/bin/xrandr"
 [ -x "$randr" ] || randr="$host/run/current-system/sw/bin/xrandr"
 [ -x "$randr" ] || return 0
 # X11Display also retries :0 when the inherited DISPLAY cannot be opened.
 geometry=$("$randr" --verbose --current 2>/dev/null) ||
  geometry=$(DISPLAY=:0 "$randr" --verbose --current 2>/dev/null) || return 0
 printf '%s\n' "$geometry" | awk '
  function edid_name(hex,   d, k, b, j, c, out) {
   for (k = 0; k + 1 < length(hex); k += 2)
    b[k / 2] = (index("0123456789abcdef", substr(hex, k + 1, 1)) - 1) * 16 + index("0123456789abcdef", substr(hex, k + 2, 1)) - 1
   for (d = 54; d <= 108; d += 18) {
    if (b[d] != 0 || b[d + 1] != 0 || b[d + 2] != 0 || b[d + 3] != 252) continue
    out = ""
    for (j = d + 5; j < d + 18; j++) {
     c = b[j]
     if (c == 10 || c < 32 || c > 126) break
     out = out sprintf("%c", c)
    }
    sub(/ +$/, "", out); gsub(/[|]/, "", out)
    return out
   }
   return ""
  }
  inedid && NF == 1 && $1 ~ /^[0-9a-fA-F]+$/ { edid[id] = edid[id] tolower($1); next }
  { inedid = 0 }
  $1 == "EDID:" { inedid = 1; next }
  $2 == "connected" || $2 == "disconnected" {
   id++
   for (i = 3; i <= NF; i++) {
    if ($i ~ /^[0-9]+x[0-9]+[+-][0-9]+[+-][0-9]+$/) {
     n++; ids[n] = id; ports[n] = $1; rect[n] = $i
     split($i, size, /[x+-]/); widths[n] = size[1]; heights[n] = size[2]
     if ($3 == "primary") primary = n
     break
    }
   }
  }
  END {
   if (!primary) primary = 1
   for (pass = 1; pass <= 2; pass++) for (i = 1; i <= n; i++) {
    if ((pass == 1) != (i == primary)) continue
    mirror = "off"
    for (j = 1; j <= n; j++) if (i != j && rect[i] == rect[j]) mirror = "on"
    internal = (ports[i] ~ /^(eDP|LVDS|DSI)(-|[0-9]|$)/) ? "internal" : ""
    name = edid_name(edid[ids[i]])
    if (name == "") name = "Display " ids[i]
    printf "%d|%s|%d|%d|%s|%s|%s|%s\n", ids[i], ports[i], widths[i], heights[i], (i == primary) ? "yes" : "no", mirror, internal, name
   }
  }'
}

screens_for_gpu() {
 [ "$owner" != none ] || return 0
 printf '%s\n' "$screen_rows" | while IFS='|' read -r sid connector width height main mirror internal sname; do
  [ -n "$sid" ] || continue
  if [ "$owner" != all ]; then
   # Attribute connectors to their DRM card when the names agree. Xwayland and
   # proprietary drivers can expose logical names with no DRM match; keep those
   # on the first card only instead of duplicating every display on every GPU.
   screen_card=
   for gpu in "$host"/sys/class/drm/card[0-9] "$host"/sys/class/drm/card[0-9][0-9]; do
    if [ -e "$gpu-$connector" ]; then screen_card=${gpu##*/}; break; fi
   done
   case "$owner" in
    first:*) [ -z "$screen_card" ] || [ "$screen_card" = "${owner#first:}" ] || continue;;
    *) [ "$screen_card" = "$owner" ] || continue;;
   esac
  fi
  echo "$sid|$connector|$width|$height|$main|$mirror|$internal|$sname"
 done
}

screen_fields() {
 [ "$internal" != internal ] || echo 'Display Type|spdisplays_display_type|spdisplays_LCD'
 echo "Resolution|spdisplays_pixelresolution|$width x $height"
 [ "$main" != yes ] || echo 'Main Display|spdisplays_main|spdisplays_yes'
 echo "Mirror|spdisplays_mirror|spdisplays_$mirror"
 echo 'Online|spdisplays_online|spdisplays_yes'
 [ "$internal" != internal ] || echo 'Connection Type|spdisplays_connection_type|spdisplays_internal'
}

fields_xml() {
 while IFS='|' read -r label key value; do
  printf '    <key>%s</key><string>%s</string>\n' "$key" "$(printf '%s' "$value" | escape)"
 done
}

fields_text() {
 while IFS='|' read -r label key value; do
  case "$value" in
   spdisplays_gpu) value=GPU;; spdisplays_pcie) value=PCIe;; spdisplays_builtin) value=Built-In;;
   spdisplays_supported) value=Supported;; spdisplays_metal3) value='Metal 3';;
   spdisplays_LCD) value=LCD;; spdisplays_yes) value=Yes;; spdisplays_off) value=Off;;
   spdisplays_on) value=On;; spdisplays_internal) value=Internal;;
  esac
  # macOS localizes the known integrated vendor as Intel without a PCI suffix.
  if [ "$key" = spdisplays_vendor ] && [ "$value" = 'Intel (0x8086)' ] && [ "$mac_identity" = 1 ]; then value=Intel; fi
  printf '%s%s: %s\n' "$1" "$label" "$value"
 done
}

display_report() { # text|xml
 screen_rows=$(screens)
 displays | while IFS='|' read -r vendor device vram name bus mac_identity owner; do
  panels=$(screens_for_gpu)
  if [ "$1" = xml ]; then
   echo '   <dict>'
   printf '    <key>_name</key><string>%s</string>\n' "$(printf '%s' "$name" | escape)"
   # Keep the old shim alias for existing consumers, alongside macOS sppci_bus.
   echo "    <key>spdisplays_bus</key><string>spdisplays_$bus</string>"
   gpu_fields | fields_xml
   if [ -n "$panels" ]; then
    echo '    <key>spdisplays_ndrvs</key><array>'
    printf '%s\n' "$panels" | while IFS='|' read -r sid connector width height main mirror internal sname; do
     echo '     <dict>'
     printf '      <key>_name</key><string>%s</string>\n' "$(printf '%s' "$sname" | escape)"
     echo "      <key>_spdisplays_displayID</key><string>$sid</string>"
     screen_fields | fields_xml
     echo '     </dict>'
    done
    echo '    </array>'
   fi
   echo '   </dict>'
  else
   printf '    %s:\n\n' "$name"
   gpu_fields | fields_text '      '
   if [ -n "$panels" ]; then
    echo '      Displays:'
    printf '%s\n' "$panels" | while IFS='|' read -r sid connector width height main mirror internal sname; do
     printf '        %s:\n' "$sname"
     screen_fields | fields_text '          '
    done
   fi
   echo
  fi
 done
}

load_platform() { # IOPlatformExpertDevice string properties, "key|value" per line
 # String values print as "value", NUL-terminated data as <"value">; other data is skipped.
 platform_properties=$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null |
  sed -n 's/^[ |]*"\([^"]*\)" = <\{0,1\}"\(.*\)">\{0,1\}$/\1|\2/p')
}
platform() {
 printf '%s\n' "$platform_properties" | awk -v key="$1" 'index($0, key "|") == 1 { print substr($0, length(key) + 2); exit }'
}

machine_name() { # model identifier -> marketing family, as in About This Mac
 case "$1" in
  MacBookPro*) echo 'MacBook Pro';; MacBookAir*) echo 'MacBook Air';; MacBook*) echo 'MacBook';;
  iMacPro*) echo 'iMac Pro';; iMac*) echo 'iMac';; Macmini*) echo 'Mac mini';; MacPro*) echo 'Mac Pro';;
  *) echo 'Mac';;
 esac
}

hardware() { # label|xml key|xml type|value for each known field
 load_platform
 model=$(platform model)
 cores=$(sysctl -n hw.physicalcpu 2>/dev/null)
 memory=$(sysctl -n hw.memsize 2>/dev/null | awk '$1 > 0 { printf "%d GB", $1 / 1073741824 + 0.5 }')
 speed=$(sysctl -n hw.cpufrequency 2>/dev/null | awk '$1 > 0 { printf "%.1f GHz", $1 / 1e9 }')
 l2=$(sysctl -n hw.l2cachesize 2>/dev/null | awk '$1 > 0 { printf "%d KB", $1 / 1024 }')
 l3=$(sysctl -n hw.l3cachesize 2>/dev/null | awk '$1 > 0 { printf "%d MB", $1 / 1048576 }')
 echo "Model Name|machine_name|string|${model:+$(machine_name "$model")}"
 echo "Model Identifier|machine_model|string|$model"
 echo "Processor Name|cpu_type|string|$(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
 echo "Processor Speed|current_processor_speed|string|$speed"
 echo "Number of Processors|packages|integer|$(sysctl -n hw.packages 2>/dev/null)"
 echo "Total Number of Cores|number_processors|integer|$cores"
 echo "L2 Cache (per Core)|l2_cache_core|string|$l2"
 echo "L3 Cache|l3_cache|string|$l3"
 echo "Memory|physical_memory|string|$memory"
 echo "Serial Number (system)|serial_number|string|$(platform IOPlatformSerialNumber)"
 echo "Hardware UUID|platform_UUID|string|$(platform IOPlatformUUID)"
 echo "Provisioning UDID|provisioning_UDID|string|$(platform provisioning_UDID)"
}

software() {
 name=$(sw_vers -productName 2>/dev/null); version=$(sw_vers -productVersion 2>/dev/null)
 build=$(sw_vers -buildVersion 2>/dev/null)
 echo "System Version|os_version|string|${version:+${name:-macOS} $version${build:+ ($build)}}"
 echo "Kernel Version|kernel_version|string|$(uname -sr 2>/dev/null)"
 echo "Boot Volume|boot_volume|string|Macintosh HD"
 echo "Boot Mode|boot_mode|string|normal_boot"
 echo "Computer Name|local_host_name|string|$(hostname -s 2>/dev/null)"
 echo "User Name|user_name|string|${USER:-$(id -un 2>/dev/null)}"
}

overview_xml() { # $1 = _name; fields on stdin; empty values are omitted
 echo '   <dict>'
 echo "    <key>_name</key><string>$1</string>"
 while IFS='|' read -r label key kind value; do
  [ -n "$value" ] || continue
  if [ "$kind" = integer ]; then
   case "$value" in *[!0-9]*) continue;; esac
   echo "    <key>$key</key><integer>$value</integer>"
  else
   echo "    <key>$key</key><string>$(printf '%s' "$value" | escape)</string>"
  fi
 done
 echo '   </dict>'
}

overview_text() { # $1 = section title, $2 = overview title; fields on stdin
 printf '%s:\n\n    %s:\n\n' "$1" "$2"
 while IFS='|' read -r label key kind value; do
  [ "$key" != boot_mode ] || value=Normal
  [ -z "$value" ] || printf '      %s: %s\n' "$label" "$value"
 done
 echo
}

interfaces() { # $1 = wired|wireless; physical host interfaces only
 for path in "$host"/sys/class/net/*; do
  [ -e "$path/device" ] || continue
  if [ -d "$path/wireless" ] || [ -e "$path/phy80211" ]; then kind=wireless; else kind=wired; fi
  [ "$kind" = "$1" ] && basename "$path"
 done
}

if [ "$xml" = 1 ]; then
 echo '<?xml version="1.0" encoding="UTF-8"?>'
 echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
 echo '<plist version="1.0">'
 echo '<array>'
 for type in $types; do
  echo ' <dict>'
  echo "  <key>_dataType</key><string>$type</string>"
  echo '  <key>_items</key>'
  echo '  <array>'
  case "$type" in
   SPHardwareDataType) hardware | overview_xml hardware_overview;;
   SPSoftwareDataType) software | overview_xml os_overview;;
   SPDisplaysDataType) display_report xml;;
   SPEthernetDataType)
    interfaces wired | while read -r name; do
     echo '   <dict>'
     echo '    <key>_name</key><string>ethernet</string>'
     echo "    <key>spethernet_BSD_Name</key><string>$name</string>"
     echo '   </dict>'
    done;;
   SPAirPortDataType)
    echo '   <dict>'
    echo '    <key>spairport_airport_interfaces</key>'
    echo '    <array>'
    interfaces wireless | while read -r name; do
     echo '     <dict>'
     echo "      <key>_name</key><string>$name</string>"
     echo '      <key>spairport_wireless_card_type</key><string>Wi-Fi</string>'
     echo '     </dict>'
    done
    echo '    </array>'
    echo '   </dict>';;
  esac
  echo '  </array>'
  echo ' </dict>'
 done
 echo '</array>'
 echo '</plist>'
 exit 0
fi

for type in $types; do
 case "$type" in
  SPHardwareDataType) hardware | overview_text Hardware 'Hardware Overview';;
  SPSoftwareDataType) software | overview_text Software 'System Software Overview';;
  SPDisplaysDataType)
   printf 'Graphics/Displays:\n\n'
   display_report text;;
  SPEthernetDataType)
   printf 'Ethernet Cards:\n\n'
   interfaces wired | while read -r name; do
    printf '    ethernet:\n\n      Type: Ethernet Controller\n      BSD name: %s\n\n' "$name"
   done;;
  SPAirPortDataType)
   printf 'Wi-Fi:\n\n      Interfaces:\n'
   interfaces wireless | while read -r name; do
    printf '        %s:\n          Card Type: Wi-Fi\n\n' "$name"
   done;;
 esac
done
