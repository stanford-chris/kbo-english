#!/usr/bin/env python3
"""Every card the bot posts must come out landscape.

⚠️ This is not a style preference. Bluesky fits a lone image inside a square
box — 515 px on the web client, 290 on mobile, neither moving with the viewport
width — and scales the image to fit. A landscape card hits the width cap and is
drawn at the full 515; a card taller than it is wide hits the HEIGHT cap and its
width falls out proportionally, so it is drawn narrower than everything around
it, and its type is scaled down with it.

Both times this happened it was found by eye, weeks or days late:

  * 8c10688 (20 Aug 2026) put the opponent line and the pitch mix on the
    starters card, two rows per pitcher. A five-fixture slate went 1240x1306 to
    1240x1721 and shipped at 371 px for two days.
  * The standings card had been portrait for as long as it had listed ten
    clubs, and nobody had ever remarked on it.

kbo_card._check_landscape is the tripwire at post time, and it deliberately
reports rather than raising: a portrait card still ships, because dropping to
the plaintext fallback would trade the bot's whole look for a loss of width.
This file is the half that runs BEFORE the change ships. It is picked up by
harden_audit.sh check 12b, which globs ~/Projects/*/test_*.py.

Renders through real Chrome, so it needs the same things a post does. Missing
Chrome or logos SKIPS rather than fails — a check that could not run must not
be dressed up as one that passed or one that failed.

⚠️ Six real renders, so this takes ~20 seconds where the other suites in
~/Projects take under a second between them. That is the cost of measuring the
actual rendered card rather than asserting against numbers copied into a
fixture, which would agree with itself forever while the feed drifted.

    python3 -m unittest test_card_ratios -v
"""

import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import kbo_card
import kbo_card_data as data

HERE = Path(__file__).resolve().parent
LOGOS = HERE / 'logos'

# The ten clubs, in a plausible table order. KBO is a fixed ten-club league, so
# the standings card's height does not vary the way the others' do.
CLUBS = [
    ('KT', 'KT Wiz'), ('SS', 'Samsung Lions'), ('HT', 'KIA Tigers'),
    ('LG', 'LG Twins'), ('OB', 'Doosan Bears'), ('NC', 'NC Dinos'),
    ('LT', 'Lotte Giants'), ('HH', 'Hanwha Eagles'), ('SK', 'SSG Landers'),
    ('WO', 'Kiwoom Heroes'),
]
# Five fixtures is a full slate: ten clubs, all playing.
FULL_SLATE = 5


def mark(code, prefix):
    return {f'{prefix}_logo': str(LOGOS / f'{code}.png')}


def fixture(i, **extra):
    """Fixture i of a full slate, away club 2i and home club 2i+1."""
    (ac, an), (hc, hn) = CLUBS[i * 2], CLUBS[i * 2 + 1]
    return {**mark(ac, 'away'), 'away_name': an,
            **mark(hc, 'home'), 'home_name': hn, **extra}


