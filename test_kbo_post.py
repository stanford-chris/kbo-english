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


def game(date, gid, home='WO', away='HT', final=True, cancel=False,
         round_code='kbo_r'):
    return {'gameId': gid, 'gameDate': date, 'gameDateTime': f'{date}T18:00:00',
            'statusCode': k.FINAL if final else 'BEFORE', 'cancel': cancel,
            'homeTeamCode': home, 'awayTeamCode': away, 'roundCode': round_code}


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


class TestPostseasonLeavesTablesAlone(Patched):
    """A playoff game moves neither the table nor the season leaderboards, so
    neither post repeats itself through October."""

    def pick(self, slates, history):
        k.fetch_games = lambda d: slates.get(d, [])
        with contextlib.redirect_stdout(io.StringIO()):
            return k.pick_standings_date(CANDIDATES, history, False)

    def test_a_playoff_night_is_an_off_day_for_standings(self):
        slate = {TODAY: [game(TODAY, 'P', round_code='kbo_ps_ks')],
                 YDAY: [game(YDAY, 'A')]}
        history = {f'results:{TODAY}': {}, f'results:{YDAY}': {},
                   f'standings:{YDAY}': {}}
        self.assertIsNone(self.pick(slate, history))

    def test_an_unfinished_playoff_game_does_not_hold_anything(self):
        slate = {TODAY: [game(TODAY, 'P', final=False, round_code='kbo_ps_wd')],
                 YDAY: [game(YDAY, 'A')]}
        self.assertEqual(self.pick(slate, {f'results:{YDAY}': {}}), YDAY)

    def test_leaders_skip_a_week_of_only_playoff_games(self):
        k.fetch_games = lambda d: [game(d, 'P', round_code='kbo_ps_po')]
        self.assertFalse(k.regular_season_week(TODAY))

    def test_leaders_skip_a_week_with_no_games(self):
        k.fetch_games = lambda d: []
        self.assertFalse(k.regular_season_week(TODAY))

    def test_leaders_post_after_a_week_with_one_regular_game(self):
        last = (NOW - datetime.timedelta(days=k.LEADERS_LOOKBACK_DAYS)).strftime('%Y-%m-%d')
        k.fetch_games = lambda d: [game(d, 'A')] if d == last else []
        self.assertTrue(k.regular_season_week(TODAY))

    def test_a_missing_round_code_counts_as_regular_season(self):
        self.assertFalse(k.is_postseason({'gameId': 'X'}))


def ps_game(date, home, away, rc, no, winner=None, outcome=None):
    g = game(date, f'{date}{away}{home}', home=home, away=away,
             final=winner is not None, round_code=rc)
    g.update(seriesGameNo=no, winner=winner, seriesOutcome=outcome)
    return g


