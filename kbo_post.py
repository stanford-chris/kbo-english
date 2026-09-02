#!/usr/bin/env python3
"""
Post English-language KBO League updates to Bluesky (@kbo-english.bsky.social).

Five post types:

  schedule   A pre-game thread: (1) tonight's matchups and start times (KST),
             with (2) the probable starting pitchers threaded underneath, each
             on its own card.
  live       A standalone box-score card posted as each game goes final,
             attendance-gated (held until KBO publishes that game's crowd
             figure). Polled through the evening; the results roundup below is
             the safety net for anything a poll misses.
  results    A nightly final-scores digest (every game's final in one post),
             then a compact box score threaded underneath per game that wasn't
             already posted live.
  standings  A rank / W-L / games-back table (from the KBO English site), posted
             each evening once the night's slate is final (polled, and gated so
             it holds until every game is in and skips off-days).
  leaders    A weekly season-leaders thread: one post per leaderboard (top 5),
             romanised via the KBO English player pages.

Every post is a rendered PNG card, built by kbo_card via kbo_card_data. The post
text is the headline alone — the card carries the detail and its alt text
repeats that detail in full, so the information is never set twice in one post.
If a card fails to render the post is held rather than shipped as plain text —
see card_only(). The compose_* functions still build a full text body per
segment regardless, because it feeds the card's own alt text.

schedule/results/leaders draw their game and stat data from Naver Sports' public
API; standings and the leaders' name romanisation read the KBO English site.
schedule runs in the morning, results and standings in the evening once the
slate is final, leaders weekly on Monday (a league off-day). Dedup is by
(mode, date) in kbo_history.json, so each card posts at most once per day.

Data sources (unauthenticated JSON, KST timestamps):
    .../schedule/games?categoryId=kbo&fromDate=...   scores, matchups, times
    .../schedule/games/{gameId}/preview              probable starters (Korean)
    .../schedule/games/{gameId}/record               box score (line score, W/L/S)
    .../statistics/.../top-players?playerType=...     season stat leaders

Team names come from the stable 2-letter TeamCode (see TEAMS), never from the
API's TeamName field, which flip-flops between Korean and English. Starting
pitchers are posted in Korean unless the pcode is in kbo_roster.json; leaderboard
names are romanised from the KBO English player pages and cached into that same
table, falling back to Korean on a miss.

Requires (only for a real post, not --dry-run):
    pip install atproto
    security add-generic-password -a "kbo-english.bsky.social" -s "kbobot-bluesky" -w

Usage:
    python3 kbo_post.py schedule  --dry-run          # tonight's games (today KST)
    python3 kbo_post.py live      --dry-run           # games gone final since last poll
    python3 kbo_post.py results   --dry-run           # tonight's finals
    python3 kbo_post.py standings --dry-run            # tonight's table, if final
    python3 kbo_post.py standings --dry-run --date 2026-08-02   # a set date, ungated
    python3 kbo_post.py leaders   --dry-run           # season stat leaders
    python3 kbo_post.py results   --dry-run --date 2026-07-16
    python3 kbo_post.py results   --dry-run --all      # ignore history (re-show)
    python3 kbo_post.py schedule                        # post for real (needs atproto)
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import kbo_lock
import net_guard

KST = ZoneInfo('Asia/Seoul')

# How long each mode will wait for a run already in progress before giving up
# its slot. live and standings poll every 15 and 30 minutes and results twice a
# night, so skipping costs nothing the next slot won't pick up; schedule runs
# once a day and leaders once a week, so they wait rather than lose the post.
LOCK_WAIT = {'live': 240, 'standings': 240, 'results': 240,
             'schedule': 900, 'leaders': 900}

# How long a finished game waits for KBO to publish its crowd figure before the
# box score goes out without one. The wait used to be unbounded, and the tail is
# what made that untenable: of the 65 box scores posted up to 23 August 2026, 37
# were held at least one poll, and while 30 of those cleared inside 45 minutes,
# one waited 255 -- a 14:00 day game on 2 August whose box score landed at 21:45.
# Bluesky posts cannot be edited, so this is a straight trade and 45 minutes is
# where it was set: 58 of the 65 would have kept their figure, and none would
# have waited longer than the cap.
ATTENDANCE_GRACE = 45 * 60

HANDLE = 'kbo-english.bsky.social'
KEYCHAIN_SERVICE = 'kbobot-bluesky'
HISTORY = Path(__file__).parent / 'kbo_history.json'
STATE = Path(__file__).parent / 'kbo_state.json'
ROSTER = Path(__file__).parent / 'kbo_roster.json'
RESULTS_ARCHIVE = Path(__file__).parent / 'kbo_results_history.json'

# KBO sends the top 5 to the postseason; the standings post draws a line there.
PLAYOFF_SPOTS = 5

API = ('https://api-gw.sports.naver.com/schedule/games'
       '?upperCategoryId=kbaseball&categoryId=kbo&fromDate={d}&toDate={d}')
PREVIEW_API = 'https://api-gw.sports.naver.com/schedule/games/{gid}/preview'
RECORD_API = 'https://api-gw.sports.naver.com/schedule/games/{gid}/record'

# Season stat leaders (weekly "leaders" post). One call per player type returns a
# fixed set of leaderboards; each ranking row also carries the stat value under a
# key matching the category type (e.g. row['hitterHra'] is the batting average).
TOP_PLAYERS_API = ('https://api-gw.sports.naver.com/statistics/categories/kbo/'
                   'seasons/{season}/top-players'
                   '?playerType={pt}&rankFlag=Y&limit={limit}&includeFields={fields}')

# KBO English player pages, by pcode (== Naver pcode). Used only by the leaders
# post to romanise leaderboard names it hasn't cached yet — hitters and pitchers
# live on different pages, so the lookup is keyed by which one the player is.
KBO_PLAYER_PAGE = {
    True:  'https://eng.koreabaseball.com/Teams/PlayerInfoPitcher/Summary.aspx?pcode={pc}',
    False: 'https://eng.koreabaseball.com/Teams/PlayerInfoHitter/Summary.aspx?pcode={pc}',
}

MAX_POST_CHARS = 290   # packing target: a conservative code-point buffer used to
                       # split threads (see pack_lines / compose_schedule).
BLUESKY_LIMIT = 300    # Bluesky's real per-post ceiling, counted in GRAPHEMES —
                       # the hard gate emit() checks (a flag emoji is 2 code
                       # points but 1 grapheme, so code-point length over-counts).

# Naver's stable 2-letter team codes -> full club names. These codes do not
# change even when a franchise rebrands (SK stayed "SK" after the SK Wyverns
# became the SSG Landers; OB stayed "OB" for the Doosan Bears), which is exactly
# why we key off them instead of the inconsistent TeamName field.
TEAMS = {
    'HT': 'KIA Tigers', 'SK': 'SSG Landers', 'LG': 'LG Twins', 'KT': 'KT Wiz',
    'LT': 'Lotte Giants', 'SS': 'Samsung Lions', 'OB': 'Doosan Bears',
    'NC': 'NC Dinos', 'WO': 'Kiwoom Heroes', 'HH': 'Hanwha Eagles',
}

# Team code -> emoji, keyed to the club nickname. Eight map cleanly to the
# animal/character in the name; Landers (🚀, evokes a landing craft) and Giants
# (🗿) are looser choices — swap freely, they carry no data meaning.
TEAM_EMOJI = {
    'HT': '🐯', 'SK': '🚀', 'LG': '👯', 'KT': '🧙', 'LT': '🗿',
    'SS': '🦁', 'OB': '🐻', 'NC': '🦖', 'WO': '🦸', 'HH': '🦅',
}

# Hashtags appended to the tagged post. #KBO is the community tag KBO fans
# follow; #baseball reaches the broader English-speaking baseball audience the
# bot exists to serve. Team tags are omitted since every post is a league-wide
# digest.
HASHTAGS = ['KBO', 'baseball']

# Short names for the compact standings table (full club names used elsewhere).
SHORT_NAMES = {
    'HT': 'KIA', 'SK': 'SSG', 'LG': 'LG', 'KT': 'KT', 'LT': 'Lotte',
    'SS': 'Samsung', 'OB': 'Doosan', 'NC': 'NC', 'WO': 'Kiwoom', 'HH': 'Hanwha',
}

# Standings come from the KBO official English site (authoritative order incl.
# tiebreakers), fetched once for the daily standings post — the one place the
# bot reads KBO English at post time (schedule/results stay Naver-only). If the
# page is unreachable the standings post simply skips.
STANDINGS_URL = 'https://eng.koreabaseball.com/Standings/TeamStandings.aspx'
STANDINGS_TEAM_CODE = {
    'SAMSUNG': 'SS', 'LG': 'LG', 'KT': 'KT', 'KIA': 'HT', 'DOOSAN': 'OB',
    'HANWHA': 'HH', 'NC': 'NC', 'LOTTE': 'LT', 'SSG': 'SK', 'KIWOOM': 'WO',
}

# KBO's official daily crowd page publishes each game's attendance in lockstep
# with the game going final (verified 19-22 Jul 2026), so every game in a
# results post has its figure by the 23:30 post time. The page lists the whole
# season oldest-first; we filter to the post's date and key by home team (one
# home game per club per day). SSG (Munhak) and Samsung (Daegu) report round
# home figures (23,000 / 24,000) — announced sellouts at capacity, not turnstile
# counts — but they are the official numbers, so they are shown as published.
CROWD_URL = 'https://www.koreabaseball.com/Record/Crowd/GraphDaily.aspx'
CROWD_ROW_RE = re.compile(
    r'<td[^>]*>\s*(\d{4}/\d\d/\d\d)\s*</td>\s*'   # date
    r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'            # day of week
    r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'            # home label
    r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'            # away label
    r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'            # stadium
    r'<td[^>]*>\s*([\d,]+)\s*</td>')              # attendance
# Crowd-page team labels (mixed English/Korean) -> our team codes.
CROWD_LABEL_TO_CODE = {
    'KIA': 'HT', 'SSG': 'SK', 'LG': 'LG', 'KT': 'KT', 'NC': 'NC',
    '두산': 'OB', '롯데': 'LT', '삼성': 'SS', '키움': 'WO', '한화': 'HH',
}
# Crowd-page stadium labels -> the romanised park names KBO fans use. Taken from
# the page's own stadium column, not derived from the home club, because Samsung
# hosts at two parks (Daegu and its Pohang sub-venue) and Jamsil is shared.
CROWD_STADIUM_EN = {
    '고척': 'Gocheok', '광주': 'Gwangju', '대구': 'Daegu', '대전': 'Daejeon',
    '문학': 'Munhak', '사직': 'Sajik', '수원': 'Suwon', '잠실': 'Jamsil',
    '창원': 'Changwon', '포항': 'Pohang',
}

# KBO lists every player surname-first ("WELLS Lachlan"); we flip Western
# imports to first-last ("Lachlan Wells"). Foreign is known from the roster
# table (salary currency). East-Asian imports (Japanese/Taiwanese/Chinese) are
# also foreign but their names are already correctly surname-first, so they are
# kept via this pcode set — maintain by hand when a new one arrives (rare).
# Seeded 2026-07-17: 54843 Shirakawa Keisho (JP), 56719 Wang Yan-Cheng (TW).
KEEP_SURNAME_FIRST = {'54843', '56719'}

# Naver statusCode values: BEFORE (scheduled), STARTED/READY (in progress),
# RESULT (final), CANCEL (postponed).
FINAL = 'RESULT'

# Leaders post — (API category key, display label), LEADER_COUNT rows each. The key
# is both the leaderboard's `type` and the stat field on each row. includeFields
# nudges the API to include these; it returns a fixed default set regardless.
HITTING_LEADERS = [('hitterHra', 'Batting average'),
                   ('hitterHr', 'Home runs'),
                   ('hitterRbi', 'R.B.I.s')]
PITCHING_LEADERS = [('pitcherEra', 'E.R.A.'),
                    ('pitcherWin', 'Wins'),
                    ('pitcherSave', 'Saves'),
                    ('pitcherKk', 'Strikeouts')]
LEADER_FIELDS = {'HITTER': 'offenseHra,offenseHr,offenseRbi',
                 'PITCHER': 'defenseEra,defenseWin,defenseSave,defenseKk'}
# Rate-stat boards are limited to qualified players (batting-title / ERA-title);
# counting-stat boards (HR, RBI, W, SV, K) include everyone.
QUALIFIED_ONLY = {'hitterHra', 'pitcherEra'}
# Names shown per board, and how many rows to ask Naver for. The fetch is
# deliberately double the display count: the rate-stat boards drop unqualified
# players after the fetch, so asking for exactly five would short the board in
# early season, when few players have the plate appearances or innings. Twice
# the rows costs nothing — it is still one call per player type.
LEADER_COUNT = 5
LEADER_FETCH = LEADER_COUNT * 2


def write_json_atomic(path, data, **dumps_kwargs):
    """Write JSON via a sibling temp file and an atomic rename, so a crash
    mid-write can never leave a truncated history, roster or state file behind.

    The temp name carries the PID. It used to be a fixed '<name>.tmp', which is
    atomic against a crash but not against a second run: on 15 August 2026 an
    overrunning live poll and the standings run wrote that same sibling at once,
    each renamed it out from under the other, and kbo_state.json was left torn —
    which killed the heartbeat for a day. kbo_lock now keeps runs from
    overlapping at all; a unique name keeps the write safe if one ever slips
    past it."""
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(data, **dumps_kwargs))
        os.replace(tmp, path)
    except BaseException:                       # never leave the temp behind
        tmp.unlink(missing_ok=True)
        raise


def team_label(code):
    """'HT' -> '🐯 KIA Tigers' (emoji + name), or just the name if no emoji."""
    name = TEAMS.get(code, code)
    emoji = TEAM_EMOJI.get(code)
    return f'{emoji} {name}' if emoji else name


def fetch_text(url, retries=4, backoff=3, accept='application/json', referer=None):
    """GET a URL as text via curl (not urllib — Homebrew Python 3.13's urllib
    fails TLS cert verification on this machine). `accept`/`referer` let the
    same retry path serve the HTML crowd page as well as the JSON APIs.

    Retries on any curl transport failure (nonzero exit: connect/DNS/TLS/
    timeout, e.g. exit 6/7/28/35) with a linear backoff, so a momentary
    network blip at the scheduled fire time doesn't crash the whole run and
    skip a post. curl returns 0 for HTTP errors like 404 without -f, so those
    are not retried here."""
    cmd = ['curl', '-s', '--compressed', '--max-time', '30',
           '-H', 'User-Agent: Mozilla/5.0', '-H', f'Accept: {accept}']
    if referer:
        cmd += ['-H', f'Referer: {referer}']
    cmd.append(url)
    last = None
    for attempt in range(retries):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        last = result.returncode
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f'curl failed ({last}) fetching {url} after {retries} tries')


def fetch_games(date_str):
    """The day's KBO games as a list of dicts."""
    try:
        data = json.loads(fetch_text(API.format(d=date_str)))
    except json.JSONDecodeError:
        raise RuntimeError('Non-JSON from schedule API')
    if not data.get('success'):
        raise RuntimeError(f'Schedule API failure: {data.get("code")}')
    return data.get('result', {}).get('games', []) or []


