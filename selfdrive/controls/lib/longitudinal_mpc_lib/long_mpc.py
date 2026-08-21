#!/usr/bin/env python3
import os
import time
import numpy as np
from cereal import log
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.common.realtime import DT_MDL
from openpilot.common.swaglog import cloudlog
# WARNING: imports outside of constants will not trigger a rebuild
from openpilot.selfdrive.modeld.constants import index_function
from openpilot.selfdrive.controls.radard import _LEAD_ACCEL_TAU
from openpilot.selfdrive.controls.lib.lead_physics import LeadDecelBelief, stopped_equivalence_distance

if __name__ == '__main__':  # generating code
  from openpilot.third_party.acados.acados_template import AcadosModel, AcadosOcp, AcadosOcpSolver
else:
  from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.c_generated_code.acados_ocp_solver_pyx import AcadosOcpSolverCython

from casadi import SX, vertcat

MODEL_NAME = 'long'
LONG_MPC_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(LONG_MPC_DIR, "c_generated_code")
JSON_FILE = os.path.join(LONG_MPC_DIR, "acados_ocp_long.json")

LongitudinalPlanSource = log.LongitudinalPlan.LongitudinalPlanSource
MPC_SOURCES = (LongitudinalPlanSource.lead0, LongitudinalPlanSource.lead1, LongitudinalPlanSource.cruise)

X_DIM = 3
U_DIM = 1
PARAM_DIM = 6
COST_E_DIM = 5
COST_DIM = COST_E_DIM + 1
CONSTR_DIM = 4

X_EGO_OBSTACLE_COST = 3.
X_EGO_COST = 0.
V_EGO_COST = 0.
A_EGO_COST = 0.
J_EGO_COST = 5.
A_CHANGE_COST = 200.
DANGER_ZONE_COST = 100.
CRASH_DISTANCE = .25
LEAD_DANGER_FACTOR = 0.75
LIMIT_COST = 1e6
ACADOS_SOLVER_TYPE = 'SQP_RTI'

# Fewer timestamps don't hurt performance and lead to
# much better convergence of the MPC with low iterations
N = 12
MAX_T = 10.0
T_IDXS_LST = [index_function(idx, max_val=MAX_T, max_idx=N) for idx in range(N+1)]

T_IDXS = np.array(T_IDXS_LST)
FCW_IDXS = T_IDXS < 5.0
T_DIFFS = np.diff(T_IDXS, prepend=[0.])
# FunnyPilot v3.2.6e: first-principles longitudinal constants.
# COMFORT_BRAKE is the planning deceleration assumed for the safe-distance
# envelope: 2.2 m/s^2 sits between stock (2.5) and the old fork value (2.0),
# giving earlier-than-stock brake initiation without the separate
# closing-rate obstacle inflation hack that double-counted braking distance.
COMFORT_BRAKE = 2.2
# Standstill gap to a stopped lead. 7.5 m is roomier than stock (6.0) without
# the cut-in-inviting 11 m gap of 3.2.5st.
STOP_DISTANCE = 7.5
# FunnyPilot v3.6.5 — -1.2 -> -1.6. THE CEILING ON WHAT A FALLING CRUISE TARGET
# CAN COMMAND, and it was set exactly where it could not work.
#
# It is used on line ~395 to clip `v_cruise` to `v_ego + T_IDXS * CRUISE_MIN_ACCEL
# * 1.05` before the cruise obstacle is built, so the MPC NEVER SEES a cruise
# target falling faster than 1.26 m/s^2 however far under the governors put it.
# SCC-M v2's approach envelope asks for 1.20 m/s^2 in its last 60 m — 95% of
# that ceiling. So the car had ~5% headroom and therefore NO ABILITY TO CATCH UP
# once it fell behind the envelope for any reason (a stale corner distance, the
# cap's own EMA, a radius read slightly loose). It never recovered, and arrived
# at the bend above the speed the cap had been asking for since 400 m out. That
# is the reported "doesn't have enough authority to slow down enough".
#
# v3.6.6 TAKES IT TO -2.0. The v3.6.5 pair (-1.6 against a 1.35 envelope) closed
# the gap but not the symptom: "the CAP values feel fair, but we're rarely ever
# actually going that speed through the corner — we're usually gas gating or
# slightly braking to decelerate prior to/through the corner." That is the car
# still SHEDDING speed at the apex, i.e. permanently behind a schedule it can
# only just follow. 2.0 * 1.05 = 2.10 against a 1.60 envelope is 24% margin
# with a firmer finish, which is what lets the car actually ARRIVE at the cap
# instead of chasing it. Still well inside comfort: COMFORT_BRAKE is 2.2 and
# ACCEL_MIN far lower.
#
# -1.6 restored ~40% headroom over the same, UNCHANGED envelope. Note what this
# is and is not: the clip is PERMISSIVE, not a demand — it bounds what the MPC
# is allowed to be told, and the solver's own cost still decides the shape. So
# this removes an artificial ceiling rather than commanding harder braking, and
# nothing that was not already asking to slow down slows down differently.
#
# STILL WELL INSIDE COMFORT: COMFORT_BRAKE above is 2.2 and ACCEL_MIN far lower.
# SLA is UNAFFECTED — its RATE_MAX is 1.2 m/s per second in its own right, so
# this simply stops being the binding constraint on a ramp that never used it.
CRUISE_MIN_ACCEL = -2.0
CRUISE_MAX_ACCEL = 1.6
MIN_X_LEAD_FACTOR = 0.5

