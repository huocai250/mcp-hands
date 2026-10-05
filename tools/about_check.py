"""Smoke test for the About dialog: it must build without exceptions and show author/repo/license."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["BRIDGE_CONFIG"] = os.path.join(ROOT, "configs", "bridge.config.mock.json")
sys.path.insert(0, ROOT)

import bridge  # noqa: E402
import gui  # noqa: E402

print("version line :", bridge.version_line())
app = gui.Console(run_server=False)
app.update()
app.show_about()
app.update()
dialogs = [w for w in app.winfo_children() if w.winfo_class() == "Toplevel"]
print("about window :", bool(dialogs), dialogs[0].title() if dialogs else "-")
text = gui.ABOUT_TEXT % {"name": bridge.APP_NAME, "version": bridge.APP_VERSION,
                         "tagline": bridge.APP_TAGLINE, "author": bridge.APP_AUTHOR,
                         "url": bridge.APP_URL, "license": bridge.APP_LICENSE,
                         "tools": "261", "servers": len(gui.SERVERS)}
for needle in ("huocai250", "github.com/huocai250/mcp-hands", "MIT"):
    print("contains %-32s: %s" % (needle, needle in text))
print("--- dialog text ---")
print(text)
for win in dialogs:
    win.destroy()
app.destroy()
print("OK")
