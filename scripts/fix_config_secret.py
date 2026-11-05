"""修复 DOUYIN_CONFIG 中搜不到的好友名，并把修正后的配置写回 Secret。

做法：不在本地猜测昵称，而是用生产代码路径（app.douyin.DouyinChat.open_target）
在抖音私信页上实测每个候选名，只有真正能打开聊天（搜索命中 + 聊天标题确认）
的写法才会被写回配置。

候选名来自 config/fixes.json：
    {"旧名": ["候选1", "候选2", ...]}

输出 artifacts/corrected-config.json，供工作流写回 DOUYIN_CONFIG Secret。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from app.browser import open_douyin, open_private_messages
from app.config import load_settings
from app.douyin import DouyinChat
from app.selectors import SEARCH_INPUTS


SEARCH_NAME_SELECTORS = (
    '[class*="SearchPanelitemtitle"]',
    '[class*="SearchPanelitemTitle"]',
    '[class*="SearchPanelitem_title"]',
    '[class*="SearchPanelitem-title"]',
    '[class*="SearchPanelitemname"]',
    '[class*="SearchPanelitemName"]',
    '[class*="SearchPanelitem_name"]',
    '[class*="SearchPanelitem-name"]',
)


async def _first_visible(page, selectors, timeout_ms):
    per = max(250, timeout_ms // max(1, len(selectors)))
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=per)
            return locator
        except Exception:
            continue
    return None


async def _read_exact_result_name(page, keyword: str) -> str | None:
    """读取搜索结果名称节点的精确 inner_text（含可能的换行/emoji 变体）。"""
    for selector in SEARCH_NAME_SELECTORS:
        try:
            nodes = page.locator(selector)
            count = await nodes.count()
        except Exception:
            continue
        for index in range(min(count, 40)):
            try:
                node = nodes.nth(index)
                if not await node.is_visible():
                    continue
                text = (await node.inner_text()).strip()
            except Exception:
                continue
            if text and keyword and keyword[:1] in text:
                return text
    return None


async def _try_open(page, chat: DouyinChat, name: str) -> tuple[bool, str]:
    """用生产路径打开聊天，返回 (是否成功, 说明)。"""
    try:
        await chat.open_target(name, retries=0)
        return True, "open_target 成功"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _probe_candidate(page, chat: DouyinChat, candidate: str) -> tuple[str | None, str]:
    """搜索候选名，拿到精确渲染文本后实测能否打开聊天。

    返回 (可用的配置值, 说明)。可用值为 None 表示该候选不可用。
    """
    search = await _first_visible(page, SEARCH_INPUTS, 10_000)
    if search is None:
        return None, "搜索框不可用"
    try:
        await search.click()
        await search.fill("")
        await page.wait_for_timeout(700)
        await search.fill(candidate)
    except Exception as exc:
        return None, f"填入失败: {exc}"
    await page.wait_for_timeout(2500)

    exact = await _read_exact_result_name(page, candidate)
    variants: list[str] = []
    if exact and exact != candidate:
        variants.append(exact)
    variants.append(candidate)

    notes: list[str] = []
    for variant in variants:
        ok, note = await _try_open(page, chat, variant)
        notes.append(f"{variant!r}: {note}")
        if ok:
            return variant, " | ".join(notes)
    return None, " | ".join(notes)


async def main() -> int:
    settings = load_settings()
    raw_config = os.environ.get("DOUYIN_CONFIG", "")
    if not raw_config.strip():
        print("错误: DOUYIN_CONFIG 为空")
        return 2

    config = json.loads(raw_config)
    fixes: dict = {}
    fixes_file = Path("config/fixes.json")
    if fixes_file.is_file():
        fixes = json.loads(fixes_file.read_text(encoding="utf-8"))

    if "friends" in config:
        names: list[str] = list(config["friends"])
    else:
        names = [t.get("name", "") for t in config.get("targets", [])]

    targets_to_fix = [n for n in names if n in fixes]
    print(f"配置好友数: {len(names)}，待修正: {targets_to_fix}", flush=True)

    replacements: dict[str, str] = {}
    if targets_to_fix:
        async with open_douyin(settings) as session:
            page = session.page
            await open_private_messages(page)
            chat = DouyinChat(page, timeout_ms=20_000)
            for old_name in targets_to_fix:
                print(f"--- 修正 {old_name!r} ---", flush=True)
                for candidate in fixes[old_name]:
                    value, note = await _probe_candidate(page, chat, candidate)
                    print(f"  候选 {candidate!r}: {note}", flush=True)
                    if value:
                        replacements[old_name] = value
                        print(f"  => 采用 {value!r}", flush=True)
                        break
                if old_name not in replacements:
                    print(f"  => 无可用候选，保留原名 {old_name!r}", flush=True)

    # 应用替换
    if "friends" in config:
        config["friends"] = [replacements.get(n, n) for n in config["friends"]]
    else:
        for target in config.get("targets", []):
            name = target.get("name", "")
            if name in replacements:
                target["name"] = replacements[name]

    out_dir = Path(settings.artifacts_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "corrected-config.json"
    out_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    print("REPLACEMENTS " + json.dumps(replacements, ensure_ascii=False), flush=True)
    print(f"修正后的配置已写入 {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
