import math
import pytest
from openpilot.selfdrive.controls.lib.steering_motion import transfer_motion_credit, MAX_ACCEL_CREDIT, MIN_CORRECTION_SCALE


@pytest.mark.parametrize('direction', [-1, 1])
def test_opposite_friction_is_not_amplified(direction):
  assert transfer_motion_credit(-direction*.05, direction*.1, .06) == -direction*.05


@pytest.mark.parametrize('direction', [-1, 1])
def test_small_friction_request_cannot_reverse(direction):
  assert transfer_motion_credit(direction*.02, direction*.1, .06) == pytest.approx(direction*.02*MIN_CORRECTION_SCALE)


@pytest.mark.parametrize('value', [-3., -.3, -.01, 0., .01, .3, 3.])
@pytest.mark.parametrize('credit', [-1., 0., .02, .12, 10.])
def test_shared_credit_cannot_enlarge_reverse_or_exceed_bounds(value, credit):
  result = transfer_motion_credit(value, value, credit)
  assert abs(result) <= abs(value)
  assert result*value >= 0.
  assert abs(result) >= MIN_CORRECTION_SCALE*abs(value) - 1e-12
  assert abs(result-value) <= MAX_ACCEL_CREDIT + 1e-12


def test_bad_credit_does_not_contaminate_friction():
  assert math.isfinite(transfer_motion_credit(.1, .2, float('nan')))
