#!/usr/bin/env python3
"""Build the paired display timing candidates without installing or starting them."""
from pathlib import Path
import shlex
import subprocess


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / 'build/darling-src'
    build = source / 'build'
    output = root / 'build/display-timing-runtime'
    output.mkdir(parents=True, exist_ok=True)
    targets = (
        ('src/external/cocotron/CoreGraphics', 'CoreGraphics', 'CoreGraphics',
         ('CGDirectDisplay.m', 'DarlingDisplayTiming.c')),
        ('src/frameworks/OpenGL', 'OpenGL', 'OpenGL', ('OpenGL.c',)),
        ('src/frameworks/CoreVideo', 'CoreVideo', 'CoreVideo', ('src/CVDisplayLink.mm',)),
        ('src/external/cocotron/QuartzCore', 'QuartzCore', 'QuartzCore',
         ('CALayerContext.m', 'CAMetalLayer.mm', 'CAMetalDrawable.mm')),
        ('src/external/cocotron/AppKit', 'X11_backend', 'X11',
         ('X11.backend/X11Display.m', 'X11.backend/X11Window.m', 'X11.backend/X11SubWindow.m',
          'X11.backend/WaylandWindow.m', 'X11.backend/WaylandSubWindow.m', 'X11.backend/X11DisplayWayland.m')),
    )
    resource = subprocess.check_output(['clang', '-print-resource-dir'], text=True).strip() + '/include'
    paired = {}
    for relative, target, name, files in targets:
        folder = build / relative
        cmake = folder / 'CMakeFiles' / (target + '.dir')
        flags = {}
        for line in (cmake / 'flags.make').read_text().splitlines():
            if ' = ' in line:
                key, value = line.split(' = ', 1)
                flags[key] = shlex.split(value)
        link = shlex.split((cmake / 'link.txt').read_text())
        for filename in files:
            kind = 'CXX' if filename.endswith('.mm') else 'C'
            includes = [resource if value.startswith('/usr/lib/clang/') and value.endswith('/include')
                        else value for value in flags[kind + '_INCLUDES']]
            obj = output / (target + '-' + Path(filename).name + '.o')
            command = ['clang++' if kind == 'CXX' else 'clang', *flags[kind + '_DEFINES'],
                       *includes, *flags[kind + '_FLAGS'], '-Wno-macro-redefined', '-c',
                       str(source / relative / filename), '-o', str(obj)]
            subprocess.run(command, check=True)
            old = f'CMakeFiles/{target}.dir/{filename}.o'
            if old in link:
                link[link.index(old)] = str(obj)
            else:
                link.append(str(obj))
        candidate = output / name
        link[link.index('-o') + 1] = str(candidate)
        # Relink each dependent against the same candidate set, including ld64's
        # indirect dylib mappings. Never modify the active per-user runtime.
        for i, arg in enumerate(link):
            if arg.startswith('-Wl,-dylib_file,'):
                prefix, path = arg.rsplit(':', 1)
                if Path(path).resolve() in paired:
                    link[i] = prefix + ':' + str(paired[Path(path).resolve()])
            elif not arg.startswith('-') and (folder / arg).resolve() in paired:
                link[i] = str(paired[(folder / arg).resolve()])
        subprocess.run(link, cwd=folder, check=True)
        paired[(folder / name).resolve()] = candidate
        print('Built paired candidate:', candidate, flush=True)
    subprocess.run(['sh', str(root / 'build/runtime/tools/build-darling-wayland.sh')], check=True)
    print('Paired timing frameworks and Wayland bridge built; nothing installed.')


if __name__ == '__main__':
    main()
