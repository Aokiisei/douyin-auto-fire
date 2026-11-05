"""诊断脚本：找出 DOUYIN_CONFIG 中无法定位的好友的真实昵称。

流程：
1. 打开抖音私信页，抓取会话列表中的全部昵称（滚动加载）。
2. 对每个配置里的好友名做精确搜索，记录搜索面板返回的真实昵称。
3. 精确搜索无结果时，依次用更短的前缀重试，把搜到的昵称作为候选。

产物写入 artifacts/friend_report.json。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from app.browser import open_douyin, open_private_messages
from app.config import load_settings, load_task
from app.selectors import SEARCH_INPUTS


CONVERSATION_ROW_SELECTORS = (
    '[data-e2e="conversation-item"]',
    '[class*="conversationConversationItem"]',
    '[class*="ConversationItem"]',
    '[class*="conversation-item"]',
)

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

PLACEHOLDER_TEXTS = {
    "未搜索到相关内容",
    "暂无搜索结果",
    "暂无数据",
    "无搜索结果",
    "没有搜索结果",
    "搜索",
    "取消",
}

SCROLL_JS = """() => {
  let moved = 0;
  document.querySelectorAll('*').forEach(el => {
    if (el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 200) {
      el.scrollTop = el.scrollHeight;
      moved += 1;
    }
  });
  return moved;
}"""

UID_RE = re.compile(r"^\d{6,}$")


def _is_real_name(text: str, keyword: str) -> bool:
    """过滤占位文案与搜索框回显，只保留真实昵称。"""
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 40:
        return False
    if cleaned in PLACEHOLDER_TEXTS:
        return False
    if cleaned == keyword.strip():
        return False
    if UID_RE.match(cleaned):
        return False
    if "/" in cleaned and any(ch.isdigit() for ch in cleaned):
        return False
    return True


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


async def _panel_texts(page, keyword: str) -> list[str]:
    """抓取搜索面板中所有真实昵称（排除占位文案）。"""
    found: list[str] = []
    for selector in SEARCH_NAME_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, 100)):
            try:
                node = locator.nth(index)
                if not await node.is_visible():
                    continue
                text = (await node.inner_text()).strip()
                if _is_real_name(text, keyword) and text not in found:
                    found.append(text)
            except Exception:
                continue
        if found:
            return found
    # 兜底：抓取搜索面板容器内的可见短文本
    try:
        panel = page.locator('[class*="SearchPanelitem"], [class*="SearchPanel"]')
        count = await panel.count()
        for index in range(min(count, 60)):
            node = panel.nth(index)
            if not await node.is_visible():
                continue
            text = (await node.inner_text()).strip()
            for line in (part.strip() for part in text.split("\n")):
                if _is_real_name(line, keyword) and line not in found:
                    found.append(line)
    except Exception:
        pass
    return found


async def _panel_has_placeholder(page) -> bool:
    try:
        placeholder = page.locator('text=未搜索到相关内容')
        return await placeholder.count() > 0 and await placeholder.first.is_visible()
    except Exception:
        return False


async def _dump_conversations(page) -> list[str]:
    """抓取会话列表全部昵称，滚动到底部触发懒加载。"""
    names: list[str] = []
    for _ in range(12):
        await page.wait_for_timeout(1200)
        await page.evaluate(SCROLL_JS)
        await page.wait_for_timeout(600)
        current = await _conversation_names(page)
        if current:
            names = current
        if len(current) >= 30:
            break
    return names


async def _conversation_names(page) -> list[str]:
    """按会话行提取昵称：取行内第一个既非 UID 也非日期的文本行。"""
    names: list[str] = []
    for selector in CONVERSATION_ROW_SELECTORS:
        try:
            rows = page.locator(selector)
            count = await rows.count()
        except Exception:
            continue
        if count == 0:
            continue
        for index in range(min(count, 200)):
            try:
                row = rows.nth(index)
                if not await row.is_visible():
                    continue
                text = (await row.inner_text()).strip()
            except Exception:
                continue
            for line in (part.strip() for part in text.split("\n")):
                if not line or len(line) > 40:
                    continue
                if UID_RE.match(line):
                    continue
                if "/" in line and any(ch.isdigit() for ch in line):
                    continue
                if line in PLACEHOLDER_TEXTS:
                    continue
                if line not in names:
                    names.append(line)
                break
        if names:
            return names
    return names


async def _search(page, keyword: str) -> list[str]:
    """在私信搜索框搜索 keyword，返回搜索面板中的真实昵称列表。"""
    search = await _first_visible(page, SEARCH_INPUTS, 8000)
    if search is None:
        return []
    try:
        await search.click()
        await search.fill("")
        await page.wait_for_timeout(700)
        await search.fill(keyword)
    except Exception:
        return []
    await page.wait_for_timeout(2200)
    names = await _panel_texts(page, keyword)
    try:
        await search.fill("")
        await page.wait_for_timeout(400)
    except Exception:
        pass
    return names


def _prefix_candidates(name: str) -> list[str]:
    """为无法精确匹配的名字生成递减前缀候选。"""
    candidates: list[str] = []
    stripped = name.strip()
    length = len(stripped)
    if length <= 1:
        return candidates
    for size in range(length - 1, 0, -1):
        prefix = stripped[:size]
        if prefix and prefix not in candidates:
            candidates.append(prefix)
    return candidates


async def main() -> int:
    settings = load_settings()
    task = load_task(settings)

    configured = [target.name for target in task.targets]
    report: dict = {
        "configured": configured,
        "conversation_list": [],
        "per_name": {},
        "unresolved": [],
    }

    async with open_douyin(settings) as session:
        page = session.page
        await open_private_messages(page)

        conversations = await _dump_conversations(page)
        report["conversation_list"] = conversations
        print(f"会话列表抓取到 {len(conversations)} 个昵称: {conversations}", flush=True)

        for name in configured:
            entry: dict = {"exact": [], "prefix_probe": {}}
            hits = await _search(page, name)
            entry["exact"] = hits
            print(f"精确搜索 {name!r} -> {hits}", flush=True)
            if not hits:
                for prefix in _prefix_candidates(name):
                    probe = await _search(page, prefix)
                    if probe:
                        entry["prefix_probe"][prefix] = probe
                        print(f"  前缀 {prefix!r} -> {probe}", flush=True)
                    else:
                        print(f"  前缀 {prefix!r} -> 无结果", flush=True)
            report["per_name"][name] = entry
            if not hits:
                report["unresolved"].append(name)

    out_dir = Path(settings.artifacts_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "friend_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {out_path}", flush=True)
    print("SUMMARY " + json.dumps({
        "conversation_count": len(report["conversation_list"]),
        "unresolved": report["unresolved"],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
