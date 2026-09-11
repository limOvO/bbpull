"""Offline tests for the pure animation helpers.

`Tween.advance(now)` takes an explicit timestamp, so easing is verified by
stepping frames deterministically instead of sleeping on real time.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull.gui import anim  # noqa: E402


class EasingTests(unittest.TestCase):
    def test_all_easings_are_anchored(self):
        for name, fn in anim.EASINGS.items():
            self.assertAlmostEqual(fn(0.0), 0.0, places=6, msg=name)
            self.assertAlmostEqual(fn(1.0), 1.0, places=6, msg=name)

    def test_easings_are_clamped_outside_range(self):
        for name, fn in anim.EASINGS.items():
            self.assertAlmostEqual(fn(-1.0), 0.0, places=6, msg=name)
            self.assertAlmostEqual(fn(2.0), 1.0, places=6, msg=name)

    def test_out_cubic_is_ahead_of_linear(self):
        self.assertGreater(anim.ease_out_cubic(0.3), 0.3)
        self.assertGreater(anim.ease_out_cubic(0.5), 0.5)

    def test_lerp_endpoints(self):
        self.assertEqual(anim.lerp(0, 10, 0), 0)
        self.assertEqual(anim.lerp(0, 10, 1), 10)
        self.assertEqual(anim.lerp(0, 10, 0.5), 5)


class ColorTests(unittest.TestCase):
    def test_hex_round_trip(self):
        self.assertEqual(anim.hex_to_rgb("#6d6cf6"), (0x6D, 0x6C, 0xF6))
        self.assertEqual(anim.rgb_to_hex((0x6D, 0x6C, 0xF6)), "#6d6cf6")

    def test_short_hex_form(self):
        self.assertEqual(anim.hex_to_rgb("#fff"), (255, 255, 255))

    def test_malformed_hex_is_safe(self):
        self.assertEqual(anim.hex_to_rgb(""), (0, 0, 0))
        self.assertEqual(anim.hex_to_rgb("nope"), (0, 0, 0))
        self.assertEqual(anim.hex_to_rgb("#zzzzzz"), (0, 0, 0))

    def test_blend_endpoints_and_middle(self):
        self.assertEqual(anim.lerp_color("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(anim.lerp_color("#000000", "#ffffff", 1), "#ffffff")
        self.assertEqual(anim.lerp_color("#000000", "#ffffff", 0.5), "#808080")

    def test_blend_is_clamped(self):
        self.assertEqual(anim.lerp_color("#000000", "#ffffff", 2), "#ffffff")
        self.assertEqual(anim.lerp_color("#000000", "#ffffff", -1), "#000000")

    def test_result_is_always_a_valid_color(self):
        for t in (0.0, 0.17, 0.5, 0.83, 1.0):
            value = anim.lerp_color("#0e1016", "#232641", t)
            self.assertRegex(value, r"^#[0-9a-f]{6}$")


class TweenTests(unittest.TestCase):
    def test_runs_to_completion_and_calls_on_done_once(self):
        frames = []
        done = []
        tween = anim.Tween(100, lambda e, r: frames.append((e, r)),
                           on_done=lambda: done.append(1), clock=lambda: 0.0)
        tween.advance(0.0)
        tween.advance(0.05)
        finished = tween.advance(0.10)
        self.assertTrue(finished)
        self.assertTrue(tween.done)
        self.assertEqual(len(done), 1)
        self.assertGreaterEqual(len(frames), 3)
        # Progress is monotonic.
        raws = [r for _e, r in frames]
        self.assertEqual(raws, sorted(raws))

    def test_first_frame_is_applied_immediately(self):
        """A short animation must never be skipped entirely."""
        seen = []
        tween = anim.Tween(1, lambda e, r: seen.append(r), clock=lambda: 0.0)
        tween.advance(0.0)
        self.assertTrue(seen, "first frame should be applied")

    def test_advance_after_done_is_a_noop(self):
        count = []
        tween = anim.Tween(10, lambda e, r: count.append(1), clock=lambda: 0.0)
        tween.advance(0.0)
        tween.advance(1.0)
        before = len(count)
        tween.advance(2.0)
        self.assertEqual(len(count), before)

    def test_cancel_stops_frames(self):
        count = []
        tween = anim.Tween(100, lambda e, r: count.append(1), clock=lambda: 0.0)
        tween.advance(0.0)
        tween.cancel()
        tween.advance(1.0)
        self.assertTrue(tween.cancelled)
        self.assertEqual(len(count), 1)

    def test_eased_and_raw_progress_agree_at_ends(self):
        seen = []
        tween = anim.Tween(100, lambda e, r: seen.append((e, r)), clock=lambda: 0.0)
        tween.advance(0.0)
        tween.advance(0.1)
        first, last = seen[0], seen[-1]
        self.assertAlmostEqual(first[0], 0.0, places=6)
        self.assertAlmostEqual(last[0], 1.0, places=6)
        self.assertAlmostEqual(last[1], 1.0, places=6)


class StaggerTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(anim.stagger_delays(0, 200), [])

    def test_single_item_starts_at_zero(self):
        self.assertEqual(anim.stagger_delays(1, 200), [0])

    def test_delays_are_monotonic_and_bounded(self):
        delays = anim.stagger_delays(10, 200)
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[0], 0)
        self.assertLessEqual(max(delays), 200)

    def test_long_lists_finish_on_time(self):
        """A 400-row list must not stagger for 6 seconds."""
        delays = anim.stagger_delays(400, 240, max_items=20)
        self.assertEqual(len(delays), 400)
        self.assertLessEqual(max(delays), 240)
        # All rows beyond the stagger window share the tail.
        self.assertEqual(delays[-1], 240)
        self.assertEqual(len(set(delays[20:])), 1)


class FakeAnimatorHost:
    """Minimal stand-in for a Tk widget: records `after` without a real loop."""

    def __init__(self):
        self.calls = []
        self.cancelled = []

    def after(self, delay, callback):
        token = f"t{len(self.calls)}"
        self.calls.append((token, delay, callback))
        return token

    def after_cancel(self, token):
        self.cancelled.append(token)


class AnimatorSchedulingTests(unittest.TestCase):
    """The Animator bookkeeping is verified without a display."""

    def test_run_completes_immediately_when_duration_is_tiny(self):
        host = FakeAnimatorHost()
        animator = anim.Animator(host, interval_ms=16)
        frames = []
        animator.run(1, lambda e, r: frames.append(r))
        # No real clock advances inside the call, but the first frame is applied
        # and exactly one timer was scheduled (or none if it finished).
        self.assertTrue(frames)

    def test_after_schedules_and_tracks(self):
        host = FakeAnimatorHost()
        animator = anim.Animator(host)
        hits = []
        token = animator.after(50, lambda: hits.append(1))
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(host.calls[0][1], 50)
        self.assertEqual(animator.active_count, 1)
        animator.cancel(token)
        self.assertEqual(animator.active_count, 0)

    def test_stop_all_cancels_everything(self):
        host = FakeAnimatorHost()
        animator = anim.Animator(host)
        animator.after(10, lambda: None)
        animator.after(20, lambda: None)
        self.assertEqual(animator.active_count, 2)
        animator.stop_all()
        self.assertEqual(animator.active_count, 0)

    def test_stopped_animator_refuses_new_work(self):
        host = FakeAnimatorHost()
        animator = anim.Animator(host)
        animator.stop_all()
        self.assertIsNone(animator.after(10, lambda: None))
        self.assertIsNone(animator.run(10, lambda e, r: None))
        animator.resume()
        self.assertIsNotNone(animator.after(10, lambda: None))

    def test_cancelling_none_is_safe(self):
        host = FakeAnimatorHost()
        animator = anim.Animator(host)
        animator.cancel(None)  # must not raise


class SpinnerTests(unittest.TestCase):
    """Spinner is exercised through a fake canvas so no Tk root is needed."""

    class FakeCanvas:
        def __init__(self):
            self.items = []
            self.config = {}

        def delete(self, _what):
            self.items = []

        def create_arc(self, *args, **kwargs):
            self.items.append(("arc", kwargs))
            return 1

        def itemconfigure(self, item, **kwargs):
            self.config[item] = kwargs

    def test_start_draws_and_schedules(self):
        host = FakeAnimatorHost()
        animator = anim.Animator(host)
        canvas = self.FakeCanvas()
        spinner = anim.Spinner(canvas, _Palette(), animator=animator, step_ms=16)
        spinner.start()
        self.assertTrue(canvas.items, "an arc should be drawn")
        self.assertTrue(host.calls, "a rotation timer should be scheduled")
        spinner.stop()
        self.assertEqual(canvas.items, [])

    def test_stop_is_idempotent(self):
        host = FakeAnimatorHost()
        animator = anim.Animator(host)
        spinner = anim.Spinner(self.FakeCanvas(), _Palette(), animator=animator)
        spinner.start()
        spinner.stop()
        spinner.stop()

    def test_without_animator_start_is_a_noop(self):
        spinner = anim.Spinner(self.FakeCanvas(), _Palette(), animator=None)
        spinner.start()
        self.assertFalse(spinner._running)


def _Palette():
    from bbpull.gui.theme import Palette

    return Palette("dark")


if __name__ == "__main__":
    unittest.main()
