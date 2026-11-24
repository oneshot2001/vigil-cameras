"""Watch logic, no cameras: python3 -m unittest tests/test_watch.py"""
import importlib.util
import os
import unittest


def split(events):
    return [r for k, r in events if k == "edge"], [r for k, r in events if k == "wake"]

_spec = importlib.util.spec_from_file_location("campub", os.path.join(os.path.dirname(__file__), "..", "libexec", "campub.py"))
campub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(campub)
R = campub.REARM_S

CAR = (-0.2, 0.0, 0.2, -0.4, "Vehicle")      # foot at (0, -0.4)
PERSON = (-0.2, 0.0, 0.2, -0.4, "Human")


def doc(utc, *objs, source="0"):
    return [(source, utc, list(objs))]


class Clock:
    def __init__(self):
        self.mono, self.utc = 100.0, 1_000_000

    def step(self, s):
        self.mono += s
        self.utc += int(s * 1000)
        return self.mono, self.utc


def watch(cls="Vehicle", rect=None, rev=1):
    w = campub.Watch()
    w.set({"cls": cls, "rect": rect, "rev": rev}, 0.0)
    return w


class NoHold(unittest.TestCase):
    """Rearm and occupancy rules on their own: the class minimum-present hold is tested in HoldRule."""
    def setUp(self):
        self._hold = campub.MIN_PRESENT_S
        campub.MIN_PRESENT_S = {}

    def tearDown(self):
        campub.MIN_PRESENT_S = self._hold


