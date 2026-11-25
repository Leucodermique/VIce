"""VIce launcher visual components. Original procedural art, no game assets."""
import tkinter as tk
import random
import math
from functools import lru_cache
from PIL import Image, ImageDraw, ImageTk, ImageFilter

def mix(a,b,t):
    return tuple(round(x+(y-x)*t) for x,y in zip(a,b))

@lru_cache(maxsize=4)
def sunset(w,h):
    w,h=max(1,w),max(1,h)
    im=Image.new("RGB",(w,h))
    d=ImageDraw.Draw(im)
    stops=[(0,(13,20,55)),(.36,(73,37,115)),(.59,(218,77,133)),(.72,(70,32,87)),(1,(11,13,29))]
    for y in range(h):
        t=y/max(1,h-1)
        for (a,c1),(b,c2) in zip(stops,stops[1:]):
            if a<=t<=b:
                d.line((0,y,w,y),fill=mix(c1,c2,(t-a)/(b-a)));break
    # Soft sun halo and distant clouds.
    glow=Image.new("RGBA",im.size); g=ImageDraw.Draw(glow)
    cx,cy=int(w*.72),int(h*.47);r=int(h*.26)
    g.ellipse((cx-r,cy-r,cx+r,cy+r),fill=(255,150,107,145))
    glow=glow.filter(ImageFilter.GaussianBlur(max(4,r//3)))
    im=Image.alpha_composite(im.convert("RGBA"),glow)
    d=ImageDraw.Draw(im)
    r=int(h*.16)
    d.ellipse((cx-r,cy-r,cx+r,cy+r),fill=(255,174,139,255))
    rng=random.Random(41)
    for _ in range(30):
        x=rng.randrange(w); y=rng.randrange(max(1,int(h*.37)))
        d.line((x,y,x+rng.randint(12,95),y),fill=(109,66,138,150),width=1)
    horizon=int(h*.66)
    # Layered downtown silhouette with roof shapes, lit windows, neon edges.
    for depth,base,lo,hi,palette in [
        (0,horizon,12,45,[(66,40,95),(72,43,107),(54,39,90)]),
        (1,horizon+8,28,82,[(29,24,64),(41,28,78),(35,26,69)]),
        (2,horizon+18,35,105,[(16,22,48),(27,22,56),(18,25,54)])]:
        x=-12
        while x<w:
            bw=rng.randint(14+depth*4,37+depth*8)
            bh=int(rng.randint(lo,hi)*h/300)
            y=base-bh; col=rng.choice(palette)
            d.rectangle((x,y,x+bw,base),fill=col)
            if rng.random()<.35:
                d.rectangle((x+bw*.23,y-6,x+bw*.77,y),fill=col)
            if rng.random()<.23:
                d.line((x+bw*.5,y-15,x+bw*.5,y),fill=(117,92,173),width=1)
            for wx in range(x+4,x+bw-3,7):
                for wy in range(y+5,base-3,8):
                    if rng.random()<(.15+depth*.13):
                        d.rectangle((wx,wy,wx+2,wy+3),fill=rng.choice([(238,144,182),(119,154,238),(208,118,224),(255,192,142)]))
            if depth==2 and rng.random()<.35:
                d.line((x+1,y+2,x+bw-1,y+2),fill=rng.choice([(211,61,204),(36,167,223)]),width=2)
            x+=bw+rng.randint(3,10)
    # Waterfront reflections.
    water=Image.new("RGBA",im.size); wd=ImageDraw.Draw(water)
    for y in range(horizon+18,h):
        a=int(105*(1-(y-horizon-18)/max(1,h-horizon-18)))
        wd.line((0,y,w,y),fill=(17,13,40,180))
        if rng.random()<.85:
            x=rng.randrange(w); length=rng.randint(12,100)
            wd.line((x,y,min(w,x+length),y),fill=rng.choice([(246,71,164,a),(92,113,249,a),(58,206,237,a)]),width=1)
    im=Image.alpha_composite(im,water)
    # Detailed palm silhouettes: tapered trunks and individual leaflets.
    palms=Image.new("RGBA",im.size);p=ImageDraw.Draw(palms)
    for x,scale,lean in [(w*.06,1.02,1),(w*.17,.68,-1),(w*.94,1.10,-1),(w*.82,.55,1)]:
        top=h*(.24 if scale>.9 else .36)
        bottom=h*.88; tx=x+lean*h*.09
        pts=[(x-5*scale,bottom),(x+6*scale,bottom),(tx+2*scale,top),(tx-3*scale,top)]
        p.polygon(pts,fill=(10,15,34,255))
        for k in range(10):
            ang=-math.pi+ k*math.pi/9
            length=h*.28*scale*(.84+rng.random()*.35)
            dx=math.cos(ang)*length;dy=math.sin(ang)*length*.45
            spine=[]
            for j in range(22):
                t=j/21
                spine.append((tx+dx*t,top+dy*t+length*.25*t*t))
            p.line(spine,fill=(11,17,35,255),width=max(1,int(3*scale)))
            for j in range(3,21):
                t=j/21; xx,yy=spine[j]
                size=length*.18*math.sin(math.pi*t)**.6
                p.line((xx,yy,xx+dx*.07-size*.32,yy+size),fill=(11,17,35,255),width=max(1,int(2*scale)))
                p.line((xx,yy,xx+dx*.07+size*.32,yy+size*.68),fill=(11,17,35,255),width=max(1,int(2*scale)))
    im=Image.alpha_composite(im,palms)
    # Dark centre for type and gradual fade into the rest of the UI.
    overlay=Image.new("RGBA",im.size); od=ImageDraw.Draw(overlay)
    for y in range(h):
        a=int(220*max(0,(y/h-.54)/.46)**1.2)
        od.line((0,y,w,y),fill=(11,13,29,a))
    im=Image.alpha_composite(im,overlay)
    return im.convert("RGB")

def round_gradient(w,h,active=True,hover=False):
    s=2; ww,hh=w*s,h*s
    im=Image.new("RGBA",(ww,hh))
    d=ImageDraw.Draw(im)
    a,b=((77,139,243),(77,139,243)) if hover else ((49,108,216),(49,108,216))
    if not active:a=b=(49,56,68)
    mask=Image.new("L",(ww,hh)); md=ImageDraw.Draw(mask)
    md.rounded_rectangle((0,0,ww-1,hh-1),radius=10*s,fill=255)
    for x in range(ww):
        d.line((x,0,x,hh),fill=mix(a,b,x/max(1,ww-1))+(255,))
    im.putalpha(mask)
    d=ImageDraw.Draw(im)
    d.rounded_rectangle((1,1,ww-2,hh-2),radius=10*s,outline=(114,160,245,180),width=s)
    return im.resize((w,h),Image.Resampling.LANCZOS)

def rounded_button_image(fill, border, radius=9):
    scale = 3
    side = 32
    im = Image.new("RGBA", (side*scale, side*scale))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, side*scale-1, side*scale-1),
                        radius=radius*scale, fill=fill, outline=border, width=scale)
    return ImageTk.PhotoImage(im.resize((side, side), Image.Resampling.LANCZOS))

def style_buttons(style):
    images = [
        rounded_button_image("#20252e", "#353c48"),
        rounded_button_image("#303846", "#526174"),
        rounded_button_image("#181c23", "#262c36"),
        rounded_button_image("#15191f", "#262b33"),
        rounded_button_image("#25344a", "#79a9fc"),
    ]
    style.element_create("VIce.Button.border", "image", images[0],
                         ("disabled", images[3]), ("pressed", images[2]),
                         ("focus", images[4]), ("active", images[1]),
                         border=11, sticky="nsew")
    style.layout("TButton", [
        ("VIce.Button.border", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})
            ]})
        ]})
    ])
    return images

