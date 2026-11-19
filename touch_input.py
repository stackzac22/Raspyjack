#!/usr/bin/env python3
"""Touchscreen -> RaspyJack button bridge (ADS7846 /dev/input/event0 -> rj_input unix DGRAM socket).
Maps a tap to one of a 3x3 d-pad grid and sends it as a virtual button press, the same path the web UI uses."""
import os, struct, time, select, socket, json

DEV  = os.environ.get("RJ_TOUCH_DEVICE", "/dev/input/event0")
SOCK = os.environ.get("RJ_INPUT_SOCK", "/dev/shm/rj_input.sock")
XMIN=int(os.environ.get("RJ_TOUCH_XMIN","290")); XMAX=int(os.environ.get("RJ_TOUCH_XMAX","3760"))
YMIN=int(os.environ.get("RJ_TOUCH_YMIN","520")); YMAX=int(os.environ.get("RJ_TOUCH_YMAX","3630"))
XINV=os.environ.get("RJ_TOUCH_XINV","1")!="0"
YINV=os.environ.get("RJ_TOUCH_YINV","0")!="0"
SW,SH=480,320
DEBOUNCE=float(os.environ.get("RJ_TOUCH_DEBOUNCE","0.25"))
FMT="llHHi"; SZ=struct.calcsize(FMT)
EV_KEY=1; EV_ABS=3; ABS_X=0; ABS_Y=1; BTN_TOUCH=330; BTN_LEFT=272
GRID={(0,0):"KEY1",(1,0):"UP",(2,0):"KEY2",
      (0,1):"LEFT",(1,1):"OK",(2,1):"RIGHT",
      (0,2):"KEY3",(1,2):"DOWN",(2,2):"OK"}

def to_screen(rx,ry):
    fx=(rx-XMIN)/float(XMAX-XMIN); fy=(ry-YMIN)/float(YMAX-YMIN)
    if XINV: fx=1-fx
    if YINV: fy=1-fy
    return min(SW-1,max(0,int(fx*(SW-1)))), min(SH-1,max(0,int(fy*(SH-1))))

def zone(sx,sy):
    col=0 if sx<SW/3 else (1 if sx<2*SW/3 else 2)
    row=0 if sy<SH/3 else (1 if sy<2*SH/3 else 2)
    return GRID[(col,row)]

def send(btn):
    s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
    try:
        for st in ("press","release"):
            s.sendto(json.dumps({"type":"input","button":btn,"state":st}).encode(),SOCK)
    except Exception: pass
    finally: s.close()

def main():
    cx=cy=None; down=False; last=0.0
    while True:
        try: f=open(DEV,"rb",buffering=0)
        except Exception: time.sleep(1); continue
        try:
            while True:
                r,_,_=select.select([f],[],[],1.0)
                if not r: continue
                data=f.read(SZ)
                if not data or len(data)<SZ: continue
                _,_,et,code,val=struct.unpack(FMT,data)
                if et==EV_ABS:
                    if code==ABS_X: cx=val
                    elif code==ABS_Y: cy=val
                elif et==EV_KEY and code in (BTN_TOUCH,BTN_LEFT):
                    if val==1: down=True
                    elif val==0 and down:
                        down=False; now=time.time()
                        if cx is not None and cy is not None and (now-last)>=DEBOUNCE:
                            last=now; sx,sy=to_screen(cx,cy); btn=zone(sx,sy)
                            print("[touch] raw(%s,%s)->px(%d,%d) %s"%(cx,cy,sx,sy,btn),flush=True)
                            send(btn)
        except Exception as e:
            print("[touch] reopen:",e,flush=True); time.sleep(1)

if __name__=="__main__": main()
