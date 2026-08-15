from pathlib import Path

p = Path('.github/workflows/oev-zero-copy-diag.yml')
s = p.read_text()

old1 = """          winner = None
          for round_num in range(1, ROUNDS + 1):
"""
new1 = """          # EXP8 harness-only resilience: a candidate is not a winner until
          # the existing frozen GPU preflight passes. If a GPU model fails,
          # remove that model from later rounds so we do not repeatedly land
          # on the same broken NVDEC host class.
          failed_gpu_models = set()
          winner = None
          for round_num in range(1, ROUNDS + 1):
"""
if s.count(old1) != 1:
    raise SystemExit(f'failed_gpu_models insertion anchor count={s.count(old1)}')
s = s.replace(old1, new1, 1)

old2 = """              gpu_type_ids = GPU_TIER_ROUND1 if round_num == 1 else GPU_TIER_WIDE
              print(f'--- Round {round_num}/{ROUNDS} (EU-RO-1 pinned via volume, gpu_tier={\"preferred-4\" if round_num == 1 else \"all-8\"}) ---')
"""
new2 = """              base_gpu_type_ids = GPU_TIER_ROUND1 if round_num == 1 else GPU_TIER_WIDE
              gpu_type_ids = [g for g in base_gpu_type_ids if g not in failed_gpu_models]
              if not gpu_type_ids:
                  print(f'::error::Round {round_num}: every eligible GPU model was rejected by preflight')
                  continue
              print(f'--- Round {round_num}/{ROUNDS} (EU-RO-1 pinned via volume, gpu_tier={\"preferred-4\" if round_num == 1 else \"all-8\"}, excluded={sorted(failed_gpu_models)}) ---')
"""
if s.count(old2) != 1:
    raise SystemExit(f'gpu tier replacement anchor count={s.count(old2)}')
s = s.replace(old2, new2, 1)

old3 = """              try:
                  ip, port = wait_for_network(pod_id)
                  if not wait_for_ssh(ip, port):
                      raise RuntimeError(f'SSH never came up on pod {pod_id} ({ip}:{port})')
              except Exception as exc:
                  print(f'Round {round_num}: {exc} -- deleting pod and continuing to next round.')
                  delete_pod(pod_id)
                  continue

              winner = {'pod_id': pod_id, 'ip': ip, 'port': port}
              break
"""
new3 = """              try:
                  ip, port = wait_for_network(pod_id)
                  if not wait_for_ssh(ip, port):
                      raise RuntimeError(f'SSH never came up on pod {pod_id} ({ip}:{port})')

                  # Hard precondition for this frame-0 experiment. This is the
                  # exact existing runpod_gpu_preflight.sh from the checked-out
                  # ffa commit; no probe logic is duplicated or modified here.
                  scp = subprocess.run(
                      ['scp', '-i', '/tmp/runpod_key', '-P', str(port),
                       '-o', 'StrictHostKeyChecking=no', 'runpod_gpu_preflight.sh',
                       f'root@{ip}:/tmp/runpod_gpu_preflight.sh'],
                      capture_output=True, text=True, timeout=30,
                  )
                  if scp.returncode != 0:
                      raise RuntimeError(f'preflight upload failed: {scp.stderr[-500:]}')
                  subprocess.run(
                      ['ssh', '-i', '/tmp/runpod_key', '-p', str(port),
                       '-o', 'StrictHostKeyChecking=no', f'root@{ip}',
                       'chmod +x /tmp/runpod_gpu_preflight.sh'],
                      check=True, timeout=20,
                  )
                  preflight = subprocess.run(
                      ['ssh', '-i', '/tmp/runpod_key', '-p', str(port),
                       '-o', 'StrictHostKeyChecking=no', f'root@{ip}',
                       'stdbuf -oL -eL /tmp/runpod_gpu_preflight.sh'],
                      capture_output=True, text=True, timeout=180,
                  )
                  print(preflight.stdout, end='')
                  if preflight.stderr:
                      print(preflight.stderr, end='')
                  model = None
                  for line in preflight.stdout.splitlines():
                      if line.startswith('PREFLIGHT_GPU_MODEL='):
                          model = line.split('=', 1)[1].strip()
                          break
                  if preflight.returncode != 0 or 'PREFLIGHT_NVDEC=PASS' not in preflight.stdout:
                      if model:
                          failed_gpu_models.add(model)
                      raise RuntimeError(
                          f'GPU preflight rejected candidate: rc={preflight.returncode} '
                          f'model={model!r} nvdec_pass={\"PREFLIGHT_NVDEC=PASS\" in preflight.stdout}'
                      )
                  print(f'Candidate accepted by hard preflight: model={model!r} pod={pod_id}')
              except Exception as exc:
                  print(f'Round {round_num}: {exc} -- deleting pod and continuing to next round.')
                  delete_pod(pod_id)
                  continue

              winner = {'pod_id': pod_id, 'ip': ip, 'port': port}
              break
"""
if s.count(old3) != 1:
    raise SystemExit(f'preflight gate replacement anchor count={s.count(old3)}')
s = s.replace(old3, new3, 1)

p.write_text(s)
