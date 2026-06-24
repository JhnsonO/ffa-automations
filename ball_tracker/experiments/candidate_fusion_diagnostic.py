#!/usr/bin/env python3
"""Candidate-fusion diagnostic — EXPERIMENT ONLY.

A fixed-sample, frame-level fusion test. It compares the raw highest-confidence
candidate against a transparent combined ranking using existing candidate fields,
pose lower-body proximity, static penalty, and temporal proximity to the current
tracklet observation. It writes review PNGs and CSV only; no pipeline state,
threshold or renderer is modified.
"""
from __future__ import annotations
import argparse, csv, importlib.util, json, math, os
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
POSE_PATH = ROOT / 'pose_guided_candidate_selection.py'
spec = importlib.util.spec_from_file_location('pose_helpers', POSE_PATH)
pose = importlib.util.module_from_spec(spec); spec.loader.exec_module(pose)

SAMPLE_IDS = ('T0001','T0025','T0093','T0080','T0079','T0036','T0130','T0030','T0090','T0175')
# Fixed diagnostic weights. No optimisation from labels; no automatic verdicts.
W = {'detector': .38, 'pose': .24, 'static': .14, 'temporal': .16, 'geometry': .08}
COL={'bg':(14,14,22),'white':(235,235,235),'dim':(145,145,160),'header':(38,42,72),'raw':(70,85,235),'fusion':(60,225,100),'track':(220,70,235),'all':(230,190,50),'person':(60,225,100),'lower':(255,205,55),'panel':(23,23,36)}

def clamp(v,lo=0.,hi=1.): return max(lo,min(hi,float(v)))
def fnt(n,b=False): return pose.font(n,b)
def raw(c): return pose.raw_conf(c)
def point(c,cy): return pose.candidate_point(c,cy)
def dist(a,b): return pose.angular_distance(a[0],a[1],b[0],b[1])

def geometry_score(c):
    b=pose.bbox(c)
    if not b: return None
    w,h=b[2]-b[0],b[3]-b[1]
    if w<=0 or h<=0:return None
    # compactness support only; not a learned football-size prior
    return clamp(1-abs(math.log((w/h)))/math.log(4))

def static_score(c):
    # Existing Stage 1 penalty is 1=neutral and lower for known hotspot/static risk.
    try:return clamp(float(c.get('penalty',1.0)))
    except:return 1.0

def temporal_score(c,obs):
    try:
        d=pose.angular_distance(float(c['yaw']),float(c['pitch']),float(obs['yaw']),float(obs['pitch']))
        return clamp(1-d/8.0)
    except:return None

def pose_score(c,people,cy):
    d=pose.lower_distance(point(c,cy),people)
    return None if d is None else clamp(1-d/pose.LOWER_BODY_RADIUS_PX)

def fusion(c,obs,people,cy):
    cues={'detector':clamp(raw(c)),'pose':pose_score(c,people,cy),'static':static_score(c),'temporal':temporal_score(c,obs),'geometry':geometry_score(c)}
    live=[(k,v) for k,v in cues.items() if v is not None]
    score=sum(W[k]*v for k,v in live)/sum(W[k] for k,_ in live) if live else 0.
    return score,cues

def same(a,b): return pose.same_candidate(a,b)
def draw_mark(im,p,col,txt): pose.draw_marker(im,p,col,txt)
def draw_people(im,people): pose.draw_people(im,people)

def select(cands,obs,people,cy):
    if not cands:return None,None,[], 'NO_CANDIDATES'
    raw_pick=max(cands,key=raw)
    rows=[]
    for c in cands:
        score,cues=fusion(c,obs,people,cy); rows.append({'candidate':c,'fusion':score,'cues':cues})
    fused=max(rows,key=lambda x:x['fusion'])['candidate']
    return raw_pick,fused,rows,'FUSED'

def selected_obs(t): return pose.selected_observations(t)

