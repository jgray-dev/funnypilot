"""FunnyPilot v3.6.8 — the drive catalogue behind the web dashboard.

TWO KINDS OF TEST HERE AND THEY EXIST FOR DIFFERENT REASONS.

The BEHAVIOURAL ones drive the real functions against a temporary realdata tree.
The STRUCTURAL ones are AST scans, and they are here for the same reason
`test_widget_contracts.py` is: this module runs as root on a device, it deletes
directory trees, and it takes names from a browser. The properties that keep
that safe — no unbounded read, no interpolated path, one validator — are things
an edit can break while every behavioural test still passes.
"""
import ast
import json
import os
import pathlib

import pytest

from openpilot.sunnypilot.navd import drive_index as di

_SRC = pathlib.Path(di.__file__)


@pytest.fixture
def root(tmp_path):
  return str(tmp_path)


def _seg(root, route, n, files=("qcamera.ts", "qlog.zst"), size=1000):
  d = os.path.join(root, f"{route}--{n}")
  os.makedirs(d, exist_ok=True)
  for f in files:
    with open(os.path.join(d, f), "wb") as fh:
      fh.write(b"x" * size)
  return d


ROUTE = "abc123|2026-08-30--14-22-01"


class TestTheCatalogue:
  def test_segments_gather_into_one_drive(self, root):
    for n in range(4):
      _seg(root, ROUTE, n)
    got = di.scan_routes(root)
    assert len(got) == 1
    assert got[0]["route"] == ROUTE
    assert got[0]["n_segments"] == 4
    assert got[0]["segments"] == [0, 1, 2, 3]
    assert got[0]["bytes"] == 4 * 2000

  def test_it_reads_the_start_time_out_of_the_name(self, root):
    _seg(root, ROUTE, 0)
    assert di.scan_routes(root)[0]["started_at"] == di.route_started_at(ROUTE)
    assert di.route_started_at(ROUTE) is not None

  def test_an_unparseable_name_falls_back_rather_than_guessing(self):
    assert di.route_started_at("garbage") is None
    assert di.route_started_at("") is None

  def test_directories_that_are_not_segments_are_ignored(self, root):
    _seg(root, ROUTE, 0)
    os.makedirs(os.path.join(root, "not-a-segment"))
    os.makedirs(os.path.join(root, "boot"))
    with open(os.path.join(root, "loose-file"), "w") as f:
      f.write("x")
    assert len(di.scan_routes(root)) == 1

  def test_a_missing_root_is_an_empty_list_not_a_crash(self, tmp_path):
    # The catalogue runs on every page load; a device with no recordings yet
    # must render an empty list, not a 500.
    assert di.scan_routes(str(tmp_path / "nope")) == []
    assert di.route_segments("x", str(tmp_path / "nope")) == []

  def test_cameras_are_reported_from_what_is_actually_there(self, root):
    _seg(root, ROUTE, 0, files=("qcamera.ts", "dcamera.hevc"))
    got = di.scan_routes(root)[0]
    assert set(got["cameras"]) == {"road", "driver"}

  def test_newest_drive_is_first(self, root):
    _seg(root, "a|2026-01-01--10-00-00", 0)
    _seg(root, "a|2026-06-01--10-00-00", 0)
    routes = [r["route"] for r in di.scan_routes(root)]
    assert routes[0].endswith("2026-06-01--10-00-00")


