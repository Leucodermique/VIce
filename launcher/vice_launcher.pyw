"""VIce developer launcher: local builds, synthetic graphics tests and diagnostics."""
from pathlib import Path
import os
import queue
import re
from collections import Counter
from datetime import datetime
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sys
from vice_visuals import studio_background, round_gradient, style_buttons, icon, Switch, ImageTk
from tkinter import font as tkfont

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VIce"
COMMANDS = {
    "black-screen": [sys.executable.replace("pythonw.exe", "python.exe"), str(ROOT / "launcher" / "black_screen.py")],
    "lab": [sys.executable.replace("pythonw.exe", "python.exe"), str(ROOT / "launcher" / "graphics_lab.py")],
    "build": ["cargo", "build", "--locked", "--profile", "diag", "-p", "victory", "--features", "diagnostics"],
    "config": ["cargo", "run", "--locked", "-p", "victory", "--features", "diagnostics", "--", "config"],
    "graphics": ["cargo", "test", "--locked", "-p", "victory-gpu", "--features", "diagnostics", "--lib", "--", "--test-threads=1"],
    "shaders": ["cargo", "test", "--locked", "-p", "victory-shader", "--lib", "--", "--test-threads=1"],
    "demo": ["cargo", "test", "--locked", "-p", "victory-gpu", "--features", "diagnostics", "--lib", "depth_bounds_tests", "--", "--nocapture", "--test-threads=1"],
}

ENGINE_BUILDS = {
    "VIce - build actuel": ROOT / "target" / "diag" / "victory.exe",
    "VIce - build debug": ROOT / "target" / "debug" / "victory.exe",
    "VIce - sauvegarde 22h15": ROOT / "launcher" / "versions" / "vice-20261124-221555.exe",
    "VIce - sauvegarde 21h11": ROOT / "launcher" / "versions" / "vice-20261124-211158.exe",
    "VIce - archive 19h44": ROOT / "launcher" / "versions" / "vice-20261124-194427.exe",
}

TITLE_STATE_FILE = DATA / "launcher" / "last_title.txt"

