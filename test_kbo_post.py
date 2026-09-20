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


class TestDatesAreUSOrder(unittest.TestCase):
    """This account writes dates month first (his call, 12 September 2026),
    over the house day-first rule. Both formatters must agree: the text
    posts abbreviate, the cards spell the month out."""

    def test_post_text_is_month_day_abbreviated(self):
        self.assertEqual(k.format_date('2026-09-12'), 'Sep 12')
        self.assertEqual(k.format_date('2026-07-01'), 'Jul 1')

    def test_card_label_is_month_day_spelled_out(self):
        import kbo_card_data
        self.assertEqual(kbo_card_data.card_date('2026-09-12'), 'September 12')
        self.assertEqual(kbo_card_data.card_date('2026-07-01'), 'July 1')


class TestBoxCardRecords(unittest.TestCase):
    """The box score card prints each club's season record after its name
    (his ask, 20 September 2026), read off the /record payload's own
    awayStandings/homeStandings block — the record as of that game, which is
    what a card for that game should say — and says it in the alt too."""

    GAME = {'gameId': '20260920WOSK02026', 'awayTeamCode': 'WO',
            'awayTeamScore': 5, 'homeTeamCode': 'SK', 'homeTeamScore': 10}
    RECORD = {'awayStandings': {'w': 45, 'l': 85, 'd': 4, 'rank': 10},
              'homeStandings': {'w': 58, 'l': 69, 'd': 5, 'rank': 7}}

    def test_record_is_w_dash_l_without_draws(self):
        import kbo_card_data
        self.assertEqual(kbo_card_data.team_record({'w': 45, 'l': 85, 'd': 4}),
                         '45-85')

    def test_missing_block_or_figure_gives_empty_not_a_crash(self):
        import kbo_card_data
        self.assertEqual(kbo_card_data.team_record(None), '')
        self.assertEqual(kbo_card_data.team_record({}), '')
        self.assertEqual(kbo_card_data.team_record({'w': 45}), '')
        self.assertEqual(kbo_card_data.team_record({'w': None, 'l': 85}), '')

    def test_box_input_carries_both_records(self):
        import kbo_card_data
        game = kbo_card_data.box_input(self.GAME, self.RECORD, {}, [])
        self.assertEqual(game['away_record'], '45-85')
        self.assertEqual(game['home_record'], '58-69')

    def test_box_input_without_standings_block_omits_them(self):
        import kbo_card_data
        game = kbo_card_data.box_input(self.GAME, {}, {}, [])
        self.assertEqual(game['away_record'], '')
        self.assertEqual(game['home_record'], '')

    def test_alt_says_the_record_the_card_shows(self):
        import kbo_card_data
        game = kbo_card_data.box_input(self.GAME, self.RECORD, {}, [])
        alt = kbo_card_data.box_alt('September 20', game)
        self.assertIn('Kiwoom Heroes (45-85) 5, SSG Landers (58-69) 10.', alt)

    def test_alt_without_a_record_has_no_empty_parentheses(self):
        import kbo_card_data
        game = kbo_card_data.box_input(self.GAME, {}, {}, [])
        alt = kbo_card_data.box_alt('September 20', game)
        self.assertIn('Kiwoom Heroes 5, SSG Landers 10.', alt)
        self.assertNotIn('()', alt)

    def test_card_html_puts_the_record_in_its_own_muted_span(self):
        import kbo_card
        import kbo_card_data
        game = kbo_card_data.box_input(self.GAME, self.RECORD, {}, [])
        seen = {}
        real = kbo_card._shoot
        kbo_card._shoot = lambda html, path, label: seen.setdefault('html', html) or (path, (1, 1))
        try:
            kbo_card.render_box_score_card('September 20', game, 'x.png')
        finally:
            kbo_card._shoot = real
        self.assertIn('Kiwoom Heroes<span class="rec">(45-85)</span>', seen['html'])
        self.assertIn('SSG Landers<span class="rec">(58-69)</span>', seen['html'])
        self.assertIn(f'.tm .n .rec{{color:{kbo_card.MUTED};font-weight:400',
                      seen['html'])

    def test_card_html_without_a_record_has_no_span(self):
        import kbo_card
        import kbo_card_data
        game = kbo_card_data.box_input(self.GAME, {}, {}, [])
        seen = {}
        real = kbo_card._shoot
        kbo_card._shoot = lambda html, path, label: seen.setdefault('html', html) or (path, (1, 1))
        try:
            kbo_card.render_box_score_card('September 20', game, 'x.png')
        finally:
            kbo_card._shoot = real
        self.assertNotIn('class="rec"', seen['html'])


