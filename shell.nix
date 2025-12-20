{ pkgs ? import <nixpkgs> { } }:
pkgs.mkShell rec {
  buildInputs = with pkgs; [
    python313
    python313Packages.tox
    python313Packages.kaleido
    python313Packages.pyqt6-webengine
    python313Packages.pyqt6
    python313Packages.pytest-xvfb
    xorg.xvfb
    libxcomposite
    libxdamage
    libxfixes
    libxrender
    libxrandr
    libxtst
    libdrm
    libxi
    alsa-lib
    libxshmfence
    libgbm
    libxkbfile
    nspr
    zlib
    zstd
    glib
    fontconfig
    libx11
    libxkbcommon
    libxft
    freetype
    dbus
    krb5
    nss
  ];
  shellHook = ''
    export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath buildInputs}:$LD_LIBRARY_PATH"
    export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"
  '';
}
