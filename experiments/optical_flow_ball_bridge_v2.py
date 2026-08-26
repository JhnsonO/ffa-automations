#!/usr/bin/env python3
"""OEV optical-flow bridge v2: conservative causal continuation.

Changes from v1 are deliberately limited to drift prevention:
- never refresh/reseed features during a detector gap;
- max bridge duration reduced to 0.35 s;
- smaller seed aperture;
- stricter forward/backward LK and dispersion gates;
- hard per-frame and cumulative pixel displacement gates;
- require >=8 surviving original-seed features throughout the bridge.

The bridge still starts only from a selected real YOLO ball Tracking observation.
A future YOLO observation is used only after the bridge has ended to report endpoint
error; it never participates in the causal continuation itself.
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
from typing import Any
import cv2
import numpy as np

BALL_CLASS_ID=32
MAX_BRIDGE_SECONDS=0.35
MIN_FEATURES=8
FB_ERROR_PX=0.80
LK_ERROR_MAX=22.0
MAX_STEP_PX=28.0
MAX_DISPERSION_PX=4.5
MAX_CUMULATIVE_DISPLACEMENT_PX=180.0
MIN_SEED_CONF=0.10
SYNTHETIC_CONF=0.05


def load_events(path:Path):
    dets={}; worlds={}
    for raw in path.read_text().splitlines():
        if not raw.strip(): continue
        ev=json.loads(raw); fi=ev.get('frame_index')
        if not isinstance(fi,int): continue
        if ev.get('kind')=='detections_raw': dets[fi]=ev
        elif ev.get('kind')=='world_state': worlds[fi]=ev
    return dets,worlds


def ball_detections(ev):
    return [] if not ev else [d for d in ev.get('detections',[]) if d.get('class_id')==BALL_CLASS_ID]


def match_selected_detection(world, det_ev):
    if not world: return None
    ball=world.get('ball')
    if not ball or ball.get('state')!='Tracking' or float(ball.get('confidence',0))<MIN_SEED_CONF: return None
    by,bp=float(ball['yaw']),float(ball['pitch']); origin=ball.get('origin')
    best=None; best_dist=float('inf')
    for d in ball_detections(det_ev):
        pos=d.get('position') or {}
        if 'yaw' not in pos or 'pitch' not in pos: continue
        penalty=0.05 if origin and d.get('camera')!=origin else 0.0
        dist=math.hypot(float(pos['yaw'])-by,float(pos['pitch'])-bp)+penalty
        if dist<best_dist: best_dist=dist; best=d
    return best if best is not None and best_dist<=0.08 else None


def open_capture(path,start_frame):
    cap=cv2.VideoCapture(str(path))
    if not cap.isOpened(): raise RuntimeError(f'could not open {path}')
    fps=float(cap.get(cv2.CAP_PROP_FPS) or 0); w=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps<=0 or w<=0 or h<=0: raise RuntimeError(f'invalid metadata {path}: {fps} {w}x{h}')
    cap.set(cv2.CAP_PROP_POS_FRAMES,start_frame)
    return cap,fps,w,h


def roi_bounds(center,size,w,h):
    cx,cy=center; bw,bh=size
    # v2: tighter aperture. The old >=24 px half-width could easily include a player/grass feature.
    half_w=max(14.0,bw*w*1.6); half_h=max(14.0,bh*h*1.6)
    px,py=cx*w,cy*h
    return max(0,int(px-half_w)),max(0,int(py-half_h)),min(w,int(px+half_w+1)),min(h,int(py+half_h+1))


def seed_features(gray,center,size):
    h,w=gray.shape[:2]; x0,y0,x1,y1=roi_bounds(center,size,w,h)
    if x1-x0<8 or y1-y0<8: return None
    roi=gray[y0:y1,x0:x1]
    pts=cv2.goodFeaturesToTrack(roi,maxCorners=40,qualityLevel=0.015,minDistance=3,blockSize=5,useHarrisDetector=False)
    if pts is None: return None
    pts[:,0,0]+=x0; pts[:,0,1]+=y0
    return pts.astype(np.float32)


def read_gray(cap):
    ok,frame=cap.read()
    if not ok or frame is None: raise EOFError
    return cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--events',required=True,type=Path); ap.add_argument('--left',required=True,type=Path); ap.add_argument('--right',required=True,type=Path)
    ap.add_argument('--start-seconds',required=True,type=float); ap.add_argument('--duration-seconds',required=True,type=float)
    ap.add_argument('--output',required=True,type=Path); ap.add_argument('--report',required=True,type=Path)
    a=ap.parse_args()
    det_events,worlds=load_events(a.events)
    probe=cv2.VideoCapture(str(a.left)); fps=float(probe.get(cv2.CAP_PROP_FPS) or 0); probe.release()
    if fps<=0: raise SystemExit('could not determine fps')
    source_start=int(round(a.start_seconds*fps)); target_frames=int(round(a.duration_seconds*fps)); max_frames=max(1,int(round(MAX_BRIDGE_SECONDS*fps)))
    lc,lfps,w,h=open_capture(a.left,source_start); rc,rfps,rw,rh=open_capture(a.right,source_start)
    if abs(lfps-rfps)>0.05 or (w,h)!=(rw,rh): raise SystemExit('left/right mismatch')
    prev={'Left':None,'Right':None}; cur={'Left':None,'Right':None}; active=None; span=None; spans=[]; emitted=[]; terms={}

    def terminate(reason,fi,reacq=None):
        nonlocal active,span
        if span is not None:
            span['end_frame']=fi-1; span['duration_frames']=max(0,fi-span['start_frame']); span['duration_seconds']=span['duration_frames']/fps; span['termination']=reason
            if reacq is not None and active is not None and reacq.get('camera')==active['camera']:
                r=reacq.get('camera_center') or [None,None]
                if r[0] is not None:
                    span['reacquisition_error_px']=math.hypot((float(r[0])-active['center'][0])*w,(float(r[1])-active['center'][1])*h)
            spans.append(span)
        terms[reason]=terms.get(reason,0)+1; active=None; span=None

    for fi in range(target_frames):
        try:
            cur['Left']=read_gray(lc); cur['Right']=read_gray(rc)
        except EOFError:
            terminate('video_eof',fi); break
        selected=match_selected_detection(worlds.get(fi),det_events.get(fi))
        if selected is not None:
            if active is not None: terminate('yolo_reacquired',fi,selected)
            cam=selected['camera']; center=tuple(map(float,selected['camera_center'])); size=tuple(map(float,selected['camera_size']))
            pts=seed_features(cur[cam],center,size)
            if pts is not None and len(pts)>=MIN_FEATURES:
                active={'camera':cam,'center':center,'seed_center':center,'size':size,'points':pts,'gap_frames':0,'seed_frame':fi,'seed_confidence':float(selected.get('confidence',0))}
        elif active is not None:
            cam=active['camera']; pgray=prev[cam]; cgray=cur[cam]
            if pgray is None: terminate('missing_previous_frame',fi)
            elif active['gap_frames']>=max_frames: terminate('max_duration',fi)
            else:
                p0=active['points']
                p1,st1,err1=cv2.calcOpticalFlowPyrLK(pgray,cgray,p0,None,winSize=(17,17),maxLevel=3,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,0.01))
                if p1 is None or st1 is None: terminate('lk_forward_failed',fi)
                else:
                    p0r,st2,_=cv2.calcOpticalFlowPyrLK(cgray,pgray,p1,None,winSize=(17,17),maxLevel=3,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,0.01))
                    if p0r is None or st2 is None: terminate('lk_backward_failed',fi)
                    else:
                        fb=np.linalg.norm(p0r[:,0,:]-p0[:,0,:],axis=1); err=err1.reshape(-1) if err1 is not None else np.zeros(len(p0))
                        good=(st1.reshape(-1)==1)&(st2.reshape(-1)==1)&(fb<=FB_ERROR_PX)&(err<=LK_ERROR_MAX)
                        old=p0[good,0,:]; new=p1[good,0,:]
                        if len(new)<MIN_FEATURES: terminate('too_few_original_features',fi)
                        else:
                            disp=new-old; med=np.median(disp,axis=0); residual=np.linalg.norm(disp-med,axis=1); mad=float(np.median(residual)); step=float(np.linalg.norm(med))
                            if not np.isfinite(med).all() or step>MAX_STEP_PX or mad>MAX_DISPERSION_PX: terminate('unstable_flow',fi)
                            else:
                                cx=active['center'][0]+float(med[0])/w; cy=active['center'][1]+float(med[1])/h
                                cumulative=math.hypot((cx-active['seed_center'][0])*w,(cy-active['seed_center'][1])*h)
                                if cumulative>MAX_CUMULATIVE_DISPLACEMENT_PX: terminate('cumulative_displacement_gate',fi)
                                elif not (0<=cx<=1 and 0<=cy<=1): terminate('out_of_frame',fi)
                                else:
                                    x0,y0,x1,y1=roi_bounds((cx,cy),active['size'],w,h)
                                    local=new[(new[:,0]>=x0)&(new[:,0]<x1)&(new[:,1]>=y0)&(new[:,1]<y1)]
                                    # Critical v2 rule: never refresh features from the new patch.
                                    # If original seed support leaves the ball aperture, stop.
                                    if len(local)<MIN_FEATURES: terminate('original_support_left_aperture',fi)
                                    else:
                                        active['center']=(cx,cy); active['gap_frames']+=1; active['points']=local[:,None,:].astype(np.float32)
                                        if span is None: span={'start_frame':fi,'seed_frame':active['seed_frame'],'camera':cam,'seed_confidence':active['seed_confidence']}
                                        emitted.append({'frame_index':fi,'detection':{'camera':cam,'class_id':BALL_CLASS_ID,'confidence':SYNTHETIC_CONF,'center_x':cx,'center_y':cy,'width':active['size'][0],'height':active['size'][1]},'quality':{'features':int(len(local)),'fb_median_px':float(np.median(fb[good])),'lk_error_median':float(np.median(err[good])),'step_px':step,'dispersion_px':mad,'cumulative_displacement_px':cumulative,'bridge_age_frames':active['gap_frames']}})
        prev['Left']=cur['Left']; prev['Right']=cur['Right']
    if active is not None: terminate('window_end',target_frames)
    lc.release(); rc.release()
    with a.output.open('w') as f:
        for e in emitted: f.write(json.dumps(e,separators=(',',':'))+'\n')
    report={'version':'v2-conservative','fps':fps,'start_seconds':a.start_seconds,'duration_seconds':a.duration_seconds,'source_start_frame':source_start,'max_bridge_frames':max_frames,'max_bridge_seconds':MAX_BRIDGE_SECONDS,'synthetic_frames':len(emitted),'bridge_spans':spans,'termination_counts':terms,'parameters':{'min_features':MIN_FEATURES,'fb_error_px':FB_ERROR_PX,'lk_error_max':LK_ERROR_MAX,'max_step_px':MAX_STEP_PX,'max_dispersion_px':MAX_DISPERSION_PX,'max_cumulative_displacement_px':MAX_CUMULATIVE_DISPLACEMENT_PX,'min_seed_conf':MIN_SEED_CONF,'synthetic_conf':SYNTHETIC_CONF,'feature_refresh':False}}
    a.report.write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
