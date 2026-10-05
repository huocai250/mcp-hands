"""Check whether the upstream pipeline preserves JSON arrays / brackets verbatim."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["BRIDGE_CONFIG"] = os.path.join(ROOT, "configs", "bridge.config.mock.json")
sys.path.insert(0, ROOT)
import bridge  # noqa: E402

CASES = [
    ("echo json arrays", '请原样输出下面这行 JSON，一个字符都不要改，也不要加解释：{"a": [1, 2, 3], "b": ["x", "y"], "c": "中文"}'),
    ("echo json with cn text", '请原样输出下面这行 JSON，不要修改：{"paragraphs": ["这是第一行", "这是第二行"], "title": "标题"}'),
    ("fenced json", '只输出一个 ```json 代码块，内容为 {"path": "C:\\\\tmp\\\\a.docx", "rows": [["name", "qty"], ["apple", 3]]}，不要任何其它文字。'),
]

for label, prompt in CASES:
    payload = bridge.upstream_chat([{"role": "user", "content": prompt}])
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    print("=== %s ===" % label)
    print((message.get("content") or "")[:600].replace("\n", "\\n"))