def render_page(t, recs):
    WID,HEI=1920,650; page=Image.new('RGB',(WID,HEI),COL['bg']);d=ImageDraw.Draw(page)
    d.rectangle([0,0,WID,58],fill=COL['header'])
    d.text((14,8),f"CANDIDATE FUSION DIAGNOSTIC — {t['id']}",fill=COL['white'],font=fnt(20,True))
    d.text((14,35),'Blue = raw detector choice | Green = fused choice | Magenta = existing tracklet | Yellow boxes = candidates | Yellow dots = lower body',fill=COL['dim'],font=fnt(13))
    for i,r in enumerate(recs):
        x0=i*640; small=cv2.resize(r['image'],(640,360),interpolation=cv2.INTER_AREA); page.paste(Image.fromarray(cv2.cvtColor(small,cv2.COLOR_BGR2RGB)),(x0,58))
        d.rectangle([x0+5,426,x0+635,620],fill=COL['panel']);obs=r['obs']
        d.text((x0+12,435),f"{r['phase']}  frame {obs['frame']}  candidates={r['candidate_count']} people={r['people_count']}",fill=COL['white'],font=fnt(16,True))
        d.text((x0+12,462),f"raw matches tracklet: {r['raw_match']} | fusion matches tracklet: {r['fusion_match']}",fill=COL['white'],font=fnt(14))
        d.text((x0+12,486),f"raw={r['raw_conf']:.3f} | fused={r['fusion_score']:.3f} | track lower-body={r['track_dist']}",fill=COL['white'],font=fnt(14))
        d.text((x0+12,512),f"fused cues: detector {r['cue_detector']} | pose {r['cue_pose']} | static {r['cue_static']} | temporal {r['cue_temporal']} | bbox {r['cue_geometry']}",fill=COL['dim'],font=fnt(12))
        d.text((x0+12,552),'Fusion is a ranked candidate suggestion only. It cannot accept/reject a ball or alter the tracker.',fill=COL['dim'],font=fnt(11))
    d.text((14,632),'Aerial/occluded frames retain detector/static/temporal/geometry evidence even where pose is unknown.',fill=COL['dim'],font=fnt(12))
    return page

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--stage1-candidates',required=True);ap.add_argument('--tracklets',required=True);ap.add_argument('--video',required=True);ap.add_argument('--output-dir',required=True);ap.add_argument('--pose-model',default='yolov8n-pose.pt');ap.add_argument('--pose-conf',type=float,default=.25);ap.add_argument('--pose-imgsz',type=int,default=960);ap.add_argument('--tracklet-ids',default=','.join(SAMPLE_IDS));ap.add_argument('--preflight',action='store_true');a=ap.parse_args()
    model=pose.load_pose(a.pose_model)
    if a.preflight:
        model.predict(np.zeros((64,64,3),dtype=np.uint8),imgsz=64,device='cpu',verbose=False);print('FUSION_PREFLIGHT_OK');return
    cdata=json.load(open(a.stage1_candidates));tdata=json.load(open(a.tracklets));by_frame={int(k):v for k,v in cdata.get('frames',{}).items()};by_id={t['id']:t for t in tdata.get('tracklets',[])};ids=[x.strip() for x in a.tracklet_ids.split(',') if x.strip()]
    missing=[x for x in ids if x not in by_id]
    if missing:raise SystemExit('FATAL missing tracklets: '+', '.join(missing))
    cap=cv2.VideoCapture(a.video)
    if not cap.isOpened():raise SystemExit('FATAL cannot open video')
    out=Path(a.output_dir);out.mkdir(parents=True,exist_ok=True);csvrows=[]
    for tid in ids:
        t=by_id[tid];recs=[];print('[fusion]',tid)
        for phase,obs in selected_obs(t):
            fi=int(obs['frame']);allc=by_frame.get(fi,[]);track=pose.match_tracklet_candidate(allc,obs);cy=int(round(float(track.get('crop_yaw')))) if track and track.get('crop_yaw') is not None else pose.nearest_crop_yaw(float(obs['yaw']))
            cands=[c for c in allc if c.get('crop_yaw') is None or int(round(float(c.get('crop_yaw'))))==cy]
            cap.set(cv2.CAP_PROP_POS_FRAMES,fi);ok,eq=cap.read()
            if not ok:raise SystemExit(f'FATAL unreadable frame {fi}')
            im=pose.extract_crop(eq,cy);people=pose.pose_people(model,im.copy(),a.pose_conf,a.pose_imgsz);rawpick,fusedpick,rows,mode=select(cands,obs,people,cy);draw_people(im,people)
            for c in cands:
                b=pose.bbox(c)
                if b:cv2.rectangle(im,tuple(map(int,b[:2])),tuple(map(int,b[2:])),COL['all'],1)
            draw_mark(im,point(rawpick,cy) if rawpick else None,COL['raw'],'RAW');draw_mark(im,point(fusedpick,cy) if fusedpick else None,COL['fusion'],'FUSION');draw_mark(im,point(track,cy) if track else pose.yp_to_pixel(float(obs['yaw']),float(obs['pitch']),cy),COL['track'],'TRACK')
            frow=next((x for x in rows if same(x['candidate'],fusedpick)),None);td=pose.lower_distance(point(track,cy),people) if track else None
            fmt=lambda x:'unknown' if x is None else f'{x:.2f}'
            rec={'phase':phase,'obs':obs,'image':im,'candidate_count':len(cands),'people_count':len(people),'raw_match':same(rawpick,track),'fusion_match':same(fusedpick,track),'raw_conf':raw(rawpick) if rawpick else 0.,'fusion_score':frow['fusion'] if frow else 0.,'track_dist':'unknown' if td is None else f'{td:.0f}px','cue_detector':fmt(frow['cues']['detector']) if frow else 'unknown','cue_pose':fmt(frow['cues']['pose']) if frow else 'unknown','cue_static':fmt(frow['cues']['static']) if frow else 'unknown','cue_temporal':fmt(frow['cues']['temporal']) if frow else 'unknown','cue_geometry':fmt(frow['cues']['geometry']) if frow else 'unknown'}
            recs.append(rec);csvrows.append({'tracklet_id':tid,'phase':phase.lower(),'frame':fi,'mode':mode,'raw_matches_tracklet':rec['raw_match'],'fusion_matches_tracklet':rec['fusion_match'],'raw_conf':rec['raw_conf'],'fusion_score':rec['fusion_score'],'people_count':len(people),'tracklet_lower_body_distance_px':td,**({f'fusion_{k}':v for k,v in (frow['cues'].items() if frow else {})})})
        render_page(t,recs).save(out/f'candidate_fusion_{tid}.png',format='PNG')
    cap.release()
    with open(out/'candidate_fusion_summary.csv','w',newline='') as f:w=csv.DictWriter(f,fieldnames=csvrows[0].keys());w.writeheader();w.writerows(csvrows)
    (out/'candidate_fusion_readme.txt').write_text('CANDIDATE FUSION DIAGNOSTIC\n\nFuses detector confidence, lower-body pose proximity, existing static penalty, proximity to the current tracklet observation, and bbox compactness. Fixed weights: detector .38, pose .24, static .14, temporal .16, geometry .08. This is diagnostic only: no candidate is automatically accepted/rejected, and no tracker file is modified.\n')
    print('WROTE',len(csvrows),'frame records')
if __name__=='__main__':main()
