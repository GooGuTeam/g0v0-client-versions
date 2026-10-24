import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
import os
from pathlib import Path
import tempfile
import traceback
from typing import TypedDict

from aiofiles import open as aio_open
from aiohttp import ClientSession
from stream_unzip import async_stream_unzip

GITHUB_API_RELEASES_API = "https://api.github.com/repos/{owner}/{repo}/releases"

# GitHub redirects release asset downloads to release-assets.githubusercontent.com with a signed
# content-disposition header that is currently ~9 KB long. aiohttp caps header field/line values at
# 8190 bytes by default, so every asset download fails with:
#   400, message='Got more than 8190 bytes (9217) when reading Header value is too long.'
# and the affected platforms silently end up without any hash. Raise the parser limits.
HTTP_MAX_HEADER_FIELD_SIZE = 64 * 1024
HTTP_MAX_HEADER_LINE_SIZE = 64 * 1024


def create_session() -> ClientSession:
    return ClientSession(
        max_field_size=HTTP_MAX_HEADER_FIELD_SIZE,
        max_line_size=HTTP_MAX_HEADER_LINE_SIZE,
    )


DEFAULT_CLIENTS_FILE = Path("default_clients.json")
COMMUNITY_CLIENTS_PATH = Path("community_clients")
DEFAULT_VERSION_LIST_OUTPUT = Path("version_list.json")
COMMUNITY_VERSION_LIST_OUTPUT = Path("community_versions")

# client


class File(TypedDict):
    asset_name: str
    internal_name: str
    type: str


class Client(TypedDict):
    name: str
    description: str
    owner: str
    repo: str
    files: dict[str, File]
    count: int
    support_android: bool
    support_ios: bool


def read_default_clients() -> list[Client]:
    clients = json.loads(DEFAULT_CLIENTS_FILE.read_text())

    # set defaults
    for client in clients:
        if "count" not in client:
            client["count"] = 1
        if "support_android" not in client:
            client["support_android"] = True
        if "support_ios" not in client:
            client["support_ios"] = True
    return clients


def read_community_clients() -> dict[str, list[Client]]:
    communities: dict[str, list[Client]] = {}
    for community_file in COMMUNITY_CLIENTS_PATH.glob("*.json"):
        clients = json.loads(community_file.read_text())

        # set defaults
        for client in clients:
            if "count" not in client:
                client["count"] = 1
            if "support_android" not in client:
                client["support_android"] = True
            if "support_ios" not in client:
                client["support_ios"] = True

        communities[community_file.name] = clients
    return communities


# github releases


class Release(TypedDict):
    release_name: str
    tag: str
    pre_release: bool
    files: dict[str, str]
    date: str


