#!/usr/bin/env bash
# 重新编译 CoreMod（需要Forge 1.12.2的universal jar）
#   FORGE_JAR=/path/to/forge-1.12.2-14.23.5.2859.jar bash build/build.sh
set -e
cd "$(dirname "$0")/.."

FORGE_JAR="${FORGE_JAR:-$HOME/.minecraft/libraries/net/minecraftforge/forge/1.12.2-14.23.5.2859/forge-1.12.2-14.23.5.2859.jar}"

if [ ! -f "$FORGE_JAR" ]; then
    echo "找不到 Forge jar: $FORGE_JAR"
    echo "用 FORGE_JAR=... 指定它，或改 build.sh 里的默认值"
    exit 1
fi

rm -rf build/out
mkdir -p build/out

javac -source 1.8 -target 1.8 -encoding UTF-8 -cp "$FORGE_JAR" -d build/out \
      src/qm/updater/DcUpdater.java src/TestRun.java

printf 'Manifest-Version: 1.0\r\nFMLCorePlugin: qm.updater.DcUpdater\r\nCreated-By: QingMing\r\n\r\n' \
      > build/MANIFEST.MF

( cd build/out && jar cfm "../../client/QingMingUpdater-1.0.jar" \
        "../MANIFEST.MF" qm )

echo "OK -> client/QingMingUpdater-1.0.jar ($(stat -c%s client/QingMingUpdater-1.0.jar) bytes)"
