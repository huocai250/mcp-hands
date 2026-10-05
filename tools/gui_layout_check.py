"""Layout smoke test: the GUI must stay usable in a small window (everything reachable)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["BRIDGE_CONFIG"] = os.path.join(ROOT, "configs", "bridge.config.mock.json")
sys.path.insert(0, ROOT)

import gui  # noqa: E402

app = gui.Console(run_server=False)
app.update()
print("window:", app.winfo_width(), "x", app.winfo_height())
print("log widget present:", bool(app.log))
print("server checkboxes:", len(app.server_vars), "->", ", ".join(sorted(app.server_vars))[:120])

def find_canvas(widget):
    for child in widget.winfo_children():
        if child.winfo_class() == "Canvas":
            return child
        found = find_canvas(child)
        if found is not None:
            return found
    return None


canvas = find_canvas(app)
print("settings canvas found:", canvas is not None)
if canvas:
    print("scrollregion at full size:", canvas.cget("scrollregion"))

for _ in range(20):
    app.update()
    app.update_idletasks()

app.geometry("520x380")
app.update()
app.update_idletasks()
try:
    app.paned.sashpos(0, int(app.paned.winfo_height() * 0.52))
except Exception as exc:
    print("sash adjust failed:", exc)
app.update()
print("small window:", app.winfo_width(), "x", app.winfo_height())
if canvas:
    print("scrollregion when small:", canvas.cget("scrollregion"))
print("log height when small:", app.log.winfo_height())
app.destroy()
print("OK")