def fetch_starters(game_id):
    """(away, home) probable starters for one game, each a dict with the Korean
    name, season w / l / era, his pitch mix and his record against tonight's
    opponent, or None if not yet announced. Returns (None, None) on any failure
    so a missing preview degrades to no pitcher line rather than crashing the
    run.

    The mix and the opponent line come out of the same preview response the
    starter's name already needed, so they cost no extra request. Both are
    optional: a pitcher with no mix published, or who has never faced tonight's
    opponent, simply carries fewer rows on the card."""
    try:
        pd = json.loads(fetch_text(PREVIEW_API.format(gid=game_id)))
        pd = pd.get('result', {}).get('previewData', {})
    except (RuntimeError, json.JSONDecodeError):
        return None, None

    def starter(side):
        s = pd.get(side) or {}
        info = s.get('playerInfo') or {}
        name = (info.get('name') or '').strip()
        if not name:
            return None
        st = s.get('currentSeasonStats') or {}
        # Naver ranks the mix already; three pitches is what fits a column.
        mix = [(m.get('type'), m.get('pit_rt'))
               for m in (s.get('currentPitKindStats') or [])[:3]
               if m.get('type') and m.get('pit_rt') is not None]
        # gameCount arrives as an int or a string depending on the game, and is
        # 0 for a pitcher who has not faced this opponent — no line, not 0-0.
        opp = s.get('currentSeasonStatsOnOpponents') or {}
        try:
            faced = int(opp.get('gameCount') or 0)
        except (TypeError, ValueError):
            faced = 0
        return {'name_ko': name, 'pcode': info.get('pCode'),
                'w': st.get('w'), 'l': st.get('l'), 'era': st.get('era'),
                'mix': mix,
                'vs': {'w': opp.get('w'), 'l': opp.get('l'),
                       'era': opp.get('era')} if faced else None}

    return starter('awayStarter'), starter('homeStarter')


