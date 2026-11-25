"""Offline analysis of VIce text diagnostics; never starts or reads title files."""
import argparse
import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VIce"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAGES = ("compute", "mesh", "vertex", "pixel", "unknown")
FPS = re.compile(r"([\d.]+) fps, ([\d.]+) ms/frame .*?(\d+) draws/frame, dropped (\d+) without a host pipeline \((\d+) being made\) and (\d+) indirect")
DROP = re.compile(r"dropped ([\d.]+)/frame without a host pipeline: (.+)")
SHADER = re.compile(r'stage\s*=\s*"(\w+)".*?hash\s*=\s*([0-9a-f]+)')
FRAME = re.compile(r"\bframe\s*=\s*(\d+)")
LEVELS = re.compile(r"levels\s*=\s*\[([0-9, ]+)\]")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")

def parse(raw):
    lines = [ANSI.sub("", line) for line in raw.splitlines()]
    stages = defaultdict(lambda: {"translated":set(),"failed":set(),"refused":set()})
    failures = {}
    reasons = Counter()
    drops = {}
    transitions = []
    perf = []
    events = []
    evidence = Counter()
    surface = []
    for n,line in enumerate(lines,1):
        match = STAMP.search(line)
        stamp = match.group() if match else ""
        shader = SHADER.search(line)
        if shader and ("shader translated" in line or "shader does not translate" in line):
            stage,key = shader.groups()
            fail = "shader does not translate" in line
            stages[stage]["failed" if fail else "translated"].add(key)
            if fail:
                reason = re.sub(r"^.*?error\s*=\s*", "",line)
                reason = re.sub(r"^.*?GFX10 code at 0x[0-9a-f]+:\s*", "",reason)
                failures[key] = (stage,reason)
                reasons[(stage,reason)] += 1
        if "host pipeline refused" in line or "host mesh pipeline refused" in line or "host root signature refused" in line:
            keys = re.findall(r"\b(compute|mesh|vertex|pixel) ([0-9a-f]{8,})",line)
            for stage,key in keys:
                stages[stage]["refused"].add(key)
            evidence["pipeline refusal lines"] += 1
            if "wave_incompatible=true" in line:
                evidence["explicit wave incompatibility"] += 1
            if "wave_incompatible=false" in line:
                evidence["other pipeline refusals"] += 1
        for term,label in [("no free dynamic descriptor slots","descriptor exhaustion"),("host view heap capped","descriptor heap cap"),("no host texture","texture creation failures"),("D3D12:","D3D12 validation messages"),("device removed","possible device removal"),("no dynamic slots for its tables","draw/dispatch descriptor drops")]:
            if term in line.lower() if term != "D3D12:" else term in line:
                evidence[label] += 1
                events.append({"line":n,"time":stamp,"kind":label,"detail":line[:900]})
        for marker in ("surface recording summary", "surface recording detail", "intermediate sample statistics", "intermediate sample unsupported format", "intermediate sample unavailable", "hardware pipeline counters", "GPU counters unavailable"):
            if marker in line:
                surface.append({"line":n,"time":stamp,"kind":marker,"detail":line})
                break
        m = FPS.search(line)
        if m:
            fps,ms,drawn,dropped,making,indirect = m.groups()
            perf.append({"line":n,"time":stamp,"fps":float(fps),"ms":float(ms),"draws":int(drawn),"pipeline_drops":int(dropped),"pending":int(making),"indirect_drops":int(indirect)})
        m = DROP.search(line)
        if m:
            val,kind = m.groups()
            val=float(val)
            prior=drops.get(kind,{"peak":0,"observations":0})
            prior["peak"]=max(val,prior["peak"])
            prior["observations"]+=1
            drops[kind]=prior
        if "a black frame after a frame that was not" in line or "levels after the black frame" in line or "black frame recovered" in line:
            f=FRAME.search(line)
            lv=LEVELS.search(line)
            transitions.append({"line":n,"time":stamp,"frame":int(f.group(1)) if f else None,
                "kind":"after" if "levels after" in line else ("recovered" if "recovered" in line else "black"),
                "levels":[int(x.strip()) for x in lv.group(1).split(",")] if lv else [],
                "draws":int(re.search(r"\bdraws\s*=\s*(\d+)",line).group(1)) if re.search(r"\bdraws\s*=\s*(\d+)",line) else None})
    for t in transitions:
        before=[p for p in perf if p["line"]<t["line"]]
        after=[p for p in perf if p["line"]>t["line"]]
        t["before"]=before[-1] if before else None
        t["after"]=after[0] if after else None
    ranked=[]
    for label,d in sorted(drops.items(),key=lambda x:x[1]["peak"],reverse=True):
        match=re.search(r"\b(compute|mesh|vertex|pixel) ([0-9a-f]{8,})",label)
        cause=failures.get(match.group(2),("", "No matching translation failure in log"))[1] if match else "No shader identifier"
        ranked.append({"label":label,**d,"cause":cause})
    return {"lines":len(lines),"stages":{k:{a:len(b) for a,b in stages[k].items()} for k in STAGES},
            "reasons":[{"stage":k[0],"reason":k[1],"events":v} for k,v in reasons.most_common()],
            "evidence":dict(evidence),"transitions":transitions,"performance":perf,"ranked_drops":ranked,"events":events,"surface":surface}

