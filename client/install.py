#!/usr/bin/env python3
"""Download everything a bot client needs into client/cache/.

Vanilla client + libraries + assets from Mojang, Fabric loader from
meta.fabricmc.net, and the mod set below. Idempotent; writes
client/cache/launch.json which launch.py consumes.
"""
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request

MC = os.environ.get("MC_VERSION", "1.21.11")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")

# Modrinth slugs: latest Fabric build for MC is used.
MODRINTH_MODS = {
    "fabric-api": "required by almost everything",
    "minescript": "Python scripting inside the client - runs the bot brain",
    "sodium": "renderer optimisation (matters: we render on CPU under Xvfb)",
    "lithium": "game logic optimisation",
    "ferrite-core": "memory usage reduction",
}
DIRECT_MODS = {
    # Meteor Client: utility client (auto-eat, kill aura, auto-tool, ...).
    "meteor-client.jar": f"https://meteorclient.com/api/download?version={MC}",
}
# Baritone pathfinder, the build Meteor is developed against.
BARITONE_META = ("https://maven.meteordev.org/snapshots/meteordevelopment/"
                 f"baritone/{MC}-SNAPSHOT/")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "minevoid-installer"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            if attempt == 4:
                raise


def get_json(url):
    return json.loads(get(url))


def fetch(url, dest, sha1=None):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = get(url)
    if sha1 and hashlib.sha1(data).hexdigest() != sha1:
        raise RuntimeError(f"sha1 mismatch for {url}")
    with open(dest + ".part", "wb") as f:
        f.write(data)
    os.replace(dest + ".part", dest)
    return dest


def rules_allow(rules):
    if not rules:
        return True
    allowed = False
    for r in rules:
        os_ = r.get("os", {})
        match = os_.get("name", "linux") == "linux" and \
            os_.get("arch", "x86_64") != "x86"
        if match:
            allowed = r["action"] == "allow"
    return allowed


def maven_path(name):
    parts = name.split(":")
    group, artifact, version = parts[:3]
    classifier = f"-{parts[3]}" if len(parts) > 3 else ""
    return (f"{group.replace('.', '/')}/{artifact}/{version}/"
            f"{artifact}-{version}{classifier}.jar")


def lib_key(name):
    parts = name.split(":")
    return ":".join(parts[:2] + parts[3:])  # drop the version


def main():
    manifest = get_json(
        "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    vurl = next(v["url"] for v in manifest["versions"] if v["id"] == MC)
    vjson = get_json(vurl)

    jobs = []  # (url, dest, sha1)
    libs = {}  # key -> path, insertion ordered; fabric overrides vanilla

    client = vjson["downloads"]["client"]
    client_jar = os.path.join(CACHE, "versions", MC, f"{MC}.jar")
    jobs.append((client["url"], client_jar, client["sha1"]))

    for lib in vjson["libraries"]:
        if not rules_allow(lib.get("rules")):
            continue
        art = lib["downloads"]["artifact"]
        dest = os.path.join(CACHE, "libraries", art["path"])
        jobs.append((art["url"], dest, art["sha1"]))
        libs[lib_key(lib["name"])] = dest

    loader = get_json(f"https://meta.fabricmc.net/v2/versions/loader/{MC}")[0]
    lver = loader["loader"]["version"]
    profile = get_json(
        f"https://meta.fabricmc.net/v2/versions/loader/{MC}/{lver}/profile/json")
    for lib in profile["libraries"]:
        path = maven_path(lib["name"])
        dest = os.path.join(CACHE, "libraries", path)
        jobs.append((lib["url"].rstrip("/") + "/" + path, dest, None))
        libs.pop(lib_key(lib["name"]), None)
        libs[lib_key(lib["name"])] = dest

    aidx = vjson["assetIndex"]
    idx_path = fetch(aidx["url"],
                     os.path.join(CACHE, "assets", "indexes", aidx["id"] + ".json"),
                     aidx["sha1"])
    with open(idx_path) as f:
        for obj in json.load(f)["objects"].values():
            h = obj["hash"]
            jobs.append((
                f"https://resources.download.minecraft.net/{h[:2]}/{h}",
                os.path.join(CACHE, "assets", "objects", h[:2], h), h))

    # Name mappings so Minescript reflection can use readable Mojang names.
    mdir = os.path.join(CACHE, "mappings", MC)
    jobs.append((vjson["downloads"]["client_mappings"]["url"],
                 os.path.join(mdir, "client.txt"), None))
    jobs.append(("https://raw.githubusercontent.com/FabricMC/intermediary/"
                 f"master/mappings/{MC}.tiny", os.path.join(mdir, f"{MC}.tiny"), None))

    mods = os.path.join(CACHE, "mods")
    for slug in MODRINTH_MODS:
        q = urllib.parse.urlencode({"game_versions": json.dumps([MC]),
                                    "loaders": '["fabric"]'})
        ver = get_json(f"https://api.modrinth.com/v2/project/{slug}/version?{q}")[0]
        jobs.append((ver["files"][0]["url"], os.path.join(mods, slug + ".jar"), None))
    for name, url in DIRECT_MODS.items():
        jobs.append((url, os.path.join(mods, name), None))
    meta = get(BARITONE_META + "maven-metadata.xml").decode()
    snap = meta.split("<value>")[1].split("</value>")[0]
    jobs.append((f"{BARITONE_META}baritone-{snap}.jar",
                 os.path.join(mods, "baritone.jar"), None))

    todo = [j for j in jobs if not os.path.exists(j[1])]
    print(f"{len(jobs)} files, {len(todo)} to download")
    with cf.ThreadPoolExecutor(16) as pool:
        for i, _ in enumerate(pool.map(lambda j: fetch(*j), todo), 1):
            if i % 500 == 0:
                print(f"  {i}/{len(todo)}")

    launch = {
        "mc_version": MC,
        "main_class": profile["mainClass"],
        "classpath": list(libs.values()) + [client_jar],
        "asset_index": aidx["id"],
        "assets_dir": os.path.join(CACHE, "assets"),
        "mods_dir": mods,
        "mappings_dir": mdir,
    }
    with open(os.path.join(CACHE, "launch.json"), "w") as f:
        json.dump(launch, f, indent=1)
    print("client installed:", MC, "fabric-loader", lver)


if __name__ == "__main__":
    sys.exit(main())
