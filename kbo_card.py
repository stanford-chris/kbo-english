#!/usr/bin/env python3
"""
Card renderer for KBO in English (@kbo-english.bsky.social).

Renders the bot's five post types as monospace "ink on cream" PNG cards.
Headless Google Chrome does the type and image layout; Pillow crops the result
to content, so no height is ever guessed.

    render_results_card()    the daily final-scores digest
    render_box_score_card()  one finished game, with a traditional line score
    render_schedule_card()   tonight's fixtures and their start times
    render_starters_card()   tonight's probable starting pitchers
    render_leaders_card()    one leaderboard's leaders
    render_standings_card()  the league table, with the postseason cut line

Cards are rendered on a magenta sentinel background and cropped, so a 4-game day
and a 5-game day both come out tight. Corners are square on purpose: Bluesky
rounds image corners itself.

All five take plain dicts, not Naver API payloads — kbo_post owns the API and
the romanization, this module owns pixels only. See __main__ for the shapes.

Raises CardRenderError on any failure so the poster can fall back to plaintext.
"""

import base64
import html
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# Chrome does the layout. Look for it rather than hardcoding a macOS path, so
# the same renderer works on a Linux CI runner: KBO_CHROME wins if set, then
# anything on PATH under its various Linux names, then the standard macOS spot.
CHROME_ENV = 'KBO_CHROME'
CHROME_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
]
CHROME_ON_PATH = ['google-chrome', 'google-chrome-stable', 'chromium',
                  'chromium-browser', 'chrome']


def find_chrome():
    """Path to a usable Chrome/Chromium, or None. Result is not cached: an
    hour-long bot run has no reason to hold a stale answer."""
    explicit = os.environ.get(CHROME_ENV)
    if explicit:
        return explicit if Path(explicit).exists() else None
    for name in CHROME_ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    return next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)


SENTINEL = 'FF00FF'          # page background; cropped away. Never appears in art.
SENTINEL_RGB = (255, 0, 255)
CARD_WIDTH = 620             # CSS px; device-scale 2 renders at 1240 px.
RENDER_HEIGHT = 1400         # generous CSS height; cropped to content after.

CREAM = '#faf7f1'
INK = '#1c2b45'              # navy: winners, scores, headings
RED = '#c8323f'              # top bar, winning score, postseason line
MUTED = '#8a8578'            # losers, labels, dates, footer
RULE = '#ded8cc'             # hairline row separators

# IBM Plex Mono, vendored in fonts/ and embedded in each rendered page rather
# than read from the system, so a card looks identical wherever it is rendered —
# this Mac today, a Linux CI runner later. Plex is SIL OFL 1.1 (see
# fonts/LICENSE.txt), which is what makes bundling it legitimate; the macOS
# stock monospaces are not redistributable and could not travel with the repo.
# Monospace throughout so score columns line up by construction.
FONT_STACK = "'IBM Plex Mono', monospace"
FONT_DIR = Path(__file__).resolve().parent / 'fonts'
FONT_FILES = [('IBMPlexMono-Regular.otf', 400), ('IBMPlexMono-Bold.otf', 700)]
_FONT_FACE_CSS = None


def _font_face_css():
    """@font-face rules with the fonts inlined as data URIs, built once. Returns
    '' if the files are missing, so rendering falls back to whatever monospace
    the system has rather than failing outright."""
    global _FONT_FACE_CSS
    if _FONT_FACE_CSS is None:
        rules = []
        for name, weight in FONT_FILES:
            path = FONT_DIR / name
            try:
                blob = base64.b64encode(path.read_bytes()).decode('ascii')
            except OSError:
                continue
            rules.append(
                f"@font-face{{font-family:'IBM Plex Mono';font-weight:{weight};"
                f"font-style:normal;src:url(data:font/otf;base64,{blob})"
                f" format('opentype')}}")
        _FONT_FACE_CSS = ''.join(rules)
    return _FONT_FACE_CSS


# Team-mark sizes in CSS px, one per context. KBO's logos are wordmarks rather
# than simple icons, so they need more room than the emoji they replaced before
# they read as anything at all.
MARK_ROW = 28        # a fixture or result line
MARK_HEADLINE = 40   # the two team rows atop a box score
MARK_LINESCORE = 26  # the line score's row labels
MARK_HR = 24         # each team's home-run group
MARK_TABLE = 28      # standings and leaderboard rows


class CardRenderError(RuntimeError):
    """Rendering failed — caller should fall back to a plaintext post."""


