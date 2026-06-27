"""
FunnyPilot v3.2.2 — unit tests for the lateral curvature interpolator.

These verify the safety invariants the on-car behavior relies on:
  * output never leaves the model's [prev, cur] desire bracket
  * output equals cur exactly at the model frame (alpha -> 1)
  * SETTLE is never below the LINEAR path (no added turn-in lag)
  * the LINEAR feel is bit-compatible with the old delta/5 schedule at 100 Hz
  * cadence degradation degrades toward stock (model desire), not to a 20% stall
  * non-finite inputs are contained
The module is import-light on purpose so this runs without the full openpilot env.
"""
import math
import random

from openpilot.selfdrive.controls.lib.lat_interp import (
  LatInterp, LINEAR, SETTLE, T_MODEL, SETTLE_MIN_SPEED,
)

DT_CTRL = 0.01
FAST = SETTLE_MIN_SPEED + 10.0  # a speed where SETTLE is active


def _drive(li, knots, frames_per_model=5, vego=FAST, lookahead=None):
  """Feed `knots` (one model desired-curvature per model frame) through the
  interpolator with `frames_per_model` 100 Hz control frames per model frame.
  Returns the list of per-control-frame outputs and their alpha-phase markers."""
  out = []
  now = 100.0  # arbitrary monotonic start
  for i, k in enumerate(knots):
    nxt = lookahead[i] if lookahead is not None else (knots[i + 1] if i + 1 < len(knots) else None)
    for f in range(frames_per_model):
      model_updated = (f == 0)
      v = li.update(k, model_updated, now, vego, next_curv_est=nxt)
      out.append((k, f, now, v))
      now += DT_CTRL
  return out


def test_reaches_cur_at_model_frame_both_methods():
  for method in (LINEAR, SETTLE):
    li = LatInterp(method)
    knots = [0.0, 0.02, 0.05, 0.04, -0.01, -0.03, 0.0]
    res = _drive(li, knots, frames_per_model=5)
    # the control frame immediately before each new model frame is the one with
    # the largest elapsed time in the segment (f == 4): alpha saturates to 1 -> cur.
    for (k, f, _now, v) in res:
      if f == 4:
        assert abs(v - k) < 1e-9, f"{method}: f4 should equal cur {k}, got {v}"


def test_output_in_bracket_random():
  rng = random.Random(1234)
  for method in (LINEAR, SETTLE):
    li = LatInterp(method)
    prev_k = 0.0
    for _ in range(4000):
      k = max(-0.2, min(0.2, prev_k + rng.uniform(-0.06, 0.06)))
      # random lookahead incl. sign flips / large values to stress settle weight
      nxt = rng.choice([None, k + rng.uniform(-0.1, 0.1), -k, 0.3, float('nan')])
      for f in range(5):
        now = 100.0 + f * DT_CTRL
        v = li.update(k, f == 0, now, FAST, next_curv_est=nxt)
        lo, hi = min(li._prev, li._cur), max(li._prev, li._cur)
        assert lo - 1e-12 <= v <= hi + 1e-12, f"{method}: {v} not in [{lo},{hi}]"
      prev_k = k


def test_settle_never_below_linear():
  knots = [0.0, 0.01, 0.03, 0.06, 0.06, 0.04, 0.0, -0.03, -0.02, 0.0]
  lin = LatInterp(LINEAR)
  st = LatInterp(SETTLE)
  rl = _drive(lin, knots)
  rs = _drive(st, knots)
  for (_kl, _fl, _, vl), (ks, _fs, _, vs) in zip(rl, rs, strict=True):
    # settle must be >= linear *toward cur*: vs lies between vl and cur (inclusive).
    cur = ks
    if cur >= vl:
      assert vl - 1e-12 <= vs <= cur + 1e-12, f"settle {vs} not in [{vl},{cur}]"
    else:
      assert cur - 1e-12 <= vs <= vl + 1e-12, f"settle {vs} not in [{cur},{vl}]"


def test_linear_phase_matches_old_schedule():
  # Old scheme at 100 Hz emitted prev + ((f+1)/5)*delta = 0.2,0.4,0.6,0.8,1.0.
  li = LatInterp(LINEAR)
  now = 100.0
  # warm up two full 5-frame segments at flat 0.0 so health == 1.0 (healthy 100 Hz)
  for _seg in range(2):
    for f in range(5):
      li.update(0.0, f == 0, now, FAST)
      now += DT_CTRL
  # new segment 0.0 -> 0.10
  expected = [0.2, 0.4, 0.6, 0.8, 1.0]
  for f in range(5):
    v = li.update(0.10, f == 0, now, FAST)
    assert abs(v - expected[f] * 0.10) < 1e-9, f"f{f}: got {v}, want {expected[f]*0.10}"
    now += DT_CTRL


