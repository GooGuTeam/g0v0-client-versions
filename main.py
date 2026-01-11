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

CLIENTS_FILE = "clients.json"

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


def read_clients() -> list[Client]:
    return json.loads(Path(CLIENTS_FILE).read_text())


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
        async with ClientSession() as session:
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
    async with ClientSession() as session, session.get(file_url) as resp:

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

        async with aio_open(temp_path, "wb") as fp, ClientSession() as session, session.get(file_url) as resp:
            async for chunk in resp.content.iter_chunked(65536):
                await fp.write(chunk)

        # run appimage extraction
        temp_path.chmod(0o755)
        process = await asyncio.create_subprocess_exec(
            str(temp_path),
            "--appimage-extract",
            cwd=tmpdirname,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate()
        extracted_path = Path(tmpdirname) / "squashfs-root" / internal_name
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


async def process_exe(file_url: str, internal_name: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_path = Path(tmpdirname) / "exe_file"

        async with aio_open(temp_path, "wb") as fp, ClientSession() as session, session.get(file_url) as resp:
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


async def main(gh_token: str = ""):
    clients = read_clients()
    version_list: list[VersionList] = []
    for i, client in enumerate(clients):
        print(f"--- ({i + 1}/{len(clients)}) Generating version for ---")
        print(f"\tClient: {client['name']}")
        print(f"\tDescription: {client['description']}")
        print(f"\tRepository: {client['owner']}/{client['repo']}")

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
            version_str = f"{tag}-Android"
            hash = compute_md5(version_str)
            version_hashes[hash] = "Android"
            print(f"\t      Android version hash: {hash}")
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
        version_list.append(
            {
                "name": client["name"],
                "versions": versions,
            }
        )
    print("--- Version list generated, output to version_list.json ---")
    Path("version_list.json").write_text(json.dumps(version_list, indent=4))


if __name__ == "__main__":
    import asyncio
    import sys

    if sys.platform != "linux":
        print("This script currently only supports Linux platform.")
        sys.exit(1)

    asyncio.run(main(os.environ.get("GH_TOKEN", "")))