def _esc(s):
    return html.escape(str(s) if s is not None else '', quote=True)


# --------------------------------------------------------------------------
# Rendering plumbing
# --------------------------------------------------------------------------

def _crop_to_content(raw_path, out_path):
    try:
        from PIL import Image, ImageChops
    except ImportError as e:
        raise CardRenderError(f'Pillow not available: {e}')
    with Image.open(raw_path) as im:
        im = im.convert('RGB')
        bg = Image.new('RGB', im.size, SENTINEL_RGB)
        bbox = ImageChops.difference(im, bg).getbbox()
        if not bbox:
            raise CardRenderError('rendered image was entirely background')
        cropped = im.crop(bbox)
        cropped.save(out_path)
        size = cropped.size
    return out_path, size


# Chrome draws a card in about three seconds. On 15 August 2026 six launches in
# a row sat silent past the then-only 60s ceiling on a loaded Mini, and all six
# of those posts went out without their cards — permanently, since a post cannot
# be re-illustrated after the fact. So an overrun is retried once at a longer
# ceiling: a spare minute costs a poll that has fifteen very little.
#
# RETRY_BUDGET caps the retrying per process, because the other failure mode is
# a night when nothing will render at all. Without it a five-card run would hold
# the lock for the better part of twenty minutes chasing cards it was never
# going to get; with it, the run stops retrying after the first few and finishes
# as text, which is where it was heading anyway.
RENDER_TIMEOUT = 60
RENDER_RETRY_TIMEOUT = 120
RETRY_BUDGET = 240

_retry_spent = 0.0