# FunnyPilot v3.2.6e: follow distance is a constant TIME headway per
# personality — the quantity that is actually invariant in human following —
# instead of the 3.2.5st speed-indexed tables (3.75 s at city speed shrinking
# to 1.5 s at highway speed, i.e. most cautious where risk is lowest). A small
# additive cushion below city speeds absorbs stop-and-go noise.
_T_FOLLOW_AGGRESSIVE = 1.25
_T_FOLLOW_STANDARD = 1.60
_T_FOLLOW_RELAXED = 2.05
_LOW_SPEED_HEADWAY_CUSHION = 0.35  # s, fully applied below 4 m/s, gone by 14 m/s

def get_jerk_factor(personality=log.LongitudinalPersonality.standard):
  if personality==log.LongitudinalPersonality.relaxed:
    return 2.0
  elif personality==log.LongitudinalPersonality.standard:
    return 1.0
  elif personality==log.LongitudinalPersonality.aggressive:
    return 0.5
  else:
    raise NotImplementedError("Longitudinal personality not supported")


def get_T_FOLLOW(personality=log.LongitudinalPersonality.standard, v_ego=None):
  if personality == log.LongitudinalPersonality.relaxed:
    t_follow = _T_FOLLOW_RELAXED
  elif personality == log.LongitudinalPersonality.standard:
    t_follow = _T_FOLLOW_STANDARD
  elif personality == log.LongitudinalPersonality.aggressive:
    t_follow = _T_FOLLOW_AGGRESSIVE
  else:
    raise NotImplementedError("Longitudinal personality not supported")
  if v_ego is not None:
    t_follow += _LOW_SPEED_HEADWAY_CUSHION * float(np.interp(v_ego, [4., 14.], [1., 0.]))
  return float(t_follow)

def get_stopped_equivalence_factor(v_lead, decel: float = COMFORT_BRAKE):
  """FunnyPilot v3.5.5 — `decel` is now the deceleration we believe the LEAD is
  running, not unconditionally our own comfort rate. See lead_physics.py for
  the derivation; the default keeps every existing call site byte-identical."""
  return stopped_equivalence_distance(v_lead, decel)

def get_safe_obstacle_distance(v_ego, t_follow):
  return (v_ego**2) / (2 * COMFORT_BRAKE) + t_follow * v_ego + STOP_DISTANCE

def gen_long_model():
  model = AcadosModel()
  model.name = MODEL_NAME

  # states
  x_ego, v_ego, a_ego = SX.sym('x_ego'), SX.sym('v_ego'), SX.sym('a_ego')
  model.x = vertcat(x_ego, v_ego, a_ego)

  # controls
  j_ego = SX.sym('j_ego')
  model.u = vertcat(j_ego)

  # xdot
  x_ego_dot = SX.sym('x_ego_dot')
  v_ego_dot = SX.sym('v_ego_dot')
  a_ego_dot = SX.sym('a_ego_dot')
  model.xdot = vertcat(x_ego_dot, v_ego_dot, a_ego_dot)

  # live parameters
  a_min = SX.sym('a_min')
  a_max = SX.sym('a_max')
  x_obstacle = SX.sym('x_obstacle')
  a_prev = SX.sym('a_prev')
  lead_t_follow = SX.sym('lead_t_follow')
  lead_danger_factor = SX.sym('lead_danger_factor')
  model.p = vertcat(a_min, a_max, x_obstacle, a_prev, lead_t_follow, lead_danger_factor)

  # dynamics model
  f_expl = vertcat(v_ego, a_ego, j_ego)
  model.f_impl_expr = model.xdot - f_expl
  model.f_expl_expr = f_expl
  return model

