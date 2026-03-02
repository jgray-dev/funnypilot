"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import os
import re
import time

import pyray as rl

from openpilot.selfdrive.ui.layouts.settings.software import SoftwareLayout
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr, tr_noop
from openpilot.system.ui.widgets import DialogResult
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog, alert_dialog
from openpilot.system.ui.widgets.option_dialog import MultiOptionDialog
from openpilot.system.ui.widgets.scroller_tici import Scroller

from openpilot.system.ui.sunnypilot.widgets.list_view import button_item_sp, toggle_item_sp
from openpilot.system.ui.sunnypilot.widgets.progress_bar import progress_item


DESCRIPTIONS = {
  'disable_updates_offroad': tr_noop("When enabled, automatic software updates will be off.<br><b>This requires a reboot to take effect.</b>"),
  'disable_updates_onroad': tr_noop("Please enable \"Always Offroad\" mode or turn off the vehicle to adjust these toggles."),
}


class SoftwareLayoutSP(SoftwareLayout):
  def __init__(self):
    super().__init__()

    self._funnypilot_display_to_branch: dict[str, str] = {}

    self.refresh_branches_btn = button_item_sp(
      lambda: tr("FunnyPilot Branches"),
      lambda: tr("REFRESH"),
      description=lambda: tr("Refresh available branches from the funnypilot remote."),
      callback=self._refresh_branch_list,
    )

    self.branch_download_progress = progress_item(tr("Branch Download"))
    self.branch_download_progress.set_visible(False)

    self.disable_updates_toggle = toggle_item_sp(
      lambda: tr("Disable Updates"),
      description="",
      initial_state=ui_state.params.get_bool("DisableUpdates"),
      callback=self._on_disable_updates_toggled,
    )

    self._uninstall_btn = button_item_sp(lambda: tr("Uninstall"), lambda: tr("UNINSTALL"), callback=self._on_uninstall)

    self._scroller = Scroller(
      [
        self._onroad_label,
        self._version_item,
        self.refresh_branches_btn,
        self._branch_btn,
        self.branch_download_progress,
        self._download_btn,
        self._install_btn,
        self._uninstall_btn,
        self.disable_updates_toggle,
      ],
      line_separator=True,
      spacing=0,
    )

  @staticmethod
  def _is_funnypilot_version_branch(branch: str) -> bool:
    return re.fullmatch(r"(?:funnypilot-)?\d+\.\d+\.\d+(?:[a-z]|-[a-z0-9._-]+)?", branch) is not None

  @staticmethod
  def _branch_sort_key(branch: str) -> tuple[int, int, int, int, str]:
    m = re.fullmatch(r"(?:funnypilot-)?(\d+)\.(\d+)\.(\d+)([a-z]|-[a-z0-9._-]+)?", branch)
    if m is None:
      return (0, 0, 0, 0, "")
    major, minor, patch, suffix_raw = m.groups()
    suffix = suffix_raw or ""
    suffix_rank = 1 if suffix else 0
    return (int(major), int(minor), int(patch), suffix_rank, suffix)

  @staticmethod
  def _display_branch_name(branch: str) -> str:
    if branch.startswith("funnypilot-"):
      return branch[len("funnypilot-") :]
    return branch

  @staticmethod
  def _parse_updater_state(state: str) -> tuple[str, int, str]:
    state = state or "idle"
    parts = state.split("|", 2)
    phase = parts[0].strip()

    default_progress = {
      "checking...": 5,
      "downloading...": 45,
      "finalizing update...": 92,
    }

    progress = default_progress.get(phase, 0)
    if len(parts) >= 2:
      try:
        progress = max(0, min(100, int(parts[1])))
      except Exception:
        pass

    detail = parts[2] if len(parts) == 3 and parts[2] else phase
    return phase, progress, detail

  def _refresh_branch_list(self):
    self._waiting_for_updater = True
    self._waiting_start_ts = time.monotonic()
    self._signal_updated(fetch=False)

  @staticmethod
  def _normalize_branch_name(branch: str) -> str:
    b = branch.strip()
    if not b:
      return ""
    if b.startswith("refs/heads/"):
      b = b[len("refs/heads/") :]
    if "/" in b and not b.startswith("funnypilot-"):
      tail = b.rsplit("/", 1)[-1]
      if re.fullmatch(r"funnypilot-\d+\.\d+\.\d+[a-z]?", tail):
        b = tail
    return b

  def _build_funnypilot_branch_map(self) -> dict[str, str]:
    branches_raw = ui_state.params.get("UpdaterAvailableBranches") or ""
    branches_str = branches_raw.decode("utf-8", "replace") if isinstance(branches_raw, bytes) else str(branches_raw)
    branches = [self._normalize_branch_name(b) for b in branches_str.split(",") if b.strip()]
    fp_branches = [b for b in branches if self._is_funnypilot_version_branch(b)]
    fp_branches = sorted(set(fp_branches), key=self._branch_sort_key, reverse=True)

    if not fp_branches:
      current_target = ui_state.params.get("UpdaterTargetBranch") or ui_state.params.get("GitBranch") or ""
      normalized_current = self._normalize_branch_name(current_target)
      if self._is_funnypilot_version_branch(normalized_current):
        fp_branches = [normalized_current]

    return {self._display_branch_name(branch): branch for branch in fp_branches}

  def _handle_reboot(self, result):
    if result == DialogResult.CONFIRM:
      ui_state.params.put_bool("DisableUpdates", self.disable_updates_toggle.action_item.get_state())
      ui_state.params.put_bool("DoReboot", True)
    else:
      self.disable_updates_toggle.action_item.set_state(ui_state.params.get_bool("DisableUpdates"))

  def _on_disable_updates_toggled(self, enabled):
    dialog = ConfirmDialog(tr("System reboot required for changes to take effect. Reboot now?"), tr("Reboot"))
    gui_app.set_modal_overlay(dialog, callback=self._handle_reboot)

  def _on_select_branch(self):
    self._funnypilot_display_to_branch = self._build_funnypilot_branch_map()

    if not self._funnypilot_display_to_branch:
      gui_app.set_modal_overlay(alert_dialog(tr("No FunnyPilot version branches available yet. Tap Refresh and try again.")))
      return

    options = list(self._funnypilot_display_to_branch.keys())
    current_target = ui_state.params.get("UpdaterTargetBranch") or ui_state.params.get("GitBranch") or ""
    current_display = self._display_branch_name(current_target)

    self._branch_dialog = MultiOptionDialog(tr("Select FunnyPilot branch"), options, current_display)

    def handle_selection(result):
      if result == DialogResult.CONFIRM and self._branch_dialog is not None and self._branch_dialog.selection:
        selected_display = self._branch_dialog.selection
        selected_branch = self._funnypilot_display_to_branch.get(selected_display)
        if selected_branch:
          ui_state.params.put("UpdaterTargetBranch", selected_branch)
          self._branch_btn.action_item.set_value(selected_display)
          self._waiting_for_updater = True
          self._waiting_start_ts = time.monotonic()
          self._signal_updated(fetch=False)
      self._branch_dialog = None

    gui_app.set_modal_overlay(self._branch_dialog, callback=handle_selection)

  def _update_state(self):
    super()._update_state()
    show_advanced = ui_state.params.get_bool("ShowAdvancedControls")

    self.refresh_branches_btn.action_item.set_enabled(ui_state.is_offroad())

    branch_value = self._display_branch_name(ui_state.params.get("UpdaterTargetBranch") or "")
    self._branch_btn.action_item.set_value(branch_value)
    self._branch_btn.action_item.set_enabled(ui_state.is_offroad())

    updater_state = ui_state.params.get("UpdaterState") or "idle"
    phase, progress, detail = self._parse_updater_state(updater_state)
    downloading = phase in ("checking...", "downloading...", "finalizing update...")
    self.branch_download_progress.set_visible(downloading)
    if downloading:
      text = f"{int(progress)}% - {detail}"
      self.branch_download_progress.action_item.update(progress, text, show_progress=True, text_color=rl.WHITE)

    self.disable_updates_toggle.action_item.set_enabled(ui_state.is_offroad())
    self.disable_updates_toggle.set_visible(show_advanced)

    disable_updates_desc = tr(DESCRIPTIONS["disable_updates_offroad"] if ui_state.is_offroad() else DESCRIPTIONS["disable_updates_onroad"])
    self.disable_updates_toggle.set_description(disable_updates_desc)
