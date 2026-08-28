#!/usr/bin/env python3
"""
Turns live Naver/KBO data into kbo_card inputs, and into the alt text that
describes each card.

This is the adapter layer: kbo_post owns the API and the romanisation, kbo_card
owns pixels, and this maps one to the other. Both the bot and the preview CLI
import it, so it must stay free of anything that posts.

Run it directly to preview a real day without posting anything:

    python3 kbo_card_data.py 2026-07-18 [TEAM]

which writes card_*.png to the cwd — the digest, one box score, the fixtures,
the probable starters, seven leaderboards and the standings.
"""

import base64
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import kbo_card
import kbo_post as k

# Naver writes innings pitched with vulgar fractions: '6', '5 ⅔', '0 ⅓'.
INNING_FRACTIONS = {'⅓': 1 / 3, '⅔': 2 / 3}

# Club logos. Naver serves each as a 184px transparent PNG keyed by the same
# two-letter code the bot already uses. They are fetched once and cached on
# disk, never hotlinked at render time, and are third-party marks — hence
# gitignored rather than committed. Set USE_LOGOS = False to fall back to the
# team emoji, which every card still supports.
USE_LOGOS = True
LOGO_URL = 'https://sports-phinf.pstatic.net/team/kbo/default/{code}.png'
LOGO_DIR = Path(__file__).resolve().parent / 'logos'
_LOGO_CACHE = {}


def logo_uri(code):
    """A data: URI for one club's logo, fetching and caching it on first use.
    Returns '' if logos are off or the fetch fails, so the card falls back to
    the emoji rather than rendering a broken image."""
    if not USE_LOGOS or not code:
        return ''
    if code in _LOGO_CACHE:
        return _LOGO_CACHE[code]
    path = LOGO_DIR / f'{code}.png'
    if not path.exists():
        LOGO_DIR.mkdir(exist_ok=True)
        # curl, not urllib: Homebrew Python 3.13 fails cert verification here.
        subprocess.run(['curl', '-s', '--max-time', '30', '-o', str(path),
                        LOGO_URL.format(code=code)], check=False)
    try:
        data = base64.b64encode(path.read_bytes()).decode('ascii')
        uri = f'data:image/png;base64,{data}'
    except OSError:
        uri = ''
    _LOGO_CACHE[code] = uri
    return uri


def team_marks(code, prefix):
    """{'<prefix>_emoji': ..., '<prefix>_logo': ...} for one club, so callers
    build card input without caring which the renderer will use."""
    return {f'{prefix}_emoji': k.TEAM_EMOJI.get(code, ''),
            f'{prefix}_logo': logo_uri(code)}

# The postseason cut line only earns its place once the race is live, so it is
# drawn in the final month and not before. The 2026 regular season ends 6
# September (KBO's published calendar; Naver's schedule feed also stops there) —
# UPDATE THIS EACH SEASON, nothing derives it automatically.
SEASON_END = date(2026, 9, 6)
CUTLINE_DAYS = 31


def show_cutline(on):
    """True within CUTLINE_DAYS of the end of the regular season, and after it
    (so the final table still shows who made it)."""
    return (SEASON_END - on).days <= CUTLINE_DAYS


def card_date(date_str):
    """'2026-07-18' -> '18 July'. Cards spell the month out; kbo_post's own
    format_date stays abbreviated because the text posts are character-capped."""
    return f'{date.fromisoformat(date_str).day} ' \
           f'{date.fromisoformat(date_str):%B}'


def innings_pitched(raw):
    """'5 ⅔' -> 5.667. Naver gives innings as a string, so parse rather than
    compare text."""
    total = 0.0
    for token in str(raw or '').split():
        if token in INNING_FRACTIONS:
            total += INNING_FRACTIONS[token]
        else:
            try:
                total += float(token)
            except ValueError:
                pass
    return total