def gen_long_ocp():
  ocp = AcadosOcp()
  ocp.model = gen_long_model()

  Tf = T_IDXS[-1]

  # set dimensions
  ocp.dims.N = N

  # set cost module
  ocp.cost.cost_type = 'NONLINEAR_LS'
  ocp.cost.cost_type_e = 'NONLINEAR_LS'

  QR = np.zeros((COST_DIM, COST_DIM))
  Q = np.zeros((COST_E_DIM, COST_E_DIM))

  ocp.cost.W = QR
  ocp.cost.W_e = Q

  x_ego, v_ego, a_ego = ocp.model.x[0], ocp.model.x[1], ocp.model.x[2]
  j_ego = ocp.model.u[0]

  a_min, a_max = ocp.model.p[0], ocp.model.p[1]
  x_obstacle = ocp.model.p[2]
  a_prev = ocp.model.p[3]
  lead_t_follow = ocp.model.p[4]
  lead_danger_factor = ocp.model.p[5]

  ocp.cost.yref = np.zeros((COST_DIM, ))
  ocp.cost.yref_e = np.zeros((COST_E_DIM, ))

  desired_dist_comfort = get_safe_obstacle_distance(v_ego, lead_t_follow)

  # The main cost in normal operation is how close you are to the "desired" distance
  # from an obstacle at every timestep. This obstacle can be a lead car
  # or other object. In e2e mode we can use x_position targets as a cost
  # instead.
  costs = [((x_obstacle - x_ego) - (desired_dist_comfort)) / (v_ego + 10.),
           x_ego,
           v_ego,
           a_ego,
           a_ego - a_prev,
           j_ego]
  ocp.model.cost_y_expr = vertcat(*costs)
  ocp.model.cost_y_expr_e = vertcat(*costs[:-1])

  # Constraints on speed, acceleration and desired distance to
  # the obstacle, which is treated as a slack constraint so it
  # behaves like an asymmetrical cost.
  constraints = vertcat(v_ego,
                        (a_ego - a_min),
                        (a_max - a_ego),
                        ((x_obstacle - x_ego) - lead_danger_factor * (desired_dist_comfort)) / (v_ego + 10.))
  ocp.model.con_h_expr = constraints

  x0 = np.zeros(X_DIM)
  ocp.constraints.x0 = x0
  ocp.parameter_values = np.array([-1.2, 1.2, 0.0, 0.0, get_T_FOLLOW(), LEAD_DANGER_FACTOR])


  # We put all constraint cost weights to 0 and only set them at runtime
  cost_weights = np.zeros(CONSTR_DIM)
  ocp.cost.zl = cost_weights
  ocp.cost.Zl = cost_weights
  ocp.cost.Zu = cost_weights
  ocp.cost.zu = cost_weights

  ocp.constraints.lh = np.zeros(CONSTR_DIM)
  ocp.constraints.uh = 1e4*np.ones(CONSTR_DIM)
  ocp.constraints.idxsh = np.arange(CONSTR_DIM)

  # The HPIPM solver can give decent solutions even when it is stopped early
  # Which is critical for our purpose where compute time is strictly bounded
  # We use HPIPM in the SPEED_ABS mode, which ensures fastest runtime. This
  # does not cause issues since the problem is well bounded.
  ocp.solver_options.qp_solver = 'PARTIAL_CONDENSING_HPIPM'
  ocp.solver_options.hessian_approx = 'GAUSS_NEWTON'
  ocp.solver_options.integrator_type = 'ERK'
  ocp.solver_options.nlp_solver_type = ACADOS_SOLVER_TYPE
  ocp.solver_options.qp_solver_cond_N = 1

  # More iterations take too much time and less lead to inaccurate convergence in
  # some situations. Ideally we would run just 1 iteration to ensure fixed runtime.
  ocp.solver_options.qp_solver_iter_max = 10
  ocp.solver_options.qp_tol = 1e-3

  # set prediction horizon
  ocp.solver_options.tf = Tf
  ocp.solver_options.shooting_nodes = T_IDXS

  ocp.code_export_directory = EXPORT_DIR
  return ocp