class WatchRules(NoHold):
    def run_seq(self, w, seq, c=None):
        """seq: (seconds after previous doc, objs…) → list of wakes."""
        c = c or Clock()
        wakes = []
        for dt, *objs in seq:
            mono, utc = c.step(dt)
            wakes += split(w.feed(doc(utc, *objs), mono))[1]
        return wakes

    def test_no_wake_on_baseline(self):
        self.assertEqual(self.run_seq(watch(), [(0.1, CAR), (0.1, CAR)]), [])

    def test_wake_after_rearm(self):
        seq = [(0.1,)] + [(1.0,)] * int(R) + [(0.1, CAR)]
        wakes = self.run_seq(watch(), seq)
        self.assertEqual([w["n"] for w in wakes], [1])

    def test_flicker_inside_rearm_does_not_wake(self):
        seq = [(0.1, CAR)] + [(0.1,)] * 3 + [(0.1, CAR)]
        self.assertEqual(self.run_seq(watch(), seq), [])

    def test_metadata_gap_is_unknown_not_absence(self):
        w = watch()
        seq = [(0.1,), (R + 1, CAR)]   # one clear doc, then silence longer than REARM, then a car
        self.assertEqual(self.run_seq(w, seq), [])
        self.assertEqual(w.state, "present")

    def test_other_class_ignored(self):
        seq = [(0.1,)] + [(1.0,)] * int(R) + [(0.1, PERSON)]
        self.assertEqual(self.run_seq(watch(), seq), [])

    def test_rect_uses_foot_point(self):
        rect = (-0.5, -0.3, 0.5, -0.5)          # a strip around y = -0.4
        above = (-0.2, 0.5, 0.2, 0.1, "Vehicle")  # box entirely above the strip
        seq = [(0.1,)] + [(1.0,)] * int(R) + [(0.1, above)]
        self.assertEqual(self.run_seq(watch(rect=rect), seq), [])
        seq = [(0.1,)] + [(1.0,)] * int(R) + [(0.1, CAR)]
        self.assertEqual(len(self.run_seq(watch(rect=rect), seq)), 1)

    def test_flipped_y_transform_same_foot(self):
        flipped = (-0.2, -0.4, 0.2, 0.0, "Vehicle")   # top/bottom swapped by a y-flip
        rect = (-0.5, -0.3, 0.5, -0.5)
        seq = [(0.1,)] + [(1.0,)] * int(R) + [(0.1, flipped)]
        self.assertEqual(len(self.run_seq(watch(rect=rect), seq)), 1)

    def test_rect_edge_inclusive(self):
        rect = (0.0, 0.0, 0.5, -0.4)   # foot (0, -0.4) sits on the left and bottom edges
        seq = [(0.1,)] + [(1.0,)] * int(R) + [(0.1, CAR)]
        self.assertEqual(len(self.run_seq(watch(rect=rect), seq)), 1)

    def test_two_sources_one_empty_does_not_clear_other(self):
        w, c = watch(), Clock()
        mono, utc = c.step(0.1)
        w.feed(doc(utc, CAR, source="A"), mono)
        mono, utc = c.step(0.1)
        w.feed(doc(utc, source="B"), mono)
        self.assertEqual(w.state, "present")

    def test_out_of_order_update_ignored(self):
        w, c = watch(), Clock()
        mono, utc = c.step(0.1)
        w.feed(doc(utc), mono)
        mono, _ = c.step(0.1)
        edges, wakes = split(w.feed(doc(utc - 50, CAR), mono))   # older than what the source already said
        self.assertEqual((edges, wakes, w.state), ([], [], "clear"))

    def test_stale_update_after_source_expiry_ignored(self):
        w, c = watch(), Clock()
        mono, utc_a = c.step(0.1)
        w.feed(doc(utc_a, CAR, source="A"), mono)
        for _ in range(int(R) + 2):                 # B says clear for longer than REARM; A expires
            mono, utc = c.step(1.0)
            w.feed(doc(utc, source="B"), mono)
        mono, _ = c.step(0.1)
        self.assertEqual(split(w.feed(doc(utc_a - 1, CAR, source="A"), mono))[1], [])

    def test_batched_frames_in_time_order(self):
        w, c = watch(), Clock()
        self.run_seq(w, [(0.1,)] + [(1.0,)] * int(R), c)    # armed
        mono, utc = c.step(0.1)
        frames = [("0", utc + 100, []), ("0", utc, [CAR])]  # arrival then departure, out of order in the document
        events = w.feed(frames, mono)
        self.assertEqual([(k, r.get("state"), r["utc_ms"]) for k, r in events],
                         [("wake", None, utc), ("edge", "present", utc), ("edge", "clear", utc + 100)])   # in the order they happened

    def test_reset_emits_unknown_edge(self):
        w, c = watch(), Clock()
        self.run_seq(w, [(0.1,)], c)
        self.assertEqual([r["state"] for _, r in w.reset(c.mono)], ["unknown"])
        self.assertEqual(w.reset(c.mono), [])      # already unknown: no second edge

    def test_silence_tick_goes_unknown(self):
        w, c = watch(), Clock()
        self.run_seq(w, [(0.1,)], c)
        self.assertEqual(w.tick(c.mono + R / 2), [])
        self.assertEqual([r["state"] for _, r in w.tick(c.mono + R + 1)], ["unknown"])

    def test_k_counts_per_watch(self):
        w, c = watch(), Clock()
        arm = [(0.1,)] + [(1.0,)] * int(R)
        self.run_seq(w, arm + [(0.1, CAR)], c)
        w.set({"cls": "Vehicle", "rect": None, "rev": 2}, c.mono)
        wakes = self.run_seq(w, arm + [(0.1, CAR)], c)
        self.assertEqual([(x["n"], x["k"]) for x in wakes], [(2, 1)])

    def test_changed_watch_rebaselines(self):
        w, c = watch(), Clock()
        seq = [(0.1,)] + [(1.0,)] * int(R)
        self.run_seq(w, seq, c)
        w.set({"cls": "Vehicle", "rect": None, "rev": 2}, c.mono)
        self.assertEqual(self.run_seq(w, [(0.1, CAR)], c), [])

    def test_edges_carry_seq(self):
        w, c = watch(), Clock()
        seqs = []
        for dt, *objs in [(0.1,), (0.1, CAR), (0.1,)]:
            mono, utc = c.step(dt)
            seqs += [e["seq"] for e in split(w.feed(doc(utc, *objs), mono))[0]]
        self.assertEqual(seqs, [1, 2, 3])


