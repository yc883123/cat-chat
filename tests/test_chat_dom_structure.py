"""聊天主网格与任务弹窗必须保持独立，输入区不能被错误闭标签挤出应用容器。"""
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class LayoutParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parents = {}
        self.launch_cards = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.parents[attrs["id"]] = list(self.stack)
        if "type-card" in attrs.get("class", "").split():
            self.launch_cards.append(list(self.stack))
        if tag not in VOID_TAGS:
            self.stack.append((tag, attrs.get("id", ""), attrs.get("class", "")))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


class ChatDomStructureTests(unittest.TestCase):
    def test_composer_and_panels_keep_their_intended_ancestors(self):
        parser = LayoutParser()
        parser.feed((ROOT / "public/index.html").read_text(encoding="utf-8"))
        composer = parser.parents["composerForm"]
        self.assertIn(("div", "appShell", "app-shell"), composer)
        self.assertIn(("main", "", "chat-main"), composer)
        self.assertEqual(composer[-1], ("section", "", "composer-wrap"))
        for dialog_id in ("superAgentLaunchDialog", "superAgentCreateDialog"):
            self.assertEqual(parser.parents[dialog_id][-1], ("main", "", "chat-main"), dialog_id)
        self.assertEqual(parser.parents["filePanel"][-1], ("div", "appShell", "app-shell"))
        self.assertEqual(len(parser.launch_cards), 5)
        for chain in parser.launch_cards:
            self.assertIn("superAgentLaunchDialog", [node_id for _, node_id, _ in chain])


if __name__ == "__main__":
    unittest.main()