def table(headers,rows):
    e=lambda s:html.escape(str(s))
    return "<table><thead><tr>"+"".join("<th>"+e(x)+"</th>" for x in headers)+"</tr></thead><tbody>"+"".join("<tr>"+"".join("<td>"+e(x)+"</td>" for x in row)+"</tr>" for row in rows)+"</tbody></table>"

def render(a,previous,source):
    page='''<!doctype html><html lang="fr"><meta charset="utf-8"><title>VIce - Diagnostic écran noir</title>
<style>body{background:#101319;color:#dfe7ef;font:15px Segoe UI,sans-serif;max-width:1220px;margin:30px auto;padding:0 20px}h1,h2{color:#86ebd3}table{border-collapse:collapse;width:100%;margin:12px 0 24px}td,th{border-bottom:1px solid #364352;padding:9px;text-align:left;overflow-wrap:anywhere}th{background:#202b38}p{line-height:1.6}a{color:#86ebd3}details{margin:12px 0}</style>
<h1>Diagnostic écran noir</h1><p>Analyse hors ligne des journaux. Les erreurs observées ne prouvent pas à elles seules la cause du rendu noir. Les compteurs de traduction sont des shaders distincts, les abandons sont des maxima observés par frame, pas des totaux.</p>'''
    page+="<p>Source : "+html.escape(str(source))+" — "+str(a["lines"])+" lignes.</p>"
    page+="<h2>1. Chronologie de l'écran noir</h2>"+table(["Frame","Heure UTC","Événement","Niveaux mesurés","Dessins","FPS avant / après"],[(t["frame"],t["time"],t["kind"],t["levels"],t["draws"]," / ".join(str(t[k]["fps"]) if t[k] else "?" for k in ("before","after"))) for t in a["transitions"]])
    page+="<h2>2. État des shaders</h2>"+table(["Étape","Traduits","Échecs de traduction","Refus par GPU","Échecs précédents"],[(k,v["translated"],v["failed"],v["refused"],previous["stages"][k]["failed"] if previous else "?") for k,v in a["stages"].items()])
    page+="<h2>3. Commandes abandonnées : priorité par fréquence</h2><p>Maximum relevé dans les échantillons du moteur, non exhaustifs. Les compilations en attente ne sont pas nécessairement des échecs permanents.</p>"+table(["Identifiant","Pic / frame","Échantillons","Motif associé"],[(d["label"],d["peak"],d["observations"],d["cause"]) for d in a["ranked_drops"][:50]])
    page+="<h2>4. Fonctions manquantes</h2>"+table(["Étape","Motif exact","Occurrences"],[(r["stage"],r["reason"],r["events"]) for r in a["reasons"]])
    page+="<h2>5. Autres indices matériels et D3D12</h2>"+table(["Observation","Occurrences"],sorted(a["evidence"].items()))
    page+="<p>Absence de diagnostic wave64 explicite : aucune conclusion possible sur la taille de wave des pipelines refusés, sauf si elle est consignée. Les niveaux de luminance ne distinguent pas géométrie absente, profondeur, éclairage et composition. Ces étapes nécessitent des tests indépendants.</p>" if not a["evidence"].get("explicit wave incompatibility") else "<p>Incompatibilités wave explicitement détectées : voir le journal original.</p>"
    page+="<h2>6. Surfaces et sondes GPU</h2><p>Les compteurs décrivent l'ordre d'enregistrement, pas nécessairement l'ordre d'exécution. Une sonde 8 x 8 ne garantit pas que toute la surface est noire. Les formats non pris en charge restent inconnus. Activer Surface statistics dans le launcher pour les surfaces intermédiaires.</p>"
    if a["surface"]:
        page+=table(["Ligne","Heure UTC","Type","Mesure"],[(x["line"],x["time"],x["kind"],x["detail"]) for x in a["surface"][:1000]])
    else:
        page+="<p>Aucun relevé de surfaces dans ce journal.</p>"
    page+="<details><summary>Échantillons de performances</summary>"+table(["Heure UTC","FPS","ms/frame","Dessins/frame","Abandons pipeline/frame","En compilation","Abandons indirects/frame"],[(p["time"],p["fps"],p["ms"],p["draws"],p["pipeline_drops"],p["pending"],p["indirect_drops"]) for p in a["performance"]])+"</details>"
    page+="<details><summary>Messages matériels détaillés</summary>"+table(["Ligne","Heure UTC","Type","Message"],[(p["line"],p["time"],p["kind"],p["detail"]) for p in a["events"][:300]])+"</details><p><a href='report.json'>Données structurées JSON</a></p></html>"
    return page