class TestDigestCardRecords(unittest.TestCase):
    """The nightly final-scores digest prints the same season record after
    each club's name as the box score card (his ask, 20 September 2026,
    right after the box card got it), from the same per-game /record block."""

    GAMES = [{'gameId': '20260919LGHH02026', 'gameDateTime': '2026-09-19T18:00',
              'awayTeamCode': 'LG', 'awayTeamScore': 2,
              'homeTeamCode': 'HH', 'homeTeamScore': 1}]
    RECORDS = {'20260919LGHH02026': {
        'awayStandings': {'w': 74, 'l': 55, 'd': 1},
        'homeStandings': {'w': 54, 'l': 71, 'd': 4},
        'pitchingResult': []}}

    def test_results_input_carries_both_records(self):
        import kbo_card_data
        rows = kbo_card_data.results_input(self.GAMES, self.RECORDS, {}, [])
        self.assertEqual(rows[0]['away_record'], '74-55')
        self.assertEqual(rows[0]['home_record'], '54-71')

    def test_a_game_with_no_record_fetched_omits_them(self):
        import kbo_card_data
        rows = kbo_card_data.results_input(self.GAMES, {}, {}, [])
        self.assertEqual(rows[0]['away_record'], '')
        self.assertEqual(rows[0]['home_record'], '')

    def test_alt_says_the_records_the_card_shows(self):
        import kbo_card_data
        rows = kbo_card_data.results_input(self.GAMES, self.RECORDS, {}, [])
        alt = kbo_card_data.results_alt('September 19', rows)
        self.assertIn('LG Twins (74-55) beat Hanwha Eagles (54-71) 2–1', alt)

    def test_alt_without_records_has_no_empty_parentheses(self):
        import kbo_card_data
        rows = kbo_card_data.results_input(self.GAMES, {}, {}, [])
        alt = kbo_card_data.results_alt('September 19', rows)
        self.assertIn('LG Twins beat Hanwha Eagles 2–1', alt)
        self.assertNotIn('()', alt)

    def _html(self, rows):
        import kbo_card
        seen = {}
        real = kbo_card._shoot
        kbo_card._shoot = lambda html, path, label: seen.setdefault('html', html) or (path, (1, 1))
        try:
            kbo_card.render_results_card('September 19', rows, 'x.png')
        finally:
            kbo_card._shoot = real
        return seen['html']

    def test_card_html_puts_the_record_in_its_own_muted_span(self):
        import kbo_card
        import kbo_card_data
        rows = kbo_card_data.results_input(self.GAMES, self.RECORDS, {}, [])
        html = self._html(rows)
        self.assertIn('LG Twins <span class="rec">(74-55)</span>', html)
        self.assertIn('Hanwha Eagles <span class="rec">(54-71)</span>', html)
        self.assertIn(f'.nm .rec{{color:{kbo_card.MUTED};font-weight:400', html)

    def test_card_html_without_records_has_no_span(self):
        import kbo_card_data
        rows = kbo_card_data.results_input(self.GAMES, {}, {}, [])
        self.assertNotIn('class="rec"', self._html(rows))


if __name__ == '__main__':
    unittest.main()