@lru_cache(maxsize=4)
def studio_background(w, h):
    im = Image.new("RGB", (w,h), "#11151c")
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h-1)
        d.line((0,y,w,y), fill=mix((22,30,43),(14,17,23),t))
    light = Image.new("RGBA", im.size)
    l = ImageDraw.Draw(light)
    r = max(w//3, 100)
    l.ellipse((w-r,h//3-r,w+r,h//3+r), fill=(65,105,179,85))
    light = light.filter(ImageFilter.GaussianBlur(max(15,r//3)))
    im = Image.alpha_composite(im.convert("RGBA"),light)
    d = ImageDraw.Draw(im)
    for k in range(6):
        x = w*.72+k*24
        d.line((x,0,x+h*.65,h),fill=(52,68,92,65),width=1)
    return im.convert("RGB")

def icon(c,kind,x,y,color):
    kw=dict(fill=color,width=2,capstyle="round",joinstyle="round")
    if kind=="chip":
        c.create_rectangle(x-8,y-8,x+8,y+8,outline=color,width=2)
        c.create_rectangle(x-3,y-3,x+3,y+3,outline=color,width=1)
        for n in (-5,0,5):
            c.create_line(x+n,y-8,x+n,y-12,**kw);c.create_line(x+n,y+8,x+n,y+12,**kw)
            c.create_line(x-8,y+n,x-12,y+n,**kw);c.create_line(x+8,y+n,x+12,y+n,**kw)
    elif kind=="clock":
        c.create_oval(x-10,y-10,x+10,y+10,outline=color,width=2)
        c.create_line(x,y-6,x,y,x+5,y+3,**kw)
    else:
        c.create_polygon(x,y-12,x+11,y-6,x+11,y+6,x,y+12,x-11,y+6,x-11,y-6,outline=color,fill="",width=2)
        c.create_line(x-11,y-6,x,y,x+11,y-6,**kw);c.create_line(x,y,x,y+12,**kw)

class Switch(tk.Frame):
    def __init__(self,parent,label,var,accent,bg):
        super().__init__(parent,bg=bg)
        self.var=var;self.accent=accent;self.bg=bg
        self.c=tk.Canvas(self,width=48,height=28,bg=bg,highlightthickness=0,takefocus=True,cursor="hand2")
        self.c.pack(side="left",padx=(0,9))
        self.label=tk.Label(self,text=label,bg=bg,fg="#eff0ff",font=("Segoe UI",11),cursor="hand2")
        self.label.pack(side="left")
        for widget in (self.c,self.label):
            widget.bind("<Button-1>",self.toggle)
        self.c.bind("<space>",self.toggle);self.c.bind("<Return>",self.toggle)
        self.c.bind("<FocusIn>",lambda _:self.draw());self.c.bind("<FocusOut>",lambda _:self.draw())
        self.trace=var.trace_add("write",lambda *_:self.draw())
        self.bind("<Destroy>",self.cleanup)
        self.draw()
    def cleanup(self,e):
        if e.widget is self:
            try:self.var.trace_remove("write",self.trace)
            except tk.TclError:pass
    def toggle(self,event=None):
        self.var.set(not self.var.get())
        return "break"
    def draw(self):
        c=self.c;c.delete("all");on=self.var.get()
        fill=self.accent if on else "#363e4a"
        c.create_oval(2,3,24,25,fill=fill,outline="")
        c.create_rectangle(13,3,35,25,fill=fill,outline="")
        c.create_oval(24,3,46,25,fill=fill,outline="")
        x=26 if on else 5
        c.create_oval(x,6,x+16,22,fill="#f7f8ff" if on else "#9da4be",outline="")
        if on:c.create_line(x+4,14,x+7,17,x+12,11,fill=fill,width=2,capstyle="round",joinstyle="round")
        if c.focus_get() is c:c.create_rectangle(0,1,47,27,outline="#d5d7ff",dash=(2,2))