class TestPostseasonLabels(Patched):
    """Series scores are worked out from the previous finished game, then
    checked here against the shape of the real 2025 feed."""

    def feed(self, games):
        k.fetch_games = lambda d: [g for g in games if g['gameDate'] == d]

    def test_wild_card_opener_carries_the_head_start(self):
        g = ps_game('2025-10-06', 'SS', 'NC', 'kbo_ps_wd', 1)
        self.feed([g])
        self.assertEqual(k.postseason_label(g, final=False),
                         ('Wild Card', 'Game 1 · Samsung leads series 1–0'))

    def test_wild_card_upset_ties_it_and_game_two_decides(self):
        g1 = ps_game('2025-10-06', 'SS', 'NC', 'kbo_ps_wd', 1, 'AWAY',
                     {'home': 1, 'draw': 0, 'away': 1})
        g2 = ps_game('2025-10-07', 'SS', 'NC', 'kbo_ps_wd', 2, 'HOME')
        self.feed([g1, g2])
        self.assertEqual(k.postseason_label(g2, final=False)[1],
                         'Game 2 · Series tied 1–1')
        self.assertEqual(k.postseason_label(g2, final=True)[1],
                         'Game 2 · Samsung advances')

    def test_wild_card_won_in_one_game_also_advances(self):
        g1 = ps_game('2025-10-06', 'SS', 'NC', 'kbo_ps_wd', 1, 'HOME')
        self.feed([g1])
        self.assertEqual(k.postseason_label(g1, final=True)[1],
                         'Game 1 · Samsung advances')

    def test_series_score_follows_the_clubs_when_home_and_away_swap(self):
        g2 = ps_game('2025-10-27', 'LG', 'HH', 'kbo_ps_ks', 2, 'HOME',
                     {'home': 2, 'draw': 0, 'away': 0})
        g3 = ps_game('2025-10-29', 'HH', 'LG', 'kbo_ps_ks', 3, 'HOME')
        self.feed([g2, g3])
        self.assertEqual(k.series_after(g3), {'LG': 2, 'HH': 1})
        self.assertEqual(k.postseason_label(g3, final=True),
                         ('Korean Series', 'Game 3 · LG leads series 2–1'))

    def test_a_rained_out_game_is_not_counted(self):
        ppd = ps_game('2025-10-10', 'SK', 'SS', 'kbo_ps_sp', 2, 'AWAY',
                      {'home': 0, 'draw': 0, 'away': 1})
        ppd['cancel'] = True
        g = ps_game('2025-10-11', 'SK', 'SS', 'kbo_ps_sp', 2)
        self.feed([ppd, g])
        self.assertEqual(k.postseason_label(g, final=False),
                         ('Semi-Playoff', 'Game 2'))

    def test_the_korean_series_clincher_is_titled_champions(self):
        g4 = ps_game('2025-10-30', 'HH', 'LG', 'kbo_ps_ks', 4, 'AWAY',
                     {'home': 1, 'draw': 0, 'away': 3})
        g5 = ps_game('2025-10-31', 'HH', 'LG', 'kbo_ps_ks', 5, 'AWAY')
        self.feed([g4, g5])
        self.assertEqual(k.postseason_label(g5, final=True),
                         ('Korean Series Champions', 'Game 5 · LG wins series 4–1'))
        self.assertEqual(k.postseason_label(g5, final=False)[0], 'Korean Series')
        self.assertEqual(k.postseason_label(g4, final=True)[0], 'Korean Series')

    def test_seven_game_series_needs_four(self):
        self.assertEqual(k.series_text({'LG': 3, 'HH': 1}, 4), 'LG leads series 3–1')
        self.assertEqual(k.series_text({'LG': 4, 'HH': 1}, 4), 'LG wins series 4–1')

    def test_regular_season_game_has_no_label(self):
        self.assertIsNone(k.postseason_label(game(TODAY, 'A'), final=True))
        self.assertIsNone(k.slate_label([game(TODAY, 'A')], final=True))

    def test_alt_sentence(self):
        import kbo_card_data as data
        self.assertEqual(data.label_alt(('Korean Series', 'Game 2 · LG leads series 2–0')),
                         'Korean Series, Game 2. LG leads series 2–0.')

    def test_alt_keeps_the_opening_the_health_check_reads(self):
        import kbo_card_data as data
        alt = data.results_alt('Monday, October 27', [], (),
                               label=('Korean Series', 'Game 2'))
        self.assertTrue(alt.startswith('Final scores for '))


class TestScheduleAltEndsOnce(unittest.TestCase):

    def test_a_time_ending_in_p_m_gets_no_second_stop(self):
        import kbo_card_data as data
        alt = data.schedule_alt('Sunday, October 26',
                                [{'away_name': 'A', 'home_name': 'B',
                                  'time': '2 p.m.'}], '')
        self.assertTrue(alt.endswith('A at B, 2 p.m.'))


class TestFieldPostsOnceTheSeasonEnds(Patched):

    def due(self, today, slates, history=None):
        k.fetch_games = lambda d: slates.get(d, [])
        return k.field_due(today, history or {}, False)

    def test_due_the_day_after_the_last_regular_season_game(self):
        self.assertTrue(self.due('2026-10-13', {'2026-10-12': [game('2026-10-12', 'A')]}))

    def test_not_due_while_a_regular_season_game_is_still_ahead(self):
        self.assertFalse(self.due('2026-10-13', {'2026-10-20': [game('2026-10-20', 'A')]}))

    def test_playoff_games_ahead_do_not_hold_it(self):
        ps = [game('2026-10-15', 'P', round_code='kbo_ps_wd')]
        self.assertTrue(self.due('2026-10-13', {'2026-10-15': ps}))

    def test_posts_once(self):
        self.assertFalse(self.due('2026-10-14', {}, {'field:2026': {}}))

    def test_never_looks_outside_the_window(self):
        def boom(d):
            raise AssertionError('fetched outside the window')
        k.fetch_games = boom
        self.assertFalse(k.field_due('2026-08-01', {}, False))
        self.assertFalse(k.field_due('2027-01-10', {}, False))


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
        # The weekday leads, his call on 24 September 2026.
        self.assertEqual(kbo_card_data.card_date('2026-09-12'), 'Saturday, September 12')
        self.assertEqual(kbo_card_data.card_date('2026-07-01'), 'Wednesday, July 1')


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


