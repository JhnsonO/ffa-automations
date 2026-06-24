#!/usr/bin/env python3
"""One-off visual benchmark; no production imports."""
import argparse,json,math,sys
from pathlib import Path
import cv2,numpy as np
from PIL import Image,ImageDraw
from huggingface_hub import hf_hub_download
from ultralytics import YOLO
BENCHMARK_TRACKLETS=["T0001","T0025","T0093","T0080","T0079","T0036","T0130","T0030","T0090","T0175"]
YAW=(0,90,180,270); W,H,FOV=1280,720,110.0; PANEL_W,PANEL_H=640,360; REPO="martinjolif/yolo-football-ball-detection"
def crop_to_equirect(frame,crop_yaw_deg):
 h,w=frame.shape[:2];f=(W/2)/math.tan(math.radians(FOV/2));x,y=np.meshgrid(np.linspace(0,W-1,W),np.linspace(0,H-1,H));rx=(x-W/2)/f;ry=-(y-H/2)/f;rz=np.ones_like(rx);n=np.sqrt(rx*rx+ry*ry+rz*rz);rx,ry,rz=rx/n,ry/n,rz/n;a=math.radians(crop_yaw_deg);wx=math.cos(a)*rx+math.sin(a)*rz;wz=-math.sin(a)*rx+math.cos(a)*rz;mx=((np.arctan2(wx,wz)/(2*math.pi))+.5)*w;my=(.5-np.arcsin(np.clip(ry,-1,1))/math.pi)*h;return cv2.remap(frame,mx.astype(np.float32),my.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP)
def pixel_to_yaw_pitch(x,y,crop_yaw_deg):
 f=(W/2)/math.tan(math.radians(FOV/2));rx=(x-W/2)/f;ry=-(y-H/2)/f;rz=1.;n=math.sqrt(rx*rx+ry*ry+rz*rz);rx,ry,rz=rx/n,ry/n,rz/n;a=math.radians(crop_yaw_deg);wx=math.cos(a)*rx+math.sin(a)*rz;wz=-math.sin(a)*rx+math.cos(a)*rz;return math.degrees(math.atan2(wx,wz)),math.degrees(math.asin(max(-1,min(1,ry))))
def frames(t):
 o=t.get('observations',t.get('frames',[]));a=sorted({int(v['frame'] if isinstance(v,dict) else v) for v in o});return a if len(a)<=3 else [a[round((len(a)-1)*q)] for q in (.1,.5,.9)]
def main():
 p=argparse.ArgumentParser();p.add_argument('--video',required=True);p.add_argument('--candidates',required=True);p.add_argument('--tracklets',required=True);p.add_argument('--output-dir',required=True);a=p.parse_args();out=Path(a.output_dir);out.mkdir(parents=True,exist_ok=True);(out/'contact_sheets').mkdir(exist_ok=True)
 try:
  m=YOLO(hf_hub_download(REPO,"yolo-football-ball-detection.pt"));names=m.names;name=names.get(0) if isinstance(names,dict) else names[0];assert str(name).lower()=='ball',f'class 0 is {name}'
 except Exception as e:
  (out/'run_summary.md').write_text(f'# Soft fail\n\nCheckpoint unavailable or invalid: `{e}`\n');print('SOFT_FAIL',e);return
 json.loads(Path(a.candidates).read_text());payload=json.loads(Path(a.tracklets).read_text());ts={str(t['id']):t for t in payload.get('tracklets',payload)};cap=cv2.VideoCapture(a.video);allc=[];seen=set();counts={}
 for tid in BENCHMARK_TRACKLETS:
  rows=[];counts[tid]=0
  for fr in frames(ts.get(tid,{})):
   if not ts.get(tid):continue
   cap.set(cv2.CAP_PROP_POS_FRAMES,fr);ok,eq=cap.read()
   if not ok:raise RuntimeError(f'cannot read {fr}')
   seen.add(fr);panels=[]
   for yaw in YAW:
    crop=crop_to_equirect(eq,yaw);r=m.predict(crop,conf=.15,imgsz=1280,verbose=False)[0];ds=[]
    if r.boxes is not None:
     for b,c,k in zip(r.boxes.xyxy.cpu().numpy(),r.boxes.conf.cpu().numpy(),r.boxes.cls.cpu().numpy().astype(int)):
      if k!=0:continue
      x1,y1,x2,y2=map(float,b);sy,sp=pixel_to_yaw_pitch((x1+x2)/2,(y1+y2)/2,yaw);d={'frame':fr,'crop_yaw':float(yaw),'bbox_xyxy':[x1,y1,x2,y2],'football_conf':float(c),'yaw':sy,'pitch':sp,'source':'modern_yolo'};allc.append(d);ds.append(d);counts[tid]+=1
    im=Image.fromarray(cv2.cvtColor(cv2.resize(crop,(PANEL_W,PANEL_H)),cv2.COLOR_BGR2RGB));dr=ImageDraw.Draw(im);best=max(ds,key=lambda z:z['football_conf'],default=None)
    if best:
     x1,y1,x2,y2=best['bbox_xyxy'];cx=((x1+x2)/2)*(PANEL_W/W);cy=((y1+y2)/2)*(PANEL_H/H);radius=max(6,round(max(x2-x1,y2-y1)*max(PANEL_W/W,PANEL_H/H)/2));dr.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),outline='lime',width=3);dr.text((8,8),f"{best['football_conf']:.2f}",fill='white')
    else:dr.text((8,8),'×',fill='red')
    dr.text((8,PANEL_H-18),f'{tid} | frame {fr} | yaw {yaw}°',fill='white');panels.append(im)
   rows.append(panels)
  if rows:
   sh=Image.new('RGB',(PANEL_W*4,PANEL_H*len(rows)),'black')
   for i,row in enumerate(rows):
    for j,im in enumerate(row):sh.paste(im,(PANEL_W*j,PANEL_H*i))
   sh.save(out/'contact_sheets'/f'{tid}.png')
 cap.release();json.dump({'checkpoint':REPO,'conf_threshold':.15,'benchmark_tracklets':BENCHMARK_TRACKLETS,'frames_sampled':sorted(seen),'candidates':allc},open(out/'modern_yolo_candidates.json','w'),indent=2);json.dump({'checkpoint':REPO,'tracklets_sampled':10,'frames_sampled':len(seen),'total_detections':len(allc),'detections_per_tracklet':counts,'notes':'Visual inspection required. No automatic pass/fail.'},open(out/'benchmark_manifest.json','w'),indent=2);(out/'run_summary.md').write_text('# Modern YOLO benchmark\n\nCheckpoint loaded; class 0 verified as ball. Visual inspection required.\n')
if __name__=='__main__':main()