def fetch_box_score(game_id):
    """A finished game's box-score record (Naver /record), or None on any
    failure so a missing box score drops that game's reply rather than
    crashing the results thread."""
    try:
        data = json.loads(fetch_text(RECORD_API.format(gid=game_id)))
    except (RuntimeError, json.JSONDecodeError):
        return None
    if not data.get('success'):
        return None
    return data.get('result', {}).get('recordData') or None


def fetch_attendance(date_str):
    """{home_team_code: ('23,000', 'Munhak')} for a KST date — the attendance
    figure and romanised venue, scraped from KBO's official daily crowd page —
    or {} on any failure (attendance is a nice-to-have and must never cost a
    post). Keyed by home team (one home game per club per day), so the caller
    looks it up by g['homeTeamCode']. Venue comes from the page's stadium
    column, not the home club, because Samsung hosts at two parks."""
    dslash = date_str.replace('-', '/')
    try:
        html = fetch_text(CROWD_URL, accept='text/html',
                          referer='https://www.koreabaseball.com/')
    except RuntimeError:
        return {}
    out = {}
    for d, dow, home, away, stadium, att in CROWD_ROW_RE.findall(html):
        if d == dslash:
            code = CROWD_LABEL_TO_CODE.get(home)
            if code:
                out[code] = (att, CROWD_STADIUM_EN.get(stadium, ''))
    return out


def format_date(date_str):
    """'2026-07-16' -> '16 Jul' (UK day-month)."""
    d = datetime.strptime(date_str, '%Y-%m-%d')
    return f'{d.day} {d:%b}'


def format_time(dt_iso):
    """'2026-07-17T18:30:00' -> '6:30 p.m.'; on-the-hour times drop the ':00'
    -> '6 p.m.' (KST; lowercase a.m./p.m.)."""
    t = datetime.strptime(dt_iso, '%Y-%m-%dT%H:%M:%S')
    hour = t.hour % 12 or 12
    meridiem = 'a.m.' if t.hour < 12 else 'p.m.'
    return f'{hour} {meridiem}' if t.minute == 0 else f'{hour}:{t.minute:02d} {meridiem}'


def final_innings(status_info):
    """Innings played for a finished game, from statusInfo ('9회말' -> 9).
    A normal game ends at 9; less means rain-shortened, more means extras."""
    m = re.search(r'(\d+)\s*회', status_info or '')
    return int(m.group(1)) if m else None


def by_start(games):
    return sorted(games, key=lambda g: g.get('gameDateTime', ''))


def result_line(game):
    """'🐯 KIA Tigers 0 @ 🚀 SSG Landers 6', with an inning tag if the game
    didn't go a regulation 9 (rain-shortened or extras)."""
    a, h = game['awayTeamScore'], game['homeTeamScore']
    line = (f'{team_label(game["awayTeamCode"])} {a} @ '
            f'{team_label(game["homeTeamCode"])} {h}')
    inn = final_innings(game.get('statusInfo'))
    if inn and inn != 9:
        line += f' ({inn})'
    return line


def schedule_line(game, show_time=True):
    """'🐯 KIA Tigers @ 🚀 SSG Landers · 6:30 p.m.' — the time is dropped when the
    header already states a single shared start time."""
    line = f'{team_label(game["awayTeamCode"])} @ {team_label(game["homeTeamCode"])}'
    if show_time:
        line += f' · {format_time(game["gameDateTime"])}'
    return line


def load_roster():
    if ROSTER.exists():
        return json.loads(ROSTER.read_text())
    return {}


def order_name(raw, foreign, pcode):
    """KBO's ALL-CAPS surname-first form -> display form. Surname is title-cased
    ('KIM' -> 'Kim'); Western imports get the surname moved to the end
    ('WELLS Lachlan' -> 'Lachlan Wells'). Korean players and East-Asian imports
    (KEEP_SURNAME_FIRST) stay surname-first."""
    parts = raw.split()
    if parts:
        parts[0] = parts[0].capitalize()
    if foreign and pcode not in KEEP_SURNAME_FIRST and len(parts) >= 2:
        parts = parts[1:] + parts[:1]
    return ' '.join(parts)


def display_name(starter, roster):
    """Romanised name from the roster table, or the Korean name if the pcode
    isn't in the table yet (so the post never depends on a live KBO lookup)."""
    pcode = str(starter.get('pcode') or '')
    entry = roster.get(pcode)
    if named(entry):
        return order_name(entry['name'], entry.get('foreign', False), pcode)
    return starter['name_ko']


def starter_label(code, starter, roster):
    """'🐯 Shirakawa Keisho (2-3, 4.88)' — emoji, name, season W-L and ERA.
    Stats are appended only when present; an unannounced starter shows TBD."""
    emoji = TEAM_EMOJI.get(code, '')
    if not starter:
        return f'{emoji} TBD'.strip()
    text = display_name(starter, roster)
    if starter['w'] is not None and starter['l'] is not None and starter['era']:
        text += f' ({starter["w"]}-{starter["l"]}, {starter["era"]})'
    return f'{emoji} {text}'.strip()


def starters_line(item, roster):
    """'🐯 Shirakawa Keisho (2-3, 4.88) vs 🚀 Kim Min Jun (2-1, 4.18)'"""
    g = item['game']
    return (f'{starter_label(g["awayTeamCode"], item["away"], roster)} vs '
            f'{starter_label(g["homeTeamCode"], item["home"], roster)}')


