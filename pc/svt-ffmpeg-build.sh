#!/usr/bin/env bash
# Build FFmpeg 8.1 with SVT-JPEG-XS so we can encode/stream JPEG XS (ST 2110-22 style).
set -e
INSTALL_DIR=/root/jxs-install
mkdir -p "$INSTALL_DIR"

echo "=== [1/4] install SVT-JPEG-XS to $INSTALL_DIR ==="
cd /root/SVT-JPEG-XS/Build/linux
./build.sh install --prefix "$INSTALL_DIR" >/tmp/svtinstall.log 2>&1 || { echo "SVT install failed:"; tail -20 /tmp/svtinstall.log; exit 1; }
export LD_LIBRARY_PATH="$INSTALL_DIR/lib:$LD_LIBRARY_PATH"
export PKG_CONFIG_PATH="$INSTALL_DIR/lib/pkgconfig:$PKG_CONFIG_PATH"
echo "SvtJpegxs pkg-config version: $(pkg-config --modversion SvtJpegxs 2>&1)"

echo "=== [2/4] clone FFmpeg release/8.1 ==="
cd /root; rm -rf ffmpeg
git clone --depth 1 --branch release/8.1 https://git.ffmpeg.org/ffmpeg.git ffmpeg >/tmp/ffclone.log 2>&1 || { echo "clone failed:"; tail -10 /tmp/ffclone.log; exit 1; }
cd ffmpeg
git config user.email build@local; git config user.name build

echo "=== [3/4] apply JPEG XS patches (8.1) ==="
git am --whitespace=fix /root/SVT-JPEG-XS/ffmpeg-plugin/8.1/*.patch 2>&1 | tail -6

echo "=== [4/4] configure (--enable-libsvtjpegxs) ==="
./configure --enable-libsvtjpegxs --enable-shared --disable-doc >/tmp/ffconfig.log 2>&1 \
  && echo "CONFIGURE OK" || { echo "CONFIGURE FAILED:"; tail -25 /tmp/ffconfig.log; exit 1; }
echo "jpegxs in config.mak:"; grep -i jpegxs /root/ffmpeg/ffbuild/config.mak | head
echo "=== ready to 'make' ==="
