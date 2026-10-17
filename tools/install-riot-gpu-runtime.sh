#!/bin/sh
# Build/install the paired GPU, NV12 and display-clock runtime overrides.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
guest_home=${RIOT_GUEST_HOME:-$HOME/.darling@GUEST_HOME@}
cd "$project_dir"
python3 tools/build-m58f333-iosurface.py
# Rebuild the native wrapper with floating-point-safe symbol resolvers.
make -C build/darling-src/build/src/native GL -j4
make -C build/darling-src/build/src/frameworks/CoreServices/src/LaunchServices LaunchServices -j4
# WebAudio gain, mixing, filters and FFTs need real vDSP kernels, not stubs.
make -C build/darling-src/build/src/frameworks/Accelerate/vecLib/vDSP vDSP -j4
python3 tools/build-m58f327-opengl.py
python3 tools/build-m58f336-quartzcore.py
python3 tools/build-m58f341-corevideo.py
python3 tools/build-riot-x11-runtime.py
python3 tools/build-riot-appkit-runtime.py
python3 tools/build-riot-coregraphics-runtime.py
python3 tools/build-riot-coretext-runtime.py
python3 tools/build-riot-carboncore-runtime.py
python3 tools/build-riot-corefoundation-runtime.py
python3 tools/build-riot-kqueue-runtime.py
python3 tools/build-riot-malloc-runtime.py
python3 tools/build-riot-pthread-runtime.py
python3 tools/build-riot-audio-runtime.py
python3 tools/build-riot-objc-runtime.py
python3 tools/build-riot-cxxabi-runtime.py
python3 tools/build-riot-indium-runtime.py
python3 tools/build-riot-metal-runtime.py
python3 tools/build-riot-server-runtime.py
python3 tools/ensure-darling-root-certificates.py
build/riot-host-message-check
cc -shared -fPIC -O2 -Wall -Wextra -Wl,-soname,"$project_dir/build/libdarling-xshm.so" tools/m58f299-xshm.c -o build/libdarling-xshm.so.next -pthread $(pkg-config --cflags --libs x11 x11-xcb xcb-shm xext)
python3 tools/stop-darling-runtime.py
# Host fileports own SCM_RIGHTS descriptors and must die with their last sender.
if sudo -n true 2>/dev/null; then
  sudo make -C "$project_dir/build/darling-src/build/src/external/darlingserver" install -j12
else
  echo 'Skipped the darlingserver install (needs sudo); the base install already ships it: sudo cmake --install build/darling-src/build' >&2
fi
# Electron's window-visibility correction lives in the AppKit overlay
# (NSWindow -occlusionState, cocotron.patch); no Riot binary is modified here.
for name in IOSurface OpenGL QuartzCore CoreVideo CoreAudio AudioToolbox CoreGraphics CoreText CoreFoundation Metal; do
 framework="$guest_home/RuntimeFrameworks/$name.framework"
 if [ ! -d "$framework" ]; then cp -a "@DARLING_SDK@/System/Library/Frameworks/$name.framework" "$framework"; fi
 binary="$framework/Versions/A/$name"
 if [ ! -f "$binary.before-gpu-direct" ]; then cp -p "$binary" "$binary.before-gpu-direct"; fi
 case "$name" in
 IOSurface) source=build/m333-IOSurface;;
 OpenGL) source=build/m327-OpenGL;;
 # Use the artifact rebuilt by this invocation, not an older optional probe build.
 QuartzCore) source=build/m336-QuartzCore;;
 CoreVideo) source=build/m341-CoreVideo;;
 CoreAudio) source=build/riot-CoreAudio;;
 AudioToolbox) source=build/riot-AudioToolbox;;
 CoreGraphics) source=build/riot-CoreGraphics;;
 CoreText) source=build/riot-CoreText;;
 CoreFoundation) source=build/riot-CoreFoundation;;
 Metal) source=build/riot-Metal;;
 esac
 install -m755 "$source" "$binary.next"
 mv "$binary.next" "$binary"
 # Dyld resolves Versions/A. Preserve the standard outer symlink as well.
 if [ ! -L "$framework/$name" ]; then rm -f "$framework/$name"; ln -s "Versions/Current/$name" "$framework/$name"; fi
