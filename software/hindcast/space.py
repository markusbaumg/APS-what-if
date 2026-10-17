"""
space.py  -  the searched parameter space of a Hindcast study (backlog H-303; DESIGN.md sections 3, 4.5, 5.4)

A space is `{'<array> <item>': contracts.SpaceParam}` plus ordering constraints such as
`['profile smb_delivery_ratio_min', '<=', 'profile smb_delivery_ratio', '<=', 'profile smb_delivery_ratio_max']`.
This module turns it into points (step grid, one-at-a-time levels, unit-cube decoding for a sampler), into VDF
text the emulator replays (through `emulator_optimize.write_vdf`, never a copy of it), and into the variant
labels `emulator_optimize.variant_name` gives, so a Hindcast variant and an optimizer variant of the same
assignment carry the same name and the same vhash.

Parameter types (contracts.SpaceParam): `float` and `int` are searched on the step grid low..high (the phone's
preference screen cannot set finer values anyway), `bool` is {False, True}, `choice` an explicit list. Every
point a sampler produces is snapped to the grid, so the same point always gives the same VDF row and vhash.

`scale` (tier C) is a multiplier of the value the profile carries in each loop, for the per-loop profile values
Nightscout carries from a time-varying schedule (contracts.SCALE_KEYS: sens, carb_ratio, current_basal,
profile_percentage; only these, and these only as a scale). Space file entry:

    "profile sens": {"type": "scale", "low": 0.7, "high": 1.4, "step": 0.05, "phone": 1.0, "target": "profile sens"}

The grid is searched like a float grid (snap, oaat, decode, encode; canonical_params / vhash take the multiplier
like any float, so `{'profile sens': 1.1}` hashes as `profile sens 1.1`). The VDF row is a Python expression the
emulator evaluates per loop on the freshly parsed profile (`scale_expression`):

    profile      sens          profile['sens']*1.1
    profile      current_basal round(profile['current_basal']*1.1/0.05)*0.05      (pump basal step, PUMP_BASAL_STEP)

The phone value of a scale is 1.0 (`phone_value` returns contracts.SCALE_PHONE for every scale key), and a scale
at 1.0 writes NO row: the replay then reads the profile exactly as base does. That is how the sentinel and null
controls express "the phone's value" for a scale - their rows for scale keys are empty, so for a scale they check
the VDF-free path only (the vhash still carries the 1.0: a control is its own variant). Caveats the tier C file
repeats per entry: only the decision reads the scaled value; ns_profile derived max_basal from the unscaled
basal, COB/IOB/deviations come from the recorded history, so a scale study is weak evidence (contracts.WEAK_EVIDENCE).

Ordering constraints are chains `k1 <= k2 <= k3` (or `<`) over float/int parameters. A sampler never sees them:
`ParamSpace.decode(u)` takes a point of the unit cube (one 0..1 coordinate per parameter), samples the chain's
centre directly and the outer members as half-widths from the centre ("ratio + half-widths", ShadowLoop section 4),
snapping towards the centre, so every decoded point satisfies the chain. `satisfies()` re-checks any point.

LOCKED KEYS (DESIGN.md section 3) are enforced here: `refuse_locked()` raises `LockedKeyError` naming every locked
parameter; the CLI maps it to exit code 3. The list lives in `contracts.locked_reason()` and is extended by the
`safety` section of a loaded ns_settings.json; this module only applies it. The lock is the default. The only way
past it is an explicit, per-study, audited unlock: `refuse_locked(names, settings, unlocked)` and
`ParamSpace(..., unlocked=[...], unlock_reason='...')` honour a list of '<array> <item>' keys, ParamSpace refuses a
non-empty list without a non-empty reason (SpaceError), and contracts.unlock_refusal() keeps new_parameter keys,
non-profile groups and `profile dia` locked whatever the list says. The list and the reason are stored in
study.json (Study.unlocked / Study.unlock_reason), and Study.unlock_caveat() is the weak-evidence text.

CLI wiring expected from the cli.py / orchestrator owner (not done here):

    hc study new <name> ... --unlock KEY [KEY ...] --unlock-reason TEXT

  * `--unlock` takes one or more '<array> <item>' keys (quoted, e.g. --unlock "profile sens" "profile max_iob");
    repeatable, the union is used, order kept, duplicates dropped. `--unlock-reason` is required whenever
    `--unlock` is given (argparse error / exit 3 otherwise) and must be non-empty after stripping.
  * They go to `ParamSpace(space, constraints, settings=settings, unlocked=keys, unlock_reason=text)`, to
    `contracts.locked_vdf_row_reason(line, settings, keys)` for the base VDF rows, and into the study as
    `Study.new(..., unlocked=keys, unlock_reason=text)`; Study.validate() refuses an empty reason.
  * Without `--unlock` nothing changes: a locked key (also from `--tier C` / `--tier S`, whose files carry
    `"_requires_unlock": true`) raises LockedKeyError -> exit 3, and the message names the keys and the flags.
  * `hc study new` prints `space_file_notice(meta)` for a space file it loaded (the `_evidence` line) and
    `study.unlock_caveat()` after creating an unlocked study; the page and the cards quote the caveat too.
  * Keys listed in `--unlock` that are not locked anyway are accepted and stay in the audit list (harmless).

Default spaces: `spaces/autoisf_tierA.json` (tier A of ShadowLoop section 4: the autoISF weights, caps and the
SMB delivery ratio family), `spaces/autoisf_tierB.json` (second pass), `spaces/profile_tierC.json` (scales of
profile ISF, CR, basal and the profile percentage; needs an unlock except for profile_percentage, weak evidence)
and `spaces/safety_tierS.json` (the caps max_iob / max_basal; needs an unlock; a cap study only reports how
often the cap binds). Each `phone` value is the phone's
current setting from ns_settings.json; `load_space_file(..., settings=...)` refreshes them and
`test_space.py` fails when the committed file and ns_settings.json disagree, so the files are re-centred by
`python -m hindcast.space --write-defaults` after a preferences import.

Nothing here changes the phone or ns_settings.json; a space says what a replay may vary, not what anyone
should set.
"""
import ast
import itertools
import json
import math
import os
import re
import sys
import tempfile

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

