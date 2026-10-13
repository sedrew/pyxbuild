#!/bin/sh
# Download a portable 64-bit Wine (Kron4ek/Wine-Builds) for running built apps on Linux.
# Usage: eval "$(tools/get_wine.sh [version] [dest])"   -> exports PYXBUILD_WINE
set -eu
version="${1:-11.10}"
dest="${2:-${XDG_CACHE_HOME:-$HOME/.cache}/pyxbuild-wine-dist}"
name="wine-${version}-amd64-wow64"
if [ ! -x "$dest/$name/bin/wine" ]; then
    mkdir -p "$dest"
    curl -fsSL "https://github.com/Kron4ek/Wine-Builds/releases/download/${version}/${name}.tar.xz" \
        | tar xJ -C "$dest"
fi
echo "export PYXBUILD_WINE=$dest/$name/bin/wine"