class LongitudinalMpc:
  def __init__(self, dt=DT_MDL):
    self.dt = dt
    # FunnyPilot v3.3.8: MPC modes restored (deleted in the v3.2.6e rewrite,
    # which made experimental/E2E long a pure min() clamp that could never
    # accelerate). 'acc' = the fork's validated cruise-obstacle formulation,
    # byte-identical to v3.2.6e..v3.3.6. 'blended' = upstream's e2e blend:
    # the MPC tracks the model's x/v/a trajectory (yref) with the cruise
    # target as a position cap — this is what makes experimental mode
    # actually drive the car, and the cap is what keeps the fork's speed
    # governors (SCC-V/M, SLA, hidden offset) binding in E2E. Runtime-only
    # (weights/params/yref) — the prebuilt aarch64 solver is unchanged.
    self.mode = 'acc'
    # v3.5.5: one belief per tracked lead. Separate instances on purpose --
    # sharing one filter would let lead1's accel bleed into lead0's obstacle,
    # and lead0 is the one that is usually braking us.
    self._lead_decel = [LeadDecelBelief(COMFORT_BRAKE, dt), LeadDecelBelief(COMFORT_BRAKE, dt)]
    self.solver = AcadosOcpSolverCython(MODEL_NAME, ACADOS_SOLVER_TYPE, N)
    self.reset()
    self.source = LongitudinalPlanSource.cruise

  def reset(self):
    self.solver.reset()

    self.x_sol = np.zeros((N+1, X_DIM))
    self.u_sol = np.zeros((N, 1))
    self.v_solution = np.zeros(N+1)
    self.a_solution = np.zeros(N+1)
    self.j_solution = np.zeros(N)
    self.a_prev = np.array(self.a_solution)
    self.yref = np.zeros((N+1, COST_DIM))

    for i in range(N):
      self.solver.cost_set(i, "yref", self.yref[i])
    self.solver.cost_set(N, "yref", self.yref[N][:COST_E_DIM])

    self.params = np.zeros((N+1, PARAM_DIM))
    for i in range(N+1):
      self.solver.set(i, 'x', np.zeros(X_DIM))

    self.last_cloudlog_t = 0
    self.status = False
    self.crash_cnt = 0.0
    self.solution_status = 0
    # timers
    self.solve_time = 0.0
    self.time_qp_solution = 0.0
    self.time_linearization = 0.0
    self.time_integrator = 0.0
    self.x0 = np.zeros(X_DIM)
    self.set_weights()

  def set_cost_weights(self, cost_weights, constraint_cost_weights):
    W = np.asfortranarray(np.diag(cost_weights))
    for i in range(N):
      # TODO don't hardcode A_CHANGE_COST idx
      # reduce the cost on (a-a_prev) later in the horizon.
      W[4,4] = cost_weights[4] * np.interp(T_IDXS[i], [0.0, 1.0, 2.0], [1.0, 1.0, 0.0])
      self.solver.cost_set(i, 'W', W)
    # Setting the slice without the copy make the array not contiguous,
    # causing issues with the C interface.
    self.solver.cost_set(N, 'W', np.copy(W[:COST_E_DIM, :COST_E_DIM]))

    # Set L2 slack cost on lower bound constraints
    Zl = np.array(constraint_cost_weights)
    for i in range(N):
      self.solver.cost_set(i, 'Zl', Zl)

  def set_weights(self, prev_accel_constraint=True, personality=log.LongitudinalPersonality.standard):
    jerk_factor = get_jerk_factor(personality)
    if self.mode == 'acc':
      a_change_cost = A_CHANGE_COST if prev_accel_constraint else 0
      cost_weights = [X_EGO_OBSTACLE_COST, X_EGO_COST, V_EGO_COST, A_EGO_COST, jerk_factor * a_change_cost, jerk_factor * J_EGO_COST]
      constraint_cost_weights = [LIMIT_COST, LIMIT_COST, LIMIT_COST, DANGER_ZONE_COST]
    elif self.mode == 'blended':
      # upstream's e2e-tracking cost set, verbatim (personality jerk factor is
      # intentionally not applied here — the model's plan owns the shape)
      a_change_cost = 40.0 if prev_accel_constraint else 0
      cost_weights = [0., 0.1, 0.2, 5.0, a_change_cost, 1.0]
      constraint_cost_weights = [LIMIT_COST, LIMIT_COST, LIMIT_COST, DANGER_ZONE_COST]
    else:
      raise NotImplementedError(f'Planner mode {self.mode} not recognized in planner cost set')
    self.set_cost_weights(cost_weights, constraint_cost_weights)

  def set_cur_state(self, v, a):
    v_prev = self.x0[1]
    self.x0[1] = v
    self.x0[2] = a
    if abs(v_prev - v) > 2.:  # probably only helps if v < v_prev
      for i in range(N+1):
        self.solver.set(i, 'x', self.x0)

  @staticmethod
  def extrapolate_lead(x_lead, v_lead, a_lead, a_lead_tau):
    a_lead_traj = a_lead * np.exp(-a_lead_tau * (T_IDXS**2)/2.)
    v_lead_traj = np.clip(v_lead + np.cumsum(T_DIFFS * a_lead_traj), 0.0, 1e8)
    x_lead_traj = x_lead + np.cumsum(T_DIFFS * v_lead_traj)
    lead_xv = np.column_stack((x_lead_traj, v_lead_traj))
    return lead_xv

  def process_lead(self, lead):
    v_ego = self.x0[1]
    if lead is not None and lead.status:
      x_lead = lead.dRel
      v_lead = lead.vLead
      a_lead = lead.aLeadK
      a_lead_tau = lead.aLeadTau
    else:
      # Fake a fast lead car, so mpc can keep running in the same mode
      x_lead = 50.0
      v_lead = v_ego + 10.0
      a_lead = 0.0
      a_lead_tau = _LEAD_ACCEL_TAU

    # MPC will not converge if immediate crash is expected
    # Clip lead distance to what is still possible to brake for
    min_x_lead = MIN_X_LEAD_FACTOR * (v_ego + v_lead) * (v_ego - v_lead) / (-ACCEL_MIN * 2)
    x_lead = np.clip(x_lead, min_x_lead, 1e8)
    v_lead = np.clip(v_lead, 0.0, 1e8)
    a_lead = np.clip(a_lead, -10., 5.)
    lead_xv = self.extrapolate_lead(x_lead, v_lead, a_lead, a_lead_tau)
    return lead_xv

  def update(self, radarstate, v_cruise, x, v, a, j, personality=log.LongitudinalPersonality.standard):
    v_ego = self.x0[1]
    # FunnyPilot: Speed-dependent follow distance
    t_follow = get_T_FOLLOW(personality, v_ego=v_ego)
    self.status = radarstate.leadOne.status or radarstate.leadTwo.status

    lead_xv_0 = self.process_lead(radarstate.leadOne)
    lead_xv_1 = self.process_lead(radarstate.leadTwo)

    # To estimate a safe distance from a moving lead, we calculate how much stopping
    # distance that lead needs as a minimum. We can add that to the current distance
    # and then treat that as a stopped car/obstacle at this new distance.
    # v3.5.5: how far each lead needs to stop is computed from the decel that
    # lead is ACTUALLY running, floored at COMFORT_BRAKE. A lead that is
    # coasting or easing off returns exactly the old number, so ordinary
    # following is unchanged; a lead braking hard places its obstacle closer,
    # so we begin braking earlier instead of catching up later. See
    # lead_physics.py -- the old constant was short by ~9 m at 20 m/s.
    decel_0 = self._lead_decel[0].update(radarstate.leadOne.aLeadK, bool(radarstate.leadOne.status))
    decel_1 = self._lead_decel[1].update(radarstate.leadTwo.aLeadK, bool(radarstate.leadTwo.status))
    lead_0_obstacle = lead_xv_0[:,0] + get_stopped_equivalence_factor(lead_xv_0[:,1], decel_0)
    lead_1_obstacle = lead_xv_1[:,0] + get_stopped_equivalence_factor(lead_xv_1[:,1], decel_1)

    self.params[:,0] = ACCEL_MIN
    self.params[:,1] = ACCEL_MAX

    if self.mode == 'acc':
      # FunnyPilot v3.2.6e..v3.3.6 validated path, unchanged.
      self.params[:,5] = LEAD_DANGER_FACTOR

      # Fake an obstacle for cruise, this ensures smooth acceleration to set speed
      # when the leads are no factor.
      v_lower = v_ego + (T_IDXS * CRUISE_MIN_ACCEL * 1.05)
      # TODO does this make sense when max_a is negative?
      v_upper = v_ego + (T_IDXS * CRUISE_MAX_ACCEL * 1.05)
      v_cruise_clipped = np.clip(v_cruise * np.ones(N+1), v_lower, v_upper)
      cruise_obstacle = np.cumsum(T_DIFFS * v_cruise_clipped) + get_safe_obstacle_distance(v_cruise_clipped, t_follow)

      x_obstacles = np.column_stack([lead_0_obstacle, lead_1_obstacle, cruise_obstacle])
      self.source = MPC_SOURCES[np.argmin(x_obstacles[0])]

      # The model trajectory is not tracked in ACC mode
      x[:], v[:], a[:], j[:] = 0.0, 0.0, 0.0, 0.0

    elif self.mode == 'blended':
      # FunnyPilot v3.3.8: upstream's e2e-tracking branch, restored. The MPC
      # follows the model's plan, with the (governed!) v_cruise as a position
      # cap — SCC-V/M, SLA and the hidden governor keep binding in E2E.
      self.params[:,5] = 1.0

      x_obstacles = np.column_stack([lead_0_obstacle,
                                     lead_1_obstacle])
      cruise_target = T_IDXS * np.clip(v_cruise, v_ego - 2.0, 1e3) + x[0]
      xforward = ((v[1:] + v[:-1]) / 2) * (T_IDXS[1:] - T_IDXS[:-1])
      x = np.cumsum(np.insert(xforward, 0, x[0]))

      x_and_cruise = np.column_stack([x, cruise_target])
      x = np.min(x_and_cruise, axis=1)

      self.source = LongitudinalPlanSource.e2e if x_and_cruise[1,0] < x_and_cruise[1,1] else LongitudinalPlanSource.cruise

    else:
      raise NotImplementedError(f'Planner mode {self.mode} not recognized in planner update')

    self.yref[:,1] = x
    self.yref[:,2] = v
    self.yref[:,3] = a
    self.yref[:,5] = j
    for i in range(N):
      self.solver.set(i, "yref", self.yref[i])
    self.solver.set(N, "yref", self.yref[N][:COST_E_DIM])

    self.params[:,2] = np.min(x_obstacles, axis=1)
    self.params[:,3] = np.copy(self.a_prev)
    self.params[:,4] = t_follow

    self.run()
    if (np.any(lead_xv_0[FCW_IDXS,0] - self.x_sol[FCW_IDXS,0] < CRASH_DISTANCE) and
            radarstate.leadOne.modelProb > 0.9):
      self.crash_cnt += 1
    else:
      self.crash_cnt = 0

    # In blended mode, report the lead as the source once the solution is
    # inside a lead's comfort range, so LeadGrace/UI see real following.
    if self.mode == 'blended':
      if np.any((lead_0_obstacle - get_safe_obstacle_distance(self.x_sol[:,1], t_follow)) - self.x_sol[:,0] < 0.0):
        self.source = LongitudinalPlanSource.lead0
      if np.any((lead_1_obstacle - get_safe_obstacle_distance(self.x_sol[:,1], t_follow)) - self.x_sol[:,0] < 0.0) and \
         (lead_1_obstacle[0] - lead_0_obstacle[0]) < 0.0:
        self.source = LongitudinalPlanSource.lead1

  def run(self):
    for i in range(N+1):
      self.solver.set(i, 'p', self.params[i])
    self.solver.constraints_set(0, "lbx", self.x0)
    self.solver.constraints_set(0, "ubx", self.x0)

    self.solution_status = self.solver.solve()
    self.solve_time = float(self.solver.get_stats('time_tot')[0])
    self.time_qp_solution = float(self.solver.get_stats('time_qp')[0])
    self.time_linearization = float(self.solver.get_stats('time_lin')[0])
    self.time_integrator = float(self.solver.get_stats('time_sim')[0])

    for i in range(N+1):
      self.x_sol[i] = self.solver.get(i, 'x')
    for i in range(N):
      self.u_sol[i] = self.solver.get(i, 'u')

    self.v_solution = self.x_sol[:,1]
    self.a_solution = self.x_sol[:,2]
    self.j_solution = self.u_sol[:,0]

    self.a_prev = np.interp(T_IDXS + self.dt, T_IDXS, self.a_solution)

    t = time.monotonic()
    if self.solution_status != 0:
      if t > self.last_cloudlog_t + 5.0:
        self.last_cloudlog_t = t
        cloudlog.warning(f"Long mpc reset, solution_status: {self.solution_status}")
      self.reset()


if __name__ == "__main__":
  ocp = gen_long_ocp()
  AcadosOcpSolver.generate(ocp, json_file=JSON_FILE)
