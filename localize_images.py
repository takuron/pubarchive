#!/usr/bin/env python3
"""
Hexo 文章图片本地化脚本
=======================

功能：
  自动扫描 source/_posts/ 下所有 Markdown 文件，将其中的外部图片链接替换为本地链接，
  并下载对应图片到 source/images/<文章名>/ 目录中（每篇文章独立子目录）。

用法：
  python localize_images.py            # 处理所有文章
  python localize_images.py --dry-run  # 仅预览，不实际修改和下载
  python localize_images.py --post id0005.md  # 仅处理指定文章
"""

import argparse
import hashlib
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests
import yaml

# ============================================================
# 配置 —— 可根据项目结构调整
# ============================================================

# 项目根目录（脚本所在目录）
PROJECT_ROOT = Path(__file__).resolve().parent

# 文章目录（相对于项目根目录）
POSTS_DIR = PROJECT_ROOT / "source" / "_posts"

# 图片本地存储目录（相对于项目根目录；Hexo 会将 source/ 映射为站点根目录）
IMAGES_DIR = PROJECT_ROOT / "source" / "images"

# 图片根目录在 Markdown 中的引用前缀
# 实际引用路径为 /images/<文章名>/xxx.png，由 process_post() 动态拼接
IMAGES_BASE_URL = "/images"

# 请求超时（秒）
REQUEST_TIMEOUT = 30

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}

# 浏览器 UA，模拟真实浏览器请求，避免被 CDN 拒绝
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# HTTP 会话（连接复用，自动携带 UA 和常见浏览器请求头）
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
)


# ============================================================
# 工具函数
# ============================================================


def extract_extension_from_url(url: str) -> str:
    """
    从 URL 中提取文件扩展名。
    会先去掉查询参数和片段，然后从路径部分提取扩展名。
    若无法识别，默认返回 ".png"。
    """
    # 去掉查询参数和片段
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    # 尝试从路径中提取扩展名
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return ext
    # 某些 CDN 链接可能在查询参数前有扩展名
    # 例如: .../image.png?imageView2/...
    # 上面的方法已经处理了这种情况（path 不含 query）
    # 如果还是没有，尝试在 path 中用正则匹配
    if not ext:
        # 有些 URL 不带扩展名，通过 Content-Type 在下载时再判断
        return ""
    return ext


def generate_filename(url: str, ext: str = "") -> str:
    """
    根据 URL 生成唯一的本地文件名。

    策略：
      取 URL（去除查询参数）的 SHA-256 前 16 位十六进制作为文件名，
      加上原始扩展名。这样相同图片不会重复下载，且文件名不会冲突。
    """
    # 使用去掉查询参数的 URL 做 hash，这样同一图片不同参数只存一份
    parsed = urllib.parse.urlparse(url)
    base_url = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
    )
    url_hash = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:16]

    if not ext:
        ext = extract_extension_from_url(url)
    if not ext:
        ext = ".png"  # 最终兜底

    return f"{url_hash}{ext}"


def download_image(url: str, save_path: Path, referer: str = "") -> bool:
    """
    下载图片到指定路径。

    参数:
      url:       图片链接
      save_path: 本地保存路径
      referer:   可选的 Referer 请求头，设置为文章来源 URL 可提高抓取成功率

    返回 True 表示下载成功，False 表示失败。
    """
    headers = {}
    if referer:
        headers["Referer"] = referer

    try:
        response = SESSION.get(
            url, timeout=REQUEST_TIMEOUT, stream=True, headers=headers
        )
        response.raise_for_status()

        # 确保目录存在
        save_path.parent.mkdir(parents=True, exist_ok=True)

        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return True
    except requests.RequestException as e:
        print(f"  [ERROR] 下载失败: {url} — {e}")
        return False


def is_external_url(url: str) -> bool:
    """
    判断是否为外部图片链接（http/https 开头）。
    """
    return url.startswith("http://") or url.startswith("https://")


# ============================================================
# 正则匹配模式
# ============================================================