def self_test():
    a=parse('WARN stage="mesh" hash=12345678 shader does not translate error=converter shader.dxil v1: GFX10 code at 0x12: example is not supported\nWARN host pipeline refused shaders=[compute aaaaaaaa] wave_incompatible=true\nINFO dropped 7.0/frame without a host pipeline: mesh 12345678\nINFO dropped 2.0/frame without a host pipeline: mesh 12345678\nWARN a black frame after a frame that was not frame=20 draws=10 levels=[44, 0]\nINFO 0.5 fps, 2000.0 ms/frame (x), 10 draws/frame, dropped 8 without a host pipeline (2 being made) and 3 indirect')
    assert a["stages"]["mesh"]["failed"]==1
    assert a["stages"]["compute"]["refused"]==1
    assert a["ranked_drops"][0]["peak"]==7
    assert a["ranked_drops"][0]["cause"]=="example is not supported"
    assert a["transitions"][0]["after"]["fps"]==0.5
    assert a["evidence"]["explicit wave incompatibility"]==1
    print("Diagnostic parser self-test passed")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--log",type=Path,default=DATA/"launcher"/"play.log")
    args=p.parse_args()
    if args.self_test:
        self_test()
        return
    a=parse(args.log.read_text(encoding="utf-8",errors="replace"))
    previous=args.log.with_name("play.previous.log")
    b=parse(previous.read_text(encoding="utf-8",errors="replace")) if previous.is_file() and previous!=args.log else None
    out=DATA/"black-screen"/datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out.mkdir(parents=True)
    (out/"report.json").write_text(json.dumps(a,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"index.html").write_text(render(a,b,args.log),encoding="utf-8")
    (out.parent/"latest.txt").write_text(str(out/"index.html"),encoding="utf-8")
    print("REPORT:",out/"index.html")
    print("STAGES:",json.dumps(a["stages"]))
    print("EVIDENCE:",json.dumps(a["evidence"]))
    print("BLACK TRANSITIONS:",[(t["frame"],t["draws"]) for t in a["transitions"] if t["kind"]=="black"])
    print("TOP DROPS:",json.dumps(a["ranked_drops"][:5],ensure_ascii=False))

if __name__=="__main__":
    main()