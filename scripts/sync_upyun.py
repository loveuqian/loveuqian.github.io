#!/usr/bin/env python3
"""获取又拍云根目录文件列表并生成静态 JSON。"""

import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_BASE_URL = "https://v0.api.upyun.com"
DEFAULT_BUCKET = "wsf-upyun"
DEFAULT_FOLDER = "/"
DEFAULT_PUBLIC_BASE_URL = "https://wsf-upyun.b0.upaiyun.com"
LAST_PAGE_ITER = "g2gCZAAEbmV4dGQAA2VvZg"


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


def make_signature(method, uri, date, password):
    password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
    message = "&".join((method, uri, date))
    digest = hmac.new(password_md5.encode("ascii"), message.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def request_page(operator, password, uri, iterator):
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    headers = {
        "Accept": "application/json",
        "Date": date,
        "Authorization": f"UPYUN {operator}:{make_signature('GET', uri, date, password)}",
        "X-List-Limit": "10000",
        "X-List-Order": "asc",
    }
    if iterator:
        headers["X-List-Iter"] = iterator

    request = Request(API_BASE_URL + uri, headers=headers, method="GET")
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def public_file_url(public_base_url, path):
    encoded_path = quote(path.lstrip("/"), safe="/@:$&+,;=-._~")
    return f"{public_base_url.rstrip('/')}/{encoded_path}"


def is_downloadable(url):
    try:
        request = Request(url, method="HEAD")
        with urlopen(request, timeout=15) as response:
            return response.status < 400
    except HTTPError as error:
        if error.code not in (403, 405, 501):
            return False
    except (URLError, TimeoutError):
        return False

    try:
        request = Request(url, headers={"Range": "bytes=0-0"}, method="GET")
        with urlopen(request, timeout=15) as response:
            return response.status < 400
    except (HTTPError, URLError, TimeoutError):
        return False


def fetch_files(operator, password, bucket, folder, public_base_url):
    normalized_folder = "/" if folder == "/" else "/" + folder.strip("/") + "/"
    uri = f"/{bucket}{normalized_folder}"
    iterator = None
    files = []
    seen_iterators = set()

    while True:
        page = request_page(operator, password, uri, iterator)
        for item in page.get("files", []):
            if item.get("type") == "folder":
                continue
            name = str(item.get("name", "")).strip()
            if not name or "/" in name:
                continue
            path = normalized_folder + name.lstrip("/")
            download_url = public_file_url(public_base_url, path)
            downloadable = is_downloadable(download_url)
            files.append(
                {
                    "name": name,
                    "path": path,
                    "size": int(item.get("length", 0) or 0),
                    "last_modified": int(item.get("last_modified", 0) or 0),
                    "download_url": download_url if downloadable else None,
                }
            )

        next_iterator = page.get("iter")
        if not next_iterator or next_iterator.rstrip("=") == LAST_PAGE_ITER.rstrip("=") or next_iterator in seen_iterators:
            break
        seen_iterators.add(next_iterator)
        iterator = next_iterator

    files.sort(key=lambda item: item["name"].casefold())
    return files


def main():
    operator = required_env("UPYUN_OPERATOR")
    password = required_env("UPYUN_PASSWORD")
    bucket = os.environ.get("UPYUN_BUCKET", DEFAULT_BUCKET).strip() or DEFAULT_BUCKET
    folder = os.environ.get("UPYUN_FOLDER", DEFAULT_FOLDER).strip() or DEFAULT_FOLDER
    public_base_url = os.environ.get("UPYUN_PUBLIC_BASE_URL", "").strip() or DEFAULT_PUBLIC_BASE_URL
    files = fetch_files(operator, password, bucket, folder, public_base_url)

    output = {
        "bucket": bucket,
        "folder": "/" if folder == "/" else "/" + folder.strip("/") + "/",
        "files": files,
    }
    output_path = Path(os.environ.get("UPYUN_OUTPUT", "data/files.json"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    unavailable = sum(1 for item in files if not item["download_url"])
    print(f"已同步 {len(files)} 个文件，其中 {unavailable} 个文件没有可用的公开下载地址。")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        unavailable_names = [item["name"] for item in files if not item["download_url"]]
        summary = [
            "## 又拍云文件同步结果",
            "",
            f"- 文件数量：{len(files)}",
            f"- 可下载数量：{len(files) - unavailable}",
        ]
        if unavailable_names:
            summary.extend(["", "以下文件没有可用的公开下载地址：", ""])
            summary.extend(f"- `{name}`" for name in unavailable_names)
        Path(summary_path).write_text("\n".join(summary) + "\n", encoding="utf-8")
    if unavailable:
        print("::warning::部分文件无法通过公开地址下载，请检查又拍云空间的域名和访问权限。")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, HTTPError, URLError, json.JSONDecodeError, ValueError) as error:
        print(f"同步失败：{error}", file=sys.stderr)
        sys.exit(1)
