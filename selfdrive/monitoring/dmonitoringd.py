#!/usr/bin/env python3
# FunnyPilot: driver monitoring disabled. dmonitoringmodeld only runs for explicit
# driver view, so instead of running the policy (selfdrive/monitoring/policy.py) this
# daemon publishes a constant, valid, "nothing to report" driverMonitoringState at
# 20 Hz so consumers' alive/freq/valid checks stay satisfied.
from cereal import log
import cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.realtime import DT_DMON, Ratekeeper, config_realtime_process

AlertLevel = log.DriverMonitoringState.AlertLevel
MonitoringPolicy = log.DriverMonitoringState.MonitoringPolicy


def default_dm_state(is_rhd: bool = False):
  dat = messaging.new_message('driverMonitoringState', valid=True)
  dm = dat.driverMonitoringState
  dm.lockout = False
  dm.alwaysOn = False
  dm.alwaysOnLockout = False
  dm.alertLevel = AlertLevel.none
  dm.activePolicy = MonitoringPolicy.wheeltouch
  dm.isRHD = is_rhd
  dm.visionPolicyState.awarenessPercent = 100
  dm.visionPolicyState.uncertainOffroadAlertPercent = 0
  dm.wheeltouchPolicyState.awarenessPercent = 100
  dm.wheeltouchPolicyState.driverInteracting = True
  return dat


def dmonitoringd_thread():
  config_realtime_process([0, 1, 2, 3], 5)

  # read once; keeps modeld's isRHD input unchanged for previously-detected RHD devices
  is_rhd = Params().get_bool("IsRhdDetected")
  pm = messaging.PubMaster(['driverMonitoringState'])
  rk = Ratekeeper(1. / DT_DMON, print_delay_threshold=None)

  while True:
    pm.send('driverMonitoringState', default_dm_state(is_rhd))
    rk.keep_time()


def main():
  dmonitoringd_thread()


if __name__ == '__main__':
  main()
