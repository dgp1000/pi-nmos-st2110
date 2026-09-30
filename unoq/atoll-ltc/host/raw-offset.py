import socket,struct,time,statistics
SO_TIMESTAMPNS=35
def mk(port):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("",port))
    s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,socket.inet_aton("224.0.1.129")+socket.inet_aton("10.10.10.77"))
    s.setsockopt(socket.SOL_SOCKET,SO_TIMESTAMPNS,1); s.settimeout(0.5); return s
ev,gen=mk(319),mk(320); syncs={}; offs=[]; end=time.time()+30
import select
while time.time()<end:
    r,_,_=select.select([ev,gen],[],[],0.5)
    for s in r:
        d,anc,fl,a=s.recvmsg(1500,1024)
        ts=None
        for lvl,typ,cd in anc:
            if lvl==socket.SOL_SOCKET and typ==SO_TIMESTAMPNS: sec,ns=struct.unpack("qq",cd[:16]); ts=sec*10**9+ns
        mt=d[0]&0xf; seq=struct.unpack("!H",d[30:32])[0]
        if mt==0 and ts: syncs[seq]=ts
        if mt==8 and seq in syncs:
            s_hi,s_lo,ns=struct.unpack("!HIi",d[34:44]); origin=((s_hi<<32)|s_lo)*10**9+ns
            offs.append((syncs.pop(seq)-origin)/1000)
d=[offs[i+1]-offs[i] for i in range(len(offs)-1)]
print("n=%d  first=%.0fus last=%.0fus  drift=%.0f us/s  step-diff std=%.0fus min=%.0f max=%.0f"%(len(offs),offs[0],offs[-1],(offs[-1]-offs[0])/max(1,len(offs)-1),statistics.pstdev(d),min(d),max(d)))
print("diffs:"," ".join("%.0f"%x for x in d))