def game_note(record):
    """A one-word tag under the score, for genuine feats only: a no-hitter or a
    complete game. Blowouts get nothing — a rout is an opinion, these are facts.

    A complete game means one pitcher covered every inning the opposition
    batted, which is what the `inn` arrays measure (a home side that wins
    without batting in the ninth has a short array, so its pitcher still needs
    the full nine)."""
    if not record:
        return ''
    sb = record.get('scoreBoard') or {}
    inn, rheb = sb.get('inn') or {}, sb.get('rheb') or {}
    pitchers = record.get('pitchersBoxscore') or {}
    if not rheb:
        return ''
    for side, opp in (('away', 'home'), ('home', 'away')):
        staff = pitchers.get(side) or []
        needed = len(inn.get(opp) or [])
        alone = (len(staff) == 1 and needed
                 and innings_pitched(staff[0].get('inn')) >= needed)
        if rheb.get(opp, {}).get('h') == 0:
            return 'no-hitter' if alone else 'combined no-hitter'
        if alone:
            return 'complete game'
    return ''


def results_input(games, records, roster, added):
    out = []
    for g in k.by_start(games):
        a, h = g['awayTeamScore'], g['homeTeamScore']
        record = records.get(g['gameId'])
        # The digest now sits each game's decisions under the clubs, so the card
        # needs them split by side. W and S credit the winning club, L the
        # losing one; a drawn game (KBO allows ties) records no decision.
        decisions = pitcher_decisions(record, roster, added) if record else []
        winner = 'away' if int(a) > int(h) else 'home' if int(h) > int(a) else None
        won = [d for d in decisions if d[0] in ('W', 'S')]
        lost = [d for d in decisions if d[0] == 'L']
        away_pitchers = won if winner == 'away' else lost if winner == 'home' else []
        home_pitchers = won if winner == 'home' else lost if winner == 'away' else []
        out.append({
            **team_marks(g['awayTeamCode'], 'away'),
            'away_name': k.TEAMS.get(g['awayTeamCode'], g['awayTeamCode']),
            'away_score': a,
            'away_pitchers': away_pitchers,
            **team_marks(g['homeTeamCode'], 'home'),
            'home_name': k.TEAMS.get(g['homeTeamCode'], g['homeTeamCode']),
            'home_score': h,
            'home_pitchers': home_pitchers,
            'winner': winner,
            'note': game_note(record),
        })
    return out


def postponed_input(cancelled):
    """Rained-out games -> the results card's postponed rows: marks and names
    only, no scores."""
    return [{
        **team_marks(g['awayTeamCode'], 'away'),
        'away_name': k.TEAMS.get(g['awayTeamCode'], g['awayTeamCode']),
        **team_marks(g['homeTeamCode'], 'home'),
        'home_name': k.TEAMS.get(g['homeTeamCode'], g['homeTeamCode']),
    } for g in k.by_start(cancelled)]


def line_input(game, record):
    """The scoreBoard block -> kbo_card's line-score shape, or None if Naver
    didn't return one (older games occasionally lack it)."""
    sb = record.get('scoreBoard') or {}
    inn, rheb = sb.get('inn') or {}, sb.get('rheb') or {}
    if not inn.get('away') or not rheb.get('away'):
        return None
    return {
        **team_marks(game['awayTeamCode'], 'away'),
        **team_marks(game['homeTeamCode'], 'home'),
        'away_inn': inn.get('away') or [],
        'home_inn': inn.get('home') or [],
        'away_rhe': (rheb['away'].get('r', 0), rheb['away'].get('h', 0),
                     rheb['away'].get('e', 0)),
        'home_rhe': (rheb['home'].get('r', 0), rheb['home'].get('h', 0),
                     rheb['home'].get('e', 0)),
    }


def pitcher_decisions(record, roster, added):
    """The W/L/S decisions as [(code, name, detail), ...] for one card row —
    only the decisions, since a 12-pitcher game will not fit. Name and record
    are kept apart so the card can bold the name alone."""
    by_result = {p.get('wls'): p for p in record.get('pitchingResult', [])}
    parts = []
    for code in ('W', 'L', 'S'):
        p = by_result.get(code)
        if not p:
            continue
        name = k.resolve_name(p.get('pCode'), p.get('name', ''), True,
                              roster, added)
        detail = (p.get('s', 0) if code == 'S'
                  else f'{p.get("w", 0)}–{p.get("l", 0)}')
        parts.append((code, name, str(detail)))
    return parts


