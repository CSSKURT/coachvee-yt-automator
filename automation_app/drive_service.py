"""
Stage 1a - Google Drive downloader (0% AI).

Finds the folder named "EDITOR | SEM" (and its subfolders), picks the files whose name
matches "Output <N>" for the batch, and downloads them one by one into
<staging_dir>\\Output_<N>\\ . A file that already exists locally with the same size is skipped.
Read-only Drive scope: nothing on Drive is moved, renamed or deleted.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from common import VIDEO_EXTS, job_dir, write_json_atomic

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FILE_FIELDS = "id, name, mimeType, size, modifiedTime, md5Checksum, shortcutDetails"
OUTPUT_RE = re.compile(r"(?<![a-z])output[\s_\-]*0*(\d+)(?!\d)", re.I)
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def output_number(name: str) -> int | None:
    m = OUTPUT_RE.search(name)
    return int(m.group(1)) if m else None


def _is_video(f: dict) -> bool:
    return f.get("mimeType", "").startswith("video/") or Path(f.get("name", "")).suffix.lower() in VIDEO_EXTS


def _q_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _rank(f: dict, n: int):
    """Prefer an exact 'Output N.mp4' name, then .mp4/.mov, then the newest upload."""
    stem = Path(f["name"]).stem.strip().lower()
    exact = bool(re.fullmatch(rf"output[\s_\-]*0*{n}", stem))
    ext_ok = Path(f["name"]).suffix.lower() in (".mp4", ".mov")
    return (exact, ext_ok, f.get("modifiedTime", ""))


class DriveService:
    def __init__(self, creds, log=print):
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.log = log

    def account_email(self) -> str:
        return self.svc.about().get(fields="user(emailAddress)").execute()["user"]["emailAddress"]

    def _list(self, q: str, corpora: str = "user") -> list[dict]:
        out, token = [], None
        while True:
            r = self.svc.files().list(
                q=q, fields=f"nextPageToken, files({FILE_FIELDS})", pageSize=1000, pageToken=token,
                includeItemsFromAllDrives=True, supportsAllDrives=True, corpora=corpora,
            ).execute()
            out += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    def find_folders(self, name: str) -> list[dict]:
        q = f"name = '{_q_escape(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        found = self._list(q, "user")
        if not found:
            try:
                found = self._list(q, "allDrives")           # shared drives
            except HttpError:
                pass
        return found

    def walk_videos(self, folder_id: str, depth: int = 0, max_depth: int = 4) -> list[dict]:
        files = []
        for f in self._list(f"'{folder_id}' in parents and trashed = false"):
            if f["mimeType"] == FOLDER_MIME:
                if depth < max_depth:
                    files += self.walk_videos(f["id"], depth + 1, max_depth)
            elif f["mimeType"] == SHORTCUT_MIME:
                sd = f.get("shortcutDetails") or {}
                if sd.get("targetMimeType", "").startswith("video/") or Path(f["name"]).suffix.lower() in VIDEO_EXTS:
                    try:
                        t = self.svc.files().get(fileId=sd["targetId"], fields=FILE_FIELDS,
                                                 supportsAllDrives=True).execute()
                        t["name"] = f["name"] if output_number(f["name"]) else t["name"]
                        files.append(t)
                    except HttpError:
                        pass
            elif _is_video(f):
                files.append(f)
        return files

    def find_outputs(self, folder_name: str, numbers: list[int]) -> dict[int, dict]:
        folders = self.find_folders(folder_name)
        if not folders:
            raise FileNotFoundError(f"No Drive folder named '{folder_name}' is visible to this account.")
        self.log(f"Found {len(folders)} folder(s) named '{folder_name}'. Scanning for Output files ...")
        wanted = set(numbers)
        cands: dict[int, list[dict]] = {}

        def add(f, n, in_own_folder):
            if n in wanted:
                f["_in_output_folder"] = in_own_folder
                cands.setdefault(n, []).append(f)

        for fo in folders:
            for child in self._list(f"'{fo['id']}' in parents and trashed = false"):
                if child["mimeType"] == FOLDER_MIME:
                    fn = output_number(child["name"])
                    if fn is not None:
                        # "OUTPUT 218 | INTERVIEW": only open the folders of this week's batch
                        if fn not in wanted:
                            continue
                        for f in self.walk_videos(child["id"], depth=1):
                            own = output_number(f["name"])
                            if own in (None, fn):
                                add(f, fn, True)
                    else:                                  # un-numbered folders (e.g. "Light Hearted Reels")
                        for f in self.walk_videos(child["id"], depth=1, max_depth=3):
                            add(f, output_number(f["name"]), False)
                elif _is_video(child):
                    add(child, output_number(child["name"]), False)
        picked = {}
        for n, fs in cands.items():
            fs.sort(key=lambda f: (f.get("_in_output_folder", False), "final" in f["name"].lower(), _rank(f, n)),
                    reverse=True)
            picked[n] = fs[0]
            if len(fs) > 1:
                self.log(f"  Output {n}: {len(fs)} matching files, using '{fs[0]['name']}' "
                         f"(others: {', '.join(x['name'] for x in fs[1:])})")
        return picked

    def download(self, f: dict, cfg: dict, n: int, on_progress=None, retries: int = 3) -> Path:
        jd = job_dir(cfg, n)
        jd.mkdir(parents=True, exist_ok=True)
        name = _BAD_CHARS.sub("_", f["name"]).strip() or f"Output {n}.mp4"
        dest = jd / name
        size = int(f.get("size") or 0)
        info = {"drive_id": f["id"], "name": f["name"], "size": size, "md5": f.get("md5Checksum"),
                "modifiedTime": f.get("modifiedTime"), "local_path": str(dest)}
        if dest.exists() and size and dest.stat().st_size == size:
            self.log(f"  Output {n}: already downloaded ({dest.name}, same size) - skipping")
            write_json_atomic(jd / "job.json", info)
            return dest
        part = dest.with_name(dest.name + ".part")
        for attempt in range(1, retries + 1):
            try:
                req = self.svc.files().get_media(fileId=f["id"], supportsAllDrives=True)
                with open(part, "wb") as fh:
                    dl = MediaIoBaseDownload(fh, req, chunksize=32 * 1024 * 1024)
                    done = False
                    while not done:
                        status, done = dl.next_chunk(num_retries=5)
                        if status and on_progress:
                            on_progress(status.progress())
                md5 = f.get("md5Checksum")
                if md5 and _md5(part) != md5:
                    raise IOError("checksum mismatch after download")
                os.replace(part, dest)
                write_json_atomic(jd / "job.json", info)
                self.log(f"  Output {n}: downloaded {dest.name} ({size / 1e6:.1f} MB)")
                return dest
            except Exception as e:
                self.log(f"  Output {n}: download attempt {attempt} failed: {e}")
                if attempt == retries:
                    raise
                time.sleep(5 * attempt)
        raise RuntimeError("unreachable")


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
