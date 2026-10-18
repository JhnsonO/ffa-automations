#!/usr/bin/env python3
"""OEV-006 step 4: public multi-frame ball detector (WASB, soccer weights) on one window.

Runs on the GPU pod. Decodes [t0-pad, t1+pad] of each camera clip at full res, tiles the
pitch band, runs WASB (HRNet, 3-frame input, 512x288) on every frame with a sliding
3-frame window, and reports how often a ball is found inside our pitch outline.

Outputs (in --out): detections.jsonl, summary.json, overlay_*.jpg (20 left-camera frames).
"""
import argparse, json, os, subprocess, sys, time
import numpy as np

FPS = 60000 / 1001
IN_W, IN_H = 512, 288
TILE_W, TILE_H = 1280, 720
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def pip(x, y, poly):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'stream=width,height', '-of', 'csv=p=0', path], capture_output=True, text=True, check=True).stdout
    w, h = out.strip().split(',')[:2]
    return int(w), int(h)


def frames(path, t0, dur, w, h):
    cmd = ['ffmpeg', '-v', 'error', '-ss', f'{t0:.3f}', '-i', path, '-t', f'{dur:.3f}',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=w * h * 3 * 4)
    n = w * h * 3
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        yield np.frombuffer(b, np.uint8).reshape(h, w, 3)
    p.wait()


