{
  description = "Unofficial Darling launcher for the macOS game client on Linux";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      llvmPackages = pkgs.llvmPackages_21;

      # Host /etc/fonts survives buildFHSEnv, but host /usr/share/fonts does
      # not. Use store paths for both text fonts and the standard alias rules.
      darlingFonts = [ pkgs.liberation_ttf pkgs.dejavu_fonts ];
      darlingFontsConf = pkgs.makeFontsConf {
        fontDirectories = darlingFonts;
        includes = [
          "${pkgs.fontconfig.out}/etc/fonts/conf.d"
          # makeFontsConf adds XDG font directories, but neither it nor
          # conf.d/50-user.conf adds the legacy ~/.fonts directory.
          (pkgs.writeText "darling-user-fonts.conf" ''
            <fontconfig>
              <dir>~/.fonts</dir>
            </fontconfig>
          '')
        ];
      };

      # Python environment with dependencies required by tooling
      pythonEnv = pkgs.python3.withPackages (
        ps: with ps; [
          requests
          urllib3
          websocket-client
          pillow
        ]
      );

      darlingEntry = pkgs.writeShellScript "darling-lol-entry" (builtins.readFile ./scripts/fhs-entry.sh);
      darlingRunner = pkgs.runCommand "darling-lol-rootless-runner" { } ''
        mkdir -p $out/bin
        cp ${./scripts/darling-rootless.sh} $out/bin/darling
        chmod +x $out/bin/darling
        patchShebangs $out/bin/darling
      '';

      # Common runtime libraries needed by Darling, Vulkan, X11, Audio, and graphics
      buildDeps = with pkgs; [
        # Build tools & compilers
        cmake
        gnumake
        ninja
        pkg-config
        llvmPackages.clang
        llvmPackages.lld
        llvmPackages.llvm
        llvmPackages.clang-unwrapped
        bison
        flex
        git
        git-lfs
        nix
        curl
        unzip
        file
        patchelf
        util-linux

        # System & core libraries
        glibc
        glibc.static
        zlib
        libffi
        libbsd
        libmd
        libcap
        libelf
        fuse
        systemdLibs # libudev
        dbus
        libxml2
        openssl
        libedit

        # Graphics & Display
        libGL
        libGLU
        mesa
        vulkan-headers
        vulkan-loader
        vulkan-tools
        cairo
        freetype
        fontconfig
        libpng
        libpng.dev
        libjpeg
        libjpeg.dev
        libtiff
        giflib
        # Darling's generated AV wrappers bind FFmpeg 8 SONAMEs (e.g. .62).
        # Do not let nixpkgs' default FFmpeg major silently break installed builds.
        ffmpeg_8

        # X11 & input
        xorgproto
        libx11
        libxext
        libxrandr
        libxcursor
        libxinerama
        libxcomposite
        libxdamage
        libxfixes
        libxi
        libxrender
        libxtst
        libxcb
        xcbutil
        xcbutilwm
        xcbutilimage
        xcbutilkeysyms
        xcbutilrenderutil
        libxkbfile
        libxkbcommon

        # Audio
        pulseaudio
        libpulseaudio
        alsa-lib

        # Python
        pythonEnv
      ];

      # FHS environment for building and running Darling and the macOS game client.
      # Darling requires root/multitasking mount & namespace semantics, /usr/local layout,
      # and dynamically compiled overlays. An FHS environment provides the standard standard
      # filesystem hierarchies (/usr/include, /usr/lib, /usr/bin) and native dynamic linker.
      darlingLolFhs = pkgs.buildFHSEnv {
        name = "darling-lol-fhs";
        targetPkgs = pkgs: buildDeps;
        # multiArch is disabled; glibc is already in targetPkgs and the FHS base.
        extraOutputsToInstall = [ "dev" "out" ];

        extraBwrapArgs = [
          # The outer user namespace maps subordinate IDs before no_new_privs.
          # These capabilities apply only inside that namespace, not on the host.
          "--cap-add" "ALL"
          "--bind-try" (''$'' + ''{DARLING_WORKSPACE}/usr-local'') "/usr-local"
        ];

        extraBuildCommands = ''
          mkdir -p "$out/usr/bin"
          rm -f "$out/usr/bin/clang" "$out/usr/bin/clang++"
          ln -sfn /usr-local "$out/usr/local"
          {
            printf '%s\n' '#!/bin/sh' \
              'wrapped=${llvmPackages.clang}/bin' \
              'unwrapped=${llvmPackages.clang-unwrapped}/bin'
            cat ${./scripts/nix-compiler.sh}
          } > "$out/usr/bin/clang"
          chmod +x "$out/usr/bin/clang"
          ln -s clang "$out/usr/bin/clang++"
          # /bin is already a guest-absolute symlink to /usr/bin.
        '';

        profile = ''
          export DPREFIX="''${DPREFIX:-$HOME/.darling}"
          export LC_ALL=C.UTF-8
          export LANG=C.UTF-8
          unset FONTCONFIG_PATH FONTCONFIG_SYSROOT
          export FONTCONFIG_FILE=${darlingFontsConf}
          export DARLING_RUNNER_DIR=${darlingRunner}/bin
          export PATH="$DARLING_RUNNER_DIR:/usr/local/bin:''${PATH:-}"
          export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:/usr/local/share/pkgconfig:/usr/lib/pkgconfig:/usr/lib64/pkgconfig:/usr/share/pkgconfig:''${PKG_CONFIG_PATH:-}"
          export CMAKE_PREFIX_PATH="/usr/local:/usr:/usr/lib/cmake/llvm:''${CMAKE_PREFIX_PATH:-}"
          unset NIX_LDFLAGS NIX_CFLAGS_COMPILE NIX_HARDENING_ENABLE CPATH LIBRARY_PATH
          # NixOS keeps the GPU driver's EGL vendor and external-platform files under
          # /run/opengl-driver, which glvnd and NVIDIA's driver in this FHS environment
          # do not search. Without them glvnd only finds Mesa, and NVIDIA cannot open
          # X11 displays (its X11 support is the egl-x11 external platform): the
          # client renders with Mesa's software rasterizer, which garbles text and
          # cannot present the game's NVIDIA Vulkan frames. FHS paths stay as fallback.
          if [ -d /run/opengl-driver/share/glvnd/egl_vendor.d ]; then
            export __EGL_VENDOR_LIBRARY_DIRS="/run/opengl-driver/share/glvnd/egl_vendor.d:/etc/glvnd/egl_vendor.d:/usr/share/glvnd/egl_vendor.d''${__EGL_VENDOR_LIBRARY_DIRS:+:$__EGL_VENDOR_LIBRARY_DIRS}"
          fi
          if [ -d /run/opengl-driver/share/egl/egl_external_platform.d ]; then
            export __EGL_EXTERNAL_PLATFORM_CONFIG_DIRS="/run/opengl-driver/share/egl/egl_external_platform.d:/etc/egl/egl_external_platform.d:/usr/share/egl/egl_external_platform.d''${__EGL_EXTERNAL_PLATFORM_CONFIG_DIRS:+:$__EGL_EXTERNAL_PLATFORM_CONFIG_DIRS}"
          fi
        '';

        runScript = pkgs.writeShellScript "darling-lol-run" ''
          if [ $# -eq 0 ]; then
            exec ${darlingEntry} bash
          else
            exec ${darlingEntry} "$@"
          fi
        '';
      };

      # Wrapper script package to run scripts directly
      darlingLol = pkgs.stdenv.mkDerivation {
        pname = "darling-lol";
        version = "0.1.0";
        src = pkgs.lib.cleanSourceWith {
          src = ./.;
          filter = path: type:
            let name = baseNameOf path; in
            !(builtins.elem name [ ".git" "build" "__pycache__" "usr-local" "darling-install" ])
            && !(pkgs.lib.hasPrefix "result" name);
        };

        nativeBuildInputs = [
          pkgs.makeWrapper
        ];

        installPhase = ''
          runHook preInstall

          mkdir -p $out/bin $out/share/darling-lol $out/share/icons/hicolor/scalable/apps $out/share/icons/hicolor/128x128/apps $out/share/pixmaps $out/share/applications
          # "." (not "*") keeps dotfiles such as .gitignore and .github that the
          # packaging check runs the repository tests against.
          cp -r . $out/share/darling-lol/
          cp darling-lol.svg $out/share/icons/hicolor/scalable/apps/darling-launcher.svg
          cp darling-lol.png $out/share/icons/hicolor/128x128/apps/darling-launcher.png
          cp darling-lol.png $out/share/pixmaps/darling-launcher.png
          cp darling-lol.svg $out/share/pixmaps/darling-launcher.svg

          # Install configured launcher
          substitute ./darling-lol-launcher.sh $out/bin/darling-lol \
            --replace-fail "@SHARE_DIR@" "$out/share/darling-lol" \
            --replace-fail "@FHS_BIN@" "${darlingLolFhs}/bin/darling-lol-fhs" \
            --replace-fail "@UNSHARE_BIN@" "${pkgs.util-linux}/bin/unshare"
          chmod +x $out/bin/darling-lol
          wrapProgram $out/bin/darling-lol --prefix PATH : ${pkgs.lib.makeBinPath [ pythonEnv pkgs.coreutils pkgs.util-linux pkgs.gawk ]}
          ln -s darling-lol $out/bin/darling-launcher

          makeWrapper $out/bin/darling-lol $out/bin/darling-lol-installer \
            --add-flags "--installer"

          makeWrapper $out/bin/darling-lol $out/bin/darling-lol-shell \
            --add-flags "--shell"

          makeWrapper $out/bin/darling-lol $out/bin/darling-lol-build \
            --add-flags "./scripts/build.sh"

          cat << 'DESKTOP_EOF' > $out/share/applications/darling-launcher.desktop
[Desktop Entry]
Categories=Game;
Comment=Unofficial launcher for the macOS game client via Darling
Exec=darling-launcher
GenericName=Game
Icon=darling-launcher
Keywords=Darling;Launcher;Compatibility;
Name=Darling Launcher
StartupWMClass=RiotClientServices
Terminal=true
Type=Application
Version=1.5
DESKTOP_EOF

          runHook postInstall
        '';

        meta = with pkgs.lib; {
          description = "Unofficial tooling and patches to run the macOS game client on Linux with Darling";
          homepage = "https://github.com/lucannez64/darling-lol";
          license = licenses.gpl3Only;
          platforms = [ "x86_64-linux" ];
        };
      };
    in
    {
      checks.${system} = {
        fonts = pkgs.runCommand "darling-lol-font-tests" {
          nativeBuildInputs = [ pkgs.fontconfig ];
        } ''
          export HOME="$TMPDIR/home with spaces" XDG_CONFIG_HOME="$TMPDIR/config with spaces"
          export XDG_DATA_HOME="$TMPDIR/data with spaces" XDG_CACHE_HOME="$TMPDIR/cache with spaces"
          unset FONTCONFIG_PATH FONTCONFIG_SYSROOT
          export FONTCONFIG_FILE=${darlingFontsConf}
          mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
          # Check real font selection with no host or user text fonts available.
          check_font() {
            actual=$(fc-match --format '%{family}' "$1")
            if [ "$actual" != "$2" ]; then
              echo "Font match for $1: expected $2, got $actual" >&2
              exit 1
            fi
            test -r "$(fc-match --format '%{file}' "$1")"
          }
          check_aliases() {
            check_font sans-serif 'DejaVu Sans'
            check_font serif 'DejaVu Serif'
            check_font monospace 'DejaVu Sans Mono'
            check_font Arial 'Liberation Sans'
            check_font 'Times New Roman' 'Liberation Serif'
            check_font 'Courier New' 'Liberation Mono'
          }
          check_aliases
          # Reproduce an icon-only user inventory without host text fonts.
          cp -R ${pkgs.font-awesome}/share/fonts "$XDG_DATA_HOME/fonts"
          fc-list --format '%{file}\n' | grep -F "$XDG_DATA_HOME/fonts/"
          check_aliases

          # Retain custom XDG and legacy fonts as well as user alias rules.
          chmod u+w "$XDG_DATA_HOME/fonts"
          mkdir -p "$HOME/.fonts" "$XDG_CONFIG_HOME/fontconfig"
          cp ${pkgs.liberation_ttf}/share/fonts/truetype/LiberationSans-Regular.ttf "$XDG_DATA_HOME/fonts/user-text.ttf"
          cp ${pkgs.liberation_ttf}/share/fonts/truetype/LiberationSerif-Regular.ttf "$HOME/.fonts/legacy-text.ttf"
          fc-list --format '%{file}\n' | grep -Fx "$XDG_DATA_HOME/fonts/user-text.ttf"
          fc-list --format '%{file}\n' | grep -Fx "$HOME/.fonts/legacy-text.ttf"
          cat > "$XDG_CONFIG_HOME/fontconfig/fonts.conf" <<'USERCONF_EOF'
          <fontconfig>
            <alias>
              <family>DarlingCompatibilityAlias</family>
              <prefer><family>Liberation Serif</family></prefer>
            </alias>
          </fontconfig>
          USERCONF_EOF
          check_font DarlingCompatibilityAlias 'Liberation Serif'
          touch "$out"
        '';

        # runCommandCC provides cc/c++ for the tests that compile host C checks.
        packaging = pkgs.runCommandCC "darling-lol-packaging-tests" {
          nativeBuildInputs = [ pythonEnv pkgs.bash pkgs.git pkgs.git-lfs pkgs.coreutils pkgs.util-linux pkgs.gawk pkgs.pkg-config ];
          buildInputs = [ pkgs.libxml2 pkgs.openssl ];
        } ''
          cp -R ${darlingLol}/share/darling-lol source
          chmod -R u+w source
          cd source
          export HOME="$TMPDIR/home" PYTHONDONTWRITEBYTECODE=1
          # The sandbox has no /etc/ssl; the root-certificate tests read a CA bundle.
          export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
          mkdir -p "$HOME"
          # Tests use private stub binaries/repositories, never Darling itself.
          python3 -m unittest discover -s tests -v
          python3 scripts/validate.py
          grep -q '^Terminal=true$' ${darlingLol}/share/applications/darling-launcher.desktop
          grep -q '^Name=Darling Launcher$' ${darlingLol}/share/applications/darling-launcher.desktop
          grep -q '^Icon=darling-launcher$' ${darlingLol}/share/applications/darling-launcher.desktop
          grep -q '^Keywords=Darling;Launcher;Compatibility;$' ${darlingLol}/share/applications/darling-launcher.desktop
          grep -q '^Categories=Game;$' ${darlingLol}/share/applications/darling-launcher.desktop
          # Darling derives WM_CLASS from argv[0], so the window class is the
          # client executable name; this is an interoperability identifier.
          grep -q '^StartupWMClass=RiotClientServices$' ${darlingLol}/share/applications/darling-launcher.desktop
          ! grep -qiE '^(Name|Comment|GenericName|Keywords)=.*(League|LoL|Riot|Vanguard|anti-cheat)' ${darlingLol}/share/applications/darling-launcher.desktop
          test -x ${darlingLol}/bin/darling-launcher
          touch "$out"
        '';
      };

      packages.${system} = {
        darling-lol = darlingLol;
        darling-lol-fhs = darlingLolFhs;
        default = darlingLol;
      };

      apps.${system} = {
        default = {
          type = "app";
          program = "${darlingLol}/bin/darling-lol";
          meta.description = "Launch the macOS game client via Darling";
        };
        shell = {
          type = "app";
          program = "${darlingLol}/bin/darling-lol-shell";
          meta.description = "Open an interactive shell inside the Darling FHS environment";
        };
        installer = {
          type = "app";
          program = "${darlingLol}/bin/darling-lol-installer";
          meta.description = "Run the Darling rootless installer";
        };
        build = {
          type = "app";
          program = "${darlingLol}/bin/darling-lol-build";
          meta.description = "Build the Darling runtime tree inside the FHS sandbox";
        };
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = buildDeps ++ [ darlingLolFhs ];
        shellHook = ''
          echo "Darling build & development environment loaded."
          echo "Tip: Run 'darling-lol-fhs' to enter the FHS build sandbox if standard paths (/usr/include, /usr/lib) are needed."
        '';
      };
    };
}
