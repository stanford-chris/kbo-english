#!/usr/bin/env python3
"""An all-postponed slate must not say 'Final scores'.

Prompted by a real card on 28 August 2026: every game on the day rained out,
and the results card still headed itself 'Final Scores' above five rows that
each just said 'postponed' -- a header that claims exactly what the card goes
on to say didn't happen. The same mismatch was in the card's own alt text and
in compose_results, the plaintext fallback a card-render failure falls back
to.

Three surfaces, one rule: a slate with zero finals gets a 'Postponed' header,
never 'Final scores' with nothing under it. A MIXED day (some finals, some
rainouts) is unchanged -- 'Final Scores' still leads, and the postponed
fixtures still list separately underneath, exactly as before this fix.

Stdlib only. render_results_card is exercised through a stubbed `_shoot`, so
this needs no Chrome and writes no file -- it inspects the HTML string and the
`label` kwarg `_shoot` would have received, which is what the real render
passes to the card title and to its portrait-warning key.

    python3 -m unittest test_kbo_postponed_wording -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kbo_card                                               # noqa: E402
import kbo_card_data as data                                  # noqa: E402
import kbo_post as k                                           # noqa: E402


def fixture(away, home):
    return {'away_name': away, 'home_name': home,
            'away_emoji': '⚾', 'home_emoji': '⚾'}


class RenderResultsCardTitle(unittest.TestCase):
    """The card's own title, captured off a stubbed `_shoot` rather than a
    real Chrome render."""

    def setUp(self):
        self._real_shoot = kbo_card._shoot
        self.captured = {}

        def fake_shoot(doc, out_path, label='card'):
            self.captured['doc'] = doc
            self.captured['label'] = label
            return (out_path, (100, 50))

        kbo_card._shoot = fake_shoot

    def tearDown(self):
        kbo_card._shoot = self._real_shoot

    def test_all_postponed_is_titled_postponed_not_final_scores(self):
        postponed = [fixture('KT Wiz', 'Samsung Lions'),
                     fixture('LG Twins', 'Lotte Giants')]
        kbo_card.render_results_card('28 August', [], 'out.png',
                                     postponed=postponed)
        self.assertEqual(self.captured['label'], 'Postponed')
        self.assertIn('Postponed</div>', self.captured['doc'])
        self.assertNotIn('Final Scores', self.captured['doc'])

    def test_mixed_slate_keeps_final_scores_title(self):
        """A day with at least one real result is unchanged: 'Final Scores'
        still leads even though some of the slate rained out."""
        finals = [{**fixture('KT Wiz', 'Samsung Lions'),
                   'away_score': '4', 'home_score': '2', 'winner': 'away'}]
        postponed = [fixture('LG Twins', 'Lotte Giants')]
        kbo_card.render_results_card('28 August', finals, 'out.png',
                                     postponed=postponed)
        self.assertEqual(self.captured['label'], 'Final Scores')
        self.assertIn('Final Scores</div>', self.captured['doc'])

    def test_explicit_title_still_overrides(self):
        """The empty-games default must not swallow a caller-supplied title."""
        kbo_card.render_results_card('28 August', [], 'out.png',
                                     title='Rain Delay',
                                     postponed=[fixture('KT Wiz', 'SSG Landers')])
        self.assertEqual(self.captured['label'], 'Rain Delay')

    def test_no_games_at_all_still_raises(self):
        """Neither finals nor postponed: the pre-existing guard, unchanged by
        the title default becoming conditional."""
        with self.assertRaises(kbo_card.CardRenderError):
            kbo_card.render_results_card('28 August', [], 'out.png')


class ResultsAltAllPostponed(unittest.TestCase):
    """The alt text a screen reader hears must match what the image now
    says, since the card is a PNG and this is its only accessible form."""

    def test_all_postponed_names_the_games_not_final_scores(self):
        postponed = [{'away_name': 'KT Wiz', 'home_name': 'Samsung Lions'},
                     {'away_name': 'LG Twins', 'home_name': 'Lotte Giants'}]
        alt = data.results_alt('28 August', [], postponed)
        self.assertNotIn('Final scores', alt)
        self.assertTrue(alt.startswith('2 games postponed for 28 August:'))
        self.assertIn('KT Wiz at Samsung Lions', alt)
        self.assertIn('LG Twins at Lotte Giants', alt)

    def test_singular_game_reads_naturally(self):
        postponed = [{'away_name': 'KT Wiz', 'home_name': 'Samsung Lions'}]
        alt = data.results_alt('28 August', [], postponed)
        self.assertTrue(alt.startswith('1 game postponed for 28 August:'))

    def test_mixed_slate_alt_text_unchanged(self):
        """Regression: a day with finals keeps the old 'Final scores for ...
        Postponed: ...' shape."""
        rows = [{'away_name': 'KT Wiz', 'home_name': 'Samsung Lions',
                 'away_score': '4', 'home_score': '2', 'winner': 'away'}]
        postponed = [{'away_name': 'LG Twins', 'home_name': 'Lotte Giants'}]
        alt = data.results_alt('28 August', rows, postponed)
        self.assertTrue(alt.startswith('Final scores for 28 August.'))
        self.assertIn('Postponed: LG Twins at Lotte Giants.', alt)

    def test_no_games_no_postponed_is_still_bare_final_scores(self):
        """An empty digest (should never be called this way in practice, per
        render_results_card's own guard) doesn't crash: it falls through to
        the ordinary opener with nothing appended."""
        self.assertEqual(data.results_alt('28 August', [], []),
                         'Final scores for 28 August.')


class ComposeResultsAllPostponed(unittest.TestCase):
    """The plaintext fallback compose_results falls back to when a card
    fails to render -- it must not claim final scores it doesn't have
    either."""

    def _game(self, away, home):
        return {'gameId': '20260828SSLG0', 'gameDate': '2026-08-28',
                'gameDateTime': '2026-08-28T18:00:00',
                'statusCode': 'BEFORE', 'cancel': True,
                'homeTeamCode': home, 'awayTeamCode': away}

    def test_all_postponed_header_says_postponed(self):
        cancelled = [self._game('KT', 'SS')]
        body, tags = k.compose_results('2026-08-28', [], cancelled)[0]
        first_line = body.strip().splitlines()[0]
        self.assertIn('Postponed', first_line)
        self.assertNotIn('Final scores', first_line)
        # No finals, so the fixture list needs no second 'Postponed:' label.
        self.assertNotIn('Postponed:\n', body)

    def test_mixed_slate_header_unchanged(self):
        """Regression: with at least one final, the header still says 'Final
        scores' and the postponed fixtures keep their own labelled section."""
        final = {'gameId': '20260828HTLG0', 'gameDate': '2026-08-28',
                 'gameDateTime': '2026-08-28T18:00:00',
                 'statusCode': k.FINAL, 'cancel': False,
                 'homeTeamCode': 'LG', 'awayTeamCode': 'HT',
                 'awayTeamScore': '4', 'homeTeamScore': '2'}
        cancelled = [self._game('KT', 'SS')]
        body, tags = k.compose_results('2026-08-28', [final], cancelled)[0]
        first_line = body.strip().splitlines()[0]
        self.assertIn('Final scores', first_line)
        self.assertIn('Postponed:\n', body)


if __name__ == '__main__':
    unittest.main()