def tiles_for(w, h, top_frac):
    sx, sy = w / 3840, h / 2160
    tw, th = int(TILE_W * sx), int(TILE_H * sy)
    y0 = int(top_frac * h)
    ys = sorted({min(y0, h - th), h - th})
    xs = [0, (w - tw) // 2, w - tw]
    return [(x, y, tw, th) for y in ys for x in xs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--left'); ap.add_argument('--right')
    ap.add_argument('--roi'); ap.add_argument('--weights'); ap.add_argument('--wasb-src')
    ap.add_argument('--t0', type=float); ap.add_argument('--t1', type=float)
    ap.add_argument('--pad', type=float, default=3.0)
    ap.add_argument('--thr', type=float, default=0.5)
    ap.add_argument('--out')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    sys.path.insert(0, a.wasb_src)
    import torch
    from omegaconf import OmegaConf
    from scipy import ndimage
    from models import build_model

    cfg = OmegaConf.create({'model': OmegaConf.load(os.path.join(a.wasb_src, 'configs/model/wasb.yaml'))})
    model = build_model(cfg)
    ck = torch.load(a.weights, map_location='cpu', weights_only=False)
    model.load_state_dict(ck['model_state_dict'])
    model = model.cuda().eval().half()
    roi = json.load(open(a.roi))
    start = max(0.0, a.t0 - a.pad)
    dur = (a.t1 + a.pad) - start
    det_f = open(os.path.join(a.out, 'detections.jsonl'), 'w')
    per = {}
    keep = {}
    want = set(int(round(a.t0 * FPS)) + int(i * (a.t1 - a.t0) * FPS / 20) for i in range(20))
    t_start = time.time()
    for cam, path in (('left', a.left), ('right', a.right)):
        w, h = probe(path)
        top = min(p[1] for p in roi[cam]) - 0.08
        tl = tiles_for(w, h, max(0.0, top))
        f0 = int(round(start * FPS))
        buf = []
        hits = {}
        def run(fi, trio):
            batch = []
            for (x, y, tw, th) in tl:
                ims = []
                for fr in trio:
                    crop = fr[y:y + th, x:x + tw]
                    crop = torch.from_numpy(crop).cuda().permute(2, 0, 1).float().unsqueeze(0) / 255
                    crop = torch.nn.functional.interpolate(crop, size=(IN_H, IN_W), mode='bilinear', align_corners=False)[0]
                    m = torch.tensor(MEAN, device='cuda').view(3, 1, 1); s = torch.tensor(STD, device='cuda').view(3, 1, 1)
                    ims.append((crop - m) / s)
                batch.append(torch.cat(ims, 0))
            with torch.no_grad():
                out = model(torch.stack(batch).half())
            hm = out[0] if isinstance(out, (dict, list, tuple)) else out
            hm = hm[:, 1].float().sigmoid().cpu().numpy()  # middle frame of the 3
            found = []
            for k, (x, y, tw, th) in enumerate(tl):
                lab, n = ndimage.label(hm[k] > a.thr)
                for j in range(1, n + 1):
                    ys_, xs_ = np.nonzero(lab == j)
                    wts = hm[k][ys_, xs_]
                    cx = x + (xs_ * wts).sum() / wts.sum() * tw / IN_W
                    cy = y + (ys_ * wts).sum() / wts.sum() * th / IN_H
                    nx, ny = cx / w, cy / h
                    found.append({'cam': cam, 'frame': fi, 't': round(fi / FPS, 3), 'x': round(nx, 5), 'y': round(ny, 5),
                                  'score': round(float(wts.max()), 3), 'in_roi': pip(nx, ny, roi[cam])})
            return found
        for i, fr in enumerate(frames(path, start, dur, w, h)):
            buf.append(fr)
            if len(buf) > 3:
                buf.pop(0)
            if len(buf) == 3:
                fi = f0 + i - 1
                fd = run(fi, buf)
                for d in fd:
                    det_f.write(json.dumps(d) + '\n')
                hits[fi] = fd
                if cam == 'left' and fi in want:
                    keep[fi] = (buf[1].copy(), fd)
        per[cam] = hits
        print(f'{cam}: {len(hits)} frames, {sum(len(v) for v in hits.values())} detections, {time.time()-t_start:.0f}s', flush=True)
    det_f.close()
    lo, hi = int(round(a.t0 * FPS)), int(round(a.t1 * FPS))
    rng = range(lo, hi)
    def rate(cam, inside):
        return round(sum(any(d['in_roi'] == inside for d in per[cam].get(f, [])) for f in rng) / len(rng), 3)
    # alignment check: our-ball rate over shifted windows (clip frame offset is unverified)
    shifts = {}
    for s in range(-int(a.pad), int(a.pad) + 1):
        r2 = range(lo + int(s * FPS), hi + int(s * FPS))
        shifts[s] = round(sum(any(d['in_roi'] for d in per['left'].get(f, [])) for f in r2) / len(r2), 3)
    summ = {'t0': a.t0, 't1': a.t1, 'thr': a.thr, 'tiles_per_frame': len(tl), 'frames': len(rng),
            'our_ball_rate': {'left': rate('left', True), 'right': rate('right', True),
                              'either': round(sum(any(d['in_roi'] for c in per for d in per[c].get(f, [])) for f in rng) / len(rng), 3)},
            'foreign_ball_rate': {'left': rate('left', False), 'right': rate('right', False)},
            'left_our_ball_rate_by_shift_s': shifts,
            'yolo26m_reference': {'left': 0.25, 'right': 0.013, 'either': 0.259, 'source': 'run 39124520077 events, class 32 in St Margaret\'s ROI'},
            'runtime_s': round(time.time() - t_start)}
    json.dump(summ, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    print(json.dumps(summ, indent=1))
    import cv2
    for fi, (img, fd) in sorted(keep.items()):
        im = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        H, W = im.shape[:2]
        pts = np.array([[int(px * W), int(py * H)] for px, py in roi['left']], np.int32)
        cv2.polylines(im, [pts], True, (0, 255, 0), 3)
        for d in fd:
            c = (0, 255, 255) if d['in_roi'] else (0, 0, 255)
            cv2.circle(im, (int(d['x'] * W), int(d['y'] * H)), 28, c, 4)
            cv2.putText(im, f"{d['score']:.2f}", (int(d['x'] * W) + 30, int(d['y'] * H)), cv2.FONT_HERSHEY_SIMPLEX, 1.2, c, 3)
        cv2.putText(im, f't={fi/FPS:.1f}s WASB  yellow=in our pitch  red=outside', (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
        cv2.imwrite(os.path.join(a.out, f'overlay_{fi/FPS:06.1f}s.jpg'), cv2.resize(im, (W // 2, H // 2)), [cv2.IMWRITE_JPEG_QUALITY, 85])


if __name__ == '__main__':
    main()
