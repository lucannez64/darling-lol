# Qt6 splash / consent demo

This directory is a **standalone UI demo**, not an installer or launcher
integration. It prototypes the banner that could be shown before the
installer starts compiling or installing Darling/League.

The intent is to collect feedback on the copy, visual hierarchy, accessibility,
translations and the eventual integration point. Nothing in `scripts/`,
`tools/` or the `.desktop` entry is changed by this demo.

## Slides

1. **How it works** — Riot installer → macOS client → Darling runtime → Linux.
2. **Account risk** — Riot does not support this environment; temporary
   suspensions have been reported; no guarantee of account safety.
3. **Protected content** — no Riot binaries, game assets or account credentials
   are distributed. The official Riot installer obtains the client.

The slides advance every three seconds. **Previous** and **Next** navigate
manually and restart the timer.

## Build and run

Requirements: CMake, a C++17 compiler, Qt6 Widgets and Qt6 Svg.

```sh
cmake -S . -B build
cmake --build build
./build/darling-splash-demo
```

The build copies `assets/` next to the binary. The SVG files are original
project graphics; the first one reuses the shapes from `darling-lol.svg`.

## Scope and feedback

This is a visual prototype. It does not install, download, patch or launch
League. The disclaimer is a product/compliance draft, not legal advice.

Feedback wanted:

- clarity and length of the disclaimer;
- whether the account-risk wording is accurate and proportionate;
- visual hierarchy and readability at 768×768;
- English wording and future Spanish translation;
- where in the installer lifecycle this should appear;
- whether the copy should require an explicit checkbox before continuing.

The demo deliberately contains no Riot logos, game assets, account data or
client binaries.