async def fetch_releases(owner: str, repo: str, count: int = 1, authorization: str = "") -> list[Release]:
    current_count = 0
    page = 1

    async def _fetch_page(page: int):
        async with create_session() as session:
            resp = await session.get(
                GITHUB_API_RELEASES_API.format(owner=owner, repo=repo),
                params={"per_page": 100, "page": page},
                headers={
                    "Authorization": f"Bearer {authorization}" if authorization else "",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            return await resp.json()

    releases: list[Release] = []
    while current_count < count:
        data = await _fetch_page(page)
        if not data:
            break
        for release_data in data:
            is_pre_release = release_data["prerelease"]
            release: Release = {
                "release_name": release_data["name"],
                "tag": release_data["tag_name"],
                "pre_release": is_pre_release,
                "files": {asset["name"]: asset["browser_download_url"] for asset in release_data["assets"]},
                "date": release_data["published_at"],
            }
            releases.append(release)
            if not is_pre_release:
                current_count += 1
            if current_count >= count:
                break
        page += 1
    return releases


# process

Processor = Callable[[str, str], Awaitable[str | None]]


async def process_zip(file_url: str, internal_name: str) -> str | None:
    md5 = hashlib.md5(usedforsecurity=False)
    async with create_session() as session, session.get(file_url) as resp:

        async def byte_stream():
            async for chunk in resp.content.iter_chunked(65536):
                yield chunk

        async for file_name, _, file_chunks in async_stream_unzip(byte_stream()):
            name = file_name.decode("utf-8", errors="ignore")
            if name != internal_name:
                async for _ in file_chunks:
                    pass
                continue
            async for data in file_chunks:
                md5.update(data)

            return md5.hexdigest()
    return None


async def process_appimage(file_url: str, internal_name: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_path = Path(tmpdirname) / "appimage_file"

        async with aio_open(temp_path, "wb") as fp, create_session() as session, session.get(file_url) as resp:
            async for chunk in resp.content.iter_chunked(65536):
                await fp.write(chunk)

        # 7z auto-detects the embedded squashfs and extracts the requested file to stdout
        process = await asyncio.create_subprocess_exec(
            "7z",
            "x",
            "-so",
            str(temp_path),
            internal_name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # calculate md5
        md5 = hashlib.md5(usedforsecurity=False)
        total = 0
        while True:
            data = await process.stdout.read(65536)
            if not data:
                break
            total += len(data)
            md5.update(data)
        await process.wait()

        if process.returncode != 0 or total == 0:
            return None
        return md5.hexdigest()


async def process_exe(file_url: str, internal_name: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_path = Path(tmpdirname) / "exe_file"

        async with aio_open(temp_path, "wb") as fp, create_session() as session, session.get(file_url) as resp:
            async for chunk in resp.content.iter_chunked(65536):
                await fp.write(chunk)

        process = await asyncio.create_subprocess_exec(
            "unzip",
            "-j",
            str(temp_path),
            internal_name,
            "-d",
            tmpdirname,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate()
        extracted_path = Path(tmpdirname) / internal_name.split("/")[-1]
        if not extracted_path.exists():
            return None

        # calculate md5
        md5 = hashlib.md5(usedforsecurity=False)
        async with aio_open(extracted_path, "rb") as fp:
            while True:
                data = await fp.read(65536)
                if not data:
                    break
                md5.update(data)
        return md5.hexdigest()


PROCESSORS: dict[str, Processor] = {
    "zip": process_zip,
    "appimage": process_appimage,
    "exe": process_exe,
}


class VersionInfo(TypedDict):
    version: str
    release_date: str
    hashes: dict[str, str]


class VersionList(TypedDict):
    name: str
    versions: list[VersionInfo]


async def generate_versions(client: Client, gh_token: str) -> list[VersionInfo]:
    versions: list[VersionInfo] = []
    releases = await fetch_releases(client["owner"], client["repo"], client["count"], authorization=gh_token)

    for release in releases:
        version_hashes: dict[str, str] = {}
        print(f"\t  Release: {release['release_name']} ({release['tag']})")
        tag = release["tag"].removeprefix("v")

        def compute_md5(s: str) -> str:
            md5 = hashlib.md5(usedforsecurity=False)
            md5.update(s.encode("utf-8"))
            return md5.hexdigest()

        async def process_one(osname, file_info):
            asset_name = file_info["asset_name"]
            internal_name = file_info["internal_name"]

            # replace placeholders
            asset_name = asset_name.replace("{tag}", release["tag"])

            if asset_name not in release["files"]:
                print(f"\t    [!] Asset '{asset_name}' not found in release.")
                return None

            file_url = release["files"][asset_name]
            type_ = file_info.get("type", "zip")
            processor = PROCESSORS.get(type_)
            if not processor:
                print(f"\t    [!] No processor found for type '{type_}'.")
                return None

            print(f"\t    Processing asset: {asset_name}")
            try:
                version_hash = await processor(file_url, internal_name)
                if version_hash:
                    print(f"\t      {osname} version hash: {version_hash}")
                    return version_hash, osname
                else:
                    print(f"\t      [!] Internal file '{internal_name}' not found in asset.")
            except Exception as e:
                print(f"\t      [!] Error processing file: {e}")
                traceback.print_exc()
            return None

        results = await asyncio.gather(
            *(process_one(osname, file_info) for osname, file_info in client["files"].items())
        )
        for res in results:
            if res:
                version_hashes[res[0]] = res[1]

        # android and ios
        # https://github.com/ppy/osu/blob/master/osu.Game/OsuGameBase.cs#L270-L275
        if client["support_android"]:
            version_str = f"{tag}-Android"
            hash = compute_md5(version_str)
            version_hashes[hash] = "Android"
            print(f"\t      Android version hash: {hash}")
        if client["support_ios"]:
            version_str = f"{tag}-iOS"
            hash = compute_md5(version_str)
            version_hashes[hash] = "iOS"
            print(f"\t      iOS version hash: {hash}")

        version_info: VersionInfo = {
            "version": tag,
            "release_date": release["date"],
            "hashes": version_hashes,
        }
        versions.append(version_info)
    return versions


def load_published_clients(output_path: Path) -> dict[str, list[VersionInfo]]:
    """Read the already published version list, keyed by client name.

    Used as a safety net so a failed run cannot erase hashes that were generated successfully
    before (asset downloads fail regularly: GitHub outages, rate limits, HTTP parser limits).
    """
    if not output_path.is_file():
        return {}
    try:
        published = json.loads(output_path.read_text())
    except (OSError, ValueError) as e:
        print(f"\t[!] Could not read existing {output_path}: {e}")
        return {}
    return {client["name"]: client.get("versions", []) for client in published}


def write_version_list(version_list: list[VersionList], output_path: Path) -> None:
    """Write the generated list, keeping previously published data where this run produced none."""
    published = load_published_clients(output_path)
    recovered_versions = 0
    recovered_hashes = 0

    for client in version_list:
        previous_versions = published.get(client["name"], [])

        if not client["versions"] and previous_versions:
            client["versions"] = previous_versions
            recovered_versions += len(previous_versions)
            print(f"\t[!] {client['name']}: no versions could be fetched, keeping previously published ones")
            continue

        previous_by_version = {version["version"]: version.get("hashes", {}) for version in previous_versions}
        for version in client["versions"]:
            # Hashes are keyed by digest and labelled with a platform name, so merge per platform:
            # a platform this run failed to hash keeps its previously published digest, while every
            # digest this run did produce replaces the old one.
            generated_platforms = set(version["hashes"].values())
            for digest, platform in previous_by_version.get(version["version"], {}).items():
                if platform in generated_platforms:
                    continue
                if digest in version["hashes"]:
                    continue
                version["hashes"][digest] = platform
                recovered_hashes += 1
                print(f"\t[!] {client['name']} {version['version']}: keeping previous {platform} hash")

    if recovered_versions or recovered_hashes:
        print(f"\t[!] Kept previously published data: {recovered_versions} version(s), {recovered_hashes} hash set(s).")

    output_path.write_text(json.dumps(version_list, indent=4))


async def main(gh_token: str = "", skip_default: bool = False, skip_community: bool = False):
    clients = read_default_clients()
    version_list: list[VersionList] = []

    if not skip_default:
        print("=== Generating version list for default clients ===")
        for i, client in enumerate(clients):
            print(f"--- ({i + 1}/{len(clients)}) Generating version for default clients ---")
            print(f"\tClient: {client['name']}")
            print(f"\tDescription: {client['description']}")
            print(f"\tRepository: {client['owner']}/{client['repo']}")

            versions = await generate_versions(client, gh_token)
            version_list.append(
                {
                    "name": client["name"],
                    "versions": versions,
                }
            )
        print("--- Default version list generated, output to version_list.json ---")
        write_version_list(version_list, DEFAULT_VERSION_LIST_OUTPUT)

    if not skip_community:
        print("=== Generating version list for community clients ===")
        communities = read_community_clients()
        i = 1
        for community_filename, community_clients in communities.items():
            version_list = []
            print(f"--- ({i}/{len(communities)}) Generating version for community client from {community_filename} ---")
            for j, cc in enumerate(community_clients):
                print(f"~~~ ({j + 1}/{len(community_clients)}) ~~~")
                print(f"\tClient: {cc['name']}")
                print(f"\tDescription: {cc['description']}")
                print(f"\tRepository: {cc['owner']}/{cc['repo']}")

                versions = await generate_versions(cc, gh_token)
                version_list.append(
                    {
                        "name": cc["name"],
                        "versions": versions,
                    }
                )

                i += 1

                COMMUNITY_VERSION_LIST_OUTPUT.mkdir(exist_ok=True)
            output_path = COMMUNITY_VERSION_LIST_OUTPUT / community_filename
            write_version_list(version_list, output_path)
            print(f"--- Community {community_filename} version list generated, output to {output_path} ---")


if __name__ == "__main__":
    import sys

    if sys.platform != "linux":
        print("This script currently only supports Linux platform.")
        sys.exit(1)

    skip_default = "--skip-default" in sys.argv
    skip_community = "--skip-community" in sys.argv
    asyncio.run(main(os.environ.get("GH_TOKEN", ""), skip_default=skip_default, skip_community=skip_community))