class Owner:
    """A stand-in stream: the Publisher only compares identity and reads ready/w/h."""


class HoldRule(unittest.TestCase):
    """Real MIN_PRESENT_S: Vehicle 1 s, Human none."""
    run_seq = WatchRules.run_seq
    ARM = [(0.1,)] + [(1.0,)] * int(R)

    def test_values(self):
        self.assertEqual(campub.MIN_PRESENT_S, {"Vehicle": 1.0})

    def test_vehicle_flicker_never_wakes(self):
        w = watch()
        self.assertEqual(self.run_seq(w, self.ARM + [(0.1, CAR)] * 5 + [(0.1,)]), [])   # 0.4 s of car
        self.assertEqual(w.n, 0)

    def test_vehicle_wakes_once_after_hold_with_arrival_time(self):
        c = Clock()
        w = watch()
        self.run_seq(w, self.ARM, c)
        arrival = c.utc + 100
        wakes = self.run_seq(w, [(0.1, CAR)] * 15, c)   # 1.4 s of car
        self.assertEqual([(x["n"], x["utc_ms"]) for x in wakes], [(1, arrival)])

    def test_flicker_breaks_absence(self):
        seq = self.ARM + [(0.1, CAR), (0.1,)] + [(1.0,)] * (int(R) - 2) + [(0.1, CAR)] * 15
        self.assertEqual(self.run_seq(watch(), seq), [])   # the flicker restarted the REARM clock

    def test_person_not_held(self):
        wakes = self.run_seq(watch(cls="Human"), self.ARM + [(0.1, PERSON), (0.1,)])
        self.assertEqual(len(wakes), 1)

    def test_hold_spanning_one_batched_document(self):
        w, c = watch(), Clock()
        self.run_seq(w, self.ARM, c)
        mono, utc = c.step(0.1)
        events = w.feed([("0", utc, [CAR]), ("0", utc + 600, [CAR]), ("0", utc + 1000, [CAR])], mono)
        self.assertEqual([(k, r.get("state"), r["utc_ms"]) for k, r in events],
                         [("edge", "present", utc), ("wake", None, utc)])

    def test_wake_not_before_hold(self):
        w, c = watch(), Clock()
        self.run_seq(w, self.ARM, c)
        self.assertEqual(self.run_seq(w, [(0.1, CAR)] * 10, c), [])   # camera time 0 .. 0.9 s present
        self.assertEqual(len(self.run_seq(w, [(0.1, CAR)], c)), 1)    # 1.0 s

    def test_other_source_empty_frame_does_not_complete_hold(self):
        w, c = watch(), Clock()
        self.run_seq(w, self.ARM, c)
        mono, utc = c.step(0.1)
        self.assertEqual(split(w.feed(doc(utc, CAR, source="A"), mono))[1], [])
        self.assertEqual(split(w.feed(doc(utc + 1000, source="B"), mono + 0.1))[1], [])   # empty B, later camera time
        self.assertEqual(split(w.feed(doc(utc + 100, source="A"), mono + 0.2))[1], [])    # A: gone after 100 ms
        self.assertEqual(w.n, 0)

    def test_reset_cancels_pending_arrival(self):
        w, c = watch(), Clock()
        self.run_seq(w, self.ARM + [(0.1, CAR)], c)
        w.reset(c.mono)
        self.assertEqual(self.run_seq(w, [(0.1, CAR)] * 15, c), [])