done
# Legacy version queries must also reach the corrected absolute subframework.
sh tools/install-riot-carboncore-runtime.sh
# Re-exported GL symbols use the native library's absolute install name.
# DYLD_LIBRARY_PATH must see this rebuilt wrapper as well as the framework copy.
mkdir -p "$guest_home/RuntimeLibraries"
gl_wrapper="$guest_home/RuntimeLibraries/libGL.dylib"
if [ ! -f "$gl_wrapper.before-float-resolver" ]; then
 if [ -f "$gl_wrapper" ]; then cp -p "$gl_wrapper" "$gl_wrapper.before-float-resolver"
 else cp -p @DARLING_SDK@/System/Library/Frameworks/OpenGL.framework/Versions/A/Libraries/libGL.dylib "$gl_wrapper.before-float-resolver"; fi
fi
install -m755 build/darling-src/build/src/native/libGL.dylib "$gl_wrapper.next"
mv "$gl_wrapper.next" "$gl_wrapper"
install -m755 build/darling-src/build/src/native/libGL.dylib "$guest_home/RuntimeFrameworks/OpenGL.framework/Versions/A/Libraries/libGL.dylib.next"
mv "$guest_home/RuntimeFrameworks/OpenGL.framework/Versions/A/Libraries/libGL.dylib.next" "$guest_home/RuntimeFrameworks/OpenGL.framework/Versions/A/Libraries/libGL.dylib"
launchservices="$guest_home/RuntimeFrameworks/LaunchServices.framework"
if [ ! -d "$launchservices" ]; then
 cp -a @DARLING_SDK@/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework "$launchservices"
fi
launchservices_binary="$launchservices/Versions/A/LaunchServices"
if [ ! -f "$launchservices_binary.before-null-error" ]; then cp -p "$launchservices_binary" "$launchservices_binary.before-null-error"; fi
install -m755 build/darling-src/build/src/frameworks/CoreServices/src/LaunchServices/LaunchServices "$launchservices_binary.next"
mv "$launchservices_binary.next" "$launchservices_binary"
# Electron opens this subframework by its absolute path, bypassing the
# framework search override. Replace the same binary in the stopped prefix.
prefix_dir="${DPREFIX:-$HOME/.darling}"
launchservices_prefix="$prefix_dir/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/LaunchServices"
mkdir -p "$(dirname "$launchservices_prefix")"
if [ ! -f "$launchservices_prefix.before-null-error" ]; then
 if [ -f "$launchservices_prefix" ]; then cp -p "$launchservices_prefix" "$launchservices_prefix.before-null-error"
 else cp -p @DARLING_SDK@/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/LaunchServices "$launchservices_prefix.before-null-error"; fi
fi
install -m755 build/darling-src/build/src/frameworks/CoreServices/src/LaunchServices/LaunchServices "$launchservices_prefix.next"
mv "$launchservices_prefix.next" "$launchservices_prefix"
# Accelerate re-exports libvDSP by absolute path, so replace it in the stopped prefix too.
vdsp_prefix="$prefix_dir/System/Library/Frameworks/Accelerate.framework/Versions/A/Frameworks/vecLib.framework/Versions/A/libvDSP.dylib"
mkdir -p "$(dirname "$vdsp_prefix")"
if [ ! -f "$vdsp_prefix.before-vdsp" ]; then
 if [ -f "$vdsp_prefix" ]; then cp -p "$vdsp_prefix" "$vdsp_prefix.before-vdsp"
 else cp -p @DARLING_SDK@/System/Library/Frameworks/Accelerate.framework/Versions/A/Frameworks/vecLib.framework/Versions/A/libvDSP.dylib "$vdsp_prefix.before-vdsp"; fi
fi
install -m755 build/darling-src/build/src/frameworks/Accelerate/vecLib/vDSP/libvDSP.dylib "$vdsp_prefix.next"
mv "$vdsp_prefix.next" "$vdsp_prefix"

component="$guest_home/Library/Audio/Plug-Ins/Components/DarlingRiotAudio.component"
if [ ! -d "$component" ]; then
 mkdir -p "$(dirname "$component")"
 cp -a @DARLING_SDK@/System/Library/Components/CoreAudio.component "$component"
fi
component_binary="$component/Contents/MacOS/CoreAudio"
if [ ! -f "$component_binary.before-gpu-direct" ]; then cp -p "$component_binary" "$component_binary.before-gpu-direct"; fi
install -m755 build/riot-CoreAudioComponent "$component_binary.next"
mv "$component_binary.next" "$component_binary"
python3 - "$component/Contents/Info.plist" <<'PY'
from pathlib import Path
import plistlib
import sys
p=Path(sys.argv[1])
info=plistlib.loads(Path('build/darling-src/src/CoreAudio/CoreAudioComponent/Info.plist').read_bytes())
for component in info['AudioComponents']:
    component['version']=3
