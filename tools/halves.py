# 割れたハート：輪郭と割れ目の交点を求めて、破片ごとに閉じたパスを作る（clipPath を使わない）
import math, json
# HEART_PATH（index.html と同じ）を手で分解：M12 21 C6.5 16.5 3 13.2 3 9.4  C3 6.5 5.2 4.4 7.9 4.4  c1.8 0 3.3 1.1 4.1 2.6  (→12,7)
#   c.8-1.5 2.3-2.6 4.1-2.6 (→16.1,4.4)  C18.8 4.4 21 6.5 21 9.4  c0 3.8-3.5 7.1-9 11.6 (→12,21) z
segs = [
  ((12,21),(6.5,16.5),(3,13.2),(3,9.4)),
  ((3,9.4),(3,6.5),(5.2,4.4),(7.9,4.4)),
  ((7.9,4.4),(9.7,4.4),(11.2,5.5),(12,7)),
  ((12,7),(12.8,5.5),(14.3,4.4),(16.1,4.4)),
  ((16.1,4.4),(18.8,4.4),(21,6.5),(21,9.4)),
  ((21,9.4),(21,13.2),(17.5,16.5),(12,21)),
]
def bez(p0,p1,p2,p3,t):
    u=1-t
    return (u*u*u*p0[0]+3*u*u*t*p1[0]+3*u*t*t*p2[0]+t*t*t*p3[0], u*u*u*p0[1]+3*u*u*t*p1[1]+3*u*t*t*p2[1]+t*t*t*p3[1])
N=120
heart=[]
for s in segs:
    for i in range(N): heart.append(bez(*s, i/N))
heart.append(heart[0])   # 閉じる（先端から始まり先端で終わる）
# 先頭に「切れ込みの上へ延ばした点」を足す：ずらした割れ目は切れ込みの中から始まって輪郭と交わらないので、
# 最初の辺の向きのまま外まで延ばして交点を作る（絵には出ない）
CRACK=[(13.8,2.8),(12,7),(10.2,11.2),(13.2,12.8),(10.8,16.4),(12.2,18.8),(12,21)]
def seg_x(a,b,c,d):
    # 線分 ab と cd の交点（パラメータ）。無ければ None
    r=(b[0]-a[0],b[1]-a[1]); s=(d[0]-c[0],d[1]-c[1]); den=r[0]*s[1]-r[1]*s[0]
    if abs(den)<1e-12: return None
    q=(c[0]-a[0],c[1]-a[1]); t=(q[0]*s[1]-q[1]*s[0])/den; u=(q[0]*r[1]-q[1]*r[0])/den
    if 0<=t<=1 and 0<=u<=1: return (a[0]+t*r[0],a[1]+t*r[1]), t, u
    return None
def half(side, off):
    # side=-1 左, +1 右。off=割れ目を中心からずらす量
    crack=[(x+side*off,y) for x,y in CRACK]
    # 割れ目（上→下）と輪郭の交点を集める：(輪郭の辺番号, t, 割れ目の辺番号, u, 点)
    hits=[]
    for ci in range(len(crack)-1):
        for hi in range(len(heart)-1):
            r=seg_x(heart[hi],heart[hi+1],crack[ci],crack[ci+1])
            if r: hits.append((hi,r[1],ci,r[2],r[0]))
    # 上（切れ込み付近）と下（先端付近）の交点＝割れ目の順で最初と最後
    hits.sort(key=lambda h:(h[2],h[3]))
    top,bot=hits[0],hits[-1]
    # 輪郭の点列は 先端→左側→切れ込み→右側→先端。左は bot(先端側,後半)→…→top(前半)ではなく
    # 左側の弧：top より前の輪郭点（切れ込みまで）… 実際は先端(0)から始まるので、左側は index 小さい側
    if side<0:
        arc=[top[4]]+[p for i,p in enumerate(heart) if bot[0]>=i>top[0]][::-1]+[bot[4]]  # 上→下（左側を下る）
        # 上の交点 top は左側の弧の終端（切れ込み側）、bot は先端側。heart の index: bot.hi は小さい(先端付近), top.hi は大きい
    else:
        arc=[top[4]]+[p for i,p in enumerate(heart) if top[0]<i<=bot[0]]+[bot[4]]
    # 左：先端側の交点 → 輪郭を上へ → 切れ込み側の交点 → 割れ目を下へ → 先端側の交点
    crack_mid=[c for k,c in enumerate(crack) if top[2]<k<=bot[2]]
    return arc, crack_mid, top, bot
