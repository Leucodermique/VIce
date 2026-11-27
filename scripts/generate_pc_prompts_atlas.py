#!/usr/bin/env python3
"""Generate the 18-tile PC prompt atlas (requires Pillow).

R8G8_UNORM: R is coverage, G is straight ink luminance; no header.
Tiles 0..9 are the losslessly embedded original art, never re-rendered.
Tiles 10..17 are rendered at 8x resolution then downsampled with Lanczos.
Run from any directory; --output and --preview override output locations.
Use --font to pin a bold TrueType font for reproducible builds across hosts.
The default uses Windows Arial Bold. No shader files are modified.
"""
import argparse
import base64
import os
from pathlib import Path
import zlib

from PIL import Image, ImageDraw, ImageFont

TILE = 128
SCALE = 8
LABELS = ('RMB', 'LMB', 'TAB', 'G', 'MAJ', 'R', 'SPACE', 'F', 'E', 'C',
          'CTRL', 'V', 'T', 'ALT', 'B', 'H', 'ZQSD', 'ECHAP')
# zlib + base85 original 1280x128 R8G8_UNORM atlas, row-major.
ORIGINAL = (
    b'c-rk<2fQ3r)xWd*UJ6M_fKWn+K!6ZR2tvSsfIxr%N>OP6X;F&uDMcVbq$r5wrI7{#LO_}bu^^(-`92$15CkQZfJiYw2rZ<%-MQa6'
    b'_ueVHGrMng-|p<p?El=~ChzT=ot<;fE&qF}5Fii;1OkCTAP@)y0)apv5C{YUfj}S-2m}IwKp+qZ1OkCTAP@)y0)apv5C{YUfj}S-'
    b'2m}IwKp+qZ1OkCTAP@)y0)apv5C{YUfk2@2k|lB?FFGo;JT(jagFqk<2m}IUfHdlfif#DX9QX%;Kp+qZ1QO}gc=@r+y5f`K5OJdT'
    b'mbj#>T_jExhl_DygOX2L;2#76fj}S-NTf;Y)nR_xL>wc2Af6EK2-!n>Pdq6Wij%|^!qJ80z&|Ll*8H5Z+PALda7KXqL6ATA2mgth'
    b'8+oJgI$7K&KJX!&g6OW$3c_};S}7hCUl+qti-Fpm|LmV^@`-vHj1b5l1o?x1@ZWmUeY3gf5^wlWPtk{ODm9y~VkD%0TSi*5?bW?H'
    b'Qnfy`JO5cG4~_Q`o02_$A8Pxypz*hkqcPUscjAC92=WI({@@?{w^A!A#QI`}c;6r&pI*z7Inyj^{&(}TL$#cK-&Tl;e_AbmC^mEN'
    b'n+~f$1^$^&&1K%<)9nGv?H7@5{<FRIYZ@cY5x*2m#Ht?ge6!mc@v8W(__o-!><K}TKM3*%|KLCV88TVCrk<|bk9=~GxneA6P@u*@'
    b'%f-10+jgX6g$Ut4Pc16{IS&P=jen70Zyzn&FNz0T{Vca)3!7h`{0AC!eUvo-S|G?D1o?x1@E<QRJ{c~49>jhQuG?7dDEF5~%ai0;'
    b'a-6|MLQa$q$$R8N`CWOe+*z*WUY+w}pvT0viAO-8{O4Tc*GKLkkB}G18|2T3;}aB*jPtgZXUfUcD+dzyBj_sUb5`2;7a8{WVLC|s'
    b'(abis(X07qwYIUpK*1er&xm881%mt`f5;#FgZ~zwpNUHI6~KP|3HKc*za?kN`SNONbLEZlV1;oy=-*SMpZ+cHk|)XyT}_yaf!-6x'
    b'Dc-CEqrWo#9qczm9wmPyUnt@83VOdo@i(3j;as_Z`elKfEiWeScaei!yKrjw&$iuXfVj?Y&$pD8I@fCbznVen6j3y%(x0jvLO=kJ'
    b'KM3*%|KLA9?f4l68@tcO|869|ATK4{w?JMcXUHzquB6}hck$nevLIK<ZfXT-8+7yzA;j@U4`}Ir=YKCtsD&ehf6HLM?Frkx?xMbe'
    b';&T=Kf02v-#>siqBf4j%69VMEx?E0@oBDdO<@{H#U6Pd6jEH!~^`pe!%-lC)((_eVr-iN4_Z(^s;Ia3z7~cd<G2{>VL;me3fAEj_'
    b'9|iqfW~|``{qVo*6N;Hmdac<8+srVni{3lH#eXLeF6wp>QjxGx!Tfx;vJwkN3&_2HeF70+2>%+jjUp7g+QofEmq+II5*PpNLU^ys'
    b'?VIVUuVyJ-;K^hUc6+mR{O3~_f897ODNA-+@uu>%^+`4-_aY7MgYtR#Zp+#-`I3B$M#(|4pSLRU3a~=#-Ov>P@`wB(|MrwW_{aQ@'
    b'f{<sH+CBNTpPWWGZ$=67bMRle{;yk>l1=Z}3Moxjw$KhI-bIQ#5@!Vs@Sj&tJXL;Z(4Jk!U3WnL*HzX-(^ao6ARKt8?CWU(<LBS`'
    b'CW~UtCyK8GDwiawolezNM!Dm@5q-pdAEQ+kpKedG7I|y_T_%YZ%d?f=Y~IlnuMm4R$O-`YL;jF|yUHK@xBL8$hJMad*u;na)|20m'
    b'*O0$oIj+%dn(_b3D-GtU#KD>XM$%d{e)n0qYbgSplc-3D0RQ}?Tgu;6z<x}u6Z}41)u|*lo_b?i03&OhJcD|1s?q}H;^*JdGwvy7'
    b'i+>~_+C56^7SD-m#ev3SH`h!4b7F{i!JGXo*&XE{jGmfNe39hax>oe*m7kIOIJ2anaz}2~0RAC=$RF}=U-^UocIH2-esof~?r&St'
    b'jm{%X6Nde!6DpiXF(02WHe!pcq_-HIUl_4waFzVBXLV-;bbewGV1R#?z~@LeTM7Hw>fK+E*3ZcRxPd&Ld?ROtB0$E<bEwBYRf+)P'
    b';NOXN|36=>dw0T{7n%Pg#lz=&*-QNSMWwU<D7ivOwwS2%Us27HBAvO+3cyeNC(+p}9SGzP`9uEgD}V6c?)*p7k8V%Z$_jq;w=1EU'
    b'*%dT@+-94|4vvc*Vo=TD@*{<%WvCXA=|aVu<wV2omxw600RP<kU-mt95d9UYonrVO?DD@o;k-%+u#52D0<xwHe>8Nn`Om97mk$c('
    b'8%?x(PGvGoYRl<}_-^Bc`DdjCuh(0Wd*HZI&Hph~B-?5#s5R*v{|aznFUAPu5BWp>?JIxq-|qa2NO2E+$ui(Xrz(81r}FumUIF{D'
    b'=igrH%F`$Z-qY0nS58s3>mw9`l@(AE<J%byZ@d=0Ni{ZdEv|_X;GduFEX7B$qLj3Z^73?-|MMw7j-cFtb1U{5djtM}w2B4+Hk*GP'
    b'&oWtjq&O?qp2516E$@$oDvGIf?f@3G0hG<hMD#H4`&E@}>q*a&ll0T_KDdIiQPxv82J(meA^-N3Kllg#Q4w;NhmaYYj3wLd^eQ-J'
    b'4tXl_DRQ=4DVNIEsJ%%)-7W{vXYZ|ot!?rUEJL?<wp;^P#y^jdqbu5drNOsPDjv%DX9PHzuyJLrDaTCj<siUj@^86($?|yjK5}!y'
    b'_xmQa?Jc)b{U|(7Sf0B-GPAjMug)_a*>WDr4M<XUf@i-IJHJwn)_FJi$*-zoj6nX7Kjh!O@(2ImKPvher?QgRPL7fhWW$_U5&dZl'
    b'!*<NiQe^Cl@@4r7wU;S|dL?PYEUNxdO}DDi?cx47=fhfx0L%E#E6ewqidJmLm+=C+?vMcgrxO1=m*jy7V(00E54ZCpz~=B@7R{`4'
    b'c=wTq$y+Jje08G7e?h(~7g5fkfllsh+eA#)S{<ABi$0V|$@Wfv*Oz1|N^TEGdA}YbHnk5(ODt;K`yhYFAM$Ts`GbG(A2mAc|C}=-'
    b'&$j;Z+oa#Cj9?hU%%j*4A+MFoX)XD;X-nkW@|X1Pm?}{q(&%2_qZ6oFk=O?MqUhhkT!3Z#=O`cJ%RP7p@aci$p05k=e-FJojIi^J'
    b's=YUhqK4PiSi4#L^Xh+r7tO3=RF0tNBEJ-p+8kP9JS=lgyv#K<dV({mKE#o2PSxn^u8QoFwF2y|*DO9Ge7#1_A%Dmp@^4@HgMaWJ'
    b'wTBKX_53@Iw0qNfKrnMjM=z+t+dDKz9sKjM{4eD(<ZQ{+@t@}HcTToz=I+dv#Q~S`&nJZ2dpsfV3N?YQygQWtp;i0N@t!=xS(Tf?'
    b'KXbL8clOVz+I>GXeP2lY46uFK!+%cw>DvKLu1QzQeR{J~dzn2BW~Sow=VTnZTaQTAOg~&x768Z}@`wD}SN`B1{727?^H)=oLd~+#'
    b'Az6&W@ZTu9_Q#YT#gBiM%10E&?g{^C8JwoMV-47AVqhJ<9S!H7(ceK4MhN3SwtwFl%K!R3;y-OcAFq47@%&pV-_h%i)|+*@1LVUA'
    b'jCb$Z4`PkVh_sQT2``W@`{t2-VYxp$#S-N|uCR6|S?gs=`he%p2b6zPa4e)0AJ$+2fcznU$iIE%5B|Y_^mF0_-9Jur*!g7pogNYY'
    b'1@X_L5%C{u`InImRC#wW_L|%h4PXiXZ2Rq$&qeSKb@1O$Livw^|JVk68O2LCg@5L1hU4?9D_lP$rCLkVM|uYstbBJBM~fnB0C<<!'
    b'O7*U-&o*OB*2c8#e$r%DSL1W<$b3WokU!+#zVZkE;J=p4h_(|Q!!^tn^r-!71pf2a&Q<!&Cz=}J9EpEj!!Al*px)!Z8LFn9uASUq'
    b'{`oGOP|SGIi)WdY+ST9L0Twy&Ld@095obb)_XkV&$i!xSs$>B;S3fzA&od&P5FmfZAM$Ts`GbG(9~~)dEI!uwSnI!{X#X06|JVm*'
    b'8O2%%9p5FzPO(P;m+;S;xK8=6NU`CKz<;a({I1G#&|v;~j9liN{&~&Zy=pF-bj$=v8QWc7*jpm@6!*kwPEYXsKQA@j9}d^^z4?Dd'
    b'#Q6{TL;jF|`^q2uga7FLbE=mchmqU72-?5K;6IJTPxbiwUhEd&68?FmoEXtNpb_}5#0Ri({OgGKC5}y-rTS2+PCb>*3X2JyTiO@m'
    b'12Hs`F9i4I#s2yKoz%_$o4xsQW2Er``9uDYfBVWG{Dc2m^rOG<TYtY4$@<$k{AbJGJ24Pa{4MqfkP`kG|J`A-6UK%A=F0zZvcNQo'
    b'f97d-|LVU@O5)vn?(zMdXecR;i8L4Zic9?S|B}?r|ApTCUl@sJ$RF~D{M%Rl;2-=)kB%8A-g2y;LiQtj<g5tR-$vm-TcNEjd7ZLV'
    b'bpPf}igAv}|6t-qMp`Qyga33-4s{}Y8pA)2kZ=3P{Dr9+^VNJIu5;#sEw0D<5BWp>kU!)P{=t9Unxfqt-#;hw%k+q-r4jf~b9lNF'
    b'10u!Iv08vj_;)hDM2ih?6#nZXed}&c3`k@6XRdzgw`t#(dh3tV<4@~c76nxiK1wa9cGdqt{*XW9kNqF~1OHX;ov-@6|2|9pXkD@6'
    b'zdJm?-f6MozY_j=bX*zH`rAnSH=k_ot_NW6`PVDrQ(nfSynG=w8IO9(hV-774QVOW^cGnEA%Dmp@`wDvKlrZ;|19vbL<!Qw&5IfT'
    b'@w)V!!k+3>cW<l_z$N_iGdvvmq}(X{H=Uxbr|2pmjp3hH!~x<>&!)YOd^e&jlE3~-8d=6yV71sRf~`D;`Onef-P8)cJyusQ(C^yu'
    b'@Bfg0Q{>-s)v`wC7K8kgIREV$YuVWu!$0-b^2;v%3*Q3@f%g)I|9JM?@2`CJEqPVU_Fukch1`(wANwo+(|3lAC;N-xU*`sYId=Q+'
    b'bgD;lg&fp4{`s@(ishMK|D_n+di+<27%Nn7)rC6uf5^Xu=YLzR`o&h_Kgb{YAMhU&|HTORv+MM~Xe$2GDB~ZVrC<ozKW9e9Eser|'
    b'eAO<DK6zHZB?iTY{{sAPB3DITDH|F8F<XvsR<+*q&!1gKyxRur|JJQt{|oLMeOlf^I^3h=UUEl@wHz(Cm%Gx~m_+a0Apf9xKj!@('
    b'@^6CtZTS*eDk`_dH2dFDD~Pi~6k3J<ApfS#e}0O$s*ZzTTahXJ)6+aCZ?9w+|2f)CTRuyBz<KhEau2x!?Eu@zvGUXMb5!59OWxay'
    b'9%wrL>m&Z{;h$lV(O={6pGHy#I{0q|Wn+#L|GE6@+Ay*u_-~%Ks`Z|Ktp730f5)cts62z7NLlYIw)H7%#dPw)s>e2n{XZG<=kH!E'
    b'H#J@&;VmcoQ2u~TDcABTB(s-F#tHCWR^~r{)>LGO)Ofr+T6g?S((q4|dEjpxee|hbjJ_n}KS!tcl)Qj2U$y>Ni}V50>dL>~bo}R9'
    b'3i$cABuqCwcKpXYp2$2#*XlQce;%p(*5D`IIQ+*Ez`N9D)xi4SQv9dq{q3}}dFxyUwY;iO$}Ml+>2R^(=j+-7u>U6w|5*|D{r+_N'
    b'zu#Rcfd8_F|MC&Xft6AGXGKQ8*~EW(RzE=+zYgCXkPq1dG%_3@KdPf2nvVb6m7=xy?<5cZZ5=EAbNNpU{@WV(FWL4Vmi(XOo@RCD'
    b'`EB*E1N5b<jsX740RFRRov^C5oY@Do>?f&wqDA1pjNv~^o$R7C9xvI$e?89w-ME*si2qocAIdY$Jnv}_U?jMy@&W+<1O5a4OYi!x'
    b'ZMaveJLMzVK=k(l|7G3&TaNnkYE9W*zTOK22>HYQ3;S=<@E?Ev@8m6}XL+<SFdX;~_z(Cm)$6~mR&#vCd2hM8f2-s?m~8xK8_t~8'
    b'Qx7;-MZMCF+_%-QP9pxxsZ&7S$UHbAI@SKSx^6!};6LC$;J;+yKmPP@<l0roK<WAq{0ID(T>Pgq7e#4yg$yWOD^$&t65a}t5kjlu'
    b'Ke?w4RlB+sSmiqcE28Ckf22$QXV>_C&sW-QqW=p!6)*zqs(Z`<|D^-}S;pHp*!s}xvCQMYd<E{=^JVu;;Xjt+uhp#1hhY5<-#%a&'
    b'PY?_E5BLxIzaD0lo%r>iKXt6x(ZhHBesW`)6I;uXas&F=yP_XmlMei6_ww(J_(WRpANTD@IZp1ZUfYq(!}UKo%B%2EotdG5|I)7i'
    b'<Mv?N>i@WHlOg=aQv55$^9K3PmEggAiH4w){tEmD{FglZS5$GVC&|h5s^0jDzm)Y4|9qqSY5sR%#U8wX{(5rRJ-<R3ljc!>eHbZD'
    b'vbF0!&-}|n(4SrhHkD^nmDN{hpJz+o8u`9_m10D0r2J3Yo9m8FApYa$nNK~s9ldnhmR=8pWGiLa{8QyN9Br=T&)gcq6MxU!awqz$'
    b'zH%FSJytdUX+h;s(AV?#pI)J-l7|2CYW4GIx%@q4ekr&8=ugyJffx8M`S_3JyDhEZb7?LG+X7nQUaPzR(}4f@r;{r1&)nZ7vy%Uh'
    b'EkMiV<{`R2{rd>o;jX9E?w^$H>p5Eg7s>C)iIfwH|MrQD;=dsNd91DQ-(8{nrv?9U-=7xwBzH&2F2)*-m8x=!*M?F4+i*?CuqyW*'
    b'A<rkw&{N;Ha{Xtn|6t<T%IE!hRKf3qD&ypFc@C`s6BCI4`0n2hNaMJWC*><I4O}@pq%ZmR8_KRbscN6|cm2_K^~4n6zuOer_mB8H'
    b'193q-W-hFA*8%=ZKK|ozaHwpL`WKbv!98ukKd(wBgy??r3h^wgunW8)uciEegEE-^h3Ddr0{G{CdUteN;J^LpGq;5Dzd?%d-#2M='
    b'^{eQ=;`ikEY01vaKix<7@8k1zsch@m&iKOUdvWVSx-`#UVkGH+s`q~D)_?8!w`1Vk$G`1C($_s%F!f(`bL#(U`nOsk?F8b#@2I@R'
    b'Ue2nV94g-nT+jTBF<y?k@n`0T^d(DhU;108FE(NW(uY>&o=3~$=1Tt5p((<DxBKqKI?mPf|LJ8rL`U7)U*NyA;J*Xu{Z%#jO=k=L'
    b'-9q<VS<$~c8~Bg;c%%aQ`>L^F70f<RRO^4itpiz*Kg$^YW7P6#T3^0Gdd=ru{PR5h_Fa1Svo)-D>BN8h)L$b`PYLjUDZRTL>Ak0?'
    b'1pl2wS9SVwHkHRQd6}FR1@Pao>Mwpx$K(q0Yjmj3NZx!)r?R2Go6z`A-z+S9_5*3vlS%_r9Xlp7|C^)L>nP*uE*0loy7K)cB&K;@'
    b'pf7p$yigV&6L!ZS{?pIO*B+8m{HJ*-DqUu-jQ@#s=70hIO9%ctsBRCxY~a7!n^aXeN&NHaez$-QfbTiYJgZ%?9(ZEwzx4wD%_r<9'
    b'<UoprUO}EuFH?JkG@ySWPe&oItD%?EiT?^J3S3Yecsc*WRJ7*;>Y;Stzi&jg|K8<W<@kQ@P{n9}CitAPe243LJp7lJpOFWU?R*#Y'
    b'X@8vLjH;uimHNLm^!!%IuCD6^eiU)tFUIHpT{r%-Tnt|xtHRe!3DIf=@gM(og?nXR_m0)7pJWOD*~;FtR>`d3>ka_^1O5a4OELeB'
    b'_AeLE0Zi>d1K_U}mmU1a7T~MYY}}CieO{rO*l)@I$h(zK;21(g(KBA96aVSj&l8o;o1gzJ)jM4^R8npw{<D>B@^PwT!|hM>TBJsT'
    b'#yVTm@6O-&C;xr;v~!c21$m6}1GK8>dNlsC)QRDJ^WIXWE6uqPQwLUEMO(H0^Ar8HZ01|a0{T>wR{i+#AM-X=<+S2Y3|8Klb_xC+'
    b'WmdZ}CbBsk|7kn1zA{fp&cT%f<$o)V!K~rGx7>b&{TKKT_%A#7Z&Hc1OLM+k70q7)_-`uyn=LPso0#~=PgC1U{k%EFzSM%F(u@CW'
    b'LNGf8_+OV|UFX!$kIBma^1A8?jmmb85dV@_`kiwr%ahK3R7uz4;lBYC4_9<Ez8vkMg`P5nwrc&?^{K6Z_Unu$t$D4$f86^AD;a4F'
    b'|05rUXIcKvmEJL?AHwioK~=_ct{hCeLe9OmvPB?E_>b3wXUlRy^PIRiMe!fD@n0%75i{2F9l(FUf34!5`F@vnc2SnsIY%p3Gj#w-'
    b'#(!D^I89Da=Uv@~P&Db%8f_hE#eaHKd_=LU8~Uog?@s=Yb0XV*lYswh<*CYT4ZRlo@ZTqmKdaO167=^HX}~OV<zJ{4q7xlEC{)+u'
    b'$A53QD@ix;E=)BwtA`lb)~x^htt%;dzclk3*YthS)B?^5{`;al+{N>s^GU+-5$II&`x3JThT%Ww$_|6(SDKxIf7_?x+{zOETSKoI'
    b'WtMXO#6zjk{}n>2+&7wvRlX7UFI({+g-r0zT>L7;uXn#DTY%!le_JR#*HuHCM(u|b2g{Lred*uhRe!9GTv4LijGGe^f&ccQC#mh('
    b'*Qgc$BI3NSid`H>dXDK-AGVf0yps0Oaf#6XY43eM{;TFUc%^GO+>oxi*Qdt|)AjiA-|GrF*%UFS^N(~S*#D3F<jJzwhw!sP&!eS~'
    b'X#DqiIm>9P{1{#1-%U;L?IUkC{0H$LU-_}J2jtwI{<6`VT69);u>zjR3jVXzTJSb`e^@2i(JZovtV&7#XTE9$*>AWlR^Q%u9iYDB'
    b'3+7vv*O;3;eeu8Tfd77x@%%6RiK!vh-<x|C-H|l>H;S+v=WMK{&7&;0pLN&#)9U2&)I{LF1L}Nw4+-r?zJPOU?!kpBt|(3TkMY}B'
    b'S6jsS-Ih__5XX8Lrt9(Jzqj0X-=Vd{Ql|&b0+_`3U%vL{kl2SSn>_R5$A9|}Ld(0lz3U8rjKm&r=l5L7_o@FL#DDAs^GmZXvP}Q#'
    b'J2N{`a@`rjf49*%U+uGFzD-^rrCI_W*$Yxq|BdJD-EuQE3V0@OBmcMIIzYbLvt;k+P^&?n&W`gVeh2dZB{%=`>GYI0(5tlFM6Y`@'
    b'!9S1B1509mEtUD~<)-P#<HmoMie>#2X`*(isg0B4sp^%J^?pmj0w?rXOQN*#^;<R$|MY_YVe}*?*75XXUH*qsZ`9L+3Ol6+|MBkp'
    b'gt^8${T*#Az~Q<c5C5$t|3y#q4#l0^t*T?>jMwDjiOm1U*IpQ6(R-jQ7P?5f^GgU#T~eWahhD#|d9{c${)45h^Bb^CCzxmDYff<`'
    b'`#PQWD&fC^I|le}+be&84}Eb#J5@mUX~utxeAoP!`etMq`r~ihh4g~OMB@M8>|bKEwayN=0qN)LhSu=oY)E6v{HXpA$%>X#{HOQ5'
    b'OUTEwe9rS~aCz0-m)WoXwr#I;$jRVeukTw2Q12);YVAzmzXY_F;=j!B9|Qj5QF)eYD;!EI9A9;Z&oUU)^?3PTEQ$<RhgyHvKDHLc'
    b'9{eMr^*_1;Io~by)Qf%3Ypd(l?NP$V*A;sb7ycVVzs+%_+K*z|^{)Jp%4pPK#>2b{_|G5t%743nerNms@va3pM|SkEeLk)DkDqrt'
    b'@&njMy*_EO8I|k)HY7h;nF{>J-*uCDYA5HXpqv-FI(K6l_rvpM)>}r0VnvimTI>J*J6`#>1pnnK)(qG^BlvGQ)p;t<>a<^zwSRHr'
    b'Kg-STUt6<$Yhp)T%}NCRtM}=1C3!*W?!lDcKker=!j1PmMryxzXZH`+^@Qbr&MQmP+LT*vc?56s)~x@GL=FjP{s+o8eSM?x-zZ;&'
    b'lVIZtD`zAc|BaQL3#h+~;N~dLKgXua?_5E@T~z`9@&7+G@AJ3*N$nIiX~?`&!PHRx(~AE@NH+6a$=K$+08jO5kBA%pEiCbI;&thv'
    b'hVF6v6wj%rvV0oS^y9zqorCKe4Wg`I*~fpC=0i_89n!&nQLT*q8kW^5YxuACEh+df1N=7~|FJgUSH5VEA1hBWo(G@vd4HAbdi?mW'
    b')3grv>h-9W%G-&w|9W|D{P=Gj@^>u9e<Q1|iXF;JCdv43NA=!;E_T_&;QB8a>u)F3HJ9W87>@tApZ0b!(3Z+8jz`KelOtTeyC!rz'
    b'r5FF@RA1#4*H%F0Ur<prg~a-Q_EhDjH<8PgXVY`OY~xwMf8oti86klGvVi}dH+gaW>-mXI&j1GdFYLd`!+%aDOr7mzsoLpR)9bn_'
    b'x}MP$)(me(ZB;QswAJ`;LID2_RxvEu!+*9L-^+OKIhFNlJ@fQ?s(Z{2#eaH*>~y0?&h>rEk>)-Ajz>e#M0WIl{+(}6;}u&>aQsh{'
    b'Du*Me#~a%=56lbTKj6Ql<G)g^wZ7MJ8Nz>-^1(lda=9E3bK9T1zcy}M{6o9*-(>t}t0>38u9fg;-`|!S3vf=Mxl&z^7yr?hGUQw@'
    b'ua!R_?6J3-Pp^;iK>N5#M%dQbe-93c{CXwe|F@;GhlhDZq)Gqh)b!)${T_YlHrM()SN%O4|M9<<`*7Yp^qp_h>n+m%UE@0yoEFjp'
    b'*}{K3mba1*yV;;+%JE;bTUPL&71FeCGw@#)@ZXER$S8-mgEM6R)%Ct^pj-u4$K2+V*YR#GkN?dK|4qh!JR)y&bwr#qav$XZ&us$b'
    b'c5&h(%5^>d`fnfm?stpIn`jxo^J}Zj&eq|-NoDIjKkc6b_-}ZR`&{zzA9H(*LVqmZUFo-@3>NIC{MakuKh7b(y>C}o$Jd7VdTk?$'
    b'2rhpX{?6a~@ZVu6!hhOhBwtb!p8IQCrTyzpc>bR#_^%n>F72ZK)4ne4i~n+F4aoxjd&Aw^Jl@7inY{d8vF3knL~C&V<96r2$@nj5'
    b'53PiDN_9Q{{C}^i%-K$M)RSFAk*j*5YF+&AIU)J~ZYzs@Es!pA3(^5@q1s6LX+XeUkbL~dIsATMs<E)7UQpJ$P7~|4x&r=ViTyFc'
    b'p1@<~=YhP5{ONmqI>KX9ss9Ul|6S7n1PJ|KI`CfyopJ^T8o##@<xI*p{^NPuMZUpL$!E&iv-Db$f&70uEBdgKS*|r1OEZ)IrJnk4'
    b'jcE<8f7b5&Hx>VBO?V%-9!5^Zym~F~=6K<3iXZttA6EZOs<@FGeRjjKiOm19AO!yfV|fd{od2unw=b4_DlPthPQDi{#|u@|8$bK?'
    b'^tY82@E^av?s99X$VY!O@%`%mqYS#UOY%lv;k)a(6?!RY_%EmW=OKB&yo+?EqNGMl&Q!&c@z=lVb^e$5@!zGX#(z3n<~x-C@$HcI'
    b'POm&b(}Mr*&UpT7=6mNdtq1@7OJ(^0{saDN5B$gD@%z4=KDw?a9RIOKX{HbV?Of$aTW9}$v`p_)IhNJmlSiZw|LONmcb|Nyk@5hQ'
    b'?`BOX{?n)9;XRB8FLLH>x3XNf!OT+Yzs+55ObPzu5&kpxiklVYD@A~vlqFrpz5n+L|NYR^d5IJMSyTfu?fCEDHm?6sS_dcCvQq!;'
    b'xDYMCv6-s>3;YNCR~`Rx2G<>Y=b#-Zzvp=JUes+o{q_m7&wIKaJAnUQQxU^OV>jK=-TN(hc;f88wyJG6qzv8g^WInXto}T=jEd5U'
    b'|AKgz<#LG8Z-(JN&t|N@Z-hLbcF6PG#y@ZD^Y2@z?)8fAIp+FugXgI_OgLsrBL2ITI9a8<8MKzTTtBcS`8yWk^?#~+{<GwNu40?='
    b'F*epOUzK|NcaWL$VXYnT-|bC`giR9un;Y-~w<x-MiOFgX{0ID(b^PZ<YUbSPP%8rWc+~gz!M>b+{`img-K+m$YDYV>^XkOKf9U6*'
    b'X!2T@N49lW=+{DO^?$r$t<m)ueVFYQwZDbqzk<r~V!7{L9P&+5%-{H4`>S`3A&<fv<%5JcCc7hp^=F@|xSl2v|2g}ftq|amWeBi7'
    b'nYRl6vE)~%(^$N1I9X2ZNY9sK{MQ^k5b$4e>i_;Dkkf%VKf>5Wfd7F1vX1|FG@j}1|CZ8zuPLeSR3sm<5oUGZ``qMOfW7>GdHETY'
    b'_hvuR9_D@Ta;>at)ka&j{`1w>2mGS=d1n|4c9!s;es^k#jeiJvF-arIa{R|@-%^wJN539d{*QiN$Zq*NAD-;1@;puojg{1%M1|Dh'
    b'KdHw4Qp%N>3)KI8v=`VQPW<QOb~=Z=*(R8X{^mCRo>SFmi28KEf53mO;h%T69m});n)ho>rK9RQYXSHV_%GG?PuG3h#E0&F5{TvG'
    b'-%c^QAim-NcU`QM|2gkwn-lWhs;g+L)_-jQUbn=*Ut22k%Kxi7L|>I={KwD!H<w$+`A3>}!|)%+gk0zI;2aguiRwLLq|d{zi{3k0'
    b'+5WuTDLMn>;Z^+Ql8yiL|JRji0Qu_C)JOH&faAq~&5dR3?Y9E|0spm*fBwwukh*`oe*IXjTH&@Jjt?o+J-2}WfdAsfe=n-4-JPnE'
    b'?YNT9@hJW^^-~{H+0xMpmE)dYsl8lZGf-u};%m0^RmbgXS|4?dUP*=!nykKtwr>6Br@A0y?lW$^-t=6z@o3aqP>%mi2mbr6s!hz2'
    b'*}23L_(>zLzDg%&yK%l(n_9;FT43~_6HKl5ezX>z6~ZI`eulz(`kuQgZ)*OGlRZ!O(_Zp}#O42vzVF!z<&6)q0bf}6?ie@z(^}qQ'
    b'jIBGe_Z0A7a`E3SbkCJOoquU7WJ3SPwJg^Q*nssp`EtW!!SGu!KRILjY4U;opbiWO{0ICOFaBHX8{1n1@ZUt=U&GcszWPI|3Flqq'
    b'%(r1xJb>0GE6__luAr@3|9S4eL*9<LK=xltd4T*a^i%1`|1#Bm!jXX}Z`}sS_uL%UI$GOlGt|{jmh{OZ6)L<vP;%8`s-0On1_siu'
    b'x~l3pO*;P5eEzvC4%kw0z)#hQ58}sv$tB>w<l?`3+93X!KQ+BV<rhIa_?;@ZeK{>78_`kpnE|Tm0q`I2UvlxEt$evA%af>`Os|W}'
    b'KDSDK!~BJ>c_1|R5o5?X<_dmwnR{GdKIv~KQ`WdHilcqo-93}K{`0flZamb=>p$?=*`MV22<cA$OYLRK`f)S)Mh+^m7$*_`6%~40'
    b'NKbnc{rihDKd^=L?#=Y?{qCpv`hThG_3NMN-NoiNzQ?T!!-df*(h=xu_}UjKH|!Mh4j4n$(OqeMoI}0VlZ_<#_)q`*)sQ&g@yY|p'
    b'2L1#7%N+i*l;6fh@>`V0C)h3`?e~p!kG~}GZ>uWzJA~Fg(C>VLR{a@d$GV^P#-CAr({IwgFw|VJsfv{Z{saC?F8+(to2MLK6%)Sx'
    b'^Xh(T2m<u41|5X?1}Q(bbku)qrbO%MVLP$%kBa};`oE0gn1WFtbL!4J0sII2m(KccP8(Nu7R+Sv&pbS(yq0qzweE5itKfCgJRA=E'
    b'2mF@;{AXA7+wj-8C|23YgI5)*c!#9H|6BX<9vq4QE#<+MEBoC_1^z2mkhWdn{bE42c8N5r@!~bx=lNx;v5{>2AKmZWkyyTLd<-NF'
    b'XYHB7fd7F1(t!W+#<M%v*e`hhjNm^l<2x&&y`17WZ@ilI&z9*c;6LEM_R#-jb^T{Az*S`F5N?$_5#kAARj7i&`0#5f)&C_!TbvI3'
    b'r}v0GLTtbX*MSj$|A7C}t^ecR**7!z&&viDW(5`%HGsf>z<<DhX<z?!)v|M4JQsETv;K$UXiotCOR4;8#eYw_XMex`_qLi*0^|?;'
    b'2mF^L{HH&?NL7T&N9={1s)_{s2mA;8m+tjnXQ%!ZWwUYg{kf`DZ!I^s3Fp^)ux1M&_WvZ|zs;099W5_g_`~y_6-=J=PWM`!Cu~EC'
    b'BCl-(K>kU@f9FxZc9x8TgWC%KmCl3ejeBQ8rtqKs{J+Sm?d<{8c7UAn2wz8j1pEj52mF`v^<S^^AJa(LHsJl@)HHQ|bN=3k)`}mS'
    b'I$^N?r5FDV53KIDhTe@a-g~O%ae1q?UOeQVMErM7K&}V075*DuV}-xZWDNf~vc663RJI4?%k}^d86Ki)gaH2m{{jD{8~-`8e>K%B'
    b'`Lf)qs{U`d{2W!ce@=SwW59pu#eX|cU#>?l-G<WZ?h1F~5poCBj2Y@)tv@j&G(RHbpDz4&5%uZ%CF9`8w!(j<^Pqa;-Z?CD_)nTV'
    b'^+ov`%5pZc$__A)_V%e%6BG4+f&YO2vVs4kFZ1CV`3z-z`kp+E@;y%^OUHhM_>QGKiCu&Zo^msS7VE44hW$TD@_&zdvb3$P`n2zc'
    b'wB?Y0>gWH*0jYi{v!DOC+B~S<KFApVrFsCaf%zn5H~ltcv^|v8|9wdt{CWC$io8ZXBwy=Q4y~r+zjej(B;cQ2y;;3YtoSc`{A2x('
    b'Y5sea9jl)cyX!prV*gLJ{Nu=)2>GX1{@KNU;J=nV!0yEk&~*IQPyEMQDK?g~Vn;tS2yxAm+c>j&rP#zgSv>eJd;AC1|8<qlAuH?u'
    b'=!C8Rwwn>Jpt4vO-P(`cRchJBx&K4{jf?*-Z?#@us|OI|pTzm!TdAfU|JXIRY~x@59#AxUfcJY#<=@jvA;hzE-=cF;oJRFM<HtW&'
    b'>4<Wk)o+WnW5s{j<3F(ecgQ7m`(Gq&{r`|+D2Xea{~>?KAM%I%q5lK_^%KvYhv~irXQjA^ut?k%(3$c|IhbcPp)j#nJySgRFPr?A'
    b'ShclX`vKV;@8oz%SD&@K^?x+hf5;#5hx{Rb@DKj$AimdiPVex{I)iMHU9oGwF3@q(gu=vcWApyYD*wFx4-!kA^`FQ5<*6F;mFI#@'
    b'UbZTusI6o+66-(Y5BWp>kU#hb|8?S@Go9XH400I7B6P(Z{V<<mWfc12r<m1X4%?4^tvh^1TeWrm^_?@}|HB%L!}VOymhwRnG^Opi'
    b'|3m(eKje@7AN&LVRiiw;_I6eZ#y?{Sjl_+Au9kc0Z)XmVj}`xAlYi#wUZYPmd_CdISdv!jPf`9y_Sf}wSzdovtAEYw|D~y$|2Ne-'
    b'{~>?KAM$Ts`GbG(UrVgOa?e+I0A(_p6|)E2EV+^6(@~5u`(JkX=Mi!lanxN}dp_M3Ab$GOovPc+*^eU4g~0run!5S_gIec5<PZ5n'
    b'{_QJ&@DKiLiGcX6b5>*oG&xocSeKk5&-TuA&(hh|iY@vhtNio%e}bdcbasH-RIcJQul|pbCc{+Q<+*Tjee3_E)Xo2?_2_>ff5;#5'
    b'Z%_GyfAC*R|D5IbfZHKv{Bt!`2Q=1CTg;Bt`kPh$89(+HZ+JTZ*PtFKpHA%#z_Z{DxryS#__ePT!y{=*xi>HM%Qs!VP>#=2UUP6$'
    b'r11gyL;jF|`^q2uga2CeUn9jDno~NaUUpI)llXF=%#`1kYirFLuT^`P(`&r?zwGhPW8^Al)H@@87*#6c9l%am)~_{c_21TTAv99{'
    b'dBmRLp7@$S!n+@N5v3&?nyqS04fDK1r$wCqkU!)P`M0n9!9Vz~%>rs`3n<U9JxM3k6`S_!F#WAXdV=_^zggv<c^XBiw&<W*&Dn<X'
    b'*XBH3NWm_kv#K*aE=M_@KTe*Uy=z$iKkxa%vjyPV)K~ymO22XTJ)Zw(MV$YTKjaVjx3B!cKlrbuhYt5V;4IlkUL1=CY`V#OY&p44'
    b'&Wzpqn_d2SZM@O10dP*wXUnC&+J)VTZO#epeR-MeTfX`~963scui94ag0|xcwUgRe(xUx8)Zv}=!v{px4@3TtKjh!O@(2ImKdQHw'
    b'CHhd^3ERtpvQsQs@TQUWYl8kpw!SW-to8A0|FX(|j@riJJ>se8?EngC$_?b#<)iY0#E<?J@=tP#s*LAaex%aV7Q|TUr+keyo{OF@'
    b'{GwEx0D1O5p?uz5pQnv$*!LiR$RF}=SNVf~@L!7t>;z9o%4@`_O-Ddoa;`j&q~XSMUD6WmUv~ND^>Gs2ca69H3*R|<Bl&50oIH_o'
    b'PbaY*FZU*YZ|8|E@A;XVDK(vf_{uxIY#1CtxH;7kEhhX7Xsdb$;~j|=h=V`YH2Wcc$RF}=U-^T7@E;W;=R~J?+VO$oQ{oV+88o|z'
    b'HqhxR_F=da`A`&ZiXrqAR!i_-R{3W%IVZRSSmXs^Ns)@y;hp@Q)&Hl8GpnXvnB<{*J)b6-dQ!>udY|I$*Sva7t?`5a`9uDYfBVWG'
    b'{Dc46GRAd#o+6{E67KY-A)qdKjl#CBXXE79BY(5czopj6JJh;qm#?fAjjWCYw|wc!@2vihh_!0O$$aeq@n6Tj?|1>9BV}UGfZFeU'
    b'rg>7K{R38s5jCFwAb-do@^4@HgMaW}3j$hLih%ZPnhkUsp`Sw>^uxIPp_Zb*?DB7^@p7yBmZFd;Zbc=T2Wj=MeGZi50QFXYv!qPm'
    b'DbO~aDP4Zcmy`P`5BWp>kU!)P{=t8Pv!LY20{I^gB{MepEY%+AYPb$;8r}6c2mSE(eMlMjtQf1lwG;pPz89NOl+HcC@!goOW^%5s'
    b'2%mMuyQRMHyaF63UoxJy1v+!tEp9~>kIA%DjSqMBvtL3t<@2!W;vMpb{2~8#l|T3g|IsyIdztK-E&@7$bYL?Zs0Cw->NWBRHx^Cl'
    b'^e44k`<G?@^{CljJY%va6oR&(1h4!B<G1muI3`lP_sX(8v-AY0Ju3!NJ@7XYb^cpMey1Dz^Ihnto?Z7Cf&3wV$iIE%5B|Y_bUo?W'
    b'et%&`K)cCVRHJrUgEU{W$*b;wQuH&cWpTgl!#|IlwTQPDeJASVH|MnW237D`S8-w0je+yD3qBUM0tmSdowjb1|CH~Ub66{M{x;>~'
    b'c!BZ~oT{?Dm&|_N{}zjZy^0Qk{2_nHzkTHo{@a`XS`g6eas;%QypVKO(|f4}>!P-Ra(Qm&Wpm<h`ALn@T;RX{9f0?MNn(*$nws<f'
    b'GI5_cMXmk4J^k?P*+jhMj{|dJA)TLw%B@@2Hgun)B;N(wELMq6_S*9o@`wB(|MrzX_-}9iqvK>@BF20Os6SaOuTt@_U3Fuhx%BxH'
    b'D6dKh`guh3ZCTcrcH`g6n7WSGomSP$Dc&Wy?fc>ks`0dei_5KsAmBa3it-geuL1c)?05O{%pBTd<;aHU{vm(JAM$Ts`Gf!V=D!wi'
    b'k%fLgIQ>Lh$gh$ebiVS2iyjl95z{>S+}TR=<wiZQ9`?SvqPf6-@AiPaNE786s2yeP?%rwz=nl?3w#esN*sNeqHoZ<A(?I_K$RF~D'
    b'{M%Lj;J@AHe{=-&ecuzPp5gzumR}}3b2Z_aSqj-`Bou62rp+X+*VXjzSLIk={UKA8e4(1xxx}qF+2@~kfsPcke8V-PJdZw2zU0Y|'
    b'h14g6_o*^<<7YqQ5BWp>?I?fnkNF?fQZU&dW!qmnQjnE=A9;qnf-un4<j*#r5YC*k#+J?n^w;_FN_nQ7XgnHSyJxpTKvPZ5l_XNT'
    b'^Pgqnxip+KuSw2_Q8ee%KT_rLq{K!+{*XW9pMLp+f6V`={WMlQ?$?6p+Ijk?L2?_yK}XBe$gVj>p6_c@<TvH%@>sc_+@2z9O7&+Z'
    b'_rP;vpI$|Cfq#(br6>F;Vu?G~iV4YxVXLRnqircwIqghDUL?pL@`wDhBY*IZ`5zSlbyDR03X_M)_OOgqW;w6?n-4ST4D>}YSFD|i'
    b'Q*FERpIy>f#MY*K7|)hu8f#^)3x8I&B{b?bFy2of$R7mxgMaWZV)wme+D6>z<{>N!pGGSeKtdJ%T6Fy_e<OBnKrBD_2Q}st*dZno'
    b'9)D7Nkcji&3d+%Yz4(HP3W9zZ@`wB(fAA0fTkQ!a#CY*zvC>6Bg@_tcjj3&efqq3f=+m~YwLAaW)&h7ad>Bdi5OE^4lUmUDdk-hS'
    b'<PkpIGsXzy4}$!`Klm3duZ+tyinNkXl|(03*JpZHJ}+j79ZT*dQk8+Az4_1j9?+36y#&q&kUt3W2mjzdIT$E!`fHpxpEOyoMv9zy'
    b'Lp&%hAza(1<Q{3HcIUqx_JMqhb6w;Ul?fg4hx{S`c9B2$$NZ0yiPNF}Vl?SX|A)}cb%D(7H;C_0t>w>)F=CML^qJ|`d$l|NAP@)y'
    b'0)g7~oRq5x%jBAp#S{F4Kp+qZ1Ol}J2jzRvXJ*-&(eC_%Kp+qZ1OkCTAP@)y0)apv5C{YUfj}S-2m}IwKp+qZ1OkCTAP@)y0)apv'
    b'5C{YUfj}S-2m}IwKp+qZ1OkCTAP@)y0)apv5C{YUfj}S-2m}IUk^Uc0e2Ss'
)


