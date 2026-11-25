"""Open the existing launcher with user-selected preferences; never auto-run a title."""
import sys, json, importlib.util, importlib.machinery
from pathlib import Path
sys.dont_write_bytecode=True
ENGINE=Path(__file__).resolve().parents[2]
source=ENGINE/"launcher"/"vice_launcher.pyw"
sys.path.insert(0,str(source.parent))
loader=importlib.machinery.SourceFileLoader("vice_native",str(source))
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec)
loader.exec_module(module)
import tkinter as tk
cfg=json.loads(sys.argv[1])
if cfg["version"]=="victory.exe":
    selected="VIce - build actuel" if module.ENGINE_BUILDS["VIce - build actuel"].is_file() else "VIce - build debug"
elif cfg["version"]=="victory-debug.exe" and "VIce - build debug" in module.ENGINE_BUILDS and module.ENGINE_BUILDS["VIce - build debug"].is_file():
    selected="VIce - build debug"
else:
    selected=next((label for label,path in module.ENGINE_BUILDS.items() if path.name==cfg["version"] and path.is_file()),None)
if selected is None:
    candidate=ENGINE/"launcher"/"versions"/Path(cfg["version"]).name
    if not candidate.is_file() or candidate.suffix.lower()!=".exe":
        raise ValueError("Build introuvable")
    selected="VIce - "+candidate.name
    module.ENGINE_BUILDS[selected]=candidate
root=tk.Tk()
app=module.Launcher(root)
app.build_choice.set(selected)
app.console.set(cfg["console"])
app.debug.set(cfg["debug"])
app.surface.set(cfg["surface"])
app.warp.set(cfg["warp"])
app.refresh_cards()
root.after_idle(app.paint_hero)
if not cfg.get("developer",False):
    from tkinter import ttk
    root.withdraw()
    win=tk.Toplevel(root)
    win.title("VIce | Jouer")
    win.geometry("510x265");win.resizable(False,False);win.configure(bg="#211a29")
    panel=tk.Frame(win,bg="#211a29",padx=24,pady=22);panel.pack(fill="both",expand=True)
    tk.Label(panel,text="VIce",font=("Segoe UI",25,"bold"),bg="#211a29",fg="#e5b7ff").pack(anchor="w")
    tk.Label(panel,text="Choisis un exécutable compatible autorisé.",bg="#211a29",fg="#eeeeee",font=("Segoe UI",11)).pack(anchor="w",pady=(5,8))
    tk.Label(panel,textvariable=app.title_path,bg="#211a29",fg="#b9a9c5",wraplength=460).pack(anchor="w")
    ttk.Button(panel,text="Choisir un fichier",command=app.select_title).pack(fill="x",pady=4)
    ttk.Button(panel,text="Lancer",command=app.play).pack(fill="x",pady=4)
    ttk.Button(panel,text="Arrêter",command=app.stop).pack(fill="x",pady=4)
    tk.Label(panel,textvariable=app.status,bg="#211a29",fg="#b9a9c5").pack(anchor="w")
    win.protocol("WM_DELETE_WINDOW",app.close)
    assert not app.debug.get() and not app.surface.get() and not app.warp.get()
    if "--self-test" not in sys.argv:
        win.lift()
        win.attributes("-topmost",True)
        win.focus_force()
        win.after(300,lambda:win.attributes("-topmost",False))
else:
    if "--self-test" not in sys.argv:
        root.lift()
        root.attributes("-topmost",True)
        root.focus_force()
        root.after(300,lambda:root.attributes("-topmost",False))
if "--self-test" in sys.argv:
    root.update_idletasks()
    expected_name = "victory.exe" if cfg["version"] in ("victory.exe", "victory-debug.exe") else cfg["version"]
    assert module.ENGINE_BUILDS[app.build_choice.get()].name==expected_name
    assert app.console.get()==cfg["console"]
    assert (app.debug.get(),app.surface.get(),app.warp.get())==(cfg["debug"],cfg["surface"],cfg["warp"])
    assert app.proc is None and not app.busy
    root.destroy()
    print("Launcher bridge options OK; no title executed")
else:
    root.mainloop()
