"""给媒体目录里的存量原图补派生档（``_m`` 640 / ``_t`` 200）。

**为什么需要它**：``_m`` 是后加的。磁盘上已有的原图（以及订单/退款快照引用的那些）
当年只生成了 ``_t``，商品卡片改成吃 ``_m`` 之后，这些图会 404。

**为什么不去改数据库**：派生图路径是**算出来的**（见 ``storage.thumb_path_of``），
库里一个字段都不用动 —— 补齐文件即可。

用法：

    uv run python scripts/backfill_image_tiers.py --dry-run   # 先看要补多少
    uv run python scripts/backfill_image_tiers.py

★ **只补缺失的档，绝不重写原图**。原图再编码一次是白掉一次画质，没有任何好处。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app.core.config import get_settings
from app.modules.files import storage


def is_derived(rel: str) -> bool:
    """派生档自己不该被当成原图再派生一次（否则会生出 ``ab_m_m.webp``）。"""
    stem = Path(rel).stem
    return stem.endswith(storage.MID_SUFFIX) or stem.endswith(storage.THUMB_SUFFIX)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()

    root = Path(get_settings().media_root).resolve()
    if not root.is_dir():
        sys.exit(f"媒体目录不存在：{root}")

    originals = [p for p in root.rglob("*.webp") if not is_derived(p.relative_to(root).as_posix())]
    added_mid = added_thumb = skipped = 0

    for original in originals:
        rel = original.relative_to(root).as_posix()
        mid = storage.absolute_path(storage.mid_path_of(rel))
        thumb = storage.absolute_path(storage.thumb_path_of(rel))
        need_mid, need_thumb = not mid.exists(), not thumb.exists()
        if not (need_mid or need_thumb):
            skipped += 1
            continue

        if args.dry_run:
            print(f"  {rel}  缺 {'_m ' if need_mid else ''}{'_t' if need_thumb else ''}")
            added_mid += need_mid
            added_thumb += need_thumb
            continue

        try:
            im = Image.open(original)
            im.load()
        except Exception as exc:
            print(f"  跳过（读不开）：{rel} —— {exc}")
            continue

        if need_mid:
            mid.write_bytes(storage.encode_webp(im, storage.MID_EDGE))
            added_mid += 1
        if need_thumb:
            thumb.write_bytes(storage.encode_webp(im, storage.THUMB_EDGE))
            added_thumb += 1

    verb = "将补" if args.dry_run else "已补"
    print(
        f"\n原图 {len(originals)} 张；{verb} _m {added_mid} 个、_t {added_thumb} 个；"
        f"已齐 {skipped} 张"
    )


if __name__ == "__main__":
    main()
