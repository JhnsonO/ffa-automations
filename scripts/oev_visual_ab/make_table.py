import json, math, csv, sys
sys.path.insert(0, "/root/oevrender")
from overlay import Cam
FPS = 60000/1001
cam = Cam(json.load(open("/root/oevsrc/match.json"))["params"]["cameraAxisOffset"])
S = [(t["yaw"], t["pitch"]) for f in ("rep_before.json","rep_after.json") for t in json.load(open(f))["sites"]["transitions"] if t["kind"]=="dormant"]
def load(p):
    det, ws, pose = {}, {}, {}
    for l in open(p):
        e = json.loads(l); i = e.get("frame_index")
        if e["kind"]=="detections_raw": det[i]=[d for d in e["detections"] if d["class_id"]==32 and d["position"]]
        elif e["kind"]=="world_state": ws[i]=e
        elif e["kind"]=="pose_presented": pose[i]=e["pose"]
    return det, ws, pose
def near_player(y,p,players):
    return min((math.degrees(math.hypot(y-q["yaw"],p-q["pitch"])) for q in players), default=999)
rows=[]
for arm,path in (("BEFORE","before_events.jsonl"),("AFTER","after_events.jsonl")):
    det,ws,pose=load(path)
    for i in sorted(ws):
        e=ws[i]; b=e["ball"]; pl=e["players"]; c=det.get(i,[])
        st=b["state"] if b else "None"
        on_static=bool(b) and any(math.hypot(b["yaw"]-y,b["pitch"]-q)<=0.0175 for y,q in S)
        dpl=round(near_player(b["yaw"],b["pitch"],pl),2) if b else ""
        # best alternative candidate: closest to a player, not the tracked one
        alts=[(near_player(d["position"]["yaw"],d["position"]["pitch"],pl),d) for d in c
              if not b or math.hypot(d["position"]["yaw"]-b["yaw"],d["position"]["pitch"]-b["pitch"])>0.01]
        alt=min(alts,key=lambda a:a[0]) if alts else None
        p=pose[i]
        rows.append({"arm":arm,"frame":i,"t_s":round(i/FPS,3),"tracker_state":st,
          "ball_yaw":round(b["yaw"],4) if b else "","ball_pitch":round(b["pitch"],4) if b else "",
          "ball_conf":round(b["confidence"],3) if b else "","ball_on_static_spot":on_static,
          "ball_deg_to_nearest_player":dpl,"n_ball_candidates":len(c),
          "best_other_candidate_deg_to_player":round(alt[0],2) if alt else "",
          "best_other_candidate_conf":round(alt[1]["confidence"],3) if alt else "",
          "cam_yaw":round(p["yaw"],4),"cam_pitch":round(p["pitch"],4),"cam_fov":round(p["fov_degrees"] or 0,2)})
with open("pack/oev_sample02_per_frame.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
print(len(rows))