def fetch_standings():
    """Parse the KBO English standings table into ranked rows, or [] on failure
    (so a bad fetch skips the post rather than crashing)."""
    try:
        html = fetch_text(STANDINGS_URL)
    except RuntimeError:
        return []
    rows = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        cells = [re.sub(r'<[^>]+>', '', c).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
        cells = [c for c in cells if c]
        # standings rows look like: rank, TEAM, games, W, L, D, .PCT, GB, streak
        if len(cells) >= 8 and cells[0].isdigit() and re.match(r'0?\.\d+$', cells[6]):
            rows.append({'rank': int(cells[0]), 'team': cells[1].upper(),
                         'w': cells[3], 'l': cells[4], 'gb': cells[7]})
    return rows


def compose_standings(date_str, rows):
    """Ranked standings post: '1. 🦁 Samsung 52-32', GB after a middot, and a
    postseason cutline after 5th (KBO sends its top 5 to the playoffs).

    The cutline only appears in the final month of the regular season, matching
    the card that accompanies this post — the two are read together, so they
    must agree."""
    import kbo_card_data
    cutline = kbo_card_data.show_cutline(
        datetime.strptime(date_str, '%Y-%m-%d').date())
    lines = []
    for r in rows:
        code = STANDINGS_TEAM_CODE.get(r['team'], r['team'])
        emoji = TEAM_EMOJI.get(code, '')
        name = SHORT_NAMES.get(code, r['team'].title())
        gb = '' if r['gb'] in ('0.0', '0', '-') else f' · {r["gb"]}'
        lines.append(f'{r["rank"]}. {emoji} {name} {r["w"]}-{r["l"]}{gb}'.strip())
        if r['rank'] == PLAYOFF_SPOTS and cutline:
            lines.append('— postseason —')
    body = (f'🇰🇷⚾ Standings · {format_date(date_str)}\n(W-L · games back)\n\n'
            + '\n'.join(lines) + '\n\n')
    return [(body, HASHTAGS)]


def fetch_leaders(season):
    """{category_key: [ranking rows]} for the season-leaders post, from Naver's
    top-players endpoint (one call per player type). Empty on failure so the post
    skips rather than crashing."""
    out = {}
    for pt, cats in (('HITTER', HITTING_LEADERS), ('PITCHER', PITCHING_LEADERS)):
        url = TOP_PLAYERS_API.format(season=season, pt=pt, limit=LEADER_FETCH,
                                     fields=LEADER_FIELDS[pt])
        try:
            data = json.loads(fetch_text(url))
        except (RuntimeError, json.JSONDecodeError):
            continue
        by_type = {c['type']: c.get('rankings', [])
                   for c in data.get('result', {}).get('topPlayers', [])}
        for key, _label in cats:
            if by_type.get(key):
                out[key] = by_type[key]
    return out


def fetch_kbo_name(pcode, is_pitcher):
    """{'name', 'foreign'} from the KBO English player page, or None. Name is the
    raw ALL-CAPS surname-first form; foreign is inferred from salary currency
    ($ = import). Hitters and pitchers live on different pages."""
    try:
        html = fetch_text(KBO_PLAYER_PAGE[is_pitcher].format(pc=pcode))
    except RuntimeError:
        return None
    m = re.search(r'<b>Name</b>\s*:\s*([^<]+?)\s*<', html)
    if not m:
        return None
    name = m.group(1).strip()
    # A page that renders the Name label with nothing after it still matches:
    # the capture takes the whitespace and .strip() empties it. Returning that
    # as an entry is worse than returning nothing, because the caller CACHES
    # it — see resolve_name. Bruce Zimmermann was stored as {'name': '',
    # 'foreign': False} that way and shipped a blank name on every card he
    # appeared on, while a fresh fetch of the same page worked fine.
    if not name:
        return None
    sal = re.search(r'<b>Salary</b>\s*:\s*([^<]+)<', html)
    return {'name': name, 'foreign': bool(sal and '$' in sal.group(1))}


def named(entry):
    """True if a roster entry actually carries a name.

    `if entry:` is not the same test and was the bug: an entry is a dict, so
    {'name': '', 'foreign': False} is truthy and counted as a hit. That made a
    single bad fetch permanent — the cached blank suppressed the re-fetch AND
    the Korean-name fallback, so the player shipped as an empty string on every
    card and in every alt text until someone edited the roster by hand.
    """
    return bool(entry and (entry.get('name') or '').strip())


def resolve_name(pcode, name_ko, is_pitcher, roster, added):
    """Romanised display name for a leaderboard player, fetching + caching into
    the roster on a miss (appending (pcode, entry) to `added`), or the Korean
    name if the KBO lookup fails.

    A cached entry with no name counts as a miss, so a bad fetch heals itself
    on the next run instead of sticking."""
    pcode = str(pcode or '')
    entry = roster.get(pcode)
    if not named(entry) and pcode:
        fetched = fetch_kbo_name(pcode, is_pitcher)
        if fetched:
            entry = fetched
            roster[pcode] = entry
            added.append((pcode, entry))
    if named(entry):
        return order_name(entry['name'], entry.get('foreign', False), pcode)
    return name_ko


def fmt_leader_value(key, value):
    """Batting average as '.360', E.R.A. as '2.19', counting stats as integers."""
    if key == 'hitterHra':
        return f'{float(value):.3f}'.lstrip('0')
    if key == 'pitcherEra':
        return f'{float(value):.2f}'
    return str(int(round(float(value))))


def leader_rows(key, rankings, roster, added):
    """Up to LEADER_COUNT (rank, name, teamCode, value) tuples for one
    leaderboard, filtering rate stats to qualified players."""
    is_pitcher = key.startswith('pitcher')
    rows = []
    for r in rankings:
        if key in QUALIFIED_ONLY and not r.get('isQualified'):
            continue
        name = resolve_name(r.get('playerId'), r.get('playerName', ''),
                            is_pitcher, roster, added)
        rows.append((r.get('ranking'), name, r.get('teamId', ''),
                     fmt_leader_value(key, r.get(key))))
        if len(rows) == LEADER_COUNT:
            break
    return rows


def compose_results(date_str, finals, cancelled=()):
    """Final-scores digest, with a Postponed section listing any cancelled games
    rather than dropping them. A day with no finals at all gets a 'Postponed'
    header instead of 'Final scores' — matching attach_results_card's card
    title — and the fixture list needs no separate 'Postponed:' label of its
    own, since the header already says so."""
    header = 'Final scores' if finals else 'Postponed'
    parts = [f'🇰🇷⚾ {header} · {format_date(date_str)}']
    if finals:
        parts.append('\n'.join(result_line(g) for g in by_start(finals)))
    if cancelled:
        pp = '\n'.join(f'{team_label(g["awayTeamCode"])} @ {team_label(g["homeTeamCode"])}'
                       for g in by_start(cancelled))
        parts.append(pp if not finals else f'Postponed:\n{pp}')
    return [('\n\n'.join(parts) + '\n\n', HASHTAGS)]


def hits_errors_line(record):
    """'Hits: 18–5 · Errors: 2–1' (away–home), or '' if the line score is
    missing. En-dash separates the two team totals."""
    r = record.get('scoreBoard', {}).get('rheb', {})
    a, h = r.get('away'), r.get('home')
    if not a or not h:
        return ''
    return (f'Hits: {a.get("h", 0)}–{h.get("h", 0)} · '
            f'Errors: {a.get("e", 0)}–{h.get("e", 0)}')


def decision_line(record, roster, added):
    """'W: Naile (6-5) · L: Hatch (1-4) · S: Lee Young-ha (14)' — the winning,
    losing and (if any) saving pitcher, romanised, with season W-L or save
    count. Holds are omitted. '' if no decision parses."""
    by_result = {p.get('wls'): p for p in record.get('pitchingResult', [])}
    parts = []
    for code, tag in (('W', 'W'), ('L', 'L'), ('S', 'S')):
        p = by_result.get(code)
        if not p:
            continue
        name = resolve_name(p.get('pCode'), p.get('name', ''), True, roster, added)
        detail = p.get('s', 0) if code == 'S' else f'{p.get("w", 0)}-{p.get("l", 0)}'
        parts.append(f'{tag}: {name} ({detail})')
    return ' · '.join(parts)


def hr_labels(game, record, roster, added):
    """Every batter with a home run, as '🐯 Kim Do-yeong' labels (team emoji +
    romanised name; a multi-homer game shows the count), away side first."""
    labels = []
    for side, code in (('away', game['awayTeamCode']),
                       ('home', game['homeTeamCode'])):
        emoji = TEAM_EMOJI.get(code, '')
        for b in record.get('battersBoxscore', {}).get(side, []):
            if b.get('hr', 0) > 0:
                name = resolve_name(b.get('playerCode'), b.get('name', ''),
                                    False, roster, added)
                label = f'{emoji} {name}'.strip()
                if b['hr'] > 1:
                    label += f' ({b["hr"]})'
                labels.append(label)
    return labels


def box_score_body(game, record, roster, added, attendance=None):
    """One game's compact box score as a post body: the matchup and final
    (with a non-regulation inning tag), then hits/errors, the pitching
    decision, attendance, and any home runs. The HR list is trimmed to keep the
    post under Bluesky's limit, appending '(+N more)' when batters are dropped
    (a slugfest with long names could otherwise overflow).

    This text is the fallback the post carries only if its card fails to render;
    the card is the usual surface."""
    a, h = game['awayTeamScore'], game['homeTeamScore']
    head = (f'{team_label(game["awayTeamCode"])} {a} @ '
            f'{team_label(game["homeTeamCode"])} {h}')
    inn = final_innings(game.get('statusInfo'))
    if inn and inn != 9:
        head += f' ({inn})'
    base = [head, '']
    for line in (hits_errors_line(record),
                 decision_line(record, roster, added)):
        if line:
            base.append(line)
    if attendance:
        base.append(f'Attendance: {attendance}')
    labels = hr_labels(game, record, roster, added)

    for n in range(len(labels), -1, -1):        # try all HRs, then trim from end
        if n == len(labels) and labels:
            hr = 'HR: ' + ', '.join(labels)
        elif n > 0:
            hr = 'HR: ' + ', '.join(labels[:n]) + f' (+{len(labels) - n} more)'
        else:
            hr = ''                             # n == 0: drop the HR line
        body = '\n'.join(base + ([hr] if hr else [])) + '\n\n'
        if grapheme_len(plain_text(body, [])) <= BLUESKY_LIMIT:
            return body
    return '\n'.join(base) + '\n\n'             # base alone over limit (unreachable)


def tags_footer(tags):
    """The rendered hashtag line appended to a post, or '' if no tags. A blank
    line separates it from the body regardless of the body's trailing newlines."""
    return ('\n\n' + ' '.join(f'#{t}' for t in tags)) if tags else ''


def pack_lines(lines, header, reserve=0):
    """Pack lines into as few post bodies as fit under the char limit, split as
    evenly as possible (so 5 lines become 3+2, not 4+1). The first post carries
    `header`; continuation posts carry just lines. `reserve` leaves room for a
    footer (e.g. hashtags) appended to every post at render time."""
    def build(chunks):
        return [('' if i else header) + '\n'.join(c) for i, c in enumerate(chunks)]

    def fits(bodies):
        return all(len(b) + reserve <= MAX_POST_CHARS for b in bodies)

    for n in range(1, len(lines) + 1):
        size = -(-len(lines) // n)                      # ceil(len/n)
        chunks = [lines[i:i + size] for i in range(0, len(lines), size)]
        bodies = build(chunks)
        if len(chunks) <= n and fits(bodies):
            return bodies
    return build([[ln] for ln in lines])                # fallback: one per post


def compose_schedule(date_str, items, roster):
    """Schedule thread: a matchups post, plus one or more probable-starters
    replies (chunked to fit) if any starter is announced. Only the matchups post
    carries the hashtags. Returns a list of (body, tags) segments."""
    items = sorted(items, key=lambda it: it['game'].get('gameDateTime', ''))
    games = [it['game'] for it in items]
    # If every game starts at the same time, say so once in the header and drop
    # the per-line times; otherwise show the time on each line.
    times = {format_time(g['gameDateTime']) for g in games}
    uniform = len(times) == 1 and len(games) > 1
    head = f'🇰🇷⚾ Today’s games · {format_date(date_str)}'
    if uniform:
        head += f' (all games start at {next(iter(times))})'
    lines = '\n'.join(schedule_line(g, show_time=not uniform) for g in games)
    matchups = f'{head}\n\n{lines}\n\n'
    segments = [(matchups, HASHTAGS)]

    if any(it['away'] or it['home'] for it in items):
        pitch_lines = [starters_line(it, roster) for it in items]
        header = '🇰🇷⚾ Probable starters\n(W-L, E.R.A.)\n\n'
        # Starters replies carry no hashtags — only the matchups post is tagged.
        for body in pack_lines(pitch_lines, header):
            segments.append((body, []))
    return segments


def plain_text(body, tags):
    body = body.rstrip('\n')
    footer = tags_footer(tags)
    return body + footer if body else footer.lstrip('\n')


def grapheme_len(s):
    """Grapheme-cluster count, matching how Bluesky measures a post's length.
    Handles the multi-scalar emoji we actually use — regional-indicator flag
    pairs (🇰🇷), ZWJ sequences, variation selectors and skin-tone modifiers —
    so a flag counts as 1, not the 2 code points len() would report."""
    n = 0
    prev_ri = prev_zwj = False
    for ch in s:
        cp = ord(ch)
        if cp == 0x200D:                              # ZWJ joins the next scalar
            prev_zwj = True
            continue
        if cp == 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF:  # VS16 / skin tone: combines
            continue
        if prev_zwj:
            prev_zwj = False
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF:                  # regional indicator
            if prev_ri:                               # 2nd half of a flag pair
                prev_ri = False
                continue
            prev_ri = True
            n += 1
            continue
        prev_ri = False
        n += 1
    return n


def keychain_password(account, service):
    result = subprocess.run(
        ['security', 'find-generic-password', '-a', account, '-s', service, '-w'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'No Keychain password for account="{account}" service="{service}".\n'
            f'Add it with:\n'
            f'  security add-generic-password -a "{account}" -s "{service}" -w'
        )
    return result.stdout.strip()


def build_tb(body, tags):
    from atproto import client_utils
    body = body.rstrip('\n')
    tb = client_utils.TextBuilder()
    tb.text(body)
    if tags:
        if body:                 # a tags-only post starts with the tags, no gap
            tb.text('\n\n')
        for i, tag in enumerate(tags):
            if i:
                tb.text(' ')
            tb.tag(f'#{tag}', tag)
    return tb


# --------------------------------------------------------------------------
# Cards. Each post can carry a rendered PNG of the same information. Rendering
# needs Chrome and Pillow, so it is always attempted inside build_card(): if
# anything fails the post is held rather than shipped as plain text.
#
# ⚠ That is a reversal of the original policy ("a missed image is not worth a
# missed post"), changed 2 September 2026 after a Chrome hang (60s, then a
# 120s retry, both timed out) on one box score put a bare text score line on
# the feed with no image at all — every other post around it, before and
# after, carried its card fine, so the failure was transient rather than
# systemic. Deleted and reposted with a card once Chrome recovered. His
# instruction: skip posting rather than post without a card.
# --------------------------------------------------------------------------

def build_card(render, alt):
    """Render one card and return {'png', 'alt', 'size'}, or None if rendering
    failed for any reason. `render` is a zero-argument callable that writes a
    PNG to the path it is given and returns (path, (w, h))."""
    import tempfile
    try:
        import kbo_card
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'card.png')
            _, size = render(path)
            return {'png': Path(path).read_bytes(), 'alt': alt, 'size': size}
    except Exception as exc:                # noqa: BLE001 - never block a post
        print(f'  (card not rendered: {exc.__class__.__name__}: {exc})')
        return None


def seg_parts(segment):
    """Segments are (body, tags) or (body, tags, card). Normalise to three."""
    body, tags = segment[0], segment[1]
    card = segment[2] if len(segment) > 2 else None
    return body, tags, card


def card_only(segment, card):
    """A segment stripped of its body text, with `card` attached: the card
    carries the whole post (its title included) and its alt text repeats it in
    full, so the only text the post needs is its hashtags. Everything else lives
    on the card.

    Returns None if the card failed to render. Cards are required, not a
    decoration — a segment with no card is held rather than posted as plain
    text (see the note above build_card)."""
    _, tags, _ = seg_parts(segment)
    if not card:
        return None
    return ('', tags, card)


def with_card(segments, index, card):
    """Return `segments` with `card` attached to the segment at `index`, that
    segment stripped down to its hashtags — or None if the card failed to
    render, meaning the whole thread this segment anchors is held rather than
    posted without it."""
    carded = card_only(segments[index], card)
    if carded is None:
        return None
    out = list(segments)
    out[index] = carded
    return out


def attach_results_card(date_str, finals, cancelled, segments, roster, added):
    """The final-scores digest gets a card of the same slate, rainouts
    included — the card carries the whole post, so a postponement dropped here
    would vanish entirely."""
    import kbo_card
    import kbo_card_data as data
    records = {g['gameId']: fetch_box_score(g['gameId']) for g in finals}
    rows = data.results_input(finals, {k: v for k, v in records.items() if v},
                              roster, added)
    ppd = data.postponed_input(cancelled)
    label = data.card_date(date_str)
    card = build_card(
        lambda path: kbo_card.render_results_card(label, rows, path,
                                                  postponed=ppd),
        data.results_alt(label, rows, ppd))
    return with_card(segments, 0, card)


def box_score_segments(finals, roster, added, attendance=None, tags=(), skip_ids=()):
    """A box-score segment per finished game, each carrying its own card.

    `tags` sets the hashtags on every segment: empty for the roundup, where
    these are threaded replies under the already-tagged digest, and HASHTAGS for
    a standalone live post, which is its own thread root. `skip_ids` drops games
    already posted live, so the nightly roundup only threads box scores for games
    the live run didn't cover — its digest card still lists every final.

    A game whose card fails to render is dropped from the returned list, not
    downgraded to a text-only segment — the live caller holds it for the next
    poll, and the roundup still lists it on the digest card even if its own
    box-score reply never comes."""
    import kbo_card
    import kbo_card_data as data
    attendance = attendance or {}
    skip_ids = set(skip_ids)
    segments = []
    for g in by_start(finals):
        if g['gameId'] in skip_ids:
            continue
        record = fetch_box_score(g['gameId'])
        if not record:
            continue
        att = attendance.get(g['homeTeamCode'])
        att_str = None
        if att:
            figure, venue = att
            att_str = f'{figure} · {venue}' if venue else figure
        body = box_score_body(g, record, roster, added, att_str)
        game = data.box_input(g, record, roster, added, att_str)
        label = data.card_date(f'{g["gameId"][:4]}-{g["gameId"][4:6]}-{g["gameId"][6:8]}')
        card = build_card(
            lambda path, game=game, label=label:
                kbo_card.render_box_score_card(label, game, path),
            data.box_alt(label, game))
        carded = card_only((body, list(tags)), card)
        if carded is not None:
            segments.append(carded)
    return segments


def attach_schedule_cards(date_str, playable, roster, segments):
    """The schedule thread, one card per post: tonight's fixtures, then the
    probable starters threaded beneath them.

    The fixtures card leaves the pitchers off, because the replies below it are
    given over to them. `segments` is what compose_schedule built — a matchups
    post followed by one or more starters replies — and the whole tail is
    replaced by one carded reply per group of at most three fixtures.

    Returns None (hold the whole thread) if the fixtures card itself fails to
    render. If any starters card fails, the starters replies are dropped and
    only the fixtures post goes out — matching card_only's rule everywhere
    else, rather than falling back to their old plain-text form."""
    import kbo_card
    import kbo_card_data as data
    label = data.card_date(date_str)

    rows, subtitle = data.schedule_input(playable)
    fixtures = build_card(
        lambda path: kbo_card.render_schedule_card(label, rows, path,
                                                   subtitle=subtitle),
        data.schedule_alt(label, rows, subtitle))
    out = with_card(segments, 0, fixtures)
    if out is None:
        return None
    if len(out) == 1:                       # no starter announced for any game
        return out

    starters = data.starters_input(playable, roster)
    # Split across as many replies as it takes to keep every card landscape:
    # see starters_chunks. A full five-fixture slate on one card is portrait,
    # and Bluesky shows a portrait image at 371 px against a landscape one's
    # 515, so the post looked narrower than the fixtures card above it.
    chunks = data.starters_chunks(starters)
    total = len(chunks)
    cards = []
    for i, chunk in enumerate(chunks, 1):
        # The counter only appears when there is something to count: a single
        # card should not be titled '(1 of 1)'.
        title = ('Probable Starters' if total == 1
                 else f'Probable Starters ({i} of {total})')
        cards.append(build_card(
            lambda path, chunk=chunk, title=title:
                kbo_card.render_starters_card(label, chunk, path, title=title),
            data.starters_alt(label, chunk, part=i, of=total)))
    # All or nothing. The text replies below were chunked by character count,
    # which splits at a different place, so carding some groups and leaving the
    # rest as text would repeat some fixtures and drop others — and posting any
    # of them without a card is exactly what this file no longer does, so a
    # partial render drops the starters replies rather than falling back to
    # their old plain-text form. The fixtures post above still goes out.
    if not all(cards):
        return out[:1]
    # One card per group replaces however many replies the text needed. Each
    # card carries its whole post, and only the fixtures post is tagged.
    return out[:1] + [('', [], card) for card in cards]


def attach_standings_card(date_str, rows, segments):
    import kbo_card
    import kbo_card_data as data
    card_rows = data.standings_input(rows)
    label = data.card_date(date_str)
    on = datetime.strptime(date_str, '%Y-%m-%d').date()
    cut = PLAYOFF_SPOTS if data.show_cutline(on) else None
    card = build_card(
        lambda path: kbo_card.render_standings_card(label, card_rows, path,
                                                    cut_after=cut),
        data.standings_alt(label, card_rows, cut))
    return with_card(segments, 0, card)


def leaders_segments(date_str, raw, roster, added):
    """One post per leaderboard, each carrying that board's card.

    Each board appears once, the post is the card alone — its title and date
    are on the card, the standings are in the alt text — so the body is empty.
    A board whose card fails to render is dropped rather than posted as its
    old text block; the surviving boards still thread together, and whichever
    ends up first carries the hashtags (there is no separate intro post: the
    date and 'season leaders' framing are on every card). Returns [] if no
    board has data or every card failed."""
    import kbo_card
    import kbo_card_data as data
    label = data.card_date(date_str)
    boards = []
    for key, title in HITTING_LEADERS + PITCHING_LEADERS:
        top = leader_rows(key, raw.get(key, []), roster, added)
        if not top:
            continue
        rows = data.leaders_input(top)
        tags = HASHTAGS if not boards else []   # first board is the thread root
        card = build_card(
            lambda path, title=title, rows=rows:
                kbo_card.render_leaders_card(label, title, rows, path),
            data.leaders_alt(label, title, rows))
        if card:
            boards.append(('', tags, card))
    return boards


def post_thread(segments):
    """Post one or more segments as a Bluesky thread (each replies to the last).
    atproto is imported lazily so --dry-run runs without the dependency."""
    from atproto import Client, models

    password = keychain_password(HANDLE, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(HANDLE, password)

    root_ref = parent_ref = None
    for segment in segments:
        body, tags, card = seg_parts(segment)
        reply = None
        if root_ref is not None:
            reply = models.AppBskyFeedPost.ReplyRef(root=root_ref, parent=parent_ref)
        if card:
            width, height = card['size']
            resp = bsky.send_image(
                text=build_tb(body, tags), image=card['png'],
                image_alt=card['alt'], reply_to=reply,
                image_aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(
                    width=width, height=height))
        else:
            resp = bsky.send_post(text=build_tb(body, tags), reply_to=reply)
        ref = models.create_strong_ref(resp)
        if root_ref is None:
            root_ref = ref
        parent_ref = ref


def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return {}


def archive_results(date_str, finals, cancelled):
    """Append the day's finals + postponements to kbo_results_history.json, a
    structured season archive for a future standings/results page."""
    arch = json.loads(RESULTS_ARCHIVE.read_text()) if RESULTS_ARCHIVE.exists() else {}
    arch[date_str] = {
        'finals': [{'away': g['awayTeamCode'], 'home': g['homeTeamCode'],
                    'away_score': g['awayTeamScore'], 'home_score': g['homeTeamScore'],
                    'innings': final_innings(g.get('statusInfo'))}
                   for g in by_start(finals)],
        'postponed': [{'away': g['awayTeamCode'], 'home': g['homeTeamCode']}
                      for g in by_start(cancelled)],
    }
    write_json_atomic(RESULTS_ARCHIVE, arch, ensure_ascii=False, indent=2, sort_keys=True)


def results_candidates(argv):
    """Dates to try for the results digest, newest first: an explicit --date, or
    [today, yesterday] so a late-night run can catch a game that finished after
    the main run held (the date having rolled past midnight)."""
    if '--date' in argv:
        return [argv[argv.index('--date') + 1]]
    now = datetime.now(KST)
    return [now.strftime('%Y-%m-%d'), (now - timedelta(days=1)).strftime('%Y-%m-%d')]


def evaluate_results(candidates, history, ignore_history):
    """First candidate date with a postable slate — all games final, at least one
    final or postponement — that isn't already posted. A date with games still in
    progress is held (skipped). Returns (date, finals, cancelled) or None."""
    for d in candidates:
        if f'results:{d}' in history and not ignore_history:
            return None                     # newest unposted date is done; stop
        games = fetch_games(d)
        cancelled = [g for g in games if g.get('cancel')]
        finals = [g for g in games if g.get('statusCode') == FINAL and not g.get('cancel')]
        live = [g for g in games if g.get('statusCode') != FINAL and not g.get('cancel')]
        if live:
            print(f'{d}: {len(live)} game(s) still unfinished — holding.')
            continue
        if finals or cancelled:
            return d, finals, cancelled
        print(f'{d}: no games.')
    return None


def pick_standings_date(candidates, history, ignore_history):
    """The date whose standings the evening run should post, newest first, or
    None if there's nothing new yet. A date settles once all of its games are
    final (or postponed): a date with a game still in progress is skipped so a
    later poll catches the late finish, and an off-day with no games falls
    through to the previous date — which the prior evening already posted, so the
    walk stops at its history entry rather than re-posting an unchanged table.

    Mirrors evaluate_results (same candidate walk, same midnight-boundary
    behaviour) because the two gates must agree on when a night is 'done', and
    then waits for the results digest to have actually gone out (see below)."""
    for d in candidates:
        if f'standings:{d}' in history and not ignore_history:
            return None                     # newest unposted date is done; stop
        games = fetch_games(d)
        noncancel = [g for g in games if not g.get('cancel')]
        live = [g for g in noncancel if g.get('statusCode') != FINAL]
        if live:
            print(f'{d}: {len(live)} game(s) still unfinished — holding.')
            continue
        if noncancel:                       # games played and all now final
            # ⚠ The night's scores lead and the table follows. Agreeing on
            # when a night is 'done' is not enough to order the two posts,
            # because results fires on two slots (23:30, 00:45) and standings
            # polls seven times from 22:00: a slate settling at 22:00 put the
            # table out 90 minutes ahead of the scores it reflects. Measured
            # 23 August 2026 over the nine posted nights since 15 August —
            # standings went first on five of them. Gate on the results entry
            # rather than on the clock, because the settling time is exactly
            # the thing nobody can predict; a later poll then carries it.
            if f'results:{d}' not in history and not ignore_history:
                print(f'{d}: settled, but the results digest has not posted '
                      f'yet — holding the table until it has.')
                return None
            return d
        print(f'{d}: no games — standings unchanged.')
    return None


def note_final_seen(history, gid, dry_run):
    """Seconds this bot has been holding a finished game for its crowd figure.

    Bounding the wait needs a start time, and nothing recorded one: the schedule
    API carries no end time (checked 23 August 2026 -- a final game reports only
    gameDateTime, statusCode and statusInfo), so the only honest clock is when
    this bot first saw the game final. The consequence to know: a Mac asleep
    through the finish starts its 45 minutes from waking, not from the last out.
    The nightly roundup remains the backstop for that."""
    key = f'final_seen:{gid}'
    now = datetime.now(timezone.utc)
    entry = history.get(key)
    if entry:
        try:
            return (now - datetime.fromisoformat(entry['first_seen'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            # A damaged stamp must not freeze the game at "0 seconds waited"
            # forever, which is what returning 0 without rewriting would do.
            print(f'  (unreadable hold stamp for {gid} — restarting its clock)')
    history[key] = {'first_seen': now.isoformat()}
    if not dry_run:
        write_json_atomic(HISTORY, history, ensure_ascii=False, indent=2)
    return 0.0


def print_segments(mode, segments):
    """Dump each segment (text, length, over-limit flag, card size + alt) to
    stdout — the shared preview used by both threaded emit() and the live path."""
    for i, segment in enumerate(segments):
        body, tags, card = seg_parts(segment)
        text = plain_text(body, tags)
        length = grapheme_len(text)
        flag = '  ⚠ OVER LIMIT' if length > BLUESKY_LIMIT else ''
        label = f'post {i + 1}/{len(segments)}' if len(segments) > 1 else 'post'
        print(f'\n{mode} {label} ({length} chars){flag}\n{"-"*40}\n{text}\n{"-"*40}')
        if card:
            w, h = card['size']
            print(f'  + card {w}x{h}, {len(card["png"])//1024} KB\n'
                  f'    alt: {card["alt"]}')
        else:
            print('  (no card — text only)')


def emit(mode, date_str, segments, dry_run, history, count):
    """Print each segment, and (unless dry-run) post the thread and record it.
    Returns True if it actually posted.

    `segments` is None when the post's anchor card failed to render (see
    with_card): nothing is posted and history is left untouched, so a mode
    that polls (standings, results, live) picks the date back up on its next
    slot. schedule and leaders run once a day with no such retry — a Chrome
    hang there costs the whole day's post, which is the trade this file's
    2 September 2026 policy accepts in exchange for never shipping a card-less
    post."""
    if segments is None:
        print(f'\n{mode}: card failed to render — holding rather than '
              f'posting without one.')
        return False
    print_segments(mode, segments)
    if dry_run:
        print('\n(dry run — nothing posted, history untouched)')
        return False
    post_thread(segments)
    history[f'{mode}:{date_str}'] = {
        'posted_at': datetime.now(timezone.utc).isoformat(), 'games': count}
    write_json_atomic(HISTORY, history, ensure_ascii=False, indent=2)
    print('Posted.')
    return True


# Mode is positional and falls back to 'results', so an unrecognised argument
# used to be silently ignored and the run posted results LIVE: `kbo_post.py
# --help` was a live post. seoul-index published a real thread through exactly
# that trap on 20 July 2026. Validated before main() rather than inside it, so
# nothing has fetched, decided or posted by the time a bad argv is rejected.
MODES = {'schedule', 'standings', 'leaders', 'live', 'results'}
FLAGS = {'--dry-run', '--all'}
VALUE_FLAGS = {'--date'}


def validate_argv(argv):
    """An error string for an argv that should not run, or None to proceed."""
    unknown, modes, i = [], [], 0
    while i < len(argv):
        arg = argv[i]
        if arg in VALUE_FLAGS:
            # Bare `--date` used to raise IndexError deep inside a mode branch,
            # and a malformed one reached the API as a nonsense query.
            if i + 1 >= len(argv):
                return f'{arg} needs a date argument (YYYY-MM-DD).'
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', argv[i + 1]):
                return f'{arg} expects YYYY-MM-DD, got {argv[i + 1]!r}.'
            i += 2
            continue
        if arg in MODES:
            modes.append(arg)
        elif arg not in FLAGS:
            unknown.append(arg)
        i += 1
    if unknown:
        return f'Unknown argument(s): {" ".join(unknown)}.'
    if len(modes) > 1:
        # Precedence used to pick one silently; a run meant for two posts made
        # one, and which one depended on the order of the chain below.
        return f'More than one mode given: {" ".join(modes)}. Pick one.'
    return None


def main():
    argv = sys.argv[1:]
    mode = ('schedule' if 'schedule' in argv
            else 'standings' if 'standings' in argv
            else 'leaders' if 'leaders' in argv
            else 'live' if 'live' in argv else 'results')
    dry_run = '--dry-run' in argv
    ignore_history = '--all' in argv

    # Taken before the network wait, not after, so a run stuck waiting for the
    # net holds the slot rather than letting a second run through to race it on
    # kbo_history.json. A dry run touches no shared state, so it never queues
    # behind a real one.
    if not dry_run and not kbo_lock.hold(mode, LOCK_WAIT[mode]):
        print(f'{mode}: another KBO run is still going — skipping this slot.')
        return

    # Five minutes only: live polls every 15 minutes and standings every 30,
    # so a longer wait would still be running when the next slot fires. Every
    # mode fetches from Naver before it can decide anything, so gate them all.
    net_guard.require_network(300)

    history = load_history()

    if mode == 'standings':
        # Standings post the day's settled table, so the run is gated on tonight's
        # slate being complete and is polled through the evening. An explicit
        # --date posts that date immediately (manual/backfill); otherwise
        # pick_standings_date holds until all of today's games are final (a later
        # poll catches a late finish) and skips off-days, where the table hasn't
        # moved since the previous evening's post.
        if '--date' in argv:
            date_str = argv[argv.index('--date') + 1]
            if f'standings:{date_str}' in history and not ignore_history:
                print(f'standings for {date_str} already posted — skipping.')
                return
        else:
            date_str = pick_standings_date(
                results_candidates(argv), history, ignore_history)
            if not date_str:
                print('No settled standings to post yet.')
                return
        rows = fetch_standings()
        if not rows:
            print('standings unavailable (KBO site) — skipping.')
            return
        segments = compose_standings(date_str, rows)
        segments = attach_standings_card(date_str, rows, segments)
        emit('standings', date_str, segments, dry_run, history, len(rows))
        return

    if mode == 'leaders':
        date_str = (argv[argv.index('--date') + 1] if '--date' in argv
                    else datetime.now(KST).strftime('%Y-%m-%d'))
        if f'leaders:{date_str}' in history and not ignore_history:
            print(f'leaders for {date_str} already posted — skipping.')
            return
        data = fetch_leaders(date_str[:4])
        roster = load_roster()
        added = []
        segments = leaders_segments(date_str, data, roster, added)
        if added:
            if not dry_run:
                write_json_atomic(ROSTER, roster, ensure_ascii=False,
                                  indent=2, sort_keys=True)
            for pc, entry in added:
                warn = ('  ⚠ NEW IMPORT — if East-Asian, add to KEEP_SURNAME_FIRST'
                        if entry.get('foreign') else '')
                print(f'  + roster {pc}: {entry["name"]}{warn}')
        if not segments:
            print('leaders unavailable (no leader data) — skipping.')
            return
        emit('leaders', date_str, segments, dry_run, history, len(segments))
        return

    if mode == 'schedule':
        date_str = (argv[argv.index('--date') + 1] if '--date' in argv
                    else datetime.now(KST).strftime('%Y-%m-%d'))
        if f'schedule:{date_str}' in history and not ignore_history:
            print(f'schedule card for {date_str} already posted — skipping.')
            return
        playable = [g for g in fetch_games(date_str) if not g.get('cancel')]
        if not playable:
            print(f'{date_str}: no games scheduled — nothing to post.')
            return
        roster = load_roster()
        items = []
        for g in by_start(playable):
            away, home = fetch_starters(g['gameId'])
            items.append({'game': g, 'away': away, 'home': home})
        segments = compose_schedule(date_str, items, roster)
        segments = attach_schedule_cards(date_str, playable, roster, segments)
        emit('schedule', date_str, segments, dry_run, history, len(playable))
        return

    if mode == 'live':
        # ⚠ A night's games belong to the date they STARTED on, so a poll past
        # midnight has to look back a day or it reads the new date's untouched
        # slate. Every 00:00, 00:15 and 00:30 poll of the season did exactly
        # that: no live box score has ever gone out later than 23:31, and a
        # game finishing after the last useful poll was left to the roundup.
        # The walk stops at a night whose roundup HAS posted, because that post
        # carries the box scores live missed — without the bound, this run
        # would post them a second time the next evening.
        dates = ([argv[argv.index('--date') + 1]] if '--date' in argv
                 else [d for d in results_candidates(argv)
                       if f'results:{d}' not in history or ignore_history])
        finals_seen = 0
        pending = []
        for d in dates:
            finals = [g for g in fetch_games(d)
                      if g.get('statusCode') == FINAL and not g.get('cancel')]
            finals_seen += len(finals)
            pending += [g for g in finals
                        if f'live:{g["gameId"]}' not in history or ignore_history]
        if not pending:
            print(f'{"/".join(dates) or "no open night"}: {finals_seen} final, '
                  f'none new to post live.')
            return
        # Attendance-gated: KBO publishes each crowd figure about when the game
        # goes final, but a game that just ended may not have its number up yet.
        # Such a game is held for a later poll; if it never publishes, the
        # nightly roundup still carries it (without the figure). One crowd-page
        # fetch serves the whole poll -- one per night still open.
        attendance_by_date = {d: fetch_attendance(d)
                              for d in {g['gameDate'] for g in pending}}
        roster = load_roster()
        added = []
        posted = 0
        for g in by_start(pending):
            gid = g['gameId']
            attendance = attendance_by_date.get(g['gameDate'], {})
            matchup = f'{g["awayTeamCode"]}@{g["homeTeamCode"]}'
            if not attendance.get(g['homeTeamCode']):
                waited = note_final_seen(history, gid, dry_run)
                if waited < ATTENDANCE_GRACE:
                    print(f'  {matchup}: final, attendance not yet published — '
                          f'holding ({int(waited // 60)} of '
                          f'{ATTENDANCE_GRACE // 60} min).')
                    continue
                print(f'  {matchup}: attendance still unpublished after '
                      f'{ATTENDANCE_GRACE // 60} min — posting without it.')
            segs = box_score_segments([g], roster, added, attendance, tags=HASHTAGS)
            if not segs:      # box score not fetchable yet, or its card didn't render
                print(f'  {matchup}: box score not ready — holding.')
                continue
            print_segments('live', segs)
            if dry_run:
                continue
            post_thread(segs)
            history[f'live:{gid}'] = {
                'posted_at': datetime.now(timezone.utc).isoformat(),
                'matchup': matchup}
            history.pop(f'final_seen:{gid}', None)      # the wait is over
            write_json_atomic(HISTORY, history, ensure_ascii=False, indent=2)
            posted += 1
        if added and not dry_run:
            write_json_atomic(ROSTER, roster, ensure_ascii=False,
                              indent=2, sort_keys=True)
            for pc, entry in added:
                warn = ('  ⚠ NEW IMPORT — if East-Asian, add to KEEP_SURNAME_FIRST'
                        if entry.get('foreign') else '')
                print(f'  + roster {pc}: {entry["name"]}{warn}')
        if dry_run:
            print('\n(dry run — nothing posted, history untouched)')
        else:
            print(f'\nPosted {posted} live game(s).')
        return

    # results
    picked = evaluate_results(results_candidates(argv), history, ignore_history)
    if not picked:
        print('No results to post.')
        return
    date_str, finals, cancelled = picked
    print(f'KBO {date_str} · results: {len(finals)} final, {len(cancelled)} postponed.')
    roster = load_roster()
    added = []
    attendance = fetch_attendance(date_str)
    segments = compose_results(date_str, finals, cancelled)
    segments = attach_results_card(date_str, finals, cancelled, segments,
                                   roster, added)
    # Games already posted live carry their box score there, so the roundup only
    # threads box scores for games the live run missed. The digest card above
    # still lists every final, so the day's slate is complete in one post.
    already_live = {g['gameId'] for g in finals
                    if f'live:{g["gameId"]}' in history}
    segments += box_score_segments(finals, roster, added, attendance,
                                   skip_ids=already_live)
    if added:
        if not dry_run:
            write_json_atomic(ROSTER, roster, ensure_ascii=False,
                              indent=2, sort_keys=True)
        for pc, entry in added:
            warn = ('  ⚠ NEW IMPORT — if East-Asian, add to KEEP_SURNAME_FIRST'
                    if entry.get('foreign') else '')
            print(f'  + roster {pc}: {entry["name"]}{warn}')
    if emit('results', date_str, segments, dry_run, history, len(finals)):
        archive_results(date_str, finals, cancelled)


def record_run(mode):
    """Heartbeat: note that a run completed without error, whether or not it
    had anything to post. Off-days and the whole off-season are legitimately
    postless, so 'last posted' cannot tell a broken bot from a quiet one --
    'last completed a run' can. Never let this break a run that already
    succeeded."""
    try:
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A damaged state file used to disable the heartbeat permanently:
            # every later run raised here and reported nothing. The file holds
            # only the last run's time and mode, both of which this call is
            # about to overwrite, so there is nothing to salvage — start again.
            print('(heartbeat state unreadable — rewriting it)')
            state = {}
        state['last_run_at'] = datetime.now(timezone.utc).isoformat()
        state['last_run_mode'] = mode
        write_json_atomic(STATE, state, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as exc:                    # noqa: BLE001 - heartbeat is best-effort
        print(f'(could not write heartbeat: {exc})')


if __name__ == '__main__':
    _err = validate_argv(sys.argv[1:])
    if _err:
        sys.exit(f'{_err}\nRecognised: {" ".join(sorted(MODES))} | '
                 f'{" ".join(sorted(FLAGS))} | --date YYYY-MM-DD. '
                 f'Refusing to run (a bare run posts live).')
    main()
    # Only real runs count as a heartbeat; a manual --dry-run should not make a
    # stalled bot look alive.
    if '--dry-run' not in sys.argv[1:]:
        record_run(next((m for m in ('schedule', 'standings', 'leaders', 'live')
                         if m in sys.argv[1:]), 'results'))
