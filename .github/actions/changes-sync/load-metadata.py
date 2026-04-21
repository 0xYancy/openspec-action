#!/usr/bin/env python3
"""从 change 目录的 .openspec.yaml 读取元数据并输出合法 JSON。

用法: python3 load-metadata.py <change_dir>

输出: 一行 JSON 对象（stdout），包含 YAML 中所有字段 + change 字段（= basename(change_dir)）
异常: 错误信息写到 stderr 并 exit 1
"""
import json
import os
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML not available; install with: pip install pyyaml\n")
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: load-metadata.py <change_dir>\n")
        sys.exit(1)

    change_dir = sys.argv[1]
    yaml_path = os.path.join(change_dir, ".openspec.yaml")
    if not os.path.isfile(yaml_path):
        sys.stderr.write(f"Metadata file not found: {yaml_path}\n")
        sys.exit(1)

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        sys.stderr.write(f"Invalid metadata structure in {yaml_path}: expected mapping\n")
        sys.exit(1)

    data["change"] = os.path.basename(os.path.normpath(change_dir))

    # default=str 处理 YAML 自动识别的 date / datetime 类型（如 created 字段）
    sys.stdout.write(json.dumps(data, ensure_ascii=False, default=str))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