import emulator_optimize as _opt                                                           # noqa: E402
from hindcast.contracts import (ContractError, SpaceParam, Phase, NS_SETTINGS_PATH, PARAM_TYPES, VDF_GROUPS,  # noqa: E402
                                SCALE_KEYS, SCALE_PHONE, WEAK_EVIDENCE, is_scale_key, scale_problems,
                                locked_reason, split_param, vhash, canonical_params)

SPACES_DIR = os.path.join(HINDCAST_DIR, 'spaces')
DEFAULT_SPACE_FILES = {'A': os.path.join(SPACES_DIR, 'autoisf_tierA.json'),
                       'B': os.path.join(SPACES_DIR, 'autoisf_tierB.json'),
                       'C': os.path.join(SPACES_DIR, 'profile_tierC.json'),
                       'S': os.path.join(SPACES_DIR, 'safety_tierS.json')}
TIER_FILE_NAMES = {'A': 'autoisf_tierA.json', 'B': 'autoisf_tierB.json', 'C': 'profile_tierC.json',
                   'S': 'safety_tierS.json'}
PUMP_BASAL_STEP = 0.05                                   # ns_settings.json safety.basal_step; ns_profile rounds current_basal to it
SETTINGS_SECTIONS = ('autoisf', 'smb', 'activity_monitor', 'flags', 'safety', 'insulin', 'glucose')
OPS = ('<=', '<')
VDF_LABEL = 'hindcast'


class SpaceError(ContractError):
    """The space definition is unusable (bad range, unknown group, constraint over an unknown key...)."""


class LockedKeyError(SpaceError):
    """A locked key (DESIGN.md section 3) was asked for; `.keys` names them. The only way past it is an explicit,
    audited per-study unlock (study.json `unlocked` + `unlock_reason`; `hc study new --unlock KEY --unlock-reason
    TEXT`), and some keys can never be unlocked (contracts.unlock_refusal)."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        self.keys = [r.split('"')[1] for r in self.reasons if '"' in r]
        super().__init__('refused, locked for unattended runs (DESIGN.md section 3):\n  ' + '\n  '.join(self.reasons)
                         + '\n(a study may unlock a key explicitly: --unlock KEY --unlock-reason TEXT; '
                         'a decision-level replay is weak evidence for such keys)')


# ----------------------------------------------------------------------------- the locked list, applied

def locked_reasons(names, settings=None, unlocked=()):
    """[reason, ...] for every name in `names` that may not be searched (empty = all allowed); `unlocked` lifts
    the lock for the keys a study unlocked explicitly (contracts.locked_reason)."""
    out = []
    for name in names:
        reason = locked_reason(name, settings, unlocked)
        if reason:
            out.append(reason)
    return out


def refuse_locked(names, settings=None, unlocked=()):
    """Raise LockedKeyError naming every locked key not in `unlocked`; return the names unchanged otherwise. The
    reason of an unlock is checked by ParamSpace / Study, which hold it."""
    reasons = locked_reasons(names, settings, unlocked)
    if reasons:
        raise LockedKeyError(reasons)
    return list(names)


def load_settings(path=NS_SETTINGS_PATH):
    """ns_settings.json as a dict, or {} when missing/unreadable (the locked list then has no live extension)."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# ----------------------------------------------------------------------------- the phone's values

def phone_value(name, settings, at_ms=None):
    """The phone's current value of `<array> <item>` from the settings sections ns_profile copies 1:1 into the
    profile (autoisf, smb, activity_monitor, flags, safety, insulin, glucose), or None when Nightscout carries it
    (basal, sens, targets...) or the key is unknown. With `at_ms` the value is rewound through
    `profile_history.changes` the way ns_profile.ProfileBuilder._apply_history does for a loop at that time.
    A scale key (contracts.SCALE_KEYS) is a multiplier of the recorded value: its phone value is always 1.0."""
    try:
        _, item = split_param(name)
    except ContractError:
        return None
    if is_scale_key(name):
        return SCALE_PHONE
    value, found = None, False
    for section in SETTINGS_SECTIONS:
        sec = settings.get(section) if isinstance(settings, dict) else None
        if isinstance(sec, dict) and item in sec:
            value, found = sec[item], True
            break
    if not found:
        return None
    if at_ms is not None:
        try:
            from ns_profile import ProfileBuilder
            history = [h for h in ProfileBuilder._parse_history(settings.get('profile_history', {})) if h[1] == item]
        except Exception:                                 # pragma: no cover - ns_profile unavailable
            history = []
        for when, _, old, _ in history:                   # ascending: the earliest future change wins
            if when > at_ms:
                return old
    return value


def phone_values(names, settings, at_ms=None):
    return {name: phone_value(name, settings, at_ms) for name in names}


# ----------------------------------------------------------------------------- parameter specs

_RANGE_RE = re.compile(r'^\s*([-+]?[0-9.]+)\s*:\s*([-+]?[0-9.]+)\s*:\s*([-+]?[0-9.]+)\s*$')


def _num(text):
    t = text.strip()
    try:
        return int(t)
    except ValueError:
        return float(t)


