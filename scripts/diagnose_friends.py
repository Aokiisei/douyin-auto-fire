"""诊断脚本：找出 DOUYIN_CONFIG 中无法定位的好友的真实昵称。

流程：
1. 打开抖音私信页，抓取会话列表中的全部昵称（滚动加载）。
2. 对每个配置里的好友名做精确搜索，记录搜索面板返回的真实昵称。
3. 精确搜索无结果时，用前缀 / 后缀 / 子串逐级回溯，找出能被搜到的写法。
4. 同时验证 config/candidates.json 里人工提供的候选名。

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

# 搜索面板里的 UI 文案（按钮、标签），不是昵称，不能作为命中依据。
UI_LABEL_TEXTS = {
    "联系人",
    "发消息",
    "继续聊天",
    "聊一聊",
    "添加好友",
    "关注",
}

# 含时间/状态后缀的行也不是昵称
_TIME_LIKE = ("天后消失", "天后", "小时前", "分钟前", "天前", "昨天", "刚刚", "周前", "月前", "已读", "未读")

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
    if cleaned in UI_LABEL_TEXTS:
        return False
    if cleaned == keyword.strip():
        return False
    if UID_RE.match(cleaned):
        return False
    if "/" in cleaned and any(ch.isdigit() for ch in cleaned):
        return False
    if any(token in cleaned for token in _TIME_LIKE):
        return False
    return True


def _is_ui_label(text: str) -> bool:
    """判断是否只是搜索面板的 UI 文案（弱命中，不足以确认真名）。"""
    cleaned = text.strip()
    return cleaned in UI_LABEL_TEXTS or any(token in cleaned for token in _TIME_LIKE)


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


async def _panel_texts(page, keyword: str) -> tuple[list[str], list[str]]:
    """抓取搜索面板内容。

    返回 (强命中昵称, 弱命中 UI 文案)。只有强命中能证明搜到了真人。
    """
    strong: list[str] = []
    weak: list[str] = []
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
            except Exception:
                continue
            if not text:
                continue
            if _is_real_name(text, keyword):
                if text not in strong:
                    strong.append(text)
            elif _is_ui_label(text) and text not in weak:
                weak.append(text)
        if strong:
            return strong, weak
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
                if not line:
                    continue
                if _is_real_name(line, keyword):
                    if line not in strong:
                        strong.append(line)
                elif _is_ui_label(line) and line not in weak:
                    weak.append(line)
    except Exception:
        pass
    return strong, weak


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


async def _search(page, keyword: str) -> tuple[list[str], list[str]]:
    """在私信搜索框搜索 keyword，返回 (强命中昵称, 弱命中 UI 文案)。"""
    search = await _first_visible(page, SEARCH_INPUTS, 8000)
    if search is None:
        return [], []
    try:
        await search.click()
        await search.fill("")
        await page.wait_for_timeout(700)
        await search.fill(keyword)
    except Exception:
        return [], []
    await page.wait_for_timeout(2200)
    strong, weak = await _panel_texts(page, keyword)
    try:
        await search.fill("")
        await page.wait_for_timeout(400)
    except Exception:
        pass
    return strong, weak


def _probe_candidates(name: str) -> list[str]:
    """生成回溯候选：前缀 -> 后缀 -> 子串。"""
    stripped = name.strip()
    length = len(stripped)
    seen: list[str] = []
    for size in range(1, length):
        seen.append(stripped[:size])
    for size in range(1, length):
        seen.append(stripped[-size:])
    for size in range(2, length):
        for start in range(0, length - size + 1):
            seen.append(stripped[start:start + size])
    ordered: list[str] = []
    for item in seen:
        if item and item not in ordered:
            ordered.append(item)
    return ordered


async def _probe(page, name: str, budget: int = 60) -> dict:
    """对无法精确匹配的名字做回溯探测，找到第一个强命中就停。"""
    probes: dict = {}
    for index, candidate in enumerate(_probe_candidates(name)):
        if index >= budget:
            break
        strong, weak = await _search(page, candidate)
        if strong:
            probes[candidate] = strong
            print(f"  候选 {candidate!r} -> 强命中 {strong}", flush=True)
            return probes
        if weak:
            probes[candidate] = f"(仅UI文案) {weak}"
    return probes


async def main() -> int:
    settings = load_settings()
    task = load_task(settings)

    candidates_file = Path("config/candidates.json")
    manual_candidates: dict = {}
    if candidates_file.is_file():
        manual_candidates = json.loads(candidates_file.read_text(encoding="utf-8"))

    configured = [target.name for target in task.targets]
    report: dict = {
        "configured": configured,
        "conversation_list": [],
        "per_name": {},
        "unresolved": [],
        "resolved_mapping": {},
    }

    async with open_douyin(settings) as session:
        page = session.page
        await open_private_messages(page)

        conversations = await _dump_conversations(page)
        report["conversation_list"] = conversations
        print(f"会话列表抓取到 {len(conversations)} 个昵称: {conversations}", flush=True)

        for name in configured:
            entry: dict = {"exact": [], "probe": {}, "manual": {}}
            strong, weak = await _search(page, name)
            entry["exact"] = strong
            if not strong and weak:
                entry["exact_weak_only"] = weak
            print(f"精确搜索 {name!r} -> {strong or ('仅UI文案:' + str(weak) if weak else '无结果')}", flush=True)

            resolved = bool(strong)
            if not strong:
                # 先试人工候选
                for candidate in manual_candidates.get(name, []):
                    cand_strong, cand_weak = await _search(page, candidate)
                    entry["manual"][candidate] = cand_strong or (f"(仅UI文案) {cand_weak}" if cand_weak else [])
                    print(f"  人工候选 {candidate!r} -> {entry['manual'][candidate]}", flush=True)
                    if cand_strong:
                        report["resolved_mapping"][name] = cand_strong
                        resolved = True
                        break

            if not resolved:
                entry["probe"] = await _probe(page, name)

            report["per_name"][name] = entry
            if not resolved:
                report["unresolved"].append(name)

    out_dir = Path(settings.artifacts_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "friend_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {out_path}", flush=True)
    print("SUMMARY " + json.dumps({
        "conversation_count": len(report["conversation_list"]),
        "resolved_mapping": report["resolved_mapping"],
        "unresolved": report["unresolved"],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