# '김도영37호(9회3점 이민우)' — batter, his season total, then the inning, the
# runs it drove in and the pitcher who allowed it. Naver publishes this in the
# same /record response the box score already needs, so reading it costs
# nothing extra.
HR_ENTRY = re.compile(r'(?P<who>\S+?)(?P<num>\d+)호\('
                      r'(?P<inn>\d+)회(?P<runs>\d+)점\s*(?P<off>[^)]*)\)')


def ordinal(n):
    """1 -> '1st'. Used for innings, which are read as ordinals aloud."""
    if 10 <= n % 100 <= 20:
        return f'{n}th'
    return f'{n}' + {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')


def game_name_index(record):
    """Korean name -> (pcode, is_pitcher) for everyone who appeared.

    etcRecords names players in Korean and carries no player codes, so it
    cannot be romanised on its own. The box score in the same payload carries
    both, which is what makes this work. ⚠️ The key differs by role — batters
    use 'playerCode', pitchers use 'pcode' — and reading only the first leaves
    every 'off X' in Hangul."""
    idx = {}
    for key, is_pitcher in (('battersBoxscore', False),
                            ('pitchersBoxscore', True)):
        for side in ('away', 'home'):
            for row in (record.get(key) or {}).get(side) or []:
                name = row.get('name')
                pcode = row.get('playerCode') or row.get('pcode')
                if name and pcode:
                    idx.setdefault(name, (str(pcode), is_pitcher))
    return idx


def hr_details(record, roster, added):
    """{korean batter name: ['(37) 9th, 3R off Lee Min Woo', ...]} — one entry
    per home run, so a two-homer game lists both. Empty when etcRecords has no
    home-run line, which is how the caller falls back to bare names."""
    raw = ''
    for row in record.get('etcRecords') or []:
        if row.get('how') == '홈런':
            raw = row.get('result') or ''
            break
    idx = game_name_index(record)

    def english(name_ko):
        hit = idx.get(name_ko)
        if not hit:
            return name_ko
        pcode, is_pitcher = hit
        return k.resolve_name(pcode, name_ko, is_pitcher, roster, added) \
            or name_ko

    out = {}
    for m in HR_ENTRY.finditer(raw):
        detail = (f'({m.group("num")}) {ordinal(int(m.group("inn")))}, '
                  f'{m.group("runs")}R')
        off = m.group('off').strip()
        if off:
            detail += f' off {english(off)}'
        out.setdefault(m.group('who'), []).append(detail)
    return out


def hr_groups(game, record, roster, added):
    """Home runs as one group per team, each carrying that club's mark once
    rather than repeating it per batter:
        [{team_emoji/team_logo, 'names': 'Kim Do Yeong (37) 9th, 3R off Lee
          Min Woo, Harold Castro (13) 3rd, 1R off Owen White'}]

    Falls back to the bare 'Park Chan Ho, An Jae Seok (2)' form for any batter
    etcRecords does not describe, so a parsing miss costs detail rather than
    the row."""
    details = hr_details(record, roster, added)
    groups = []
    for side, code in (('away', game['awayTeamCode']),
                       ('home', game['homeTeamCode'])):
        names = []
        for b in record.get('battersBoxscore', {}).get(side, []):
            if b.get('hr', 0) > 0:
                name = k.resolve_name(b.get('playerCode'), b.get('name', ''),
                                      False, roster, added)
                found = details.get(b.get('name', ''))
                if found:
                    names += [f'{name} {d}' for d in found]
                else:
                    names.append(name
                                 + (f' ({b["hr"]})' if b['hr'] > 1 else ''))
        if names:
            groups.append({**team_marks(code, 'team'),
                           'team_name': k.TEAMS.get(code, code),
                           'names': ', '.join(names)})
    return groups


def box_input(game, record, roster, added, attendance=None):
    out = {
        **team_marks(game['awayTeamCode'], 'away'),
        'away_name': k.TEAMS.get(game['awayTeamCode'], game['awayTeamCode']),
        'away_score': game['awayTeamScore'],
        **team_marks(game['homeTeamCode'], 'home'),
        'home_name': k.TEAMS.get(game['homeTeamCode'], game['homeTeamCode']),
        'home_score': game['homeTeamScore'],
        'line': line_input(game, record),
        'pitchers': pitcher_decisions(record, roster, added),
        'hr': hr_groups(game, record, roster, added),
    }
    # Attendance rides the card's generic 'extra' KV row, below the home runs.
    if attendance:
        out['extra'] = [('Attendance', attendance)]
    return out


def starter_text(starter, roster):
    """'James Naile (5-5, 3.77)' — name plus season W-L and E.R.A. when the API
    has them. '' when the starter hasn't been announced; the card prints TBD."""
    if not starter:
        return ''
    text = k.display_name(starter, roster)
    if starter['w'] is not None and starter['l'] is not None and starter['era']:
        text += f' ({starter["w"]}-{starter["l"]}, {starter["era"]})'
    return text


def schedule_input(games):
    """Fixtures alone. Returns (rows, subtitle): when every game starts at the
    same time the subtitle carries it once and the rows drop it, matching what
    compose_schedule does for the text post.

    The pitchers are deliberately absent: they get their own card, threaded
    beneath this one, and printing them in both places would say the same thing
    twice in one thread. Hence no `roster` argument, unlike starters_input()."""
    games = k.by_start(games)
    times = {k.format_time(g['gameDateTime']) for g in games}
    uniform = len(times) == 1 and len(games) > 1
    rows = []
    for g in games:
        rows.append({
            **team_marks(g['awayTeamCode'], 'away'),
            'away_name': k.TEAMS.get(g['awayTeamCode'], g['awayTeamCode']),
            **team_marks(g['homeTeamCode'], 'home'),
            'home_name': k.TEAMS.get(g['homeTeamCode'], g['homeTeamCode']),
            'time': '' if uniform else k.format_time(g['gameDateTime']),
        })
    subtitle = (f'All games start at {next(iter(times))}' if uniform
                else '')
    return rows, subtitle


def split_record(text):
    """'Park Jun Young (2-4, 4.69)' -> ('Park Jun Young', '2-4, 4.69').

    starter_text() builds the bracketed form the text post wants; the starters
    card sets name and record in separate rows, so it needs the pieces back.
    A name with no season line returns ('Park Jun Young', '')."""
    if not text:
        return '', ''
    if text.endswith(')') and ' (' in text:
        name, _, rec = text.rpartition(' (')
        return name, rec[:-1]
    return text, ''


# Naver's pitch codes, spelled out. Abbreviations were rejected: this card is
# read by people meeting the KBO for the first time, the same reason the
# leaders card carries short club names rather than the club emoji.
PITCH_NAMES = {'FAST': 'Fastball', 'TWOS': 'Two-seam', 'CUTT': 'Cutter',
               'SINK': 'Sinker', 'SLID': 'Slider', 'SWEE': 'Sweeper',
               'CURV': 'Curveball', 'FORK': 'Forkball', 'CHUP': 'Changeup',
               'KNUC': 'Knuckleball'}


def pitch_mix(starter):
    """'Fastball 45%  Changeup 24%  Cutter 18%', or '' if none is published.
    An unrecognised code is printed as-is rather than dropped, so a pitch type
    Naver adds shows up as a prompt to name it instead of vanishing."""
    if not starter:
        return ''
    return '  '.join(f'{PITCH_NAMES.get(kind, kind)} {round(rate)}%'
                     for kind, rate in starter.get('mix') or [])


def versus(starter, opp_code):
    """'vs Hanwha 3-0, 1.29' — the starter's record against tonight's
    opponent, or '' when he has not faced them this season."""
    vs = (starter or {}).get('vs')
    if not vs:
        return ''
    return (f'vs {k.SHORT_NAMES.get(opp_code, opp_code)} '
            f'{vs["w"]}-{vs["l"]}, {vs["era"]}')


def starters_input(games, roster):
    """One row per fixture for the probable-starters card: each side's pitcher,
    his record against tonight's opponent, his season W-L and E.R.A., and his
    pitch mix — one row each, in that order."""
    rows = []
    for g in k.by_start(games):
        away, home = k.fetch_starters(g['gameId'])
        away_name, away_rec = split_record(starter_text(away, roster))
        home_name, home_rec = split_record(starter_text(home, roster))
        rows.append({
            **team_marks(g['awayTeamCode'], 'away'),
            'away_name': k.TEAMS.get(g['awayTeamCode'], g['awayTeamCode']),
            'away_pitcher': away_name, 'away_record': away_rec,
            'away_mix': pitch_mix(away),
            'away_vs': versus(away, g['homeTeamCode']),
            **team_marks(g['homeTeamCode'], 'home'),
            'home_name': k.TEAMS.get(g['homeTeamCode'], g['homeTeamCode']),
            'home_pitcher': home_name, 'home_record': home_rec,
            'home_mix': pitch_mix(home),
            'home_vs': versus(home, g['awayTeamCode']),
        })
    return rows


# Bluesky fits a lone image inside a square box — 515 px on the web client,
# 290 on mobile, and neither moves with the viewport width. A card wider than
# it is tall hits the width cap and is shown at the full 515; a card taller
# than it is wide hits the *height* cap instead and its width falls out
# proportionally. The five-fixture starters card renders 1240x1721, which the
# client draws 371 px wide against the fixtures card's 515, so it reads as a
# narrower post sitting under a wider one. Three fixtures (1240x1155) is the
# last count that stays landscape; four is already 1240x1438 and loses width.
#
# ⚠️ Widening CARD_WIDTH does not fix this. A height-capped image is scaled by
# its height, so the apparent text size is 515/height either way: taking the
# card to 880 px wide leaves the type fractionally *smaller*, not larger.
# Fewer rows per card is the only lever, which is why this splits the post.
STARTERS_PER_CARD = 3


def starters_chunks(rows, per_card=STARTERS_PER_CARD):
    """Split the starters rows into balanced groups of at most `per_card`.

    Balanced, not greedy: four fixtures go 2+2 rather than 3+1, because a
    lone-fixture card threaded under a full one reads as an afterthought
    rather than as the second half of a pair. Five go 3+2, six 3+3."""
    n = len(rows)
    if n <= per_card:
        return [list(rows)]
    groups = -(-n // per_card)                  # ceil
    out, start = [], 0
    for i in range(groups):
        size = n // groups + (1 if i < n % groups else 0)
        out.append(list(rows[start:start + size]))
        start += size
    return out


def leaders_input(rows):
    """kbo_post's (rank, name, teamCode, value) tuples -> card rows."""
    return [{'rank': rank, 'name': name, 'value': value,
             **team_marks(team, 'team')}
            for rank, name, team, value in rows]


def slug(label):
    """'R.B.I.s' -> 'rbis', for one filename per leaderboard."""
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-',
                                     label.lower().replace('.', ''))).strip('-')