def parse_param_spec(text):
    """Command line spec -> SpaceParam.

        1.1:1.8:0.05      float grid low:high:step        50:100:5   int grid (all three integers)
        bool              {False, True}                   1,2,3,5    choice (comma list of literals)
        true,false        choice of booleans is read as bool
    """
    t = str(text).strip()
    if t.lower() in ('bool', 'boolean', 'true,false', 'false,true'):
        return SpaceParam(type='bool')
    m = _RANGE_RE.match(t)
    if m:
        low, high, step = (_num(g) for g in m.groups())
        kind = 'int' if all(isinstance(v, int) for v in (low, high, step)) else 'float'
        return SpaceParam(type=kind, low=float(low), high=float(high), step=float(step))
    if ',' in t:
        return SpaceParam(type='choice', choices=[_opt.parse_literal(v) for v in t.split(',') if v.strip()])
    raise SpaceError(f'parameter spec "{text}": expected low:high:step, bool or a comma list of values')


def space_from_grid(grid_params):
    """emulator_optimize grid parameters {name: [values]} -> {name: SpaceParam(type='choice')} (what --from-grid imports)."""
    out = {}
    for name, values in grid_params.items():
        split_param(name)
        if not isinstance(values, list) or not values:
            raise SpaceError(f'grid parameter "{name}" needs a non-empty list of values')
        if all(isinstance(v, bool) for v in values) and set(values) == {False, True}:
            out[name] = SpaceParam(type='bool')
        else:
            out[name] = SpaceParam(type='choice', choices=list(values))
    return out


def load_grid_file(path):
    """variants/grid_autoISF.json -> (space, Phase(kind='grid', grid=params), mode)."""
    with open(path, 'r', encoding='utf-8') as fh:
        grid = json.load(fh)
    params = grid.get('parameters') or {}
    if not params:
        raise SpaceError(f'{path}: no "parameters"')
    mode = grid.get('mode', 'grid')
    if mode not in ('grid', 'one_at_a_time'):
        raise SpaceError(f'{path}: unknown mode "{mode}" (grid | one_at_a_time)')
    space = space_from_grid(params)
    return space, Phase(kind='grid' if mode == 'grid' else 'oaat', grid=dict(params)), mode