class CardRatioTests(unittest.TestCase):
    """Each card at the largest it can realistically be on a given day."""

    @classmethod
    def setUpClass(cls):
        if not kbo_card.find_chrome():
            raise unittest.SkipTest('no Chrome — card ratios NOT CHECKED')
        if not LOGOS.is_dir() or not any(LOGOS.glob('*.png')):
            raise unittest.SkipTest('no club logos — card ratios NOT CHECKED')
        cls._tmp = TemporaryDirectory()
        cls.out = Path(cls._tmp.name)
        # ⚠️ Silence the shared estate log for the duration. A test that fails
        # renders a genuinely portrait card, so the live tripwire fires and
        # files a finding — and a finding raised by a test asserts to the
        # Sunday review that a card in the FEED is portrait, which it is not.
        # Two such lines were written while this file was being verified, and
        # had to be taken back out of memory/observations.jsonl by hand.
        cls._real_observe = kbo_card._observe_portrait
        kbo_card._observe_portrait = lambda label, text: None

    @classmethod
    def tearDownClass(cls):
        kbo_card._observe_portrait = cls._real_observe
        cls._tmp.cleanup()

    def assertLandscape(self, name, size):
        w, h = size
        shown = round(kbo_card.FIT_BOX * w / h)
        self.assertGreaterEqual(
            w, h,
            f'the {name} card is portrait at {w}x{h} (ratio {w / h:.3f}). '
            f'Bluesky will draw it {shown}px wide instead of '
            f'{kbo_card.FIT_BOX}. Shorten it — widening the card does not '
            f'help, because a height-capped image is scaled by '
            f'{kbo_card.FIT_BOX}/height however wide it is.')

    def path(self, name):
        return str(self.out / f'{name}.png')

    # -- the two that have actually gone portrait ---------------------------

    def test_standings_full_table_with_cutline(self):
        """Ten clubs and the postseason line: this card's worst case, and the
        only shape it ever takes on a day the table has moved."""
        rows = [{**mark(c, 'team'), 'name': n, 'w': '63', 'l': '41',
                 'gb': '' if i == 0 else '26.5'}
                for i, (c, n) in enumerate(CLUBS)]
        _, size = kbo_card.render_standings_card(
            '20 August', rows, self.path('standings'), cut_after=5)
        self.assertLandscape('standings', size)

    def test_starters_every_chunk_of_a_full_slate(self):
        """⚠️ Goes through starters_chunks rather than hardcoding its cap, so
        raising STARTERS_PER_CARD fails here instead of on the feed. The cap
        exists only to hold this constraint; nothing else enforces it."""
        rows = [fixture(i, away_pitcher='Bruce Zimmermann',
                        home_pitcher='Wang Yan-Cheng',
                        away_record='10-4, 3.43', home_record='0-2, 9.00',
                        away_vs='vs Hanwha 0-0, 7.59',
                        home_vs='vs LG 0-0, 0.00',
                        # The longest mix that has gone out: 41 characters.
                        away_mix='Fastball 40%  Curveball 17%  Two-seam 15%',
                        home_mix='Slider 26%  Two-seam 20%  Changeup 19%')
                for i in range(FULL_SLATE)]
        chunks = data.starters_chunks(rows)
        self.assertTrue(chunks)
        for i, chunk in enumerate(chunks, 1):
            _, size = kbo_card.render_starters_card(
                '21 August', chunk, self.path(f'starters{i}'),
                title=f'Probable Starters ({i} of {len(chunks)})')
            self.assertLandscape(f'starters, part {i} of {len(chunks)}', size)

    # -- the rest, so a new row anywhere is caught the same way -------------

    def test_schedule_full_slate_with_staggered_times(self):
        """Per-fixture times are the taller of the card's two forms: a uniform
        start puts the time in the subtitle and drops the per-line column."""
        games = [fixture(i, time='6:30 p.m.') for i in range(FULL_SLATE)]
        _, size = kbo_card.render_schedule_card(
            '21 August', games, self.path('schedule'))
        self.assertLandscape('schedule', size)

    def test_results_full_slate_plus_a_rainout(self):
        games = [{**fixture(i), 'away_score': '16', 'home_score': '4',
                  'winner': 'away', 'note': 'rout'}
                 for i in range(FULL_SLATE - 1)]
        _, size = kbo_card.render_results_card(
            '20 August', games, self.path('results'),
            postponed=[fixture(FULL_SLATE - 1)])
        self.assertLandscape('final scores', size)

    def test_box_score_extra_innings_and_every_decision(self):
        """A 12-inning game with all three pitcher decisions, home runs for
        both clubs and an attendance row: the tallest this card gets."""
        game = {**fixture(0), 'away_score': '7', 'home_score': '6',
                'line': {**mark('KT', 'away'), **mark('SS', 'home'),
                         'away_inn': list('001000200013'),
                         'home_inn': list('20000010001'),
                         'away_rhe': ('7', '14', '1'),
                         'home_rhe': ('6', '11', '2')},
                'pitchers': [('W', 'Noh Kyung Eun', '8-5'),
                             ('S', 'Jo Byeong Hyeon', '14'),
                             ('L', 'Satoshi Miyamori', '3-9')],
                'hr': [{**mark('KT', 'team'),
                        'names': 'Park Chan Ho, An Jae Seok (2)'},
                       {**mark('SS', 'team'), 'names': 'Koo Ja Wook (18)'}],
                'extra': [('Attendance', '17,813 · Jamsil')]}
        _, size = kbo_card.render_box_score_card(
            '20 August', game, self.path('box'))
        self.assertLandscape('box score', size)

    def test_leaders_at_the_current_row_count(self):
        """25b883b took these from three names to five. No five-row leaders
        card had gone out when this was written, so it had never been measured
        against a real post."""
        rows = [{'rank': i, **mark(CLUBS[i - 1][0], 'team'),
                 'name': 'Victor Reyes', 'value': '.354'}
                for i in range(1, 6)]
        _, size = kbo_card.render_leaders_card(
            '20 August', 'Batting average', rows, self.path('leaders'))
        self.assertLandscape('leaders', size)


class LandscapeCheckTests(unittest.TestCase):
    """The tripwire itself. Renders nothing, so it runs anywhere.

    Its stdout is swallowed: these cases feed it deliberately portrait sizes,
    and a passing suite that signs off with two '!!' warnings reads as a
    failing one."""

    def setUp(self):
        self.logged = []
        self._real = kbo_card._observe_portrait
        kbo_card._observe_portrait = lambda label, text: self.logged.append(
            (label, text))

    def tearDown(self):
        kbo_card._observe_portrait = self._real

    def check(self, label, w, h):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            kbo_card._check_landscape(label, w, h)
        return out.getvalue()

    def test_landscape_card_is_silent(self):
        self.assertEqual(self.check('Standings', 1240, 1178), '')
        self.assertEqual(self.logged, [])

    def test_square_card_is_silent(self):
        """Exactly square fills the box in both directions, so it passes."""
        self.assertEqual(self.check('Standings', 1240, 1240), '')
        self.assertEqual(self.logged, [])

    def test_portrait_card_is_reported(self):
        printed = self.check('Standings', 1240, 1408)
        self.assertEqual(len(self.logged), 1)
        label, text = self.logged[0]
        self.assertEqual(label, 'Standings')
        # The width the reader would actually get, not just the ratio.
        self.assertIn('454px wide', text)
        self.assertIn('454px wide', printed)

    def test_report_names_the_card(self):
        """Two cards going portrait must read as two findings, not a repeat —
        which is what the observation key is derived from."""
        self.check('Probable Starters (1 of 2)', 1240, 1721)
        self.assertIn('Probable Starters (1 of 2)', self.logged[0][1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
