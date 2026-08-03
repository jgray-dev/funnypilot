"""FunnyPilot v3.5.8 — what the web flash is allowed to delete.

The flash script runs as ROOT and now deletes things. Every assertion here is
about the SOURCE rather than about a live run, because there is no safe way to
exercise `sudo rm -rf` in a test — and because the failure mode being guarded
is an edit that looks reasonable in review.
"""
import ast
import pathlib
import re

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "nav_webserver.py"
_TEXT = _SRC.read_text()

# Paths that must never be a deletion target, at any point, for any reason.
PROTECTED = ("/", "/data", "/data/media", "/data/media/0", "/data/openpilot",
             "/data/params", "/data/community", "/data/log", "/home", "/persist")


def _const(name):
  tree = ast.parse(_TEXT)
  for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
      return ast.literal_eval(node.value)
  raise AssertionError(f"{name} not found")


class TestDeletionTargets:
  def test_the_source_is_not_empty(self):
    """Anti-vacuous: every check below scans _TEXT, so an unreadable file
    would make the whole suite pass without testing anything."""
    assert "async def handle_flash" in _TEXT and len(_TEXT) > 5000

  @pytest.mark.parametrize("path", PROTECTED)
  def test_no_protected_path_is_ever_an_rm_target(self, path):
    for target in re.findall(r"rm -rf ([^\s;\"']+)", _TEXT):
      assert target != path, f"rm -rf would target {path}"

  @pytest.mark.parametrize("path", PROTECTED)
  def test_no_protected_path_roots_a_find_delete(self, path):
    for root in re.findall(r"find (\S+) -mindepth", _TEXT):
      assert root != path, f"find-delete rooted at {path}"

  def test_drive_data_dir_is_specific_and_survives(self):
    """-mindepth 1 empties the directory but keeps it: loggerd expects it to
    exist. MUTATION: drop -mindepth and the directory itself goes."""
    d = _const("_DRIVE_DATA_DIR")
    assert d.startswith("/data/media/0/") and d.count("/") >= 4
    assert d not in PROTECTED
    # assert on the COMMAND, not on the file text -- the comment above it also
    # contains the words "-mindepth 1", and an earlier version of this test
    # matched that instead and passed with the flag actually removed.
    assert re.search(r"find \S+ -mindepth 1 -maxdepth 1 -exec rm", _TEXT), \
      "the find must keep the directory itself"

  def test_nothing_is_deleted_before_the_checkout_succeeds(self):
    """A FAILED flash must never delete anything. Checked on the ASSEMBLED
    script, not on source order: the deletions must be spliced into the tail
    block, which the `&&` chain only reaches once reset --hard has succeeded.
    MUTATION: move `prune`/`purge` ahead of the fetch and this fails."""
    body = _TEXT[_TEXT.index("script = ("):]
    body = body[:body.index("\n    )")]
    assert "reset --hard" in body and "+ prune + purge" in body
    assert body.index("reset --hard") < body.index("+ prune + purge")
    # and they must be inside the braces, after the last `&&`
    assert body.rindex("&&") < body.index("+ prune + purge")


class TestBranchPrune:
  def test_stable_branches_are_kept(self):
    assert _const("KEEP_BRANCH_SUFFIX") == "st"
    assert "grep -vE '{KEEP_BRANCH_SUFFIX}$'" in _TEXT or "KEEP_BRANCH_SUFFIX}$" in _TEXT

  def test_the_branch_being_flashed_is_excluded(self):
    """Deleting the branch just checked out would leave the device on a
    detached ref with no name to fetch back to."""
    assert "grep -vx '{branch}'" in _TEXT

  def test_the_prune_only_touches_local_heads(self):
    """refs/heads, never refs/remotes: remote-tracking refs are how the device
    finds anything again after a prune."""
    assert "refs/heads" in _TEXT
    assert "git branch -D" in _TEXT and "-r" not in _TEXT.split("git branch -D")[1][:6]


class TestBranchNameIsShellSafe:
  def test_a_branch_name_regex_exists(self):
    """`branch` is interpolated into a command that runs as root. The
    startswith('funnypilot-') test alone lets "funnypilot-;<anything>" through.
    MUTATION: delete _BRANCH_RE and the check that uses it."""
    assert "_BRANCH_RE" in _TEXT and "_BRANCH_RE.match(branch)" in _TEXT

  @pytest.mark.parametrize("bad", [
    "funnypilot-; rm -rf /data", "funnypilot-$(reboot)", "funnypilot-`id`",
    "funnypilot-a b", "funnypilot-a|b", "funnypilot-a&b", "../etc", "main;ls",
  ])
  def test_shell_metacharacters_are_rejected(self, bad):
    pat = re.compile(_const_regex())
    assert not pat.match(bad), f"{bad!r} must not be accepted"

  @pytest.mark.parametrize("good", ["funnypilot-3.5.8", "funnypilot-3.2.3st", "main", "dev", "staging"])
  def test_real_branch_names_are_accepted(self, good):
    assert re.compile(_const_regex()).match(good)


def _const_regex() -> str:
  m = re.search(r'_BRANCH_RE = re\.compile\(r"([^"]+)"\)', _TEXT)
  assert m, "_BRANCH_RE not found"
  return m.group(1)