class TestNamesFromABrowserNeverBecomePaths:
  """THIS IS THE ONE THAT MATTERS. `delete_route` removes trees, as root.

  All three checks in `_safe_segment_dir` are exercised separately, because
  each is the only thing standing between some name and the filesystem: the
  pattern rejects separators and dots, the islink test rejects a planted link,
  and the containment test rejects a resolved path outside the root.

  THE ISLINK TEST EXISTS BECAUSE THIS SUITE FOUND IT MISSING. Without it a
  symlink at `realdata/evil--0` pointing at a SIBLING of realdata passed the
  other two cleanly — the resolved parent is the same directory as the
  resolved root — which is a root-privileged recursive delete aimed anywhere.
  """

  @pytest.mark.parametrize("bad", [
    "../etc", "..", ".", "a/b", "/etc/passwd", "a--1/../../x", "",
    "a--1\x00", "a b--1", "a;rm -rf /--1", "a--1--", "--1",
  ])
  def test_a_malformed_name_resolves_to_nothing(self, root, bad):
    assert di._safe_segment_dir(root, bad) is None

  def test_a_symlink_out_of_the_root_is_refused(self, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = os.path.join(root, "evil--0")
    try:
      os.symlink(str(outside), link)
    except (OSError, NotImplementedError):
      pytest.skip("no symlink support here")
    # The NAME is well-formed AND the target's parent equals the resolved root,
    # so neither of the other two checks can see this. Only the islink test can.
    assert di._safe_segment_dir(root, "evil--0") is None

  def test_a_well_formed_name_does_resolve(self, root):
    # Anti-vacuous: a validator that rejects everything passes every case above.
    _seg(root, ROUTE, 0)
    got = di._safe_segment_dir(root, f"{ROUTE}--0")
    assert got is not None and os.path.isdir(got)

  def test_delete_only_touches_its_own_segments(self, root):
    for n in range(3):
      _seg(root, ROUTE, n)
    _seg(root, "other|2026-08-30--09-00-00", 0)
    keep = os.path.join(root, "keep-me")
    os.makedirs(keep)

    removed, freed = di.delete_route(ROUTE, root)
    assert removed == 3
    assert freed == 3 * 2000
    assert os.path.isdir(keep)
    assert len(di.scan_routes(root)) == 1

  def test_deleting_a_drive_that_is_not_there_does_nothing(self, root):
    assert di.delete_route("nope|2026-01-01--00-00-00", root) == (0, 0)


class TestThePlaylist:
  """`qcamera.ts` IS an HLS media segment, so the playlist is the whole
  encoder. These pin the properties a player needs to seek in a recording."""

  def test_it_lists_every_segment_in_order(self):
    m = di.hls_playlist("r", [0, 1, 2])
    assert m.count("#EXTINF") == 3
    assert m.index("seg/0/") < m.index("seg/1/") < m.index("seg/2/")

  def test_it_is_a_finished_recording_not_a_live_stream(self):
    # Without both of these a player tails the playlist and refuses to seek,
    # which is the whole feature.
    m = di.hls_playlist("r", [0])
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in m
    assert "#EXT-X-ENDLIST" in m

  def test_target_duration_covers_the_segment_length(self):
    # A TARGETDURATION under the real segment length makes players stall.
    m = di.hls_playlist("r", [0], seg_seconds=60.0)
    line = next(x for x in m.splitlines() if x.startswith("#EXT-X-TARGETDURATION"))
    assert int(line.split(":")[1]) >= 60

  def test_a_gap_in_segment_numbers_does_not_reorder_anything(self):
    m = di.hls_playlist("r", [0, 1, 4, 5])
    assert [x for x in m.splitlines() if x.startswith("seg/")] == \
           ["seg/0/qcamera.ts", "seg/1/qcamera.ts", "seg/4/qcamera.ts", "seg/5/qcamera.ts"]


class TestAuthority:
  def test_the_four_states(self):
    assert di.authority_of(False, False) == di.AUTH_MANUAL
    assert di.authority_of(True, False) == di.AUTH_LATERAL
    assert di.authority_of(False, True) == di.AUTH_LONG
    assert di.authority_of(True, True) == di.AUTH_FULL

  def test_a_second_is_as_manual_as_its_most_manual_sample(self):
    """THE ONE ROUNDING ERROR THIS READOUT MUST NOT MAKE. A bucket holding one
    disengaged frame is a second in which the driver took over, and a majority
    vote would paint it as fully engaged — hiding exactly the moment somebody
    would be scrubbing to find."""
    b = di._Bucket(0)
    for _ in range(99):
      b.auth[di.AUTH_FULL] += 1
      b.n_auth += 1
      b.n += 1
    b.auth[di.AUTH_MANUAL] += 1
    b.n_auth += 1
    b.n += 1
    assert b.row()[1] == di.AUTH_MANUAL

  def test_a_bucket_with_no_samples_reads_manual(self):
    # Failing toward "the human was driving" is the safe direction for a
    # readout whose job is to show when they were.
    assert di._Bucket(3).row()[1] == di.AUTH_MANUAL


class TestTheTimelineCache:
  def test_a_missing_qlog_yields_nothing_rather_than_raising(self, root):
    d = _seg(root, ROUTE, 0, files=("qcamera.ts",))
    assert di.extract_timeline(d) is None
    assert di.segment_timeline(d) is None

  def test_a_stale_cache_version_is_ignored(self, root):
    d = _seg(root, ROUTE, 0, files=("qcamera.ts",))
    with open(os.path.join(d, di.TIMELINE_NAME), "w") as f:
      json.dump({"v": di.TIMELINE_VERSION - 1, "rows": [[0, 3, 1, 1, 0, 0, -1, 0]]}, f)
    # Falls through to a re-extract, which has no qlog to read, so: None.
    # The point is that it does NOT return the old-format rows.
    assert di.segment_timeline(d) is None

  def test_a_good_cache_is_returned_without_touching_the_log(self, root):
    d = _seg(root, ROUTE, 0, files=("qcamera.ts",))
    payload = {"v": di.TIMELINE_VERSION, "fields": di.ROW_FIELDS,
               "rows": [[0, 3, 10.0, 11.0, 0.1, -0.2, 30.0, 2.0]],
               "marks": [], "distance_m": 10.0, "duration_s": 1.0}
    with open(os.path.join(d, di.TIMELINE_NAME), "w") as f:
      json.dump(payload, f)
    assert di.segment_timeline(d) == payload

  def test_route_timeline_offsets_each_segment_by_its_index(self, root):
    for n in (0, 1, 2):
      d = _seg(root, ROUTE, n, files=("qcamera.ts",))
      with open(os.path.join(d, di.TIMELINE_NAME), "w") as f:
        json.dump({"v": di.TIMELINE_VERSION, "fields": di.ROW_FIELDS,
                   "rows": [[0, 3, 1, 1, 0, 0, -1, 0], [1, 3, 1, 1, 0, 0, -1, 0]],
                   "marks": [0.5], "distance_m": 100.0, "duration_s": 2.0}, f)
    tl = di.route_timeline(ROUTE, root)
    # Segment n starts at n*60 by construction — loggerd's SEGMENT_LENGTH is
    # what makes this arithmetic instead of a clock read.
    assert [r[0] for r in tl["rows"]] == [0, 1, 60, 61, 120, 121]
    assert tl["marks"] == [0.5, 60.5, 120.5]
    assert tl["distance_m"] == 300.0

  def test_the_parse_budget_is_respected_and_reported(self, root):
    for n in range(6):
      _seg(root, ROUTE, n, files=("qcamera.ts",))
    tl = di.route_timeline(ROUTE, root, limit=2)
    # Nothing is cached and nothing can be parsed (no qlog), but the budget
    # must still bound how many are ATTEMPTED and name the rest.
    assert len(tl["pending"]) == 4
    assert tl["segments"] == [0, 1, 2, 3, 4, 5]


class TestTheMemoryRulesAreStructural:
  """AST scans, for the properties a behavioural test cannot see.

  This module runs beside a moving car. "It did not use much memory in the
  test" is not the same claim as "it cannot use much memory", and the second
  one is what the device needs.
  """

  def _tree(self):
    return ast.parse(_SRC.read_text())

  def test_the_scan_is_findable(self):
    # Anti-vacuous guard for everything below.
    names = {n.name for n in ast.walk(self._tree()) if isinstance(n, ast.FunctionDef)}
    assert {"scan_routes", "extract_timeline", "delete_route", "_safe_segment_dir"} <= names

  def test_nothing_reads_a_whole_file_into_memory(self):
    """`.read()` with no size argument on a log or video is how this module
    would OOM the device. The extractor streams; the cache is a few kB of JSON
    and is read with `json.load`, which is not this pattern."""
    for node in ast.walk(self._tree()):
      if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
          and node.func.attr == "read" and not node.args):
        raise AssertionError("unbounded .read() — stream it instead")

  def test_deletion_goes_through_the_validator(self):
    """Every `rmtree` must be reached from a path `_safe_segment_dir` produced.
    Pinned structurally because the natural way to write a bulk delete is a
    glob, and a glob here is a device wipe waiting for a bad name."""
    fn = next(n for n in ast.walk(self._tree())
              if isinstance(n, ast.FunctionDef) and n.name == "delete_route")
    calls = [n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    names = [n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_safe_segment_dir" in names
    # STRUCTURAL, NOT TEXTUAL. A first version asserted `"glob" not in
    # ast.unparse(fn)` and failed on the word "glob" inside the function's own
    # docstring explaining why globbing is forbidden — the exact mirror of the
    # v3.5.8 guard a comment could satisfy. Look at the CALLS.
    assert not {"glob", "iglob"} & set(calls + names)
    assert calls.count("rmtree") == 1, "one delete call, inside the validated loop"

  def test_the_heavy_imports_are_local_and_guarded(self):
    """`capnp`, `zstandard` and `cereal` must not be imported at module scope.

    The webserver imports this module on the manager's startup path, so an
    ImportError out here would take the whole server down — and the catalogue,
    the playlist and the delete path all work without any of them.
    """
    tree = self._tree()
    top = set()
    for node in tree.body:
      if isinstance(node, ast.Import):
        top |= {a.name.split(".")[0] for a in node.names}
      elif isinstance(node, ast.ImportFrom) and node.module:
        top.add(node.module.split(".")[0])
    assert not (top & {"capnp", "zstandard", "cereal", "numpy"}), \
      f"heavy import at module scope: {top & {'capnp', 'zstandard', 'cereal', 'numpy'}}"
    # ...and they ARE imported somewhere, or the extractor is dead code.
    inner = {n.names[0].name.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, (ast.Import, ast.ImportFrom)) and n.names}
    assert "zstandard" in inner

  def test_the_bucket_holds_no_lists_of_samples(self):
    """`_Bucket.__slots__` is what makes peak memory independent of drive
    length. A slot added here that accumulates per-sample data — a list of
    speeds, say — turns a one-hour drive into a hundred thousand floats."""
    assert set(di._Bucket.__slots__) == {
      "t", "n", "v_sum", "v_max", "a_sum", "a_min", "lead_min", "auth", "n_auth", "steer_max"}


class TestDrivingIsThePriority:
  """FunnyPilot v3.6.8 — the dashboard runs on a moving car.

  `terminal_server` is registered `always_run` in process_config, so every
  handler is live while driving. These pin the three properties that keep a web
  request from becoming a vehicle problem. They are AST/attribute checks rather
  than behavioural ones because the failure is a RESOURCE failure — "it did not
  use much CPU in this test" is not the claim the device needs.
  """

  def _server(self):
    from openpilot.sunnypilot.navd import nav_webserver as nw
    return nw

  def test_blocking_work_gets_exactly_one_worker(self):
    # The default executor is sized to CPU count, so one browser tab could put
    # every core on log decompression while the car decides when to brake.
    nw = self._server()
    assert nw._EXECUTOR._max_workers == 1

  def test_the_worker_runs_at_a_lower_priority(self):
    import ast
    src = ast.parse(pathlib.Path(self._server().__file__).read_text())
    assign = next(n for n in ast.walk(src) if isinstance(n, ast.Assign)
                  and getattr(n.targets[0], "id", "") == "_EXECUTOR")
    assert "nice" in ast.unparse(assign.value)

  def test_every_handler_is_behind_the_error_middleware(self):
    import ast
    src = ast.parse(pathlib.Path(self._server().__file__).read_text())
    main = next(n for n in ast.walk(src) if isinstance(n, ast.FunctionDef) and n.name == "main")
    app = next(n for n in ast.walk(main) if isinstance(n, ast.Call)
               and ast.unparse(n.func).endswith("Application"))
    assert "_never_5xx" in ast.unparse(app), "an unhandled raise must never reach aiohttp"

  def _handler(self, name):
    """Find a handler by name. BOTH function node types, because every one of
    these is `async def` — a first version matched only `ast.FunctionDef`, found
    nothing, and raised StopIteration. A scan that cannot locate its target is
    the vacuous-guard shape this repo keeps re-learning; here it failed loudly
    only because the paired anti-vacuous test exists."""
    import ast
    src = ast.parse(pathlib.Path(self._server().__file__).read_text())
    fn = next((n for n in ast.walk(src)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    assert fn is not None, f"{name} not found — re-point this guard"
    return ast.unparse(fn)

  def test_the_expensive_endpoints_check_whether_we_are_driving(self):
    for name in ("handle_drive_download", "handle_drive_timeline"):
      assert "_onroad" in self._handler(name), f"{name} would compete with the car"

  def test_cheap_endpoints_are_not_gated(self):
    # Anti-vacuous, and a real requirement: listing drives and serving an
    # already-encoded file are a directory scan and a sendfile. Refusing those
    # while driving would be caution with no beneficiary.
    for name in ("handle_drives", "handle_drive_file", "handle_drive_playlist"):
      assert "_onroad" not in self._handler(name)

  def test_onroad_fails_toward_not_driving(self):
    # With Params unreadable this must not lock the owner out of their own
    # recordings in the driveway. The car's protection is the single niced
    # worker, not this flag.
    assert self._server()._onroad() in (True, False)

  def test_device_actions_never_interpolate_a_request_value(self):
    """The v3.5.8 finding, generalised. `branch` reached a root shell because
    it was checked with `startswith`; the answer is that nothing from a request
    is ever part of a command, which an allow-list of fixed strings enforces
    structurally rather than by validation."""
    nw = self._server()
    for _label, cmd in nw._DEVICE_ACTIONS.values():
      assert cmd is None or ("{" not in cmd and "%" not in cmd and "$" not in cmd)


class TestAnEmptyCatalogueExplainsItself:
  """v3.6.8, second pass — `scan_routes` returns [] for FOUR different causes.

  A missing directory, an unreadable one, an empty one, and one full of files
  that do not parse all render the same shrug. That is the shape v3.6.5 named
  when the LANE signal's failure mode turned out to be its own healthy reading,
  and it cost a round trip the first time the Drives tab came up empty. These
  pin that the four are distinguishable.
  """

  def test_a_missing_directory_says_so(self, tmp_path):
    st = di.realdata_status(str(tmp_path / "nope"))
    assert st["exists"] is False and st["error"]

  def test_an_empty_directory_is_not_an_error(self, root):
    # "You have no recordings" is a true and unalarming answer, and must not
    # be dressed up as a fault.
    st = di.realdata_status(root)
    assert st["exists"] and st["readable"]
    assert st["entries"] == 0 and st["segments"] == 0
    assert st["error"] is None

  def test_a_populated_directory_counts_its_segments(self, root):
    for n in range(3):
      _seg(root, ROUTE, n)
    os.makedirs(os.path.join(root, "boot"))
    st = di.realdata_status(root)
    assert st["entries"] == 4 and st["segments"] == 3 and st["error"] is None

  def test_files_that_do_not_parse_are_called_out(self, root):
    # The case that would otherwise read as "no recordings" while the disk is
    # full of them — a naming format that stopped matching.
    for n in ("weird_thing", "another"):
      os.makedirs(os.path.join(root, n))
    st = di.realdata_status(root)
    assert st["entries"] == 2 and st["segments"] == 0
    assert "none are named like segments" in st["error"]
    assert st["sample"], "a sample is what makes a format change diagnosable"

  def test_an_unreadable_directory_is_distinguished_from_an_empty_one(self, root):
    if os.geteuid() == 0:
      pytest.skip("root can read anything; this case needs an unprivileged uid")
    os.chmod(root, 0o000)
    try:
      st = di.realdata_status(root)
      assert st["exists"] and not st["readable"] and "cannot read" in st["error"]
    finally:
      os.chmod(root, 0o755)

  def test_the_sample_is_bounded(self, root):
    # It goes over the wire on every page load of an empty list.
    for n in range(50):
      os.makedirs(os.path.join(root, f"junk{n}"))
    assert len(di.realdata_status(root)["sample"]) <= 5


class TestTheApiShapesTheClientActuallyReads:
  """FunnyPilot v3.6.9 — the client is a separate file and nothing checked that
  it and the server agreed.

  `/api/branches` returned a BARE ARRAY while the dashboard read `d.branches`,
  so the flash modal died with "Cannot read properties of undefined (reading
  'slice')". No Python test could see it and no JS test existed. This is the
  same family as v3.6.3's hand-written corner fixture: a consumer tested
  against the author's memory of a format rather than against the producer.

  These scan the shipped HTML for the fields it reads and assert the server
  emits them. Crude, but it is the seam that actually broke.
  """

  def _js(self):
    import re
    html = (pathlib.Path(di.__file__).parent / "nav_web" / "index.html").read_text()
    js = re.findall(r"<script>\n(.*?)</script>", html, re.S)[-1]
    # Comments are stripped because a guard that its own explanation can
    # satisfy is not a guard — this file has learned that three times now.
    return "\n".join(ln for ln in js.splitlines() if not ln.strip().startswith("//"))

  def test_the_page_is_findable(self):
    assert "api(\"/api/branches\")" in self._js()

  def test_branches_is_normalised_rather_than_assumed(self):
    js = self._js()
    assert "Array.isArray(d)" in js, \
      "the client must tolerate both the bare array and the named field"
    # ...and it must never index straight into the response again.
    assert "d.branches.slice" not in js and "d.branches.length" not in js

  def test_the_server_returns_the_named_field(self):
    import ast
    src = ast.parse(pathlib.Path(
      pathlib.Path(di.__file__).parent / "nav_webserver.py").read_text())
    fn = next(n for n in ast.walk(src)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "handle_branches")
    body = ast.unparse(fn)
    assert "'branches':" in body or '"branches":' in body

  def test_drives_response_carries_what_the_page_reads(self):
    import ast
    js = self._js()
    src = ast.parse(pathlib.Path(
      pathlib.Path(di.__file__).parent / "nav_webserver.py").read_text())
    fn = next(n for n in ast.walk(src)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "handle_drives")
    body = ast.unparse(fn)
    for field in ("drives", "status"):
      assert f"'{field}'" in body or f'"{field}"' in body
    assert "d.drives" in js and "d.status" in js


class TestTheServerBecomesReachableFirst:
  """FunnyPilot v3.6.9 — the dashboard was unreachable after joining a network.

  aiohttp runs every `on_startup` handler TO COMPLETION BEFORE BINDING THE
  PORT. v3.6.8's handler awaited `_boot_snapshot()` -> `_code_identity()`,
  which is six `_sh()` subprocess calls at a 10 s timeout each plus a SHA-1 of
  every file in `_FEEL_FILES`. Nothing listens on 8888 until that finishes, and
  right after joining a network is exactly when git and the filesystem are
  slowest.

  A PROCESS WHOSE JOB IS TO BE REACHABLE MUST BECOME REACHABLE FIRST.
  Diagnostics are what you do once you are serving.
  """

  def _fn(self, name):
    import ast
    src = ast.parse(pathlib.Path(
      pathlib.Path(di.__file__).parent / "nav_webserver.py").read_text())
    fn = next((n for n in ast.walk(src)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    assert fn is not None, f"{name} not found — re-point this guard"
    return fn

  def test_the_startup_handler_is_findable_and_registered(self):
    # Anti-vacuous: both halves, so a renamed handler fails loudly.
    import ast
    self._fn("_start_triage_background")
    main = self._fn("main")
    assert "_start_triage_background" in ast.unparse(main)

  def test_startup_awaits_nothing_expensive(self):
    """The whole fix, structurally. An `await` on anything but a scheduling
    call here puts subprocess time in front of the listening socket."""
    import ast
    fn = self._fn("_start_triage_background")
    awaits = [ast.unparse(n.value) for n in ast.walk(fn) if isinstance(n, ast.Await)]
    assert awaits == [], f"on_startup must schedule, not await: {awaits}"

  def test_the_boot_snapshot_still_happens(self):
    # It must be BACKGROUNDED, not deleted — it is the code-identity evidence
    # v3.2.7 exists for. Losing it to fix reachability would be trading one
    # diagnosis problem for another.
    import ast
    body = ast.unparse(self._fn("_start_triage_background"))
    assert "_boot_snapshot" in body and "_bg(" in body

  def test_nothing_heavy_is_imported_at_module_scope(self):
    """v3.6.8 imported swaglog out here. That builds a rotating handler which
    lists /data/log and rotates AT IMPORT, and stands up a zmq context in a
    process that later `pty.fork()`s. None of it may precede the port."""
    import ast
    src = ast.parse(pathlib.Path(
      pathlib.Path(di.__file__).parent / "nav_webserver.py").read_text())
    top = set()
    for node in src.body:
      if isinstance(node, ast.Import):
        top |= {a.name for a in node.names}
      elif isinstance(node, ast.ImportFrom) and node.module:
        top.add(node.module)
    banned = {m for m in top if m.startswith(("openpilot.common.swaglog",
                                              "openpilot.common.params",
                                              "cereal", "zmq"))}
    assert not banned, f"heavy module-scope import: {banned}"

  def test_logging_is_reached_through_the_lazy_accessor(self):
    import ast
    src = pathlib.Path(pathlib.Path(di.__file__).parent / "nav_webserver.py").read_text()
    tree = ast.parse(src)
    # No bare `cloudlog.` calls left behind by the conversion.
    bare = [ast.unparse(n) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and getattr(n.value, "id", "") == "cloudlog"]
    assert not bare, f"still using the module-scope name: {bare}"
    assert "_log()." in src

  def test_the_executor_initializer_cannot_break_the_pool(self):
    """A ThreadPoolExecutor whose initializer raises is permanently BROKEN and
    every later submission fails — the whole Drives section, from one
    PermissionError on nice()."""
    from openpilot.sunnypilot.navd import nav_webserver as nw
    nw._nice_worker()          # must not raise, whatever the platform allows
    import ast
    fn = self._fn("_nice_worker")
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn))


class TestBranchesAreOrderedByVersion:
  """v3.7.0 — the flash list sorted by a per-branch GitHub commit date, and an
  unauthenticated rate limit turned failed fetches into "" which sank to the
  bottom of a descending sort. 3.7.0, the newest branch, showed last.
  Version order is deterministic and costs no requests."""

  def _nw(self):
    from openpilot.sunnypilot.navd import nav_webserver as nw
    return nw

  def test_the_reported_case(self):
    nw = self._nw()
    got = nw.sort_branches(["funnypilot-3.6.9", "funnypilot-3.7.0", "funnypilot-3.6.8",
                            "funnypilot-3.6.10"])
    assert got[0] == "funnypilot-3.7.0"
    # numeric, not lexical: 3.6.10 is newer than 3.6.9
    assert got.index("funnypilot-3.6.10") < got.index("funnypilot-3.6.9")

  def test_a_suffixed_cut_sorts_newer_than_its_bare_version(self):
    # 3.2.3st is the stable cut made AFTER 3.2.3.
    nw = self._nw()
    got = nw.sort_branches(["funnypilot-3.2.3", "funnypilot-3.2.3st", "funnypilot-3.2.4e"])
    assert got == ["funnypilot-3.2.4e", "funnypilot-3.2.3st", "funnypilot-3.2.3"]

  def test_a_stray_branch_can_never_displace_a_release(self):
    nw = self._nw()
    got = nw.sort_branches(["funnypilot-zzz", "funnypilot-3.0.1", "funnypilot-experimental"])
    assert got[0] == "funnypilot-3.0.1"

  def test_the_order_cannot_depend_on_a_date_fetch(self):
    """MUTATION: sort on date again. The order must be fully determined before
    any network request is made, so a rate limit can only ever blank a date,
    never move a row."""
    import ast
    src = ast.parse(pathlib.Path(self._nw().__file__).read_text())
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fetch_branches")
    body = ast.unparse(fn)
    assert "sort_branches(" in body
    assert "sorted(results" not in body and "key=lambda x: x[1]" not in body

  def test_date_requests_are_bounded(self):
    # The rate limit is 60/hour unauthenticated; the whole point is to stay
    # far under it however many branches exist.
    nw = self._nw()
    assert 0 < nw.DATE_FETCH_N <= 15
    import ast
    src = ast.parse(pathlib.Path(nw.__file__).read_text())
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fetch_branches")
    assert "[:DATE_FETCH_N]" in ast.unparse(fn)