class Promotion(NoHold):
    def setUp(self):
        super().setUp()
        self.out = []
        self._emit = campub.emit
        campub.emit = lambda **kv: self.out.append(kv)
        self.pub = campub.Publisher(0)
        self.pub._publish = lambda *a: True
        self.old, self.new = Owner(), Owner()
        self.new.ready, self.new.w, self.new.h = b"x", 1, 1
        self.pub.owner = self.old
        self.pub.watch_set({"cls": "Vehicle", "rect": None, "rev": 1})
        self.c = Clock()
        for dt in [0.1] + [1.0] * int(R):   # clear for REARM: armed
            mono, utc = self.c.step(dt)
            self.pub.watch_feed(self.old, doc(utc), mono)

    def tearDown(self):
        campub.emit = self._emit
        super().tearDown()

    def wakes(self):
        return [o["wake"] for o in self.out if "wake" in o]

    def test_old_owner_sees_arrival_first(self):
        mono, utc = self.c.step(0.1)
        self.pub.watch_feed(self.old, doc(utc, CAR), mono)   # wakes
        self.assertTrue(self.pub.promote(self.new))
        self.pub.watch_feed(self.new, doc(utc, CAR), mono + 0.05)   # same arrival via the new stream: baseline
        self.pub.watch_feed(self.old, doc(utc + 100, CAR), mono + 0.1)   # retired: dropped
        self.assertEqual([w["n"] for w in self.wakes()], [1])

    def test_new_stream_sees_arrival_first(self):
        mono, utc = self.c.step(0.1)
        self.pub.watch_feed(self.new, doc(utc, CAR), mono)   # not owner yet: dropped
        self.assertTrue(self.pub.promote(self.new))
        self.pub.watch_feed(self.new, doc(utc + 100, CAR), mono + 0.1)   # baseline after reset
        self.pub.watch_feed(self.old, doc(utc + 100, CAR), mono + 0.1)   # retired: dropped
        self.assertEqual(self.wakes(), [])

    def test_stats_count_survives_promotion(self):
        mono, utc = self.c.step(0.1)
        self.pub.watch_feed(self.old, doc(utc, CAR), mono)
        self.pub.promote(self.new)
        s = self.pub.watch.stats(True)
        self.assertEqual((s["wakes"], s["last"]["n"], s["state"]), (1, 1, "waiting"))


class Control(unittest.TestCase):
    def poll(self, body):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(body)
        want = campub.Want(("live", 640, 360))
        try:
            changed = campub.Control(f.name, want).poll()
        finally:
            os.unlink(f.name)
        return changed, want

    def test_watch_only_change_is_not_a_size_change(self):
        changed, want = self.poll('{"boxes":"live","size":"640x360","watch":{"cls":"Human","rev":1}}')
        self.assertEqual((changed, want.v, want.watch), (False, ("live", 640, 360), {"cls": "Human", "rect": None, "rev": 1}))

    def test_null_and_absent_watch(self):
        for body in ('{"boxes":"live","size":"640x360","watch":null}', '{"boxes":"live","size":"640x360"}'):
            self.assertIsNone(self.poll(body)[1].watch)

    def test_invalid_watch_rejects_the_file(self):
        for w in ('{"cls":"Dog","rev":1}', '{"cls":"Human","rev":"1"}', '{"cls":"Human","rev":1,"rect":[0.5,0,-0.5,-1]}',
                  '{"cls":"Human","rev":1,"rect":[0,0,1]}', '{"cls":"Human","rev":1,"rect":[-2,0,0,-1]}',
                  '{"cls":"Human","rev":1,"rect":[%s,0,1,-1]}' % ("9" * 401), '{"cls":"Human","rev":1,"rect":[false,true,1,0]}',
                  '{"cls":"Human","rev":1,"rect":["-1","0","1","-1"]}', '{"cls":"Human","rev":1,"rect":{"a":1,"b":2,"c":3,"d":4}}', '[1,2]'):
            changed, want = self.poll('{"boxes":"off","size":"640x360","watch":%s}' % w)
            self.assertEqual((changed, want.v, want.watch), (False, ("live", 640, 360), None), w)


if __name__ == "__main__":
    unittest.main()