def _run_chrome(cmd, timeout):
    """Run Chrome, or None if it outlasted `timeout` and was killed."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


# Bluesky fits a lone image inside a square box and scales it to fit: 515 px
# on the web client, 290 on mobile, neither moving with the viewport width
# (measured at 375, 1280 and 1600 px on 21 August 2026). A landscape card hits
# the width cap and is drawn at the full 515. A card taller than it is wide
# hits the HEIGHT cap instead, and its width falls out proportionally — so it
# is drawn narrower than every other card in the feed.
FIT_BOX = 515


def _observe_portrait(label, text):
    """Record the finding in the shared estate log, so a card that has gone
    portrait reaches the Sunday review rather than only whoever reads a launchd
    log. The key carries the card's name, so two cards going portrait are two
    findings rather than one that looks like a repeat.

    Best-effort twice over: posting must never fail because note-taking did,
    and this repo must not require ~/Scripts to be present at all."""
    observe = Path.home() / 'Scripts' / 'observe.py'
    if not observe.exists():
        return
    key = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-') or 'card'
    try:
        subprocess.run(
            ['python3', str(observe), 'add', '--source', 'kbo-card',
             '--kind', 'finding', '--key', f'kbo-card-portrait-{key}', text],
            capture_output=True, text=True, check=False, timeout=20)
    except Exception:      # noqa: BLE001 — never let the notebook break a post
        pass


def _check_landscape(label, w, h):
    """Report a card that will be shown narrowed, and let it go out anyway.

    ⚠️ Deliberately not a CardRenderError. Raising here would drop the post to
    the plaintext fallback, trading the bot's entire look for a cosmetic loss
    of width — a far worse outcome than the thing being guarded against. The
    card ships; the condition becomes visible.

    It exists because this is invisible from the code and was both times found
    by eye. 8c10688 added two rows per pitcher and the starters card shipped at
    371 px for two days; the standings card had been portrait for as long as
    there had been ten clubs in it, and nobody had ever remarked on it."""
    if h <= w:
        return
    shown = round(FIT_BOX * w / h)
    text = (f'{label} card is portrait ({w}x{h}, ratio {w / h:.3f}): Bluesky '
            f'will draw it {shown}px wide, not {FIT_BOX}. Shorten it — '
            f'widening does not help, because a height-capped card is scaled '
            f'by {FIT_BOX}/height however wide it is.')
    print(f'  !! {text}')
    _observe_portrait(label, text)


def _shoot(doc, out_path, label='card'):
    """Render an HTML doc to a content-cropped PNG. Returns (out_path, (w, h)).
    Retries once on an overrun, within this process's RETRY_BUDGET.

    `label` names the card in the portrait warning and in its observation key."""
    global _retry_spent
    chrome = find_chrome()
    if not chrome:
        raise CardRenderError(
            'no Chrome or Chromium found (set KBO_CHROME to its path)')
    out_path = str(out_path)
    with tempfile.TemporaryDirectory() as td:
        html_path = Path(td) / 'card.html'
        raw_png = Path(td) / 'raw.png'
        html_path.write_text(doc, encoding='utf-8')
        cmd = [
            chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--force-device-scale-factor=2',
            f'--window-size={CARD_WIDTH},{RENDER_HEIGHT}',
            f'--default-background-color={SENTINEL}FF',
            f'--screenshot={raw_png}', f'file://{html_path}',
        ]
        r = _run_chrome(cmd, RENDER_TIMEOUT)
        if r is None:
            if _retry_spent >= RETRY_BUDGET:
                raise CardRenderError(
                    f'Chrome exceeded {RENDER_TIMEOUT}s, and this run has spent '
                    f'its {RETRY_BUDGET}s retry budget')
            print(f'  (card render passed {RENDER_TIMEOUT}s — retrying once at '
                  f'{RENDER_RETRY_TIMEOUT}s)')
            # The killed attempt can leave a half-written screenshot behind,
            # which would otherwise be cropped and posted as the card.
            raw_png.unlink(missing_ok=True)
            started = time.monotonic()
            r = _run_chrome(cmd, RENDER_RETRY_TIMEOUT)
            _retry_spent += time.monotonic() - started
            if r is None:
                raise CardRenderError(
                    f'Chrome timed out twice ({RENDER_TIMEOUT}s, then '
                    f'{RENDER_RETRY_TIMEOUT}s)')
        if not raw_png.exists():
            raise CardRenderError(
                f'Chrome produced no image (exit {r.returncode}): '
                f'{(r.stderr or r.stdout or "").strip()[:200]}')
        _, size = _crop_to_content(raw_png, out_path)
    _check_landscape(label, *size)
    return out_path, size


BASE_CSS = f"""
html,body{{margin:0;background:#{SENTINEL}}}
.card{{width:{CARD_WIDTH}px;box-sizing:border-box;background:{CREAM};color:{INK};
  border-top:5px solid {RED};padding:26px 30px 18px;font-family:{FONT_STACK}}}
.top{{display:flex;align-items:baseline;justify-content:space-between}}
.top .t{{font-size:19px;font-weight:700}}
.top .d{{font-size:13px;color:{MUTED}}}
.sub{{margin-top:6px;font-size:12px;color:{MUTED}}}
.hr{{border-bottom:2px solid {INK};margin:14px 0 0}}
.foot{{margin-top:14px;padding-top:12px;border-top:1px solid {RULE};
  font-size:11px;color:{MUTED};letter-spacing:0.06em}}
/* Club logos center on the text's middle rather than sitting on its baseline:
   they are much taller than the type, so a baseline leaves them riding high.
   Unlike a fixed em offset, this holds as the MARK_* sizes change. */
img.lg{{vertical-align:middle;object-fit:contain}}
"""

FOOTER = '<div class="foot">@kbo-english.bsky.social</div>'


def _document(css, body):
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>'
            f'{_font_face_css()}{BASE_CSS}{css}</style></head><body>{body}'
            f'</body></html>')


def _mark(item, prefix, size):
    """A team's mark at `size` px: its logo if the caller supplied one, else its
    emoji. Callers pass a dict and a field prefix ('away' -> away_logo /
    away_emoji), so a card renders logos or emoji without knowing which."""
    logo = item.get(f'{prefix}_logo')
    if logo:
        return (f'<img class="lg" src="{_esc(logo)}" '
                f'style="height:{size}px;width:{size}px">')
    return _esc(item.get(f'{prefix}_emoji') or '')


def _head(title, date_label, emoji='🇰🇷 ⚾', subtitle=''):
    sub = f'<div class="sub">{_esc(subtitle)}</div>' if subtitle else ''
    return (f'<div class="top"><div class="t">{_esc(emoji)} {_esc(title)}</div>'
            f'<div class="d">{_esc(date_label)}</div></div>{sub}'
            f'<div class="hr"></div>')


# --------------------------------------------------------------------------
# Final scores digest
# --------------------------------------------------------------------------

# The winner takes the left column; both clubs are set in bold and the score is
# picked out in red. Each club's pitcher decisions sit beneath its name, one per
# line (winner's W then any save, loser's L).
RESULTS_CSS = f"""
.g{{padding:16px 0 14px}}
.g + .g{{border-top:1px solid {RULE}}}
.row{{display:grid;grid-template-columns:1fr auto 1fr;align-items:baseline;
  column-gap:16px}}
.row .l{{text-align:right}}
.row .r{{text-align:left}}
.nm{{font-size:17px;font-weight:700}}
.s{{font-size:17px;white-space:nowrap;letter-spacing:0.04em}}
.s b{{color:{RED};font-weight:700}}
.s .dot{{color:{RED}}}
.pit{{font-size:12px;color:{MUTED};margin-top:5px;line-height:1.5}}
.pit b{{color:{INK};font-weight:700}}
.row.ppd .s{{font-size:13px;color:{MUTED}}}
.note{{margin-top:9px;text-align:center;font-size:12px;color:{MUTED};
  letter-spacing:0.04em}}
"""


def _decisions(pitchers):
    """A club's pitcher decisions, one per line: 'W Yang (8-4)', then a second
    line for a save. Only the surname is bold, matching the box score's pitcher
    row."""
    return ''.join(
        f'<div>{_esc(code)} <b>{_esc(name)}</b> ({_esc(detail)})</div>'
        for code, name, detail in pitchers)


def _result_side(g, side, cls):
    """One club's cell: its mark and name, with its pitcher decisions beneath."""
    nm = (f'<div class="nm">{_mark(g, side, MARK_ROW)} '
          f'{_esc(g[f"{side}_name"])}</div>')
    pitchers = g.get(f'{side}_pitchers') or []
    pit = f'<div class="pit">{_decisions(pitchers)}</div>' if pitchers else ''
    return f'<div class="{cls}">{nm}{pit}</div>'


def _game_block(g):
    winner = g.get('winner')                     # 'away' | 'home' | None (tie)
    left, right = ('home', 'away') if winner == 'home' else ('away', 'home')
    left_html = _result_side(g, left, 'l')
    right_html = _result_side(g, right, 'r')
    ls, rs = g[f'{left}_score'], g[f'{right}_score']
    score = (f'<span class="s"><b>{ls}</b> '
             f'<span class="dot">&bull;</span> <b>{rs}</b></span>')
    note = (f'<div class="note">{_esc(g["note"])}</div>' if g.get('note') else '')
    return (f'<div class="g"><div class="row">{left_html}{score}{right_html}</div>'
            f'{note}</div>')


def _postponed_block(g):
    """A rained-out fixture: 'postponed' sits where the score would, and the
    clubs keep their away–home order since there is no winner to lead with."""
    away = _result_side(g, 'away', 'l')
    home = _result_side(g, 'home', 'r')
    mid = '<span class="s">postponed</span>'
    return f'<div class="g"><div class="row ppd">{away}{mid}{home}</div></div>'


def render_results_card(date_label, games, out_path, title=None,
                        postponed=()):
    """The daily digest. `games` is a list of dicts:
        {away_emoji, away_name, away_score, home_emoji, home_name, home_score,
         note}  — note is a short tag ('rout', 'shutout') or '' for none.
    `postponed` lists rained-out games in the same shape minus the scores; they
    render after the finals, so an all-rainout day still makes a card.

    `title` defaults to 'Final Scores', or to 'Postponed' when `games` is
    empty: a slate that produced no finals at all has nothing to call a
    final score, and heading an all-rainout card 'Final Scores' above a page
    of rows that each just say 'postponed' reads as a mistake rather than a
    rainout.
    Returns (path, (w, h))."""
    if not games and not postponed:
        raise CardRenderError('no games to render')
    if title is None:
        title = 'Final Scores' if games else 'Postponed'
    body = (f'<div class="card">{_head(title, date_label)}'
            f'{"".join(_game_block(g) for g in games)}'
            f'{"".join(_postponed_block(g) for g in postponed)}{FOOTER}</div>')
    return _shoot(_document(RESULTS_CSS, body), out_path, label=title)


# --------------------------------------------------------------------------
# Tonight's games
# --------------------------------------------------------------------------

SCHEDULE_CSS = f"""
.g{{padding:16px 0 14px}}
.g + .g{{border-top:1px solid {RULE}}}
.row{{display:grid;grid-template-columns:1fr auto 1fr;align-items:baseline;
  column-gap:16px;font-size:17px}}
.row .a{{text-align:right;font-weight:700}}
.row .h{{text-align:left;font-weight:700}}
.row .mid{{color:{MUTED};white-space:nowrap;font-size:13px}}
"""


def _fixture_block(g):
    away = (f'<span class="a">{_mark(g, "away", MARK_ROW)} '
            f'{_esc(g["away_name"])}</span>')
    home = (f'<span class="h">{_mark(g, "home", MARK_ROW)} '
            f'{_esc(g["home_name"])}</span>')
    mid = f'<span class="mid">{_esc(g.get("time") or "@")}</span>'
    return f'<div class="g"><div class="row">{away}{mid}{home}</div></div>'


def render_schedule_card(date_label, games, out_path, title='Today’s Games',
                         subtitle=''):
    """Tonight's fixtures. `games` is a list of dicts:
        {away_emoji/away_logo, away_name, home_..., time}
    `time` is '6:30 p.m.' per fixture, or '' when every game starts together and
    the caller has put the time in `subtitle` instead. The probable starters are
    render_starters_card's job, in its own post. Returns (path, (w, h))."""
    if not games:
        raise CardRenderError('no fixtures to render')
    body = (f'<div class="card">{_head(title, date_label, subtitle=subtitle)}'
            f'{"".join(_fixture_block(g) for g in games)}{FOOTER}</div>')
    return _shoot(_document(SCHEDULE_CSS, body), out_path, label=title)


# --------------------------------------------------------------------------
# Probable starters
# --------------------------------------------------------------------------

# Mirrored columns, echoing the fixtures card's away/home split: each side
# aligns to the gutter, so the two records meet in the middle and the eye can
# compare them without crossing the card. The gutter holds '@', not a middot:
# this card travels in its own post now, so nothing above it says which pitcher
# is the visitor and column position alone gives the reader no key.
# One mirrored column: the card's content width less the two grid gaps and the
# '@' between them. The pitch-mix row is measured against this.
STARTER_COL_WIDTH = (CARD_WIDTH - 60 - 14 * 2 - 8) // 2

STARTERS_CSS = f"""
.gm{{padding:15px 0 13px}}
.gm + .gm{{border-top:1px solid {RULE}}}
.duo{{display:grid;grid-template-columns:1fr auto 1fr;align-items:start;
  column-gap:14px}}
.duo .a{{text-align:right}}
.duo .h{{text-align:left}}
.duo .sep{{color:{MUTED};font-size:13px;align-self:start;line-height:19px;
  margin-top:{MARK_ROW + 5}px}}
.duo .nm{{font-size:16px;font-weight:700;margin-top:5px;line-height:19px}}
.duo .rec{{font-size:12px;color:{MUTED};margin-top:3px;letter-spacing:0.04em}}
.duo .tbd{{font-weight:400;color:{MUTED}}}
/* The opponent line leads, in red: a starter's record against tonight's club
   is the number that makes the fixture, and it is routinely nothing like his
   season line. Season W-L then sits under it as the baseline to read it
   against. */
.duo .vs{{font-size:12px;color:{RED};margin-top:5px;letter-spacing:0.02em;
  white-space:nowrap}}
.duo .mix{{color:{INK};margin-top:6px;letter-spacing:0.02em;white-space:nowrap}}
"""


def _starter_col(g, side, cls):
    """One side of a fixture: the club's mark above its probable starter and
    that pitcher's season line. A starter who hasn't been named shows TBD, set
    in regular weight so it reads as an absence rather than a surname."""
    name = g.get(f'{side}_pitcher') or 'TBD'
    nm_cls = 'nm' if g.get(f'{side}_pitcher') else 'nm tbd'
    rec = g.get(f'{side}_record') or ''
    vs = g.get(f'{side}_vs') or ''
    mix = g.get(f'{side}_mix') or ''
    rows = ''
    if vs:
        rows += f'<div class="vs">{_esc(vs)}</div>'
    rows += f'<div class="rec">{_esc(rec)}</div>'
    if mix:
        # Spelled-out pitch names can outrun the column: 'Fastball 40%
        # Curveball 17% Two-seam 15%' is 41 characters against 40 of room, and
        # that line has genuinely gone out. Shrink rather than clip or wrap.
        size = _fit_size(mix, base=11, floor=9, width=STARTER_COL_WIDTH)
        rows += f'<div class="mix" style="font-size:{size}px">{_esc(mix)}</div>'
    return (f'<div class="{cls}">{_mark(g, side, MARK_ROW)}'
            f'<div class="{nm_cls}">{_esc(name)}</div>{rows}</div>')


def render_starters_card(date_label, games, out_path,
                         title='Probable Starters',
                         subtitle='Record against tonight’s opponent, '
                                  'season W-L and E.R.A., pitch mix'):
    """The probable-starters post. `games` is a list of dicts:
        {away_emoji/away_logo, away_pitcher, away_record, home_...}
    `record` is '2-4, 4.69', or '' when the API has no season line for him yet —
    the card sets name and record in separate rows, so the brackets the text
    post uses would only be noise here. Returns (path, (w, h))."""
    if not games:
        raise CardRenderError('no starters to render')
    blocks = ''.join(
        f'<div class="gm"><div class="duo">'
        f'{_starter_col(g, "away", "a")}'
        f'<span class="sep">@</span>'
        f'{_starter_col(g, "home", "h")}</div></div>' for g in games)
    body = (f'<div class="card">{_head(title, date_label, subtitle=subtitle)}'
            f'{blocks}{FOOTER}</div>')
    return _shoot(_document(STARTERS_CSS, body), out_path, label=title)


# --------------------------------------------------------------------------
# Box score
# --------------------------------------------------------------------------

# Label gutter, sized to the longest label ('Home runs', 9 chars at 11px with
# 0.06em tracking, plus its 10px right padding). Value column takes what is left
# of the card after its 30px side padding. Plex's advance is exactly 0.6em
# (measured), so a monospace line's width is char_count * 0.6 * font_size and
# the fit below is arithmetic, not a guess. Menlo, which this replaced, was
# 0.6021, so the constant very slightly under-read it.
KV_LABEL_WIDTH = 80
KV_VALUE_WIDTH = CARD_WIDTH - 60 - KV_LABEL_WIDTH
MONO_ADVANCE = 0.6


BOX_CSS = f"""
/* Center the mark against the club name rather than sitting it on the text
   baseline: the logos are far taller than the type, so a baseline puts them
   visibly high. Flex centring holds at any MARK_HEADLINE size. */
.tm{{display:flex;align-items:center;justify-content:space-between;
  margin:18px 0}}
.tm .n{{display:flex;align-items:center;gap:16px;font-size:22px;
  font-weight:700}}
.tm .sc{{font-size:26px;font-weight:700}}
.hr2{{border-bottom:2px solid {INK};margin:4px 0 0}}
table.ls{{width:100%;border-collapse:collapse;margin:16px 0 4px;
  font-size:13px;table-layout:fixed}}
table.ls th,table.ls td{{text-align:right;padding:5px 0;font-weight:400}}
table.ls th{{color:{MUTED};font-size:11px;border-bottom:1px solid {RULE}}}
table.ls td.lab,table.ls th.lab{{text-align:left;width:40px}}
table.ls td.t,table.ls th.t{{font-weight:700;width:34px}}
table.ls .t.first{{border-left:1px solid {RULE};padding-left:10px}}
table.ls .li{{padding-right:10px}}   /* keep the last inning off the divider */
table.ls tr.r td{{border-bottom:1px solid {RULE}}}
.kv{{display:flex;align-items:flex-start;padding:11px 0;font-size:14px}}
.kv + .kv{{border-top:1px solid {RULE}}}
.kv .k{{color:{MUTED};font-size:11px;letter-spacing:0.06em;
  width:{KV_LABEL_WIDTH}px;box-sizing:border-box;padding:3px 10px 0 0;
  flex:none}}
.kv .v{{line-height:1.5}}
.kv .sep{{color:{RED}}}
/* Home runs list one team per line, the way a box score prints them. */
.hrg + .hrg{{margin-top:5px}}
"""


def _line_score_table(line):
    """A traditional line score: runs by inning, then R/H/E. `line` is
        {away_emoji, home_emoji, away_inn:[...], home_inn:[...],
         away_rhe:(r,h,e), home_rhe:(r,h,e)}
    A home side that didn't bat in the last inning has a short list, and gets
    the conventional X in that cell."""
    innings = max(len(line['away_inn']), len(line['home_inn']))
    last = f' class="li"'                 # last inning column: gets right padding
    head = ''.join(f'<th{last if i == innings else ""}>{i}</th>'
                   for i in range(1, innings + 1))
    head = (f'<tr><th class="lab"></th>{head}'
            f'<th class="t first">R</th><th class="t">H</th>'
            f'<th class="t">E</th></tr>')

    rows = ''
    for side in ('away', 'home'):
        cells = ''
        for i in range(innings):
            got = line[f'{side}_inn']
            # Short home list = didn't bat (walk-off or home win); print X.
            val = got[i] if i < len(got) else ('X' if side == 'home' else '')
            cells += f'<td{last if i == innings - 1 else ""}>{_esc(val)}</td>'
        r, h, e = line[f'{side}_rhe']
        rows += (f'<tr class="r"><td class="lab">{_mark(line, side, MARK_LINESCORE)}</td>'
                 f'{cells}<td class="t first">{r}</td><td class="t">{h}</td>'
                 f'<td class="t">{e}</td></tr>')
    return f'<table class="ls">{head}{rows}</table>'


def _kv(key, value, style=''):
    style = f' style="{style}"' if style else ''
    return (f'<div class="kv"><div class="k">{_esc(key)}</div>'
            f'<div class="v"{style}>{value}</div></div>')


def _fit_size(text, base=14, floor=11, width=KV_VALUE_WIDTH):
    """Largest whole-px size in [floor, base] that keeps `text` on one line."""
    if not text:
        return base
    for size in range(base, floor - 1, -1):
        if len(text) * MONO_ADVANCE * size <= width:
            return size
    return floor


def render_box_score_card(date_label, game, out_path, title='Final'):
    """One finished game. `game` is a dict:
        {away_emoji, away_name, away_score, home_emoji, home_name, home_score,
         line:   {...} for the line score (see _line_score_table), or None
         pitchers: [('W', 'Takada', '1-1'), ('S', 'Lee Young Ha', '14'), ...]
                   — only the name is bold; the code, the record and the red
                   separators between entries are all regular weight
         hr:     [{emoji/logo per team, 'names': 'Park Chan Ho, An Jae Seok (2)'}]
         extra:  optional [(label, value)] rows appended after HR}
    Returns (path, (w, h))."""
    away = (f'<div class="tm"><div class="n">{_mark(game, "away", MARK_HEADLINE)}'
            f'{_esc(game["away_name"])}</div>'
            f'<div class="sc">{game["away_score"]}</div></div>')
    home = (f'<div class="tm"><div class="n">{_mark(game, "home", MARK_HEADLINE)}'
            f'{_esc(game["home_name"])}</div>'
            f'<div class="sc">{game["home_score"]}</div></div>')

    parts = [_head(title, date_label), away, home, '<div class="hr2"></div>']
    if game.get('line'):
        parts.append(_line_score_table(game['line']))
    if game.get('pitchers'):
        # All three decisions share one row, shrinking a little if the names are
        # long, rather than wrapping a lone '(14)' onto a second line. Bold and
        # regular Plex share an advance width, so the fit maths is unaffected
        # by emboldening the W/L/S codes.
        pitchers = game['pitchers']
        plain = ' · '.join(f'{code} {name} ({detail})'
                           for code, name, detail in pitchers)
        marked = '<span class="sep"> · </span>'.join(
            f'{_esc(code)} <b>{_esc(name)}</b> ({_esc(detail)})'
            for code, name, detail in pitchers)
        parts.append(_kv('Pitchers', marked,
                         f'font-size:{_fit_size(plain)}px;white-space:nowrap'))
    if game.get('hr'):
        groups = ''.join(f'<div class="hrg">{_mark(g, "team", MARK_HR)} '
                         f'{_esc(g["names"])}</div>' for g in game['hr'])
        parts.append(_kv('Home runs', groups))
    for label, value in game.get('extra') or ():
        parts.append(_kv(label, _esc(value)))
    parts.append(FOOTER)
    return _shoot(_document(BOX_CSS, f'<div class="card">{"".join(parts)}</div>'),
                  out_path, label='Box score')


# --------------------------------------------------------------------------
# Standings
# --------------------------------------------------------------------------

# ⚠️ This card is deliberately tighter than the others, and the tightening is
# load-bearing rather than taste. Bluesky fits a lone image inside a square box
# (515 px on the web, 290 on mobile), so a portrait card is scaled down by its
# HEIGHT and loses width: at the old spacing the ten-club table rendered
# 1240x1408 and the client drew it 454 px wide against every other card's 515.
# See starters_chunks in kbo_card_data.py for the full measurement.
#
# The table cannot be split the way the starters card is — cutting a league
# table at rank 5 severs the comparison that makes it a table, and that is
# exactly where the postseason line already falls — so the height comes out of
# the card instead. The row padding and the chrome below take it to 1240x1178,
# ratio 1.053 with the cutline drawn and 1.098 without, so both daily variants
# clear it. Being landscape is also what makes the type BIGGER on screen, not
# smaller: a height-capped card is scaled by 515/height, so at the old spacing
# the 16px table set 14% smaller than it does now.
#
# Anything that adds a row or loosens this spacing needs re-measuring: the
# margin is 5%, and the club count is fixed at ten so nothing else absorbs it.
STANDINGS_CSS = f"""
.card{{padding:20px 30px 14px}}
.hr{{margin-top:10px}}
.foot{{margin-top:10px;padding-top:9px}}
table.st{{width:100%;border-collapse:collapse;margin-top:0;font-size:16px}}
table.st th{{color:{MUTED};font-size:11px;font-weight:400;letter-spacing:0.06em;
  text-align:right;padding:6px 0 4px}}
table.st td{{padding:7px 0;border-bottom:1px solid {RULE}}}
table.st tr:last-child td{{border-bottom:0}}
table.st td.rk{{color:{MUTED};font-size:12px;width:26px}}
table.st td.tm{{font-weight:700}}
table.st td.wl{{text-align:right;font-weight:700;white-space:nowrap}}
table.st td.gb{{text-align:right;color:{MUTED};width:64px}}
tr.cut td{{border-bottom:0;padding:0}}
.cutline{{display:flex;align-items:center;gap:10px;color:{RED};font-size:11px;
  letter-spacing:0.1em;padding:5px 0}}
.cutline::before,.cutline::after{{content:"";flex:1;
  border-bottom:1px dashed {RED}}}
"""


LEADERS_CSS = f"""
table.ld{{width:100%;border-collapse:collapse;margin-top:6px;font-size:18px}}
table.ld td{{padding:16px 0;border-bottom:1px solid {RULE}}}
table.ld tr:last-child td{{border-bottom:0}}
table.ld td.rk{{color:{MUTED};font-size:12px;width:26px}}
table.ld td.nm{{font-weight:700}}
table.ld td.vl{{text-align:right;font-weight:700;white-space:nowrap;
  font-size:22px}}
"""


def render_leaders_card(date_label, title, rows, out_path,
                        subtitle='Season leaders'):
    """One leaderboard: a stat's leaders, as many rows as it is given. `rows`
    is a list of dicts:
        {rank, team_emoji/team_logo, name, value}
    Ranks come from the API as-is, so a three-way tie prints 1, 1, 1 rather
    than being renumbered. Returns (path, (w, h))."""
    if not rows:
        raise CardRenderError('no leaderboard rows to render')
    body = ''
    for r in rows:
        body += (f'<tr><td class="rk">{_esc(r["rank"])}</td>'
                 f'<td class="nm">{_mark(r, "team", MARK_TABLE)} {_esc(r["name"])}</td>'
                 f'<td class="vl">{_esc(r["value"])}</td></tr>')
    card = (f'<div class="card">'
            f'{_head(title, date_label, subtitle=subtitle)}'
            f'<table class="ld">{body}</table>{FOOTER}</div>')
    return _shoot(_document(LEADERS_CSS, card), out_path, label=title)


def render_standings_card(date_label, rows, out_path, cut_after=5,
                          title='Standings'):
    """The league table. `rows` is a list of dicts in standings order:
        {emoji, name, w, l, gb}  — gb is '' for the leader, else '2.5'.
    A dashed POSTSEASON LINE is drawn after `cut_after` rows (None for none).
    Returns (path, (w, h))."""
    if not rows:
        raise CardRenderError('no standings rows to render')
    body = ('<tr><th class="rk"></th><th></th><th class="wl">W&ndash;L</th>'
            '<th class="gb">GB</th></tr>')
    for i, r in enumerate(rows, start=1):
        body += (f'<tr><td class="rk">{i}</td>'
                 f'<td class="tm">{_mark(r, "team", MARK_TABLE)} {_esc(r["name"])}</td>'
                 f'<td class="wl">{r["w"]}&ndash;{r["l"]}</td>'
                 f'<td class="gb">{_esc(r.get("gb", ""))}</td></tr>')
        if cut_after and i == cut_after and i < len(rows):
            body += ('<tr class="cut"><td colspan="4">'
                     '<div class="cutline">POSTSEASON LINE</div></td></tr>')
    card = (f'<div class="card">{_head(title, date_label)}'
            f'<table class="st">{body}</table>{FOOTER}</div>')
    return _shoot(_document(STANDINGS_CSS, card), out_path, label=title)
