"""Build demo_5, render ASCII (screen space), list square-corner switches,
check depot platform reachability from SW_LEAD. Screen: col=x+y, row=x-y."""
from collections import deque
import demo_5
from track import EDGE_MASKS, OPPOSITE_EDGE, BIT_TO_EDGES

net = demo_5.create_comprehensive_demo()
ANG = {1:135, 2:45, 4:90, 8:0, 16:0, 32:90}
NAME = {1:"NS",2:"EW",4:"NE",8:"NW",16:"SE",32:"SW"}
def square(bits):
    a = {ANG[b] for b in (1,2,4,8,16,32) if bits & b}
    return (0 in a) and (90 in a) and not ({45,135} & a)

print(f"counts: tracks={len(net.tracks)} blocks={len(net.blocks)} "
      f"platforms={len(net.platforms)} switches={len(net.switches)}")
flagged = []
for sid, sw in sorted(net.switches.items()):
    t = net.tracks.get((sw.x, sw.y))
    if t and square(t.track_bits):
        flagged.append(sid)
print("SQUARE-CORNER switches (should be []):", flagged)

# drivability BFS from SW_LEAD
NEI = {"N":(0,1),"E":(1,0),"S":(0,-1),"W":(-1,0)}
def opens(xy,e): return (net.tracks[xy].track_bits & EDGE_MASKS[e]) != 0
def trav(xy):
    b = net.tracks[xy].track_bits; out=[]
    for i,bit in enumerate((1,2,4,8,16,32)):
        if b & bit:
            a,c = BIT_TO_EDGES[i]; out.append((a,c)); out.append((c,a))
    return out
start=(7,9); seen={start}; st=set(); q=deque()
for a,_ in trav(start):
    if (start,a) not in st: st.add((start,a)); q.append((start,a))
while q:
    cur,en=q.popleft()
    for e1,e2 in trav(cur):
        if e1!=en: continue
        dx,dy=NEI[e2]; nb=(cur[0]+dx,cur[1]+dy)
        if nb not in net.tracks or not opens(nb,OPPOSITE_EDGE[e2]): continue
        seen.add(nb)
        if (nb,OPPOSITE_EDGE[e2]) not in st: st.add((nb,OPPOSITE_EDGE[e2])); q.append((nb,OPPOSITE_EDGE[e2]))
print("depot platforms reachable from SW_LEAD:")
for pid in [f"PLT_DEPOT_{i}" for i in range(1,7)]:
    p=net.platforms[pid]; print(f"  {pid} {p.positions} {'OK' if any(x in seen for x in p.positions) else 'NOT REACHABLE'}")

# ASCII (throat+depot region only: cols 8..26, rows -12..2)
sw_at = {(s.x,s.y):sid for sid,s in net.switches.items()}
plat = {}
for pid,p in net.platforms.items():
    for xy in p.positions: plat[xy]=pid
def cell(x,y):
    if (x,y) in sw_at:
        s=sw_at[(x,y)]
        return {"SW_LEAD":"LD","SW_XOVER_N":"Cn","SW_XOVER_S":"Cs","SW_DEPOT_FAN":"FN","SW_X1":"X1","SW_X2":"X2"}.get(s, "L"+s[-1] if s.startswith("SW_LAD") else s[:2])
    if (x,y) in plat:
        pid=plat[(x,y)]; return ("d"+pid[-1]) if pid.startswith("PLT_DEPOT") else pid.replace("PLT_","")[:2].lower()
    return ".." if (x,y) in net.tracks else None
tiles=list(net.tracks); cmin=8; cmax=26; rmin=-12; rmax=2
print(f"\nASCII (col {cmin}..{cmax}, row {rmin}..{rmax}):\n")
W=4; print("    "+"".join(f"{c:>{W}}" for c in range(cmin,cmax+1)))
for r in range(rmin,rmax+1):
    s=f"{r:>4}"
    for c in range(cmin,cmax+1):
        if (c+r)%2: s+=" "*W; continue
        x,y=(c+r)//2,(c-r)//2
        cc=cell(x,y); s+= f"{cc:>{W}}" if cc else " "*W
    print(s)
