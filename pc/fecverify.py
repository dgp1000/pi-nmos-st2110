#!/usr/bin/env python3
"""
Is ST 2022-1 recovery BYTE-CORRECT, or does rtpst2022-1-fecdec emit plausible-but-wrong packets?

Packet counts alone cannot answer this: a decoder that fabricates a packet keeps the sequence
complete (so "residual 0%") while corrupting the payload -- which is exactly the pattern we see
(complete counts, elevated h264 decode errors, torn picture).

Harness: ONE udpsrc, tee'd, so both branches see the identical packet stream.
    reference branch : untouched            -> seq -> payload
    test branch      : loss injector -> fecdec -> seq -> payload
Packets the injector dropped are known exactly (reference seqs minus post-injector seqs). For each
of those, we ask whether fecdec produced it, and whether the bytes match the reference.

    recovered + identical  -> FEC is correct
    recovered + DIFFERENT  -> FEC is fabricating data   <-- would explain everything
    never reappeared       -> honest unrecovered loss
"""
import gi, os, sys, subprocess, struct, time
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
Gst.init(None)

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "FEC_GRP", "FEC_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
C = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
IFC, G, P = C["ISLAND_IFACE"], C["FEC_GRP"], int(C["FEC_PORT"])
LOSS = float(sys.argv[1]) if len(sys.argv) > 1 else 0.01
SECS = int(sys.argv[2]) if len(sys.argv) > 2 else 30
MC = "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33"
FC = "application/x-rtp,payload=96,clock-rate=90000"

pipe = Gst.parse_launch(
    f'udpsrc address={G} port={P} multicast-iface={IFC} auto-multicast=true buffer-size=16777216 caps="{MC}" '
    f'! tee name=t '
    f't. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 ! identity name=ref ! fakesink sync=false '
    f't. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 ! identity name=lossy drop-probability={LOSS} '
    f'  ! identity name=survived ! rtpst2022-1-fecdec name=fd size-time=4000000000 ! identity name=out ! fakesink sync=false '
    f'udpsrc address={G} port={P+2} multicast-iface={IFC} auto-multicast=true caps="{FC}" ! queue ! fd.fec_0 '
    f'udpsrc address={G} port={P+4} multicast-iface={IFC} auto-multicast=true caps="{FC}" ! queue ! fd.fec_1')

ref, out = {}, {}
survived = set()
order = []          # sequence numbers in the order fecdec emitted them

def grab(store):
    def cb(_pad, info):
        b = info.get_buffer()
        ok, mi = b.map(Gst.MapFlags.READ)
        if ok:
            try:
                d = bytes(mi.data)
                if len(d) >= 12:
                    seq = struct.unpack("!H", d[2:4])[0]
                    if isinstance(store, set):
                        store.add(seq)
                    else:
                        if store is out:
                            order.append(seq)
                        if seq not in store:
                            store[seq] = d
            finally:
                b.unmap(mi)
        return Gst.PadProbeReturn.OK
    return cb

for name, store in (("ref", ref), ("survived", survived), ("out", out)):
    e = pipe.get_by_name(name)
    e.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, grab(store))

pipe.set_state(Gst.State.PLAYING)
loop = GLib.MainLoop()
GLib.timeout_add_seconds(SECS, lambda: (loop.quit(), False)[1])
loop.run()
pipe.set_state(Gst.State.NULL)

# Only judge sequences seen in the reference, trimmed at both ends so partial FEC matrices at
# start/stop are not miscounted as failures.
seqs = sorted(ref)
if len(seqs) < 400:
    print(f"  too few packets captured ({len(seqs)}) -- is the fec sender running?")
    sys.exit(1)
lo, hi = seqs[200], seqs[-200]
def between(s):
    return ((s - lo) & 0xFFFF) < ((hi - lo) & 0xFFFF)

dropped = [s for s in ref if between(s) and s not in survived]
recovered_ok = recovered_bad = never = 0
for s in dropped:
    if s not in out:
        never += 1
    elif out[s][12:] == ref[s][12:]:
        recovered_ok += 1
    else:
        recovered_bad += 1

total = sum(1 for s in ref if between(s))
print(f"  window            {total:,} reference packets, {LOSS*100:.1f}% injected loss")
print(f"  dropped by us     {len(dropped):,}")
print(f"  -> recovered OK   {recovered_ok:,}   (byte-identical to reference)")
print(f"  -> recovered WRONG{recovered_bad:>6,}   (same seq, DIFFERENT bytes)")
print(f"  -> never returned {never:,}")
if dropped:
    print(f"  verdict: {100.0*recovered_ok/len(dropped):.1f}% of dropped packets came back byte-correct")
# also: did fecdec alter any packet it was NOT asked to recover?
altered = sum(1 for s in ref if between(s) and s in survived and s in out and out[s][12:] != ref[s][12:])
print(f"  sanity: {altered:,} untouched packets were altered in transit (should be 0)")

# ORDER: correct bytes are useless if they arrive after the depayloader has moved on.
inv = worst = 0
hi = None
for s in order:
    if hi is not None:
        back = (hi - s) & 0xFFFF
        if 0 < back < 4000:
            inv += 1
            worst = max(worst, back)
    if hi is None or ((s - hi) & 0xFFFF) < 4000:
        hi = s
print(f"  ORDER : {len(order):,} emitted, {inv:,} out of order, worst {worst} packets behind the leading edge")
if inv:
    print("  -> recovered packets arrive LATE; anything downstream that judges lateness will discard them")
