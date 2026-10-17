OEV sample_02 diagnostic pack (BEFORE = corrected timing, old spare-ball logic;
AFTER = + Phase 3). Same detections, calibration and camera settings in both.

frames/tNNNN.Ns.jpg  BEFORE (left) | AFTER (right) diagnostic broadcast frame
                     every 0.5 s for 10-24 s, 31-46 s, 114-144 s.
  Big ring = ball the AI follows: green Tracking, amber Coasting (holding last
  position), cyan Bridged (interpolated between real sightings).
  Small white rings = every ball detection that frame.
  Red ring "excluded" = spot the AI has ruled out as a stationary spare.

oev_sample02_per_frame.csv  one row per video frame per arm (59.94 fps):
  tracker_state, ball_yaw/pitch (rad), ball_conf,
  ball_on_static_spot (within 1 deg of any spot either arm excluded),
  ball_deg_to_nearest_player, n_ball_candidates,
  best_other_candidate_deg_to_player / _conf (the untracked detection closest
  to a player), cam_yaw/pitch/fov (presented camera pose).
Location fields are proxies; there are no human ball-identity labels.