p.write_bytes(plistlib.dumps(info))
PY
x11="$guest_home/RuntimeFrameworks/AppKit.framework/Versions/C/Resources/Backends/X11.backend/Contents/MacOS/X11"
appkit="$guest_home/RuntimeFrameworks/AppKit.framework/Versions/C/AppKit"
if [ ! -f "$appkit.before-gpu-direct" ]; then cp -p "$appkit" "$appkit.before-gpu-direct"; fi
install -m755 build/riot-AppKit "$appkit.next"
mv "$appkit.next" "$appkit"
if [ ! -f "$x11.before-gpu-direct" ]; then cp -p "$x11" "$x11.before-gpu-direct"; fi
install -m755 build/riot-X11 "$x11.next"
mv "$x11.next" "$x11"
kernel="$guest_home/RuntimeLibraries/libsystem_kernel.dylib"
if [ ! -f "$kernel.before-gpu-direct" ]; then cp -p "$kernel" "$kernel.before-gpu-direct"; fi
install -m755 build/riot-libsystem_kernel.dylib "$kernel.next"
mv "$kernel.next" "$kernel"
allocator="$guest_home/RuntimeLibraries/libsystem_malloc.dylib"
if [ ! -f "$allocator.before-gpu-direct" ]; then
 if [ -f "$allocator" ]; then cp -p "$allocator" "$allocator.before-gpu-direct"
 else cp -p @DARLING_SDK@/usr/lib/system/libsystem_malloc.dylib "$allocator.before-gpu-direct"; fi
fi
install -m755 build/riot-libsystem_malloc.dylib "$allocator.next"
mv "$allocator.next" "$allocator"
if [ -f build/libdarling-xshm.so ] && [ ! -f build/libdarling-xshm.so.before-gpu-direct ]; then cp -p build/libdarling-xshm.so build/libdarling-xshm.so.before-gpu-direct; fi
mv build/libdarling-xshm.so.next build/libdarling-xshm.so

objc_library="$guest_home/RuntimeLibraries/libobjc.A.dylib"
if [ ! -f "$objc_library.before-m761" ]; then
 if [ -f "$objc_library" ]; then cp -p "$objc_library" "$objc_library.before-m761"
 else cp -p @DARLING_SDK@/usr/lib/libobjc.A.dylib "$objc_library.before-m761"; fi
fi
install -m755 build/riot-libobjc.A.dylib "$objc_library.next"
mv "$objc_library.next" "$objc_library"

cxxabi_library="$guest_home/RuntimeLibraries/libc++abi.dylib"
if [ ! -f "$cxxabi_library.before-m765" ]; then
 if [ -f "$cxxabi_library" ]; then cp -p "$cxxabi_library" "$cxxabi_library.before-m765"
 else cp -p @DARLING_SDK@/usr/lib/libc++abi.dylib "$cxxabi_library.before-m765"; fi
fi
install -m755 build/riot-libc++abi.dylib "$cxxabi_library.next"
mv "$cxxabi_library.next" "$cxxabi_library"

indium_library="$guest_home/RuntimeLibraries/libindium.dylib"
if [ ! -f "$indium_library.before-m766" ]; then
 if [ -f "$indium_library" ]; then cp -p "$indium_library" "$indium_library.before-m766"
 else cp -p @DARLING_SDK@/usr/lib/darling/libindium.dylib "$indium_library.before-m766"; fi
fi
install -m755 build/riot-libindium.dylib "$indium_library.next"
mv "$indium_library.next" "$indium_library"

# libindium loads libiridium (shader translation and reflection) from the library path
iridium_library="$guest_home/RuntimeLibraries/libiridium.dylib"
if [ ! -f "$iridium_library.before-m767" ]; then
 if [ -f "$iridium_library" ]; then cp -p "$iridium_library" "$iridium_library.before-m767"
 else cp -p @DARLING_SDK@/usr/lib/darling/libiridium.dylib "$iridium_library.before-m767"; fi
fi
install -m755 build/riot-libiridium.dylib "$iridium_library.next"
mv "$iridium_library.next" "$iridium_library"

pthread_library="$guest_home/RuntimeLibraries/libsystem_pthread.dylib"
if [ ! -f "$pthread_library.before-gpu-direct" ]; then
 if [ -f "$pthread_library" ]; then cp -p "$pthread_library" "$pthread_library.before-gpu-direct"
 else cp -p @DARLING_SDK@/usr/lib/system/libsystem_pthread.dylib "$pthread_library.before-gpu-direct"; fi
fi
install -m755 build/riot-libsystem_pthread.dylib "$pthread_library.next"
mv "$pthread_library.next" "$pthread_library"
