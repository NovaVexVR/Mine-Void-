#!/usr/bin/env bash
# Downloads Paper + plugins into server/. Idempotent: existing jars are kept.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MC_VERSION="${MC_VERSION:-1.21.11}"
cd "$ROOT/server"
mkdir -p plugins

fetch() { # url dest
  [ -s "$2" ] && return 0
  echo "downloading $(basename "$2")"
  curl -fsSL --retry 4 --retry-delay 2 -o "$2.part" "$1" && mv "$2.part" "$2"
}

paper_url=$(curl -fsSL "https://fill.papermc.io/v3/projects/paper/versions/$MC_VERSION/builds/latest" |
  python3 -c "import json,sys;print(json.load(sys.stdin)['downloads']['server:default']['url'])")
fetch "$paper_url" paper.jar

# Latest Modrinth build of each plugin for this Minecraft version.
modrinth() { # slug
  curl -fsSL -G "https://api.modrinth.com/v2/project/$1/version" \
    --data-urlencode "game_versions=[\"$MC_VERSION\"]" \
    --data-urlencode 'loaders=["paper","bukkit","spigot"]' |
    python3 -c "import json,sys;print(json.load(sys.stdin)[0]['files'][0]['url'])"
}
# grimac      anti-cheat (movement / combat / reach / timer checks)
# luckperms   permissions
# essentialsx basic commands (homes, kits, tp, ...)
# coreprotect block logging / rollback, lets us audit what bots did
# chunky      world pre-generation
for slug in grimac luckperms essentialsx coreprotect chunky; do
  [ -s "plugins/$slug.jar" ] || fetch "$(modrinth "$slug")" "plugins/$slug.jar"
done

echo "eula=true" > eula.txt
echo "server ready in $ROOT/server"