def standings_input(rows):
    out = []
    for r in rows:
        code = k.STANDINGS_TEAM_CODE.get(r['team'], r['team'])
        out.append({
            **team_marks(code, 'team'),
            'name': k.TEAMS.get(code, r['team'].title()),
            'w': r['w'], 'l': r['l'],
            'gb': '' if r['gb'] in ('0.0', '0', '-') else r['gb'],
        })
    return out


# --------------------------------------------------------------------------
# Alt text. A card is a PNG, so everything it says is invisible to a screen
# reader unless it is also said here. These read the same data the cards do,
# so the two cannot drift.
# --------------------------------------------------------------------------

def results_alt(date_label, rows, postponed=()):
    if not rows and postponed:
        # No finals at all: matches render_results_card's 'Postponed' title
        # rather than opening with a claim of final scores it doesn't have.
        listed = '; '.join(f'{p["away_name"]} at {p["home_name"]}'
                           for p in postponed)
        return (f'{plural(len(postponed), "game").capitalize()} postponed '
                f'for {date_label}: {listed}.')
    parts = [f'Final scores for {date_label}.']
    for r in rows:
        w = r.get('winner')
        if w == 'away':
            line = (f'{r["away_name"]} beat {r["home_name"]} '
                    f'{r["away_score"]}–{r["home_score"]}')
        elif w == 'home':
            line = (f'{r["home_name"]} beat {r["away_name"]} '
                    f'{r["home_score"]}–{r["away_score"]}')
        else:
            line = (f'{r["away_name"]} and {r["home_name"]} tied '
                    f'{r["away_score"]}–{r["home_score"]}')
        extra = []
        decs = (r.get('away_pitchers') or []) + (r.get('home_pitchers') or [])
        decs.sort(key=lambda d: {'W': 0, 'S': 1, 'L': 2}.get(d[0], 3))
        if decs:
            extra.append(', '.join(f'{code} {name}' for code, name, _ in decs))
        if r.get('note'):
            extra.append(r['note'])
        if extra:
            line += ' (' + '; '.join(extra) + ')'
        parts.append(line + '.')
    if postponed:
        listed = '; '.join(f'{p["away_name"]} at {p["home_name"]}'
                           for p in postponed)
        parts.append(f'Postponed: {listed}.')
    return ' '.join(parts)


