"""在运行时把 config.json 里搜不到的好友名替换为实际可用的写法。

映射来自 config/name_fixes.json，由 ``Repair Friend Names`` 工作流的实测结果固化而来。
只改好友名，其余配置（消息、间隔、防重复等）原样保留。

日志刻意不打印昵称，避免仓库/日志泄露隐私。
"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    config_path = Path("config.json")
    fixes_path = Path("config/name_fixes.json")

    if not config_path.is_file():
        print("未找到 config.json，跳过好友名修正")
        return 0
    if not fixes_path.is_file():
        print("未找到 config/name_fixes.json，跳过好友名修正")
        return 0

    config = json.loads(config_path.read_text(encoding="utf-8"))
    fixes = json.loads(fixes_path.read_text(encoding="utf-8"))
    if not isinstance(fixes, dict) or not fixes:
        print("好友名映射为空，跳过")
        return 0

    applied = 0
    if "friends" in config:
        friends = list(config["friends"])
        new_friends = []
        for name in friends:
            if name in fixes and fixes[name]:
                new_friends.append(fixes[name])
                applied += 1
            else:
                new_friends.append(name)
        config["friends"] = new_friends
    else:
        for target in config.get("targets", []):
            name = target.get("name")
            if name in fixes and fixes[name]:
                target["name"] = fixes[name]
                applied += 1

    if applied:
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已应用好友名修正: {applied} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