def build(side, off):
    crack=[(x+side*off,y) for x,y in CRACK]
    hits=[]
    for ci in range(len(crack)-1):
        for hi in range(len(heart)-1):
            r=seg_x(heart[hi],heart[hi+1],crack[ci],crack[ci+1])
            if r: hits.append((hi,r[1],ci,r[2],r[0]))
    hits.sort(key=lambda h:(h[2],h[3]))
    top,bot=hits[0],hits[-1]
    if side<0:
        arc=[p for i,p in enumerate(heart) if bot[0]<i<=top[0]]          # 先端側→切れ込み側（左側を上る）
        pts=[bot[4]]+arc+[top[4]]+[c for k,c in enumerate(crack) if top[2]<k<=bot[2]]
    else:
        arc=[p for i,p in enumerate(heart) if top[0]<i<=bot[0]]          # 切れ込み側→先端側（右側を下る）
        pts=[top[4]]+arc+[bot[4]]+[c for k,c in enumerate(crack) if top[2]<k<=bot[2]][::-1]
    return pts
def path(pts):
    # 点列をパスに。曲線部は点が密なので折れ線でよいが、点数を減らすため 1/6 に間引く（角＝割れ目の点はそのまま）
    out=[]
    for i,(x,y) in enumerate(pts):
        out.append(('M' if i==0 else 'L')+f'{x:.1f} {y:.1f}')
    return ' '.join(out)+' Z'
def thin(pts, keep):
    res=[]
    for i,p in enumerate(pts):
        if i==0 or i==len(pts)-1 or p in keep or i%5==0: res.append(p)
    return res
OFF=1.4
L=build(-1,OFF); R=build(+1,OFF)
keep=set((x+s*OFF,y) for x,y in CRACK for s in (-1,1))
Lp=path(thin(L,keep)); Rp=path(thin(R,keep))
print(len(Lp), len(Rp))
json.dump({'L':Lp,'R':Rp}, open('/private/tmp/claude-501/-Users-qramo-Downloads-webrtc-call/dd7b3059-5d06-42a0-ad43-e242ecc06103/scratchpad/heart/halves.json','w'))
print(Lp[:200]); print(Rp[:200])

# ── ベジェのまま出す（折れ線だと拡大でぎこちない）。交点のパラメータで曲線を分割する ──
def split(p0,p1,p2,p3,t0,t1):
    # 区間 [t0,t1] の部分曲線を de Casteljau で切り出す
    def cut_after(p0,p1,p2,p3,t):   # [t,1]
        a=lerp(p0,p1,t); b=lerp(p1,p2,t); c=lerp(p2,p3,t); d=lerp(a,b,t); e=lerp(b,c,t); f=lerp(d,e,t)
        return f,e,c,p3
    def cut_before(p0,p1,p2,p3,t):  # [0,t]
        a=lerp(p0,p1,t); b=lerp(p1,p2,t); c=lerp(p2,p3,t); d=lerp(a,b,t); e=lerp(b,c,t); f=lerp(d,e,t)
        return p0,a,d,f
    q=cut_after(p0,p1,p2,p3,t0)
    if t1>=1: return q
    return cut_before(*q,(t1-t0)/(1-t0))
def lerp(a,b,t): return (a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t)
def fmt(p): return f'{p[0]:.2f} {p[1]:.2f}'.replace('.00','')
def curve(seg): return 'C'+fmt(seg[1])+' '+fmt(seg[2])+' '+fmt(seg[3])
def build_bez(side, off):
    crack=[(x+side*off,y) for x,y in CRACK]
    hits=[]
    for ci in range(len(crack)-1):
        for hi in range(len(heart)-1):
            r=seg_x(heart[hi],heart[hi+1],crack[ci],crack[ci+1])
            if r: hits.append((hi,r[1],ci,r[2],r[0]))
    hits.sort(key=lambda h:(h[2],h[3]))
    top,bot=hits[0],hits[-1]
    loc=lambda h:(h[0]//N, (h[0]%N+h[1])/N)   # (セグメント番号, 局所 t)
    (st,tt),(sb,tb)=loc(top),loc(bot)
    inner=[c for k,c in enumerate(crack) if top[2]<k<=bot[2]]
    if side<0:
        # 先端側の交点(seg sb, tb) → 輪郭を上る（seg 0,1,2）→ 切れ込み側の交点(seg st, tt) → 割れ目を下る
        parts=['M'+fmt(bot[4])]
        for sg in range(sb, st+1):
            t0=tb if sg==sb else 0; t1=tt if sg==st else 1
            parts.append(curve(split(*segs[sg],t0,t1)))
        parts += ['L'+fmt(c) for c in inner]
    else:
        parts=['M'+fmt(top[4])]
        for sg in range(st, sb+1):
            t0=tt if sg==st else 0; t1=tb if sg==sb else 1
            parts.append(curve(split(*segs[sg],t0,t1)))
        parts += ['L'+fmt(c) for c in inner[::-1]]
    return ' '.join(parts)+'Z'
Lb=build_bez(-1,OFF); Rb=build_bez(+1,OFF)
print('bezier', len(Lb), len(Rb)); print(Lb); print(Rb)
json.dump({'L':Lb,'R':Rb}, open('/private/tmp/claude-501/-Users-qramo-Downloads-webrtc-call/dd7b3059-5d06-42a0-ad43-e242ecc06103/scratchpad/heart/halves.json','w'))