def plural(n, word):
    """'1 error', '2 errors' — alt text is read aloud, so it should read."""
    return f'{n} {word}' if n == 1 else f'{n} {word}s'


def box_alt(date_label, game):
    parts = [f'Box score for {date_label}.',
             f'{game["away_name"]} {game["away_score"]}, '
             f'{game["home_name"]} {game["home_score"]}.']
    line = game.get('line')
    if line:
        innings = max(len(line['away_inn']), len(line['home_inn']))
        for side, name in (('away', game['away_name']),
                           ('home', game['home_name'])):
            got = line[f'{side}_inn']
            by_inn = ' '.join(
                str(got[i]) if i < len(got) else ('X' if side == 'home' else '')
                for i in range(innings)).strip()
            r, h, e = line[f'{side}_rhe']
            parts.append(f'{name} by inning: {by_inn}. '
                         f'{plural(r, "run")}, {plural(h, "hit")}, '
                         f'{plural(e, "error")}.')
    for code, name, detail in game.get('pitchers') or ():
        word = {'W': 'Winning pitcher', 'L': 'Losing pitcher',
                'S': 'Save'}.get(code, code)
        parts.append(f'{word}: {name} ({detail}).')
    groups = game.get('hr') or ()
    if groups:
        # One 'Home runs:' for the lot, each team's batters named after it —
        # the card attributes them by logo, which alt text cannot.
        listed = '; '.join(f'{g.get("team_name", "")} {g["names"]}'.strip()
                           for g in groups)
        parts.append(f'Home runs: {listed}.')
    for label, value in game.get('extra') or ():
        parts.append(f'{label}: {value}.')
    return ' '.join(parts)


