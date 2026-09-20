#!/usr/bin/env python3
"""Generate the human and machine-readable tool contract from server.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import TOOLS  # noqa: E402


DOCS = ROOT / "docs"


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "tools.json").write_text(
        json.dumps({"tools": TOOLS}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Hugo MCP 工具合同",
        "",
        "本文件由 `scripts/generate_tools_docs.py` 从 `server.py` 的 `TOOLS` 注册表生成。",
        f"实际工具列表、参数 Schema 与本文保持一致；当前服务提供 {len(TOOLS)} 个工具。",
        "",
    ]
    for tool in TOOLS:
        lines.extend(
            [
                f"## `{tool['name']}`",
                "",
                tool["description"],
                "",
                "### 输入 Schema",
                "",
                "```json",
                json.dumps(tool["inputSchema"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    (DOCS / "tools.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"generated {len(TOOLS)} tools")


if __name__ == "__main__":
    main()
