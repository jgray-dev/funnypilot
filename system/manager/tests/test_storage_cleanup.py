"""FunnyPilot v3.4.5: guards for the startup storage cleanup.

TWO CLASSES OF TEST HERE, and the split matters:

  * TestBootPath is the IMPORT guard. storage_cleanup is called from
    manager_init(), so a NameError/ImportError/annotation blowup in it is a
    car that sits on the comma splash. Two consecutive releases (v3.4.0,
    v3.4.1) shipped "all tests green" and bricked the car for exactly this
    reason — nothing imported the module on the boot path. Logic tests do
    not substitute for this.

  * Everything else pins the SAFETY INVARIANTS. The dangerous failure mode
    of a cleaner is not "it reclaimed too little", it is "it deleted the
    running system". So: the allow-list is asserted to exclude the paths
    that must never be touched, and `cleanup()` is asserted to be
    total — no input makes it raise.

Plain pytest (`unittest` is banned repo-wide by ruff).
"""
import ast
import importlib
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from openpilot.system.manager import storage_cleanup as sc

ALL_TARGETS = (sc.OLD_OPENPILOT,) + sc.LOW_SPACE_TARGETS
FORBIDDEN = ("/data/openpilot", "/data/params", "/data/media",
             "/data/safe_staging/merged", "/data/safe_staging", "/data", "/")


class TestBootPath:
  def test_module_imports(self):
    # The whole point: this must not raise at import time.
    m = importlib.import_module("openpilot.system.manager.storage_cleanup")
    assert callable(m.cleanup_async)

  def test_stdlib_only(self):
    # An openpilot import here would drag cereal/params onto the boot path
    # before manager has set them up, and would make this module's failure
    # modes depend on things it has no business depending on.
    src = pathlib.Path(sc.__file__).read_text()
    assert "openpilot." not in src.split('"""', 2)[-1]
    assert "import cereal" not in src


class TestAllowList:
  @pytest.mark.parametrize("p", ALL_TARGETS)
  def test_target_is_absolute(self, p):
    assert p.startswith("/"), p

  @pytest.mark.parametrize("p", ALL_TARGETS)
  def test_never_targets_load_bearing_paths(self, p):
    assert p.rstrip("/") not in FORBIDDEN

  @pytest.mark.parametrize("p", ALL_TARGETS)
  def test_no_globbing(self, p):
    # A glob would turn the allow-list into a pattern match, i.e. back into
    # "walk and decide" — the thing this design deliberately avoids.
    assert "*" not in p


class TestRm:
  def test_removes_tree_and_reports_size(self, tmp_path):
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_bytes(b"x" * 4096)
    n = sc._rm(str(d))
    assert not d.exists()
    assert n >= 4096

  def test_missing_path_is_zero(self, tmp_path):
    assert sc._rm(str(tmp_path / "nope")) == 0

  def test_symlink_not_followed(self, tmp_path):
    # A symlink pointing at something huge must be unlinked, not walked --
    # and must certainly not cause the target to be removed.
    real = tmp_path / "real"
    real.mkdir()
    (real / "big").write_bytes(b"y" * 8192)
    link = tmp_path / "link"
    os.symlink(str(real), str(link))
    sc._rm(str(link))
    assert not os.path.lexists(str(link))
    assert (real / "big").exists()


class TestDirSize:
  def test_sums_nested(self, tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    for p in ("a/f1", "a/b/f2"):
      (tmp_path / p).write_bytes(b"z" * 1000)
    assert sc.dir_size(str(tmp_path)) >= 2000

  def test_missing_is_zero(self, tmp_path):
    assert sc.dir_size(str(tmp_path / "gone")) == 0

  def test_symlink_loop_terminates(self, tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    os.symlink(str(tmp_path), str(d / "loop"))
    assert sc.dir_size(str(tmp_path)) >= 0  # must simply return


class TestTotality:
  """cleanup() must never raise -- it runs before the car can start."""

  def test_survives_broken_free_space(self, monkeypatch):
    monkeypatch.setattr(sc, "free_space",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "error" in sc.cleanup()

  def test_survives_broken_logger(self, monkeypatch):
    monkeypatch.setattr(sc, "_rm", lambda p: 123)  # non-zero reclaim so `log` is invoked
    r = sc.cleanup(log=lambda _: (_ for _ in ()).throw(ValueError("nope")))
    assert r["reclaimed"] >= 123

  def test_free_space_bad_path(self):
    assert sc.free_space("/definitely/not/here") == (-1, -1.0)

  def test_async_returns_thread(self):
    t = sc.cleanup_async()
    assert t is not None
    t.join(timeout=30)


class TestThresholds:
  def test_above_deleter_floor(self):
    # deleter.py trims drive segments to hold 5 GB / 10% free. This pass must
    # engage BEFORE that, or it only ever runs after logs were already eaten.
    assert sc.LOW_BYTES > 5 * 1024 ** 3
    assert sc.LOW_PERCENT > 10.0

  def test_gc_is_bounded(self):
    assert sc.GIT_GC_TIMEOUT_S <= 600
    assert sc.GIT_GC_MIN_BYTES > 0


class TestManagerWiring:
  """AST, not grep -- importing manager.py needs the full device env.

  This started as a substring check and was MUTATION-TESTED: commenting the
  call out left `storage_cleanup.cleanup_async` in the file as a comment and
  the test still passed. A guard that a disabled call satisfies is not a
  guard. AST only sees code that actually runs.
  """

  @staticmethod
  def _manager_tree():
    return ast.parse((pathlib.Path(sc.__file__).parent / "manager.py").read_text())

  @classmethod
  def _calls(cls, fn_name: str):
    return [node for node in ast.walk(cls._manager_tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == fn_name and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "storage_cleanup"]

  def test_manager_calls_cleanup_async(self):
    assert self._calls("cleanup_async"), "manager_init must call storage_cleanup.cleanup_async"

  def test_cleanup_is_not_called_synchronously(self):
    # A blocking call here would add `du` + `git gc` to every boot.
    assert not self._calls("cleanup"), "cleanup() must not be called on the boot thread"

  def test_module_is_imported(self):
    assert any(
      isinstance(n, ast.ImportFrom) and n.module == "openpilot.system.manager"
      and any(a.name == "storage_cleanup" for a in n.names)
      for n in ast.walk(self._manager_tree())
    )

  def test_call_is_inside_manager_init(self):
    # Anywhere else (e.g. module scope) would run it on import, in every
    # process that imports manager, not once at startup.
    fns = [n for n in ast.walk(self._manager_tree())
           if isinstance(n, ast.FunctionDef) and n.name == "manager_init"]
    assert len(fns) == 1
    assert [n for n in ast.walk(fns[0])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "cleanup_async"]