def keycap(label, font_path):
    coverage = Image.new('L', (TILE * SCALE, TILE * SCALE))
    ink = Image.new('L', coverage.size)
    # Preserve the original white face dimensions, surrounded by a 6px
    # black rounded outline. Coverage includes the outline; ink stays zero there.
    face = (13, 27, 115, 99) if len(label) > 1 else (20, 20, 108, 105)
    border = 6
    bounds = (face[0] - border, face[1] - border,
              face[2] + border, face[3] + border)
    ImageDraw.Draw(coverage).rounded_rectangle(
        tuple(v * SCALE for v in bounds), radius=20 * SCALE, fill=255)
    ImageDraw.Draw(ink).rounded_rectangle(
        tuple(v * SCALE for v in face), radius=14 * SCALE, fill=255)
    size = 58 if len(label) == 1 else 38
    draw = ImageDraw.Draw(ink)
    while True:
        font = ImageFont.truetype(str(font_path), size * SCALE)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        if right - left <= (face[2] - face[0] - 24) * SCALE:
            break
        size -= 1
    x = (bounds[0] + bounds[2]) * SCALE / 2 - (left + right) / 2
    y = (bounds[1] + bounds[3]) * SCALE / 2 - (top + bottom) / 2
    draw.text((x, y), label, font=font, fill=0)
    coverage = coverage.resize((TILE, TILE), Image.Resampling.LANCZOS)
    # Ink is straight luminance; preserve the black outline at the alpha boundary.
    ink = ink.resize((TILE, TILE), Image.Resampling.LANCZOS)
    ink = Image.frombytes('L', (TILE, TILE), bytes(
        min(255, round(g * 255 / a)) if a else 0
        for a, g in zip(coverage.tobytes(), ink.tobytes())))
    return Image.merge('LA', (coverage, ink))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]
                        / 'crates/victory-gpu/src/pc_prompts_atlas.bin')
    parser.add_argument('--preview', type=Path)
    parser.add_argument('--font', type=Path,
                        default=Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts/arialbd.ttf')
    args = parser.parse_args()
    if not args.font.is_file():
        parser.error('Bold TrueType font not found; provide --font PATH')
    original = zlib.decompress(base64.b85decode(ORIGINAL))
    if len(original) != 10 * TILE * TILE * 2:
        raise ValueError('Invalid embedded original atlas size')
    atlas = Image.new('LA', (len(LABELS) * TILE, TILE))
    atlas.paste(Image.frombytes('LA', (10 * TILE, TILE), original), (0, 0))
    for index, label in enumerate(LABELS[10:], 10):
        atlas.paste(keycap(label, args.font), (index * TILE, 0))
    raw = atlas.tobytes()
    assert len(raw) == 589824
    assert atlas.crop((0, 0, 10 * TILE, TILE)).tobytes() == original
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    if args.preview:
        coverage, ink = atlas.split()
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        Image.merge('RGBA', (ink, ink, ink, coverage)).save(args.preview)
    print(f'{args.output}: 2304x128 R8G8_UNORM, {len(raw)} bytes, 18 tiles')


if __name__ == '__main__':
    main()