try:
    import atproto                                            # noqa: F401
    from atproto_client.exceptions import InvokeTimeoutError
except ImportError:                                           # pragma: no cover
    atproto = None


@unittest.skipIf(atproto is None, 'needs atproto (post_thread imports it)')
class TestTimedOutSendThatLanded(unittest.TestCase):
    """30 September 2026: a live box score's send raised InvokeTimeoutError
    after the server had created the post, so it was never recorded and the
    results roundup posted the same card again five minutes later. A timed-out
    send now finds itself on the feed and carries on; one that did not land
    still raises, so the next poll retries as before."""

    ALT = 'Box score for Wednesday, September 30. NC Dinos 5, Doosan Bears 6.'

    def setUp(self):
        self._saved = (k.keychain_password, atproto.Client, k.time.sleep)
        k.keychain_password = lambda a, s: 'pw'
        k.time.sleep = lambda s: None
        self.client, self.preload = None, []
        test = self

        class FakeClient:
            def __init__(self):
                test.client = self
                self.feed, self.sent = list(test.preload), []

            def login(self, *a):
                pass

            def _post(self, text, alt, reply_to):
                n = len(self.sent) + 1
                self.sent.append((text, alt, reply_to))
                post = _Post(f'at://did/app.bsky.feed.post/{n}', text, alt,
                             datetime.datetime.now(UTC).isoformat())
                if alt == test.times_out:                  # server keeps it, reply lost
                    if test.lands:
                        self.feed.insert(0, post)
                    raise InvokeTimeoutError()
                self.feed.insert(0, post)
                return post

            def send_image(self, text, image, image_alt, reply_to, image_aspect_ratio):
                return self._post(text.build_text(), image_alt, reply_to)

            def send_post(self, text, reply_to):
                return self._post(text.build_text(), None, reply_to)

            def get_author_feed(self, actor, limit, filter):
                return _Ns(feed=[_Ns(post=p) for p in self.feed[:limit]])

        atproto.Client = FakeClient

    def tearDown(self):
        k.keychain_password, atproto.Client, k.time.sleep = self._saved

    def segs(self):
        card = lambda alt: {'png': b'', 'alt': alt, 'size': (10, 10)}
        return [('Final scores', k.HASHTAGS, card('digest')),
                ('', (), card(self.ALT)),
                ('', (), card('Box score, Kiwoom at Lotte.'))]

    def run_thread(self):
        with contextlib.redirect_stdout(io.StringIO()):
            k.post_thread(self.segs())

    def test_landed_send_is_adopted_and_the_thread_carries_on(self):
        self.times_out, self.lands = self.ALT, True
        self.run_thread()
        self.assertEqual(len(self.client.sent), 3)           # nothing re-sent
        last_reply = self.client.sent[2][2]
        self.assertEqual(last_reply.parent.uri, 'at://did/app.bsky.feed.post/2')

    def test_send_that_did_not_land_still_raises(self):
        self.times_out, self.lands = self.ALT, False
        with self.assertRaises(InvokeTimeoutError):
            self.run_thread()

    def test_another_recent_box_score_is_not_mistaken_for_it(self):
        # The live run had posted LG@SSG six seconds before NC@OB timed out:
        # same empty text, different card.
        self.times_out, self.lands = self.ALT, False
        self.preload = [_Post('at://did/lg', '', 'Box score, LG at SSG.',
                              datetime.datetime.now(UTC).isoformat())]
        with self.assertRaises(InvokeTimeoutError):
            self.run_thread()

    def test_an_older_post_with_the_same_card_is_not_mistaken_for_it(self):
        self.times_out, self.lands = self.ALT, False
        old = (datetime.datetime.now(UTC)
               - datetime.timedelta(seconds=k.LANDED_SLACK + 60)).isoformat()
        with contextlib.redirect_stdout(io.StringIO()):
            k.post_thread([])                                 # builds the client
        self.client.feed = [_Post('at://did/old', '', self.ALT, old)]
        orig = atproto.Client
        atproto.Client = lambda: self.client
        try:
            with self.assertRaises(InvokeTimeoutError):
                self.run_thread()
        finally:
            atproto.Client = orig


class _Ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _Post(uri, text, alt, created):
    embed = _Ns(images=[_Ns(alt=alt)]) if alt is not None else None
    return _Ns(uri=uri, cid='bafy' + uri[-1],
               record=_Ns(text=text, embed=embed, created_at=created))


if __name__ == '__main__':
    unittest.main()