def starters_alt(date_label, rows, part=None, of=None):
    """`part`/`of` name this card's place when the fixtures are split across
    more than one, so a screen reader is told the same thing the card's own
    title says rather than hearing two openings that sound identical."""
    head = f'Probable starting pitchers, {date_label}.'
    if of and of > 1:
        head = (f'Probable starting pitchers, {date_label}, '
                f'part {part} of {of}.')
    parts = [head]
    for r in rows:
        for side in ('away', 'home'):
            name = r.get(f'{side}_pitcher')
            if not name:
                parts.append(f'{r[f"{side}_name"]}, starter not yet named.')
                continue
            bits = [f'{r[f"{side}_name"]}, {name}']
            vs = r.get(f'{side}_vs')
            if vs:
                bits.append(vs.replace('vs ', 'against ', 1))
            rec = r.get(f'{side}_record')
            if rec:
                bits.append(f'{rec} this season')
            mix = r.get(f'{side}_mix')
            if mix:
                bits.append(mix.replace('  ', ', '))
            parts.append(', '.join(bits) + '.')
    return ' '.join(parts)


def schedule_alt(date_label, rows, subtitle):
    parts = [f'Today’s games, {date_label}.']
    if subtitle:
        # 'All games start at 6:30 p.m.' already ends in a stop.
        parts.append(subtitle if subtitle.endswith('.') else subtitle + '.')
    for r in rows:
        line = f'{r["away_name"]} at {r["home_name"]}'
        if r.get('time'):
            line += f', {r["time"]}'
        parts.append(line + '.')
    return ' '.join(parts)