def load_space_file(path, settings=None):
    """spaces/*.json -> (space dict, constraints list, meta dict). With `settings` the `phone` values are refreshed."""
    with open(path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    raw = data.get('space') or {}
    if not raw:
        raise SpaceError(f'{path}: no "space"')
    space = {}
    for name, spec in raw.items():
        split_param(name)
        if not isinstance(spec, dict):
            raise SpaceError(f'{path}: "{name}" must be an object')
        space[name] = SpaceParam.from_dict({k: v for k, v in spec.items() if not k.startswith('_')})
    constraints = [list(c) for c in data.get('constraints') or []]
    if settings:
        for name, p in space.items():
            v = phone_value(name, settings)
            if v is not None:
                p.phone = v
    meta = {k: v for k, v in data.items() if k not in ('space', 'constraints')}
    meta['requires_unlock'] = bool(data.get('_requires_unlock'))
    meta['evidence'] = data.get('_evidence')
    meta['locked_keys'] = [n for n in space if locked_reason(n, settings)]
    return space, constraints, meta


def space_file_notice(meta):
    """Lines `hc study new` prints for a loaded space file: the evidence caveat and the keys that need an unlock
    (empty list for tiers A / B)."""
    out = []
    if meta.get('evidence'):
        out.append(f'evidence: {meta["evidence"]}')
    if meta.get('requires_unlock') or meta.get('locked_keys'):
        keys = meta.get('locked_keys') or []
        out.append('this space needs an explicit unlock' + (': --unlock ' + ' '.join(f'"{k}"' for k in keys) if keys else '')
                   + ' --unlock-reason TEXT')
    return out


# ----------------------------------------------------------------------------- grid arithmetic

def _digits(step):
    """decimal places that represent the grid exactly (0.05 -> 3, 0.1 -> 2, 9 -> 0)"""
    if step <= 0 or float(step).is_integer():
        return 0
    return max(0, -int(f'{step:e}'.split('e')[1]) + 1)


def snap(param, value, mode='nearest'):
    """A value onto the param's grid: nearest (default), 'floor' (never above value) or 'ceil'; clipped to low..high.
    bool/choice: the value itself when it is a legal choice, else SpaceError."""
    if param.type in ('bool', 'choice'):
        legal = param.values()
        if value in legal:
            return value
        raise SpaceError(f'{value!r} is not one of {legal}')
    low, high, step = param.low, param.high, param.step
    k = (float(value) - low) / step
    k = math.floor(k + 1e-9) if mode == 'floor' else math.ceil(k - 1e-9) if mode == 'ceil' else round(k)
    k = max(0, min(k, int(round((high - low) / step))))
    v = low + k * step
    return int(round(v)) if param.type == 'int' else round(v, _digits(step))


def on_grid(param, value):
    """True when the value is a point of the param's grid / choices."""
    if param.type in ('bool', 'choice'):
        return value in param.values()
    try:
        return abs(float(value) - float(snap(param, value))) < 1e-9 and param.low - 1e-9 <= float(value) <= param.high + 1e-9
    except (TypeError, ValueError):
        return False


def pick(param, u):
    """unit coordinate 0 <= u <= 1 -> a grid point / choice (equal-width bins)."""
    values = param.values()
    i = min(int(max(0.0, min(1.0, float(u))) * len(values)), len(values) - 1)
    return values[i]


# ----------------------------------------------------------------------------- the space

class Constraint:
    """One ordering chain k1 op k2 op k3 ... with op in ('<=', '<')."""

    def __init__(self, tokens):
        tokens = list(tokens)
        if len(tokens) < 3 or len(tokens) % 2 == 0:
            raise SpaceError(f'constraint {tokens}: expected "<key> <op> <key> [<op> <key>...]"')
        self.names = tokens[0::2]
        self.ops = tokens[1::2]
        for op in self.ops:
            if op not in OPS:
                raise SpaceError(f'constraint {tokens}: operator "{op}" must be one of {OPS}')
        if len(set(self.names)) != len(self.names):
            raise SpaceError(f'constraint {tokens}: a key appears twice')

    def tokens(self):
        out = [self.names[0]]
        for op, name in zip(self.ops, self.names[1:]):
            out += [op, name]
        return out

    def satisfied(self, params):
        for a, op, b in zip(self.names, self.ops, self.names[1:]):
            if params.get(a) is None or params.get(b) is None:
                continue                                   # a partial point (oaat) cannot violate the chain
            va, vb = float(params[a]), float(params[b])
            if (op == '<=' and not va <= vb + 1e-9) or (op == '<' and not va < vb - 1e-9):
                return False
        return True

    def __repr__(self):
        return ' '.join(self.tokens())


class ParamSpace:
    """The searched space: `space` {name: SpaceParam}, `constraints` [Constraint], `names` in definition order.

    Construction validates the definition (SpaceError) and applies the locked list (LockedKeyError) unless
    `check_locked=False` (only for reading an old study.json whose lock the contract already checked)."""

    def __init__(self, space, constraints=None, settings=None, check_locked=True, unlocked=(), unlock_reason=None):
        if not space:
            raise SpaceError('space: at least one parameter')
        self.unlocked = list(dict.fromkeys(unlocked or ()))
        self.unlock_reason = unlock_reason
        if self.unlocked and not str(unlock_reason or '').strip():
            raise SpaceError('unlocking ' + ', '.join(self.unlocked) + ' needs a non-empty unlock reason '
                             '(an unlock is explicit and audited)')
        self.space = {}
        for name, p in space.items():
            split_param(name)
            if isinstance(p, dict):
                p = SpaceParam.from_dict(p)
            if p.type not in PARAM_TYPES:
                raise SpaceError(f'"{name}": type must be one of {PARAM_TYPES}')
            problems = []
            p._check(problems, name=f'"{name}"')
            problems += scale_problems(name, p)
            if problems:
                raise SpaceError('; '.join(problems))
            self.space[name] = p
        self.names = list(self.space)
        if check_locked:
            from hindcast.contracts import unlock_refusal
            refused = [r for r in (unlock_refusal(u) for u in self.unlocked) if r]
            if refused:
                raise LockedKeyError(refused)
            refuse_locked(self.names, settings, self.unlocked)
        self.constraints = []
        chained = set()
        for tokens in constraints or []:
            c = tokens if isinstance(tokens, Constraint) else Constraint(tokens)
            for name in c.names:
                if name not in self.space:
                    raise SpaceError(f'constraint {c}: "{name}" is not a searched parameter')
                if self.space[name].type not in ('float', 'int'):
                    raise SpaceError(f'constraint {c}: "{name}" must be a float or int parameter')
                if name in chained:
                    raise SpaceError(f'constraint {c}: "{name}" is in two chains')
                chained.add(name)
            self.constraints.append(c)
        self.chained = chained

    @classmethod
    def from_study(cls, study, settings=None, check_locked=True):
        return cls(study.space, study.constraints, settings=settings, check_locked=check_locked,
                   unlocked=getattr(study, 'unlocked', ()) or (), unlock_reason=getattr(study, 'unlock_reason', None))

    def __len__(self):
        return len(self.names)

    def __contains__(self, name):
        return name in self.space

    def __getitem__(self, name):
        return self.space[name]

    # ---- validity
    def satisfies(self, params):
        return all(c.satisfied(params) for c in self.constraints)

    def violations(self, params):
        """[text, ...]: off-grid values, unknown names, broken chains (empty = a legal point)."""
        out = []
        for name, value in params.items():
            if name not in self.space:
                out.append(f'"{name}" is not a searched parameter')
            elif not on_grid(self.space[name], value):
                out.append(f'"{name}" = {value!r} is not on the step grid / choices')
        for c in self.constraints:
            if not c.satisfied(params):
                out.append(f'constraint {c} violated')
        return out

    def check(self, params):
        """Raise SpaceError listing the violations; return params unchanged otherwise."""
        v = self.violations(params)
        if v:
            raise SpaceError('illegal point: ' + '; '.join(v))
        return params

    def normalize(self, params):
        """Values snapped onto the grid (nearest), keys in space order, unknown keys refused."""
        for name in params:
            if name not in self.space:
                raise SpaceError(f'"{name}" is not a searched parameter')
        return {name: snap(self.space[name], params[name]) for name in self.names if name in params}

    # ---- points
    def phone_point(self, settings, at_ms=None):
        """{name: phone value} for the parameters whose value ns_settings.json carries (the sentinel / oaat centre)."""
        out = {}
        for name, p in self.space.items():
            v = phone_value(name, settings, at_ms)
            if v is None:
                v = p.phone
            if v is not None:
                out[name] = v
        return out

    def grid_points(self, grid=None):
        """Cartesian product over `grid` ({name: [values]}, default: every parameter's full step grid), legal
        points only, each in space order. The optimizer's `combinations(params, 'grid')` without its base entry."""
        grid = grid or {name: p.values() for name, p in self.space.items()}
        names = [n for n in self.names if n in grid]
        out = []
        for values in itertools.product(*[grid[n] for n in names]):
            point = dict(zip(names, values))
            if self.satisfies(point):
                out.append(point)
        return out

    def oaat_points(self, levels=5, centre=None):
        """One-at-a-time: for each parameter `levels` grid values spread over its range around `centre` (the phone's
        value when given, else the range middle); points are single-key dicts, the centre itself excluded.
        bool/choice parameters contribute every other choice. The optimizer's 'one_at_a_time' shape."""
        centre = centre or {}
        out = []
        for name, p in self.space.items():
            values = p.values()
            c = centre.get(name)
            if p.type in ('bool', 'choice'):
                chosen = [v for v in values if v != c]
            else:
                c = snap(p, c) if c is not None else values[len(values) // 2]
                if levels >= len(values):
                    chosen = list(values)
                else:                                                  # evenly over the whole range, ends included
                    idx = {int(round(k / (levels - 1) * (len(values) - 1))) for k in range(levels)} if levels > 1 else {0}
                    chosen = [values[i] for i in sorted(idx)]
                chosen = [v for v in chosen if v != c]
            out += [{name: v} for v in chosen]
        return [pt for pt in out if self.satisfies(pt)]

    def decode(self, u):
        """A unit-cube point {name: 0..1} -> legal grid point in space order ("ratio + half-widths" for chains).

        Chain members: the centre member is picked directly from its grid; walking outwards each neighbour is
        placed at `u` of the way between its own bound and the previous member, snapped towards the previous
        member (floor on the lower side, ceil on the upper), so the chain is satisfied by construction. For a
        strict `<` a snapped equal value steps one grid point away when the range allows it."""
        params = {}
        for c in self.constraints:
            names = c.names
            mid = len(names) // 2
            centre = names[mid]
            params[centre] = pick(self.space[centre], u.get(centre, 0.5))
            prev = float(params[centre])
            for i in range(mid - 1, -1, -1):                # towards smaller values
                p = self.space[names[i]]
                hi = min(p.high, prev)
                v = snap(p, p.low + float(u.get(names[i], 0.5)) * max(0.0, hi - p.low), 'floor')
                if c.ops[i] == '<' and float(v) >= prev - 1e-9:
                    v = snap(p, float(v) - p.step, 'floor')
                params[names[i]] = v
                prev = float(v)
            prev = float(params[centre])
            for i in range(mid + 1, len(names)):             # towards larger values
                p = self.space[names[i]]
                lo = max(p.low, prev)
                v = snap(p, lo + float(u.get(names[i], 0.5)) * max(0.0, p.high - lo), 'ceil')
                if c.ops[i - 1] == '<' and float(v) <= prev + 1e-9:
                    v = snap(p, float(v) + p.step, 'ceil')
                params[names[i]] = v
                prev = float(v)
        for name, p in self.space.items():
            if name not in params:
                params[name] = pick(p, u.get(name, 0.5))
        return {name: params[name] for name in self.names}

    def encode(self, params):
        """Legal point -> unit-cube coordinates (inverse of pick for unchained parameters; chain members as the
        fraction of their feasible interval). Round-trips through decode() for legal points."""
        u = {}
        done = set()
        for c in self.constraints:
            names = c.names
            mid = len(names) // 2
            u[names[mid]] = self._unit(names[mid], params[names[mid]])
            prev = float(params[names[mid]])
            for i in range(mid - 1, -1, -1):
                p = self.space[names[i]]
                hi = min(p.high, prev)
                u[names[i]] = (float(params[names[i]]) - p.low) / (hi - p.low) if hi > p.low else 0.0
                prev = float(params[names[i]])
            prev = float(params[names[mid]])
            for i in range(mid + 1, len(names)):
                p = self.space[names[i]]
                lo = max(p.low, prev)
                u[names[i]] = (float(params[names[i]]) - lo) / (p.high - lo) if p.high > lo else 0.0
                prev = float(params[names[i]])
            done |= set(names)
        for name in self.names:
            if name not in done:
                u[name] = self._unit(name, params[name])
        return u

    def _unit(self, name, value):
        values = self.space[name].values()
        i = values.index(value) if value in values else values.index(snap(self.space[name], value))
        return (i + 0.5) / len(values)

    # ---- identity and text
    def variant_name(self, params):
        """The optimizer's label for this point (`base` for {}); same text for the same assignment on both sides."""
        return variant_name(params, self.names)

    def vhash(self, params):
        return vhash(self.normalize(params) if params else {})

    def to_dict(self):
        return {'space': {n: p.to_dict() for n, p in self.space.items()},
                'constraints': [c.tokens() for c in self.constraints]}

    def scale_names(self):
        return [n for n, p in self.space.items() if p.type == 'scale']


# ----------------------------------------------------------------------------- VDF text

def assignment(params, names=None):
    """{name: value} -> [(name, value), ...] in `names` order (space order), then the rest sorted by name."""
    names = list(names or [])
    items = [(n, params[n]) for n in names if n in params]
    items += sorted((n, v) for n, v in params.items() if n not in names)
    return items


def variant_name(params, names=None):
    """emulator_optimize.variant_name over the assignment in space order; 'base' for an empty point."""
    names = list(names or params)
    return _opt.variant_name(assignment(params, names), {n: None for n in names})


def scale_expression(name, value):
    """The VDF value expression of a scale parameter (contracts.SCALE_KEYS) at multiplier `value`, or None for the
    phone value 1.0 (no row). current_basal is re-rounded to the pump basal step as ns_profile does.

        scale_expression('profile sens', 1.1)           -> "profile['sens']*1.1"
        scale_expression('profile current_basal', 1.1)  -> "round(profile['current_basal']*1.1/0.05)*0.05"
    """
    if not is_scale_key(name):
        raise SpaceError(f'"{name}" is not a scale key ({", ".join(SCALE_KEYS)})')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpaceError(f'"{name}": a scale needs a number, got {value!r}')
    if abs(float(value) - SCALE_PHONE) < 1e-12:
        return None
    array, item = split_param(name)
    factor = _opt.vdf_value(value)
    if item == 'current_basal':
        step = _opt.vdf_value(PUMP_BASAL_STEP)
        return f"round({array}['{item}']*{factor}/{step})*{step}"
    return f"{array}['{item}']*{factor}"


def scale_row(name, value, label=VDF_LABEL):
    """One VDF row for a scale (write_vdf's field layout and comment), or None for the phone value 1.0."""
    expr = scale_expression(name, value)
    if expr is None:
        return None
    array, item = split_param(name)
    return f'{array:<12} {item:<32} {expr:<16} ### emulator_optimize {label} scale x{_opt.vdf_value(value)}'


def write_variant_vdf(path, params, base_lines=(), label=VDF_LABEL, names=None):
    """Write base lines + one row per parameter with emulator_optimize.write_vdf (its field layout, its comment).
    Scale keys (contracts.SCALE_KEYS, decided by the name so a spawned worker needs no space) become expression
    rows (scale_row) after the base lines, a scale at 1.0 writes no row; every other value is a literal row."""
    items = assignment(params, names)
    scale_rows = [r for r in (scale_row(n, v, label) for n, v in items if is_scale_key(n)) if r]
    literal = [(n, v) for n, v in items if not is_scale_key(n)]
    _opt.write_vdf(path, list(base_lines) + scale_rows, literal, label)
    return path


def vdf_text(params, base_lines=(), label=VDF_LABEL, names=None):
    """The VDF text emulator_optimize.write_vdf would write for this point (for cards, round-trip tests, display)."""
    fd, tmp = tempfile.mkstemp(suffix='.vdf', prefix='hindcast_')
    os.close(fd)
    try:
        write_variant_vdf(tmp, params, base_lines, label, names)
        with open(tmp, 'r', encoding='utf-8') as fh:
            return fh.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def parse_vdf_row(line):
    """One VDF row the way emulator_core.setVariant splits it: (array, item, value text) or None for an
    unusable row (blank, fewer than three fields). The value text is what setVariant evals (comment after ### cut)."""
    zeile = line.replace('\t', ' ').rstrip('\n')
    if not zeile.strip():
        return None
    wo = zeile.find(' ')
    if wo < 0:
        return None
    array, zeile = zeile[:wo], zeile[wo:].lstrip(' ')
    wo = zeile.find(' ')
    if wo < 0:
        return None
    item, zeile = zeile[:wo], zeile[wo:].lstrip(' ')
    wo = zeile.find('###')
    value = (zeile if wo < 0 else zeile[:wo]).rstrip(' ')
    if not array or not item or not value:
        return None
    return array, item, value


_SCALE_RE = re.compile(r"^(\w+)\['(\w+)'\]\*([0-9.eE+-]+)$")
_SCALE_BASAL_RE = re.compile(r"^round\((\w+)\['(\w+)'\]\*([0-9.eE+-]+)/([0-9.eE+-]+)\)\*([0-9.eE+-]+)$")


def parse_scale_expression(array, item, value):
    """The multiplier of a row scale_expression wrote for '<array> <item>', or None for any other text."""
    if not is_scale_key(f'{array} {item}'):
        return None
    m = _SCALE_BASAL_RE.match(value) if item == 'current_basal' else _SCALE_RE.match(value)
    if not m or m.group(1) != array or m.group(2) != item:
        return None
    try:
        return ast.literal_eval(m.group(3))
    except (ValueError, SyntaxError):
        return None


def parse_vdf_text(text):
    """VDF text -> {'<array> <item>': literal value} for the rows whose value is a Python literal (what write_vdf
    emits) and the multiplier of a scale row (scale_expression); rows with other expressions
    (`profile['min_bg'] + 10`) are returned with their text under the same key."""
    out = {}
    for line in text.splitlines():
        row = parse_vdf_row(line)
        if not row:
            continue
        array, item, value = row
        try:
            out[f'{array} {item}'] = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            factor = parse_scale_expression(array, item, value)
            out[f'{array} {item}'] = value if factor is None else factor
    return out


def vdf_rows_set_searched(base_lines, names):
    """Base VDF rows that set a searched parameter (a study refuses them: the row would shadow the search)."""
    hits = []
    for line in base_lines:
        row = parse_vdf_row(line)
        if row and f'{row[0]} {row[1]}' in names:
            hits.append(line.rstrip('\n'))
    return hits


# ----------------------------------------------------------------------------- default space files

_TIER_A = {
    'profile autoISF_max':                 dict(type='float', low=1.0, high=2.0, step=0.05),
    'profile autoISF_min':                 dict(type='float', low=0.3, high=1.0, step=0.05),
    'profile dura_ISF_weight':             dict(type='float', low=0.0, high=1.5, step=0.1),
    'profile bgAccel_ISF_weight':          dict(type='float', low=0.0, high=0.30, step=0.01),
    'profile bgBrake_ISF_weight':          dict(type='float', low=0.0, high=0.30, step=0.01),
    'profile smb_delivery_ratio':          dict(type='float', low=0.1, high=0.7, step=0.05),
    'profile smb_delivery_ratio_min':      dict(type='float', low=0.05, high=0.4, step=0.05),
    'profile smb_delivery_ratio_max':      dict(type='float', low=0.3, high=0.9, step=0.05),
    'profile smb_delivery_ratio_bg_range': dict(type='float', low=18.0, high=90.0, step=9.0),
    'profile iob_threshold_percent':       dict(type='int', low=50, high=100, step=5),
}
_TIER_A_CONSTRAINTS = [
    ['profile smb_delivery_ratio_min', '<=', 'profile smb_delivery_ratio', '<=', 'profile smb_delivery_ratio_max'],
    ['profile autoISF_min', '<=', 'profile autoISF_max'],
]
_TIER_A_NOTES = {
    'profile autoISF_max': 'caps every weight; when the factor sits on the cap in > 25 % of loops the weights are unobservable (vary the cap first)',
    'profile autoISF_min': 'floor of the ISF factor (hypo side)',
    'profile dura_ISF_weight': 'plateau strength; interacts with enable_dura_ISF_with_COB',
    'profile bgAccel_ISF_weight': 'with bgBrake_ISF_weight and parabola_fit_source',
    'profile bgBrake_ISF_weight': 'hypo side of the acceleration term',
    'profile smb_delivery_ratio': 'with _min / _max / _bg_range (the interpolation); chain min <= ratio <= max',
    'profile smb_delivery_ratio_min': 'lower end of the delivery-ratio interpolation',
    'profile smb_delivery_ratio_max': 'upper end of the delivery-ratio interpolation',
    'profile smb_delivery_ratio_bg_range': 'mg/dl; the phone value is itself fitted to the echoes (_assumed / _overrides in ns_settings.json)',
    'profile iob_threshold_percent': 'gates SMB entirely above the threshold (iobTH)',
}
_TIER_B = {
    'profile pp_ISF_weight':            dict(type='float', low=0.0, high=0.05, step=0.002),
    'profile pp_ISF_hours':             dict(type='choice', choices=[0, 1, 2, 3, 4, 6]),
    'profile enable_pp_ISF_always':     dict(type='bool'),
    'profile higher_ISFrange_weight':   dict(type='float', low=0.0, high=1.0, step=0.05),
    'profile lower_ISFrange_weight':    dict(type='float', low=0.0, high=1.0, step=0.05),
    'profile delta_ISFrange_weight':    dict(type='float', low=0.0, high=1.0, step=0.05),
    'profile smb_max_range_extension':  dict(type='float', low=1.0, high=3.0, step=0.1),
    'profile SMBInterval':              dict(type='choice', choices=[1, 2, 3, 5]),
    'profile maxSMBBasalMinutes':       dict(type='choice', choices=[30, 60, 90, 120, 180, 240]),
    'profile maxUAMSMBBasalMinutes':    dict(type='choice', choices=[30, 60, 90, 120, 180, 240]),
    'profile activity_scale_factor':    dict(type='float', low=1.0, high=1.5, step=0.05),
    'profile inactivity_scale_factor':  dict(type='float', low=0.0, high=1.0, step=0.1),
    'profile enable_dura_ISF_with_COB': dict(type='bool'),
    'profile parabola_fit_source':      dict(type='choice', choices=[3, 5]),
}
_TIER_B_NOTES = {
    'profile inactivity_scale_factor': 'only observable on days with the activity-monitor echo',
    'profile smb_max_range_extension': 'the phone value 1.8 is fitted to the printed maxBolus (ns_settings.json _assumed)',
}

_WEAK = ('Weak evidence: decision-level replay, no glucose response - the replay keeps the glucose and insulin '
         'history the phone\'s own settings produced, so it says how the decisions would have moved, never what '
         'glucose would have done. ')
_SCALE = dict(type='scale', low=0.7, high=1.4, step=0.05)
_TIER_C = {
    'profile sens':               dict(_SCALE, target='profile sens'),
    'profile carb_ratio':         dict(_SCALE, target='profile carb_ratio'),
    'profile current_basal':      dict(_SCALE, target='profile current_basal'),
    'profile profile_percentage': dict(_SCALE, target='profile profile_percentage'),
}
_TIER_C_NOTES = {
    'profile sens': _WEAK + 'Multiplier of the profile ISF of each loop (Nightscout schedule, mg/dl/U): the VDF row '
                    "is profile['sens']*x. It moves the autoISF-final ISF, insulinReq, SMB and TBR and the carb "
                    'sensitivity (csf = sens / carb_ratio) of the COB prediction.',
    'profile carb_ratio': _WEAK + 'Multiplier of the profile CR of each loop. In determine_basal.py it enters the COB '
                          'prediction (csf = sens / carb_ratio) and the meal insulin echo only; COB itself is the '
                          "phone's (devicestatus), so the replay cannot follow a different carb absorption or bolus.",
    'profile current_basal': _WEAK + 'Multiplier of the scheduled basal of each loop, re-rounded to the 0.05 U/h pump '
                             "step (round(profile['current_basal']*x/0.05)*0.05). It moves the neutral temp, the TBR "
                             'arithmetic and maxBolus (smb_max_range_extension * basal * minutes), but not max_basal '
                             '(ns_profile derived it from the unscaled basal) and not the IOB of past temp basals '
                             '(net IOB is relative to the recorded schedule). At small rates neighbouring multipliers '
                             'round to the same pump rate (0.15 U/h x 0.9 -> 0.15, x 0.8 -> 0.10): a dead stretch of the '
                             'grid is the pump step, not a result.',
    'profile profile_percentage': _WEAK + 'Multiplier of the Profile Switch percentage the loop ran under (100 % = '
                                  '1.0). determine_basal.py reads it only in iobTH_reduction_ratio (percentage/100 * '
                                  'exercise * activity ratio, active because iob_threshold_percent is not 100); it does '
                                  'NOT rescale sens / CR / basal here, ns_profile already applied the recorded '
                                  'percentage. To mimic a Profile Switch of x combine it with sens and carb_ratio near '
                                  '1/x and current_basal near x. Not locked itself.',
}
_TIER_S = {
    'profile max_iob':   dict(type='float', low=3.0, high=12.0, step=0.5),
    'profile max_basal': dict(type='float', low=1.0, high=6.0, step=0.25),
}
_TIER_S_NOTES = {
    'profile max_iob': _WEAK + 'Safety cap (U): a cap study only reports how often and in which loops the cap binds '
                       '(insulinReq / SMB limited by max_iob, iobTH = iob_threshold_percent of it) and how much more '
                       'or less the loop would have asked; it cannot show what the extra or missing insulin would '
                       'have done.',
    'profile max_basal': _WEAK + 'Safety cap (U/h) on every temp basal: a cap study only reports how often the TBR '
                         'cap binds. ns_profile derives the profile value as min(max(preference, max_daily_basal), '
                         'floor(current_basal_safety_multiplier * basal), floor(max_daily_safety_multiplier * '
                         'max_daily_basal)); a VDF row replaces that derived value with the raw number (also the '
                         'sentinel row), so it can differ from base where the derivation does not return the preference.',
}
_TIERS = {'A': (_TIER_A, _TIER_A_CONSTRAINTS, _TIER_A_NOTES), 'B': (_TIER_B, [], _TIER_B_NOTES),
          'C': (_TIER_C, [], _TIER_C_NOTES), 'S': (_TIER_S, [], _TIER_S_NOTES)}
REQUIRES_UNLOCK = ('C', 'S')
EVIDENCE_WEAK = 'weak: decision-level replay, no glucose response'


def default_space(tier='A', settings=None):
    """The documented default space of a tier as a JSON-ready dict (what the spaces/ files hold), `phone` filled
    from `settings` (ns_settings.json) when given. Tiers C and S carry `_requires_unlock` and `_evidence`."""
    tier = tier.upper()
    if tier not in _TIERS:
        raise SpaceError(f'unknown tier "{tier}" (one of {", ".join(_TIERS)})')
    params, constraints, notes = _TIERS[tier]
    if tier not in REQUIRES_UNLOCK:
        refuse_locked(params, settings)
    space = {}
    for name, spec in params.items():
        entry = {k: v for k, v in spec.items() if k != 'target'}
        pv = phone_value(name, settings or {})
        entry['phone'] = pv
        if 'target' in spec:
            entry['target'] = spec['target']
        if name in notes:
            entry['_note'] = notes[name]
        space[name] = entry
    about = [
        f'Hindcast default parameter space, tier {tier} (ShadowLoop proposal section 4, DESIGN.md section 5.4).',
        'One entry per VDF key "<array> <item>": type float/int (low, high, step: the searched step grid), bool, or',
        'choice (explicit values). "phone" is the current value in ns_settings.json (the centre of the one-at-a-time',
        'screen and the sentinel); regenerate with `python -m hindcast.space --write-defaults` after a preferences import.',
        'Constraints are ordering chains the sampler keeps by construction. Locked keys (DESIGN.md section 3: safety',
        'section, profile sens/carb_ratio/current_basal/dia, every new_parameter key) are refused and never listed here.',
        'This file says what a replay may vary; it recommends nothing. Nothing in Hindcast changes the phone.',
    ]
    if tier == 'A':
        about.append('Tier A: the autoISF caps and weights with the most leverage, plus the SMB delivery-ratio family.')
    elif tier == 'B':
        about.append('Tier B: the second pass (pp/range/drift weights, SMB timing caps, activity factors, fit source).')
    elif tier == 'C':
        about[5] = ('section, profile sens/carb_ratio/current_basal/dia, every new_parameter key) stay locked unless a '
                    'study unlocks them explicitly.')
        about += ['Tier C: the profile itself as multipliers of the value each loop recorded (type scale, phone 1.0 = the '
                  'recorded profile, which writes no VDF row). sens, carb_ratio and current_basal are locked by default: '
                  'a study needs --unlock KEY --unlock-reason TEXT (stored in study.json); profile_percentage is not locked.',
                  'A decision-level replay cannot judge a different profile: it shows how the loop would have decided '
                  'with the scaled value over the glucose the phone\'s own profile produced. Weak evidence; for review by hand.']
    else:
        about[5] = ('section, profile sens/carb_ratio/current_basal/dia, every new_parameter key) stay locked unless a '
                    'study unlocks them explicitly.')
        about += ['Tier S: the safety caps max_iob and max_basal (locked by default: a study needs --unlock KEY '
                  '--unlock-reason TEXT, stored in study.json). maxSMBBasalMinutes / maxUAMSMBBasalMinutes are in tier B.',
                  'A cap study only reports how often the cap binds and how the decisions move when it does; it cannot say '
                  'what the extra or missing insulin would have done. Weak evidence; for review by hand.']
    out = {'_about': about, 'tier': tier, 'phone_source': 'ns_settings.json'}
    if tier in REQUIRES_UNLOCK:
        out['_requires_unlock'] = True
        out['_evidence'] = EVIDENCE_WEAK
    out.update({'space': space, 'constraints': constraints})
    return out


def write_default_spaces(directory=SPACES_DIR, settings=None):
    """Write spaces/autoisf_tierA.json, autoisf_tierB.json, profile_tierC.json, safety_tierS.json; returns the paths."""
    settings = settings if settings is not None else load_settings()
    os.makedirs(directory, exist_ok=True)
    paths = []
    for tier, fname in TIER_FILE_NAMES.items():
        path = os.path.join(directory, fname)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(default_space(tier, settings), fh, indent=1, ensure_ascii=False)
            fh.write('\n')
        paths.append(path)
    return paths


def stale_default_spaces(directory=SPACES_DIR, settings=None):
    """Names of the default space files whose phone values or ranges differ from what default_space() generates."""
    settings = settings if settings is not None else load_settings()
    stale = []
    for tier, fname in TIER_FILE_NAMES.items():
        path = os.path.join(directory, fname)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                current = json.load(fh)
        except (OSError, ValueError):
            stale.append(os.path.basename(path))
            continue
        want = default_space(tier, settings)
        if any(current.get(k) != want.get(k) for k in ('space', 'constraints', '_requires_unlock', '_evidence')):
            stale.append(os.path.basename(path))
    return stale


if __name__ == '__main__':
    if '--write-defaults' in sys.argv:
        for p in write_default_spaces():
            print('wrote', p)
    else:
        print(__doc__)
        print('stale default spaces:', stale_default_spaces() or 'none')
