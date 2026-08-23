#!/usr/bin/env python3
"""Ordering and promptness guards for the posting logic.

Three invariants, each of which was broken in production and each of which is
invisible when it breaks -- the bot goes on posting, just wrongly ordered or
hours late, and the only symptom is a feed nobody is auditing at 23:00:

  1. The standings table must never precede the night's final scores. Both
     gates agree on when a night is 'done', which is NOT enough to order the
     two posts; standings went first on five of the seven nights posted
     between 16 and 22 August 2026.
  2. A live poll past midnight must look back a day. Games belong to the date
     they started on, so every 00:00, 00:15 and 00:30 poll of the season read
     the new day's untouched slate and no live box score ever went out later
     than 23:31.
  3. The wait for a crowd figure must be bounded. It was not, and one game
     (2 August 2026) was held 255 minutes.

Stdlib only: kbo_post imports nothing beyond the standard library at module
level, and every network call and post is stubbed here. Nothing is fetched,
nothing is posted, no state file is touched.
"""

import contextlib
import datetime
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kbo_post as k                                          # noqa: E402
import kbo_lock                                               # noqa: E402
import net_guard                                              # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(k.KST)
TODAY = NOW.strftime('%Y-%m-%d')
YDAY = (NOW - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
CANDIDATES = [TODAY, YDAY]


def game(date, gid, home='WO', away='HT', final=True, cancel=False):
    return {'gameId': gid, 'gameDate': date, 'gameDateTime': f'{date}T18:00:00',
            'statusCode': k.FINAL if final else 'BEFORE', 'cancel': cancel,
            'homeTeamCode': home, 'awayTeamCode': away}


class Patched(unittest.TestCase):
    """Swap out everything that would reach the network, the Keychain, Bluesky
    or a state file, and put it all back afterwards."""

    STUBS = ('fetch_games', 'fetch_attendance', 'fetch_standings', 'post_thread',
             'write_json_atomic', 'load_roster', 'load_history', 'print_segments',
             'box_score_segments', 'compose_standings', 'attach_standings_card')

    def setUp(self):
        self._saved = {n: getattr(k, n) for n in self.STUBS}
        self._lock, self._net = kbo_lock.hold, net_guard.require_network
        self._argv = sys.argv
        self.posted, self.written = [], []
        k.post_thread = lambda segs: self.posted.append(segs)
        k.write_json_atomic = lambda path, data, **kw: self.written.append(dict(data))
        k.load_roster = lambda: {}
        k.print_segments = lambda *a, **kw: None
        k.fetch_attendance = lambda d: {}
        kbo_lock.hold = lambda mode, wait: True
        net_guard.require_network = lambda t: None

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(k, n, v)
        kbo_lock.hold, net_guard.require_network = self._lock, self._net
        sys.argv = self._argv

    def run_mode(self, *args):
        sys.argv = ['kbo_post.py', *args]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            k.main()
        return out.getvalue()


class TestStandingsFollowsResults(Patched):
    """The table follows the scores, and is gated on the results post itself
    rather than on a clock -- the settling time is the one thing nobody can
    predict."""

    def pick(self, slates, history, ignore=False):
        k.fetch_games = lambda d: slates.get(d, [])
        with contextlib.redirect_stdout(io.StringIO()):
            return k.pick_standings_date(CANDIDATES, history, ignore)

    def test_posts_once_the_results_digest_has(self):
        self.assertEqual(
            self.pick({TODAY: [game(TODAY, 'A')]}, {f'results:{TODAY}': {}}), TODAY)

    def test_holds_while_the_results_digest_has_not(self):
        self.assertIsNone(self.pick({TODAY: [game(TODAY, 'A')]}, {}))

    def test_all_flag_overrides_the_gate(self):
        self.assertEqual(
            self.pick({TODAY: [game(TODAY, 'A')]}, {}, ignore=True), TODAY)

    def test_a_game_still_running_holds(self):
        slate = {TODAY: [game(TODAY, 'A'), game(TODAY, 'B', final=False)]}
        self.assertIsNone(self.pick(slate, {f'results:{TODAY}': {}}))

    def test_already_posted_holds(self):
        history = {f'results:{TODAY}': {}, f'standings:{TODAY}': {}}
        self.assertIsNone(self.pick({TODAY: [game(TODAY, 'A')]}, history))

    def test_off_day_walks_back_and_needs_that_night_posted_too(self):
        slate = {TODAY: [], YDAY: [game(YDAY, 'A')]}
        self.assertIsNone(self.pick(slate, {}))
        self.assertEqual(self.pick(slate, {f'results:{YDAY}': {}}), YDAY)

    def test_explicit_date_bypasses_the_gate(self):
        """A --date run is manual backfill; the human is the gate there."""
        k.fetch_games = lambda d: [game(d, 'A')]
        k.fetch_standings = lambda: [{'rank': 1}]
        k.compose_standings = lambda d, rows: [{'text': 'table'}]
        k.attach_standings_card = lambda d, rows, segs: segs
        k.load_history = lambda: {}
        self.run_mode('standings', '--date', YDAY, '--dry-run')
        self.assertEqual(self.posted, [])          # dry run posts nothing...
        k.load_history = lambda: {}
        self.run_mode('standings', '--date', YDAY)
        self.assertEqual(len(self.posted), 1)      # ...but a real one is not gated


class TestLiveWalksBackANight(Patched):

    def setUp(self):
        super().setUp()
        k.box_score_segments = lambda games, *a, **kw: [{'gid': games[0]['gameId']}]
        k.fetch_attendance = lambda d: {'WO': ('16,000', 'Gocheok')}

    def live(self, slates, history):
        k.fetch_games = lambda d: slates.get(d, [])
        k.load_history = lambda: dict(history)
        self.run_mode('live')
        return [s[0]['gid'] for s in self.posted]

    def test_posts_tonights_finished_game(self):
        self.assertEqual(self.live({TODAY: [game(TODAY, 'A')]}, {}), ['A'])

    def test_past_midnight_looks_back_a_day(self):
        self.assertEqual(self.live({TODAY: [], YDAY: [game(YDAY, 'A')]}, {}), ['A'])

    def test_stops_once_that_nights_roundup_has_posted(self):
        """The roundup carries the box scores live missed. Without this bound
        they would be posted a second time the following evening."""
        self.assertEqual(
            self.live({TODAY: [], YDAY: [game(YDAY, 'A')]}, {f'results:{YDAY}': {}}), [])

    def test_never_repeats_a_game_it_posted(self):
        self.assertEqual(self.live({TODAY: [game(TODAY, 'A')]}, {'live:A': {}}), [])

    def test_two_open_nights_post_oldest_first(self):
        got = self.live({TODAY: [game(TODAY, 'B')], YDAY: [game(YDAY, 'A')]}, {})
        self.assertEqual(got, ['A', 'B'])


class TestAttendanceWaitIsBounded(Patched):

    def setUp(self):
        super().setUp()
        k.fetch_games = lambda d: [game(TODAY, 'A')] if d == TODAY else []
        k.box_score_segments = lambda games, roster, added, attendance=None, **kw: (
            [{'att': (attendance or {}).get('WO')}])

    def live(self, published, waited_min):
        k.fetch_attendance = lambda d: (
            {'WO': ('16,000', 'Gocheok')} if published else {})
        history = {}
        if waited_min is not None:
            stamp = datetime.datetime.now(UTC) - datetime.timedelta(minutes=waited_min)
            history['final_seen:A'] = {'first_seen': stamp.isoformat()}
        k.load_history = lambda: dict(history)
        log = self.run_mode('live')
        return (self.posted[0][0]['att'] if self.posted else None), log

    def test_published_figure_is_carried(self):
        att, _ = self.live(True, None)
        self.assertEqual(att, ('16,000', 'Gocheok'))

    def test_first_sighting_holds_and_starts_the_clock(self):
        att, _ = self.live(False, None)
        self.assertIsNone(att)
        self.assertEqual(self.posted, [])
        self.assertIn('final_seen:A', self.written[-1])

    def test_still_holding_inside_the_grace_period(self):
        self.assertEqual(self.posted, [])
        for waited in (1, 20, 44):
            with self.subTest(waited=waited):
                self.live(False, waited)
                self.assertEqual(self.posted, [])

    def test_posts_without_the_figure_once_the_grace_is_up(self):
        att, log = self.live(False, 46)
        self.assertEqual(len(self.posted), 1)
        self.assertIsNone(att)
        self.assertIn('posting without it', log)

    def test_the_255_minute_case_no_longer_waits(self):
        """2 August 2026: a 14:00 day game held until 21:45."""
        att, _ = self.live(False, 255)
        self.assertEqual(len(self.posted), 1)
        self.assertIsNone(att)

    def test_a_damaged_stamp_restarts_rather_than_freezing_the_game(self):
        """Returning 0 without rewriting would hold the game for ever, which is
        worse than the unbounded wait this replaced."""
        k.fetch_attendance = lambda d: {}
        k.load_history = lambda: {'final_seen:A': {'first_seen': 'not-a-date'}}
        log = self.run_mode('live')
        self.assertIn('restarting its clock', log)
        self.assertNotEqual(self.written[-1]['final_seen:A']['first_seen'], 'not-a-date')

    def test_the_hold_stamp_is_dropped_once_the_game_is_out(self):
        self.live(True, 20)
        self.assertNotIn('final_seen:A', self.written[-1])


if __name__ == '__main__':
    unittest.main()
