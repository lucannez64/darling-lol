#!/bin/sh
# system_profiler for the Darling prefix: reports the host's real display and
# network hardware for the data types applications query at startup. The hardware
# and software overviews come from the guest's own IOKit, sysctl and sw_vers, so they
# match what other APIs report (including a configure-darling-platform.py override).
# Installed as /usr/sbin/system_profiler by tools/install-darling-system-profiler.py.
host=/Volumes/SystemRoot
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
}

displays() { # one line per GPU: vendor|device|vram MB or empty|name|bus
 # Same switch as the Metal device name: a Mac profile presents the MacBookPro16,1
 # discrete GPU instead of the host card, so system_profiler and Metal agree.
 mac_spoof=0
 case "${DARLING_METAL_MAC_GPU_NAME:-}" in '') ;; 0) ;; *) mac_spoof=1;; esac
 for card in "$host"/sys/class/drm/card[0-9] "$host"/sys/class/drm/card[0-9][0-9]; do
  [ -r "$card/device/vendor" ] || continue
  vendor=$(sed 's/^0x//' "$card/device/vendor"); device=$(sed 's/^0x//' "$card/device/device")
  vram=
  if [ -r "$card/device/mem_info_vram_total" ]; then
   vram=$(awk '{ printf "%d", $1 / 1048576 }' "$card/device/mem_info_vram_total")
  fi
  name=$(pci_name "$vendor" "$device"); [ -n "$name" ] || name="Display"
  bus=builtin
  if [ "$mac_spoof" = 1 ]; then
   case "$vendor" in
    1002|10de) vendor=1002; device=7340; name='AMD Radeon Pro 5500M'
               vram=${DARLING_METAL_VRAM_MB:-4096}
               case "$vram" in ''|*[!0-9]*) vram=4096;; esac
               # Keep the numeric test overflow-safe: a long digit string must
               # fall back before the shell compares it.
               if [ "${#vram}" -gt 7 ] || [ "$vram" -eq 0 ] || [ "$vram" -gt 1048576 ]; then
                vram=4096
               fi
               bus=pcie;;
    8086) vendor=8086; device=3e9b; name='Intel UHD Graphics 630'
          vram=; bus=builtin;;
   esac
  fi
  echo "$vendor|$device|$vram|$name|$bus"
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
   SPDisplaysDataType)
    displays | while IFS='|' read -r vendor device vram name bus; do
     name=$(printf '%s' "$name" | escape)
     echo '   <dict>'
     echo "    <key>_name</key><string>$name</string>"
     echo "    <key>spdisplays_bus</key><string>spdisplays_$bus</string>"
     echo "    <key>spdisplays_device-id</key><string>0x$device</string>"
     echo "    <key>spdisplays_vendor</key><string>${name%% *} (0x$vendor)</string>"
     [ -z "$vram" ] || echo "    <key>spdisplays_vram</key><string>$vram MB</string>"
     echo "    <key>sppci_model</key><string>$name</string>"
     echo '    <key>sppci_device_type</key><string>spdisplays_gpu</string>'
     echo '   </dict>'
    done;;
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
   displays | while IFS='|' read -r vendor device vram name bus; do
    [ "$bus" = pcie ] && bus_text=PCIe || bus_text='Built-In'
    printf '    %s:\n\n      Chipset Model: %s\n      Type: GPU\n      Bus: %s\n' "$name" "$name" "$bus_text"
    [ -z "$vram" ] || printf '      VRAM (Total): %s MB\n' "$vram"
    printf '      Vendor: %s (0x%s)\n      Device ID: 0x%s\n\n' "${name%% *}" "$vendor" "$device"
   done;;
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