# 匹配 Markdown 图片语法: ![alt](url)
#   - alt 可以为空
#   - url 不能包含空格（否则不是合法图片链接）
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\s*\(\s*(https?://[^\s)]+)\s*\)")


def find_external_images(content: str) -> list[tuple[int, int, str, str]]:
    """
    在 Markdown 文本中查找所有外部图片引用。

    返回列表，每项为 (起始位置, 结束位置, alt文本, url)。
    """
    results = []
    for match in MARKDOWN_IMAGE_RE.finditer(content):
        alt_text = match.group(1)
        url = match.group(2)
        results.append((match.start(), match.end(), alt_text, url))
    return results


# ============================================================
# Front Matter 解析
# ============================================================

# 匹配 Hexo 文章的 YAML front matter（位于 --- 分隔符之间）
FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def extract_source_url(content: str) -> str:
    """
    从 Markdown 文件的 front matter 中提取 source_url。
    若没有则返回空字符串。
    """
    match = FRONT_MATTER_RE.search(content)
    if not match:
        return ""

    yaml_text = match.group(1)
    try:
        front_matter = yaml.safe_load(yaml_text)
        if isinstance(front_matter, dict):
            return front_matter.get("source_url", "")
    except yaml.YAMLError:
        # YAML 解析失败时，用正则兜底提取 source_url
        fm_match = re.search(r"^source_url:\s*(.+)$", yaml_text, re.MULTILINE)
        if fm_match:
            return fm_match.group(1).strip()

    return ""


# ============================================================
# 主处理逻辑
# ============================================================


def process_post(md_path: Path, dry_run: bool = False) -> dict:
    """
    处理单个 Markdown 文件。

    图片将存储在 source/images/<文章名>/ 子目录下，
    例如 source/images/id0005/xxx.png。

    返回统计字典: {"downloaded": int, "skipped": int, "failed": int, "replaced": int}
    """
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "replaced": 0}

    # 文章标识（文件名去掉 .md 后缀）
    post_slug = md_path.stem
    post_images_dir = IMAGES_DIR / post_slug
    # 该文章在 Markdown 中的图片引用前缀
    image_url_prefix = f"{IMAGES_BASE_URL}/{post_slug}/"

    print(f"\n{'=' * 60}")
    print(f"处理文章: {md_path.relative_to(PROJECT_ROOT)}")
    print(f"  图片子目录: {post_images_dir.relative_to(PROJECT_ROOT)}")
    print(f"{'=' * 60}")

    # 读取文件内容
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 提取文章来源 URL，作为 Referer 提高图片抓取成功率
    source_url = extract_source_url(content)
    if source_url:
        print(f"  原文链接: {source_url}")

    # 查找外部图片
    images = find_external_images(content)
    if not images:
        print("  (无外部图片)")
        return stats

    print(f"  发现 {len(images)} 个外部图片链接")

    # 从后往前替换，避免位置偏移
    # 先收集所有替换信息
    replacements = []  # (start, end, new_text)
    url_to_local = {}  # 缓存：url -> local_filename

    for start, end, alt_text, url in images:
        # 检查是否已经处理过这个 URL
        if url in url_to_local:
            local_filename = url_to_local[url]
            stats["skipped"] += 1
        else:
            ext = extract_extension_from_url(url)
            local_filename = generate_filename(url, ext)
            save_path = post_images_dir / local_filename

            # 检查本地是否已存在（幂等）
            if save_path.exists():
                print(f"  [SKIP] 已存在: {url} -> {local_filename}")
                stats["skipped"] += 1
            else:
                if dry_run:
                    print(f"  [DRY-RUN] 将下载: {url} -> {local_filename}")
                    stats["downloaded"] += 1
                else:
                    print(f"  [DOWNLOAD] {url} -> {local_filename}")
                    if download_image(url, save_path, referer=source_url):
                        stats["downloaded"] += 1
                    else:
                        stats["failed"] += 1
                        continue  # 下载失败则不替换链接

            url_to_local[url] = local_filename

        # 构造新的本地引用
        local_url = f"{image_url_prefix}{local_filename}"
        if alt_text:
            new_text = f"![{alt_text}]({local_url})"
        else:
            new_text = f"![]({local_url})"

        replacements.append((start, end, new_text))
        stats["replaced"] += 1

    if dry_run:
        print(
            f"\n  [DRY-RUN] 将替换 {stats['replaced']} 处链接，"
            f"下载 {stats['downloaded']} 张新图片，"
            f"跳过 {stats['skipped']} 张已存在图片"
        )
        return stats

    # 执行替换（从后往前）
    new_content = content
    for start, end, new_text in reversed(replacements):
        new_content = new_content[:start] + new_text + new_content[end:]

    # 写回文件
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(
        f"\n  完成: 替换 {stats['replaced']} 处, "
        f"下载 {stats['downloaded']} 张, "
        f"跳过 {stats['skipped']} 张, "
        f"失败 {stats['failed']} 张"
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Hexo 文章图片本地化脚本 —— 将 Markdown 中的外链图片下载到本地并替换链接"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将要执行的操作，不实际下载或修改文件",
    )
    parser.add_argument(
        "--post",
        type=str,
        default=None,
        help="仅处理指定的文章文件（例如: id0005.md），不指定则处理全部",
    )
    args = parser.parse_args()

    # 确保图片根目录存在（子目录在下载时按需创建）
    if not args.dry_run:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # 收集要处理的 Markdown 文件
    if args.post:
        post_path = POSTS_DIR / args.post
        if not post_path.exists():
            print(f"错误: 文件不存在 — {post_path}")
            sys.exit(1)
        md_files = [post_path]
    else:
        md_files = sorted(POSTS_DIR.glob("*.md"))
        if not md_files:
            print(f"在 {POSTS_DIR} 下未找到任何 .md 文件")
            sys.exit(0)

    print(f"项目根目录: {PROJECT_ROOT}")
    print(f"文章目录:   {POSTS_DIR.relative_to(PROJECT_ROOT)}")
    print(f"图片目录:   {IMAGES_DIR.relative_to(PROJECT_ROOT)}")
    print(f"模式:       {'DRY-RUN (仅预览)' if args.dry_run else '正式运行'}")
    print(f"待处理文件: {len(md_files)} 个")

    # 汇总统计
    total = {"downloaded": 0, "skipped": 0, "failed": 0, "replaced": 0}

    for md_path in md_files:
        stats = process_post(md_path, dry_run=args.dry_run)
        for key in total:
            total[key] += stats[key]

    # 输出总结
    print(f"\n{'=' * 60}")
    print(f"全部完成！")
    print(f"  替换链接: {total['replaced']} 处")
    print(f"  下载图片: {total['downloaded']} 张")
    print(f"  跳过已有: {total['skipped']} 张")
    print(f"  下载失败: {total['failed']} 张")
    if total["failed"] > 0:
        print(f"  ⚠️  有图片下载失败，请检查网络后重新运行脚本")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