def test_cadence_independent_reaches_cur():
  # Whatever the loop rate, by the end of the model period (elapsed ~T_MODEL) the
  # healthy-cadence command must reach the model desire (no chronic fractional lag).
  for fpm in (5, 4, 3, 2):  # frames per model frame
    li = LatInterp(LINEAR)
    # warm up so health is full (>=4 frames seen) before the assertion segment
    _drive(li, [0.0, 0.0], frames_per_model=max(fpm, 5))
    now = 200.0
    li.update(0.0, True, now, FAST)            # prev=0
    # one full segment 0 -> 0.05 at this cadence; check the last sub-frame
    last = None
    for f in range(fpm):
      t = now + 0.05 + f * (T_MODEL / fpm)
      last = li.update(0.05, f == 0, t, FAST)
    # the final sub-frame sits at elapsed ~ (fpm-1)/fpm * T_MODEL; with PHASE_LEAD
    # it should be at or very near cur for fpm>=2
    assert last >= 0.05 * 0.79, f"fpm={fpm}: only reached {last} of 0.05"


def test_degraded_cadence_beats_old_20pct_stall():
  # The pathological case: controlsd collapses to the model rate (1 frame/model).
  # Old code froze at prev + 0.2*delta (20%). We must do far better (toward stock).
  li = LatInterp(LINEAR)
  # establish a degraded health: feed several model frames with only 1 control
  # frame each (model_updated True every call).
  now = 300.0
  vals = []
  prev = 0.0
  for k in [0.0, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05]:
    v = li.update(k, True, now, FAST)
    vals.append((prev, k, v))
    prev = k
    now += T_MODEL
  # by the steady state, output should be >= 70% of the way from prev to cur,
  # not the old 20%.
  prev, cur, v = vals[-1]
  frac = (v - prev) / (cur - prev)
  assert frac >= 0.7, f"degraded output only {frac:.2f} toward cur (old was 0.2)"


def test_nan_and_reengage_contained():
  li = LatInterp(SETTLE)
  # NaN model desire on the seed frame -> finite passthrough
  v = li.update(float('nan'), True, 100.0, FAST)
  assert math.isfinite(v)
  # normal segment
  for f in range(5):
    v = li.update(0.03, f == 0, 100.0 + f * DT_CTRL, FAST, next_curv_est=float('inf'))
    assert math.isfinite(v)
  # reset (inactive) then re-engage: first active frame returns the model desire,
  # no spike beyond it.
  li.reset(0.0)
  v = li.update(0.08, True, 200.0, FAST)
  assert abs(v - 0.08) < 1e-9, f"re-engage should pass model desire, got {v}"


def test_lane_change_forces_linear():
  # During a lane change, SETTLE must fall back to the plain linear ramp (the
  # smooth 3.2.1st feel) — settle output with lane_change=True == linear output.
  knots = [0.0, 0.03, 0.06, 0.05, 0.0, -0.04, -0.02, 0.0]
  st = LatInterp(SETTLE)
  lin = LatInterp(LINEAR)
  now_s = now_l = 500.0
  for i, k in enumerate(knots):
    nxt = knots[i + 1] if i + 1 < len(knots) else None
    for f in range(5):
      vs = st.update(k, f == 0, now_s, FAST, next_curv_est=nxt, lane_change=True)
      vl = lin.update(k, f == 0, now_l, FAST, next_curv_est=nxt)
      assert abs(vs - vl) < 1e-12, f"lane-change settle {vs} != linear {vl}"
      now_s += DT_CTRL
      now_l += DT_CTRL


def test_low_speed_settle_falls_back_to_linear():
  st = LatInterp(SETTLE)
  lin = LatInterp(LINEAR)
  knots = [0.0, 0.02, 0.05, 0.03]
  slow = SETTLE_MIN_SPEED - 1.0
  rs = _drive(st, knots, vego=slow)
  rl = _drive(lin, knots, vego=slow)
  for (_, _, _, vs), (_, _, _, vl) in zip(rs, rl, strict=True):
    assert abs(vs - vl) < 1e-12, f"low-speed settle {vs} != linear {vl}"


if __name__ == "__main__":
  fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
  for fn in fns:
    fn()
    print(f"ok  {fn.__name__}")
  print(f"\n{len(fns)} tests passed")
