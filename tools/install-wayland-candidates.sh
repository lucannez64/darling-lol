# Sourced by the runtime installers after stopping Darling.
if [ -f "$project_dir/build/libdarling-wayland.next.so" ] && [ -f "$project_dir/build/libDarlingWayland.next.dylib" ]; then
  install -m755 "$project_dir/build/libDarlingWayland.next.dylib" "$guest_home/libDarlingWayland.next.dylib"
  mv -f "$project_dir/build/libdarling-wayland.next.so" "$project_dir/build/libdarling-wayland.so"
  mv -f "$guest_home/libDarlingWayland.next.dylib" "$guest_home/libDarlingWayland.dylib"
  rm -f "$project_dir/build/libDarlingWayland.next.dylib"
fi
