import struct, time, sys, select
DEV="/dev/input/event0"
FMT="llHHi"; SZ=struct.calcsize(FMT)
EV_SYN=0; EV_KEY=1; EV_ABS=3; ABS_X=0; ABS_Y=1; BTN_TOUCH=330; BTN_LEFT=272
dur=float(sys.argv[1]) if len(sys.argv)>1 else 90.0
f=open(DEV,"rb",buffering=0)
cx=cy=None; down=False; taps=[]; raw=0
end=time.time()+dur
log=open("/dev/shm/touch_cal.log","w")
def out(s): log.write(s+"\n"); log.flush()
out("capture %ss from %s (raw+taps)"%(dur,DEV))
while time.time()<end:
    r,_,_=select.select([f],[],[],0.5)
    if not r: continue
    data=f.read(SZ)
    if not data or len(data)<SZ: continue
    sec,usec,etype,code,val=struct.unpack(FMT,data)
    if etype!=EV_SYN and raw<60:
        raw+=1; out("RAW type=%d code=%d val=%d"%(etype,code,val))
    if etype==EV_ABS:
        if code==ABS_X: cx=val
        elif code==ABS_Y: cy=val
    elif etype==EV_KEY and code in (BTN_TOUCH,BTN_LEFT):
        if val==1: down=True
        elif val==0 and down:
            down=False; taps.append((cx,cy)); out("TAP %d: x=%s y=%s"%(len(taps),cx,cy))
xs=[t[0] for t in taps if t[0] is not None]; ys=[t[1] for t in taps if t[1] is not None]
if xs and ys: out("SUMMARY taps=%d x[%d..%d] y[%d..%d]"%(len(taps),min(xs),max(xs),min(ys),max(ys)))
else: out("SUMMARY no taps; raw_events=%d"%raw)
out("DONE")