def default_title_path():
    try:
        if TITLE_STATE_FILE.is_file():
            saved = Path(TITLE_STATE_FILE.read_text(encoding="utf-8").strip())
            if saved.is_file() and saved.suffix.lower() == ".exe":
                return str(saved)
    except OSError:
        pass
    desktop = Path.home() / "Desktop"
    for candidate in (
        desktop / "GTA6.Xbox.v1.0.2.1-CYBERLEEK" / "game_scarlett_master.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    if desktop.is_dir():
        for folder in sorted(desktop.glob("GTA6*")):
            exe = folder / "game_scarlett_master.exe"
            if exe.is_file():
                return str(exe)
    return ""

def save_title_path(path):
    try:
        TITLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TITLE_STATE_FILE.write_text(str(path), encoding="utf-8")
    except OSError:
        pass

def play_command(path, console, build="VIce - build actuel"):
    engine = ENGINE_BUILDS.get(build)
    if engine is None:
        raise ValueError("Version inconnue.")
    if not engine.is_file():
        raise ValueError("Build the engine first.")
    title = Path(path).expanduser().resolve(strict=True)
    if not title.is_file() or title.suffix.lower() != ".exe":
        raise ValueError("Select a compatible executable file.")
    if console not in ("series-x", "series-s"):
        raise ValueError("Invalid console profile.")
    return [str(engine), "run", str(title), "--execute", "--console", console]

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def clean_log(line):
    return ANSI.sub("", line)

def log_category(line):
    for text, label in (
        ("host pipeline refused", "Pipeline rejected"),
        ("host mesh pipeline refused", "Mesh pipeline rejected"),
        ("host root signature refused", "Root signature rejected"),
        ("slow host submission", "Slow GPU submission"),
        ("no host texture", "Texture rejected"),
        ("D3D12:", "Direct3D validation message"),
        ("is not supported", "Unsupported operation"),
    ):
        if text in line:
            return label
    return None

def environment(warp, debug, surface=False):
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    for key, value in [("VICTORY_D3D12_WARP", warp), ("VICTORY_D3D12_DEBUG", debug), ("VICE_SURFACE_STATS", surface)]:
        env.pop(key, None)
        if value:
            env[key] = "1"
    return env

class Launcher:
    def __init__(self, root):
        self.root=root
        self.events=queue.Queue()
        self.proc=None
        self.busy=False
        self.warp=tk.BooleanVar(value=False)
        self.title_path=tk.StringVar(value=default_title_path())
        self.console=tk.StringVar(value="series-x")
        self.build_choice=tk.StringVar(value="VIce - build actuel")
        self.debug=tk.BooleanVar(value=False)
        self.surface=tk.BooleanVar(value=False)
        self.status=tk.StringVar(value="Prêt à lancer")
        self.version=tk.StringVar(value="Non compilé")
        self.engine_state=tk.StringVar(value="Non testé")
        self.last_run=tk.StringVar(value="Aucun lancement")
        self.buttons=[]
        self.advanced=None
        self.bg="#101319"; self.panel="#191e26"; self.fg="#edf0f5"; self.muted="#a1aab8"
        available = set(tkfont.families(root))
        self.font = "Segoe UI Variable Text" if "Segoe UI Variable Text" in available else "Segoe UI"
        root.option_add("*Font", (self.font, 11))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=self.font, size=11)
        root.title("VIce | Launcher")
        root.geometry("1080x760")
        root.minsize(850,650)
        root.configure(bg=self.bg)
        style=ttk.Style(root)
        style.theme_use("clam")
        self.button_images=style_buttons(style)
        style.configure("TButton",background=self.bg,foreground=self.fg,padding=(15,9),font=(self.font,11))
        style.map("TButton",foreground=[("disabled","#737d8a"),("active","#ffffff")])
        style.configure("TCombobox",font=(self.font,11),padding=8,fieldbackground=self.panel,background=self.panel,foreground=self.fg,arrowcolor=self.fg,bordercolor="#353c48")
        style.map("TCombobox",fieldbackground=[("readonly",self.panel)],foreground=[("readonly",self.fg)])
        root.option_add("*TCombobox*Listbox.background", self.panel)
        root.option_add("*TCombobox*Listbox.foreground", self.fg)
        root.option_add("*TCombobox*Listbox.selectBackground", "#344965")
        style.configure("TCheckbutton",background=self.panel,foreground=self.fg,font=(self.font,10))
        style.map("TCheckbutton",background=[("active",self.panel)])
        style.configure("TLabel",background=self.bg,foreground=self.fg)
        top=tk.Frame(root,bg="#171b22",padx=25,pady=12)
        top.pack(fill="x")
        tk.Label(top,text="VI",font=(self.font,25,"bold"),fg="#92b8fc",bg="#171b22").pack(side="left")
        tk.Label(top,text="ce",font=(self.font,25,"bold"),fg=self.fg,bg="#171b22").pack(side="left")
        tk.Label(top,text="|   Launcher expérimental",font=(self.font,11),fg=self.muted,bg="#171b22").pack(side="left",padx=14)
        ttk.Button(top,text="Paramètres",command=self.open_advanced).pack(side="right")
        self.hero=tk.Canvas(root,height=300,bg=self.bg,highlightthickness=0,takefocus=True)
        self.hero.pack(fill="both",expand=True)
        self.hero.bind("<Configure>",self.paint_hero)
        self.hero.bind("<Button-1>",self.hero_click)
        self.hero.bind("<Motion>",self.hero_motion)
        self.hero.bind("<Leave>",lambda _:self.set_hover(False))
        self.hover=False
        self.hero.bind("<Key-Return>",lambda _:self.play())
        body=tk.Frame(root,bg=self.bg,padx=24,pady=8)
        body.pack(fill="x")
        selector=tk.Frame(body,bg=self.panel,padx=12,pady=8,highlightbackground="#303844",highlightthickness=1)
        selector.pack(fill="x",pady=(0,10),padx=5)
        tk.Label(selector,text="VERSION DU MOTEUR",bg=self.panel,fg="#9db9ed",font=(self.font,10,"bold")).pack(side="left",padx=(0,12))
        self.build_select=ttk.Combobox(selector,textvariable=self.build_choice,values=tuple(ENGINE_BUILDS),state="readonly",width=30)
        self.build_select.pack(side="left",fill="x",expand=True)
        self.build_select.bind("<<ComboboxSelected>>",lambda _:self.refresh_cards())
        ttk.Button(selector,text="Sources",command=self.open_sources).pack(side="right",padx=(10,0))
        actions=tk.Frame(body,bg=self.bg)
        actions.pack(fill="x",pady=(0,18))
        for title,fn,tracked in (("Relancer",self.play,True),("Analyser le dernier crash",lambda:self.start("black-screen"),True),("Ouvrir les logs",self.open_log_window,False)):
            b=ttk.Button(actions,text=title,command=fn)
            b.pack(side="left",fill="x",expand=True,padx=5)
            if tracked:self.buttons.append(b)
        cards=tk.Frame(body,bg=self.bg)
        cards.pack(fill="x",pady=(0,16))
        for i,(label,var,accent) in enumerate((("Version moteur",self.version,"#9db9ed"),("État GPU",self.engine_state,"#9db9ed"),("Dernier lancement",self.last_run,"#9db9ed"))):
            cards.columnconfigure(i,weight=1,uniform="cards")
            box=tk.Frame(cards,bg=self.panel,highlightbackground="#303844",highlightthickness=1,padx=15,pady=14)
            box.grid(row=0,column=i,sticky="nsew",padx=5)
            head=tk.Frame(box,bg=self.panel)
            head.pack(fill="x")
            glyph=tk.Canvas(head,width=32,height=30,bg=self.panel,highlightthickness=0)
            glyph.pack(side="left",padx=(0,7))
            icon(glyph,("box","chip","clock")[i],16,15,accent)
            tk.Label(head,text=label,font=(self.font,10,"bold"),fg=accent,bg=self.panel).pack(side="left")
            tk.Label(box,textvariable=var,font=(self.font,13,"bold"),fg=self.fg,bg=self.panel,wraplength=210,justify="left").pack(anchor="w",pady=(6,0))
        opts=tk.Frame(body,bg=self.panel,padx=10,pady=13,highlightbackground="#303844",highlightthickness=1)
        opts.pack(fill="x")
        for title,var,accent in (("Debug Direct3D",self.debug,"#5488df"),("Statistiques de surface",self.surface,"#5488df"),("Mode WARP",self.warp,"#5488df")):
            Switch(opts,title,var,accent,self.panel).pack(side="left",expand=True,padx=5)
        tk.Label(body,text="Debug Direct3D : journal de validation GPU (moteur compil? avec diagnostics).",bg=self.bg,fg=self.muted,font=(self.font,10)).pack(anchor="w",padx=8,pady=(6,0))
        footer=tk.Frame(root,bg="#171b22",padx=24,pady=12)
        footer.pack(fill="x")
        tk.Label(footer,textvariable=self.status,font=(self.font,12,"bold"),fg="#83eeb7",bg="#171b22").pack(side="left")
        ttk.Button(footer,text="Arrêter",command=self.stop).pack(side="right")
        self.logwin=tk.Toplevel(root)
        self.logwin.withdraw()
        self.logwin.title("VIce | Journaux")
        self.logwin.geometry("900x600")
        self.logwin.configure(bg=self.bg)
        self.logwin.protocol("WM_DELETE_WINDOW",self.logwin.withdraw)
        bar=tk.Frame(self.logwin,bg=self.bg,padx=12,pady=8)
        bar.pack(fill="x")
        ttk.Button(bar,text="Dossier des logs",command=self.open_logs).pack(side="left")
        ttk.Button(bar,text="Rapport de crash",command=self.open_black_screen).pack(side="left",padx=8)
        self.log=tk.Text(self.logwin,bg="#101319",fg="#d5dce7",font=("Consolas",10),wrap="word",padx=12,pady=12)
        scroll=ttk.Scrollbar(self.logwin,orient="vertical",command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right",fill="y"); self.log.pack(fill="both",expand=True)
        self.refresh_cards()
        self.write("VIce - interface simplifiee\nJournaux complets conserves sur disque.\n")
        root.after(80,self.pump)
        root.protocol("WM_DELETE_WINDOW",self.close)

    def refresh_cards(self):
        engine=ENGINE_BUILDS[self.build_choice.get()]
        if engine.is_file():self.version.set(datetime.fromtimestamp(engine.stat().st_mtime).strftime("Build %d/%m %H:%M"))
        else:self.version.set("Build absent")
        path=DATA/"launcher"/"play.log"
        if path.is_file():
            self.last_run.set(datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m a %H:%M"))
            try:
                with path.open("rb") as f:
                    f.seek(max(0,path.stat().st_size-18000))
                    tail=f.read().decode("utf-8",errors="replace")
                self.engine_state.set("Dernier crash GPU" if "host GPU failure" in tail or "host GPU device removed" in tail else "Dernier arrêt normal" if "Exit code: 0" in tail else "État non confirmé")
            except OSError:self.engine_state.set("État non confirmé")

    def paint_hero(self,event=None):
        c=self.hero
        w,h=c.winfo_width(),c.winfo_height()
        if w<20 or h<20:w,h=1080,300
        c.delete("all")
        self.hero_image=ImageTk.PhotoImage(studio_background(w,h))
        c.create_image(0,0,image=self.hero_image,anchor="nw")
        left=48
        c.create_text(left,h*.18,anchor="w",text="MOTEUR DE COMPATIBILITE",font=(self.font,10,"bold"),fill="#8faee0")
        c.create_text(left,h*.35,anchor="w",text="VIce",font=(self.font,44,"bold"),fill="#f4f6fa")
        c.create_text(left,h*.51,anchor="w",text="Environnement experimental  /  Xbox Series X ou S",font=(self.font,12),fill="#acb8ca")
        bw,bh=min(320,w-96),52
        x0=left;y0=max(h*.61,h*.51+20);x1=x0+bw;y1=y0+bh
        self.play_bounds=(x0,y0,x1,y1)
        self.play_image=ImageTk.PhotoImage(round_gradient(bw,bh,not self.busy,self.hover))
        c.create_image(x0,y0,image=self.play_image,anchor="nw",tags="play")
        c.create_text((x0+x1)/2,(y0+y1)/2,text="Lancement en cours..." if self.busy else "\u25b6   Lancer le jeu",font=(self.font,14,"bold"),fill="white",tags="play")

    def hero_click(self,e):
        x0,y0,x1,y1=getattr(self,"play_bounds",(0,0,0,0))
        if x0<=e.x<=x1 and y0<=e.y<=y1 and not self.busy:self.play()

    def set_hover(self,on):
        if self.hover!=on:
            self.hover=on
            self.paint_hero()

    def hero_motion(self,e):
        x0,y0,x1,y1=getattr(self,"play_bounds",(0,0,0,0))
        on=x0<=e.x<=x1 and y0<=e.y<=y1 and not self.busy
        self.hero.configure(cursor="hand2" if on else "")
        self.set_hover(on)

    def open_log_window(self):
        self.logwin.deiconify()
        self.logwin.lift()

    def open_sources(self):
        import webbrowser
        win=tk.Toplevel(self.root)
        win.title("VIce | Versions et sources")
        win.geometry("640x360")
        win.configure(bg=self.bg)
        p=tk.Frame(win,bg=self.bg,padx=22,pady=20)
        p.pack(fill="both",expand=True)
        tk.Label(p,text="Versions locales",fg=self.fg,bg=self.bg,font=(self.font,18,"bold")).pack(anchor="w")
        tk.Label(p,text="Les versions proposées sont des builds locaux. Aucune ne certifie un rendu complet.",wraplength=580,justify="left",fg=self.muted,bg=self.bg).pack(anchor="w",pady=(4,14))
        for title,url in (
            ("VIctory - code public", "https://github.com/Marvisak/VIctory"),
            ("XCL - code public", "https://github.com/ivall/xcl"),
            ("Rapports de tests VIctory", "https://github.com/Marvisak/VIctory/issues/49"),
        ):
            ttk.Button(p,text=title,command=lambda u=url:webbrowser.open(u)).pack(fill="x",pady=4)
        tk.Label(p,text="CGBJ : code prive. Aucune version externe non verifiee installee.",wraplength=580,fg="#d9a5ea",bg=self.bg,justify="left").pack(anchor="w",pady=12)

    def open_advanced(self):
        if self.advanced is not None and self.advanced.winfo_exists():
            self.advanced.lift();return
        win=self.advanced=tk.Toplevel(self.root)
        win.title("VIce | Paramètres et outils")
        win.geometry("550x720");win.configure(bg=self.bg)
        panel=tk.Frame(win,bg=self.bg,padx=20,pady=18);panel.pack(fill="both",expand=True)
        ttk.Label(panel,text="Titre compatible autorise").pack(anchor="w")
        ttk.Label(panel,textvariable=self.title_path,wraplength=490).pack(anchor="w",pady=8)
        ttk.Button(panel,text="Choisir un exécutable",command=self.select_title).pack(fill="x")
        ttk.Combobox(panel,textvariable=self.console,values=("series-x","series-s"),state="readonly").pack(fill="x",pady=8)
        for title,key in (("Compiler le moteur","build"),("Vérifier la configuration","config"),("Laboratoire graphique","lab"),("Demo graphique","demo"),("Tests GPU","graphics"),("Tests des shaders","shaders")):
            ttk.Button(panel,text=title,command=lambda k=key:self.start(k)).pack(fill="x",pady=3)
        ttk.Button(panel,text="Rapport graphique",command=self.open_lab).pack(fill="x",pady=3)
        ttk.Button(panel,text="Rapport écran noir",command=self.open_black_screen).pack(fill="x",pady=3)
        ttk.Button(panel,text="Dossier VIce",command=lambda:os.startfile(ROOT)).pack(fill="x",pady=3)

    def write(self, msg):
        self.log.insert("end", msg)
        self.log.see("end")
        # Keep the interface responsive during long runs; the disk log is complete.
        if int(self.log.index("end-1c").split(".")[0]) > 8000:
            self.log.delete("1.0", "1001.0")

    def open_logs(self):
        path = DATA / "launcher"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_black_screen(self):
        latest = DATA / "black-screen" / "latest.txt"
        if latest.is_file():
            path = Path(latest.read_text(encoding="utf-8"))
            if path.is_file():
                os.startfile(path)
                return
        messagebox.showinfo("VIce", "Run Analyze black screen first.")

    def open_lab(self):
        latest = DATA / "graphics-lab" / "latest.txt"
        if latest.is_file():
            path = Path(latest.read_text(encoding="utf-8"))
            if path.is_file():
                os.startfile(path)
                return
        messagebox.showinfo("VIce", "Run Visual graphics lab first.")

    def select_title(self):
        selected = filedialog.askopenfilename(
            title="Select a trusted, authorized compatible title",
            filetypes=[("Executable files", "*.exe")])
        if selected:
            self.title_path.set(selected)
            save_title_path(selected)

    def play(self):
        if self.busy:
            return
        if not self.title_path.get():
            self.select_title()
        if not self.title_path.get():
            return
        try:
            cmd = play_command(self.title_path.get(), self.console.get(), self.build_choice.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("VIce", str(exc))
            return
        self.start("play", cmd)

    def start(self, key, cmd=None):
        if self.busy:
            return
        self.busy = True
        self.build_select.configure(state="disabled")
        self.status.set("En cours / " + key.upper())
        self.paint_hero()
        for b in self.buttons:
            b.configure(state="disabled")
        cmd = COMMANDS[key] if cmd is None else cmd
        env = environment(self.warp.get(), self.debug.get(), self.surface.get())
        self.write("\n> " + subprocess.list2cmdline(cmd) + "\n")
        threading.Thread(target=self.worker, args=(key, cmd, env), daemon=True).start()

    def worker(self, key, cmd, env):
        code = 1
        try:
            folder = DATA / "launcher"
            folder.mkdir(parents=True, exist_ok=True)
            current = folder / (key + ".log")
            if current.exists():
                current.replace(folder / (key + ".previous.log"))
            counts = Counter()
            with current.open("w", encoding="utf-8") as log:
                log.write("VIce session " + datetime.now().isoformat() + "\n")
                if key == "play":
                    engine = Path(cmd[0])
                    log.write("Engine: " + str(engine) + "\n")
                    log.write("Engine build: " + datetime.fromtimestamp(engine.stat().st_mtime).isoformat() + "\n")
                    log.write("Direct3D debug requested: " + env.get("VICTORY_D3D12_DEBUG", "0") + "\n")
                    log.write("WARP requested: " + env.get("VICTORY_D3D12_WARP", "0") + "\n")
                    log.flush()
                self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for line in self.proc.stdout:
                    line = clean_log(line)
                    category = log_category(line)
                    if category:
                        counts[category] += 1
                    log.write(line)
                    log.flush()
                    self.events.put(("line", line))
                code = self.proc.wait()
                summary = "\nDIAGNOSTIC SUMMARY\n" + "\n".join(
                    f"{label}: {number}" for label, number in counts.most_common()
                ) + "\nExit code: " + str(code) + "\n"
                log.write(summary)
                self.events.put(("line", summary))
        except Exception as exc:
            self.events.put(("line", str(exc) + "\n"))
        finally:
            self.proc = None
            self.events.put(("done", code))

    def pump(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self.write(value)
            else:
                self.busy = False
                self.build_select.configure(state="readonly")
                for b in self.buttons:
                    b.configure(state="normal")
                self.status.set("Terminé / Code " + str(value))
                self.refresh_cards()
                self.paint_hero()
                self.write("\nExit code: " + str(value) + "\n")
        self.root.after(80, self.pump)

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)

    def close(self):
        self.stop()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = Launcher(root)
    if "--self-test" in sys.argv:
        root.update_idletasks()
        assert len(app.buttons) == 2
        app.paint_hero()
        assert app.hero.find_all()
        assert app.logwin.winfo_exists()
        app.open_advanced()
        assert app.advanced.winfo_exists()
        assert app.title_path.get() == ""
        assert len(ENGINE_BUILDS) == 4
        assert all(p.is_file() for p in ENGINE_BUILDS.values())
        assert app.build_choice.get() == "VIce - build actuel"
        app.build_choice.set("VIce - archive 19h44")
        app.refresh_cards()
        assert "19:44" in app.version.get()
        assert play_command(str(ENGINE_BUILDS["VIce - build actuel"]), "series-x", app.build_choice.get())[0] == str(ENGINE_BUILDS[app.build_choice.get()])
        assert not app.warp.get()
        assert environment(False, True)["VICTORY_D3D12_DEBUG"] == "1"
        assert "VICTORY_D3D12_DEBUG" not in environment(False, False)
        assert app.button_images
        assert clean_log("\x1b[33mWARN\x1b[0m") == "WARN"
        assert "diagnostics" in COMMANDS["build"]
        assert log_category("WARN host pipeline refused") == "Pipeline rejected"
        assert log_category("INFO shader translated") is None
        assert environment(True, False)["VICTORY_D3D12_WARP"] == "1"
        assert "VICTORY_D3D12_WARP" not in environment(False, False)
        print("VIce launcher UI smoke test passed")
        root.destroy()
    else:
        root.mainloop()