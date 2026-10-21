#!/usr/bin/env python3
"""把页面移出 wiki 草稿：从 manifest 中删除，并删掉页面与素材文件。

用法：python3 drop_pages.py <草稿目录> <页面 id>...

移出的页面不进入本次发布，仍处于待更新状态，下次整理时重试。打印剩余页面数。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def drop(draft: Path, pages: set[str]) -> int:
    manifest = json.loads((draft / "manifest.json").read_text(encoding="utf-8"))
    manifest["pages"] = [p for p in manifest["pages"] if p["id"] not in pages]
    (draft / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for page in pages:
        (draft / "pages" / f"{page}.md").unlink(missing_ok=True)
        (draft / "context" / f"{page}.json").unlink(missing_ok=True)
    return len(manifest["pages"])


def main() -> int:
    print(drop(Path(sys.argv[1]), set(sys.argv[2:])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