def leaders_alt(date_label, title, rows):
    parts = [f'{title}, season leaders, {date_label}.']
    parts += [f'{r["rank"]}. {r["name"]}, {r["value"]}.' for r in rows]
    return ' '.join(parts)


def standings_alt(date_label, rows, cut_after):
    parts = [f'KBO standings, {date_label}.']
    for i, r in enumerate(rows, start=1):
        gb = f", {r['gb']} games back" if r.get('gb') else ''
        parts.append(f'{i}. {r["name"]}, {r["w"]}-{r["l"]}{gb}.')
        if cut_after and i == cut_after and i < len(rows):
            parts.append('Postseason line.')
    return ' '.join(parts)


def main(argv):
    date_str = argv[1] if len(argv) > 1 else str(date.today() - timedelta(days=1))
    roster, added = k.load_roster(), []

    all_games = k.fetch_games(date_str)
    games = [g for g in all_games
             if g.get('statusCode') == k.FINAL and not g.get('cancel')]
    cancelled = [g for g in all_games if g.get('cancel')]
    if not games and not cancelled:
        print(f'no finished games on {date_str}')
        return 1
    label = card_date(date_str)

    # One fetch per game, shared: the digest needs box scores too now, to spot
    # a no-hitter or complete game. The bot already fetches these for its
    # box-score replies, so wiring this in costs no extra calls.
    records = {}
    for g in games:
        rec = k.fetch_box_score(g['gameId'])
        if rec:
            records[g['gameId']] = rec

    print(kbo_card.render_results_card(label,
                                       results_input(games, records, roster, added),
                                       'card_results.png',
                                       postponed=postponed_input(cancelled)))

    # Box score: the game named by a second argument (a team code such as OB,
    # or a full gameId), else the first game of the day that had a home run.
    want = argv[2].upper() if len(argv) > 2 else ''
    pick = record = None
    for g in k.by_start(games):
        rec = records.get(g['gameId'])
        if not rec:
            continue
        hit = (want in (g['awayTeamCode'], g['homeTeamCode'])
               or want == g['gameId']) if want else \
            bool(hr_groups(g, rec, roster, added))
        if hit:
            pick, record = g, rec
            break
    if not record and games:             # nothing matched — fall back to game 1
        pick = k.by_start(games)[0]
        record = records.get(pick['gameId'])
    if not record:
        print('no box score available')
        return 1
    print(kbo_card.render_box_score_card(label, box_input(pick, record, roster,
                                                          added),
                                         'card_box.png'))

    # Tonight's games: the fixtures for the day after the results being shown,
    # which is the pairing the bot posts (yesterday's results, today's card).
    next_day = date.fromisoformat(date_str) + timedelta(days=1)
    fixtures = k.fetch_games(str(next_day))
    if fixtures:
        rows, subtitle = schedule_input(fixtures)
        print(kbo_card.render_schedule_card(card_date(str(next_day)), rows,
                                            'card_schedule.png',
                                            subtitle=subtitle))
        print(kbo_card.render_starters_card(card_date(str(next_day)),
                                            starters_input(fixtures, roster),
                                            'card_starters.png'))
    else:
        print(f'no fixtures on {next_day} (KBO rests on Mondays) — skipped')

    # Season leaders: one card per leaderboard, seven in all.
    data = k.fetch_leaders(date.fromisoformat(date_str).year)
    for key, label in k.HITTING_LEADERS + k.PITCHING_LEADERS:
        top = k.leader_rows(key, data.get(key, []), roster, added)
        if not top:
            print(f'no data for {label} — skipped')
            continue
        print(kbo_card.render_leaders_card(card_date(date_str), label,
                                           leaders_input(top),
                                           f'card_leaders_{slug(label)}.png'))

    rows = k.fetch_standings()
    if rows:
        as_of = date.fromisoformat(date_str) + timedelta(days=1)
        print(kbo_card.render_standings_card(
            card_date(str(as_of)), standings_input(rows), 'card_standings.png',
            cut_after=k.PLAYOFF_SPOTS if show_cutline(as_of) else None))
    else:
        print('standings unavailable — skipped')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
