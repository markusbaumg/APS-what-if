"""
contracts.py  -  the Hindcast data contracts (backlog H-001; DESIGN.md section 4.5)

One dataclass per document, each with `to_dict()` / `to_json()` / `from_dict()` / `from_json()` and a `validate()`
that raises `ContractError` naming every problem (or `problems()` returning them as a list):

    Study      studies/<study>/study.json      written once by `hc study new`; only phases[].state changes later
    TrialRow   studies/<study>/trials.jsonl    one object per line, append-only, < 4 KB per line
    State      studies/<study>/STATE.json      rewritten by atomic rename after every trial
    Status     studies/<study>/status.json     what the skill reads: <= 2 KB
    Round      studies/<study>/round.json      per round, <= 2 KB per variant, with a verdict and a `why` per variant

The JSON Schema files under schemas/ are GENERATED from these dataclasses by `write_schemas()`; the test
`test_contracts.py::test_schema_files_are_current` fails when they are stale (regenerate with
`python -m hindcast.contracts --write-schemas`). The dataclasses are the truth, the schema files are for other
tools (the page, an editor) and for reading.

Reading is strict: `from_dict()` type-checks every field against the dataclass annotations and refuses unknown
keys (naming them) unless `strict=False` drops them. `to_dict()` omits a field whose value is None when its
default is None, so compact files round-trip.

Hashes (all sha1 hex, 40 characters; `short()` gives the 12-character display form used in file names):

    vhash(params)                  over the canonical parameter text only -> identifies a variant across studies
    key(params, day, settings_sha, code_sha, freeze_sha, objective_version)
                                   -> identifies one (variant, day) evaluation; a key with status 'done' is never re-run
    code_sha(software_dir)         over determine_basal.py, emulator_core.py and ns_*.py (name + bytes)
    settings_sha(path)             over ns_settings.json with nightscout.token / nightscout.api_secret_sha1 blanked

Canonical parameter text: one line per parameter, sorted by name, `<array> <item> <vdf_value>` with
`emulator_optimize.vdf_value` as the value repr, so `1.3` and `1.30` hash alike but `1` and `1.0` do not (the
VDF rows differ too).

Locked keys (DESIGN.md section 3): every `new_parameter` key, `profile sens`, `profile carb_ratio`,
`profile current_basal`, `profile dia` and every key of the `safety` section of ns_settings.json (copied 1:1 into
the profile by ns_profile). `locked_reason(name)` returns the reason text or None. The same locks hold for the rows
of a study's base VDF (`locked_vdf_row_reason(line)`): what may not be searched may not be set through the base VDF
either.

Unlocking (the person whose data this is asked for studies over profile ISF / CR / basal and the safety caps; he
reviews every proposal by hand): the lock stays the default. A study lifts it only explicitly, per study, and
audited in study.json: `Study.unlocked` lists the '<array> <item>' keys, `Study.unlock_reason` (required, non-empty
when the list is) says why. `locked_reason(name, settings, unlocked)` returns None for a listed key that may be
unlocked at all (`unlock_refusal(name)`: never a `new_parameter` key, never a non-profile group, never
`profile dia`, which enters through the recorded IOB and would be silently inert), the reason text otherwise.
Every surface that shows an unlocked study adds `WEAK_EVIDENCE` (`Study.unlock_caveat()`): a decision-level
replay keeps the glucose the phone's own profile produced, so it cannot judge a different profile.

Scale parameters (`SpaceParam(type='scale')`, DESIGN.md section 5.4 tier C): the profile values Nightscout carries
per loop from a time-varying schedule (`SCALE_KEYS`: sens, carb_ratio, current_basal, profile_percentage) are
searched only as a multiplier of the recorded value (phone value `SCALE_PHONE` = 1.0); a scale on any other key and
any other type on these keys are refused. The VDF row text is space.py's (`space.scale_expression`).

The honesty statement every user-facing surface quotes verbatim is `HONESTY_STATEMENT`; `NOT_TESTABLE` is the
"cannot be tested with this method" list for the page.
"""
import dataclasses
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from emulator_optimize import vdf_value                                                   # noqa: E402

SCHEMAS_DIR = os.path.join(HINDCAST_DIR, 'schemas')
NS_SETTINGS_PATH = os.path.join(SOFTWARE_DIR, 'ns_settings.json')
CONTRACT_VERSION = 1                                     # bump when a document shape changes incompatibly

HONESTY_STATEMENT = (
    "Hindcast replays the loop's decisions under other settings over the glucose and insulin history that the "
    "phone's own settings produced. It says where and how much the loop would have asked differently; it cannot "
    "say what glucose would have done. A variant that boluses earlier is charged for its bolus and for the later "
    "ones the phone actually gave, so deltas overstate aggressive settings and understate gentle ones. The base "
    "replay disagrees with the phone in about 4 % of loops; differences smaller than that floor are ties. Nothing "
    "here changes the phone; settings are for you and your care team to review. This is one person's insulin "
    "dosing, not medical advice.")

NOT_TESTABLE = (
    'basal rates', 'DIA and insulin peak', 'carb ratio', 'autosens ratio', 'the UKF glucose smoother',
)                                                        # they enter through the recorded inputs the replay takes as given

VDF_GROUPS = ('profile', 'new_parameter', 'glucose_status', 'iob_data', 'meal_data', 'autosens_data',
              'currenttemp', 'state', 'temp')
SEARCHABLE_GROUPS = ('profile',)                         # the only group a study may vary; the rest are inputs or scratch
LOCKED_GROUPS = ('new_parameter',)
LOCKED_PROFILE_KEYS = (
    'sens', 'carb_ratio', 'current_basal', 'dia',                                   # the profile proper
    'max_iob', 'max_basal', 'max_daily_safety_multiplier', 'current_basal_safety_multiplier',        # safety section
    'lgsThreshold', 'treatmentssafety_maxbolus', 'treatmentssafety_maxcarbs', 'autosens_min', 'autosens_max',
    'use_dynamic_sensitivity', 'DynISFAdjust', 'openaps_smb_min_5m_carbimpact', 'absorption_cutoff',
    'maxCarbAbsorptionRate', 'bolus_increment', 'carbsReqThreshold', 'remainingCarbsCap', 'maxCOB', 'use_autosens',
    'dynisf_adjust_sensitivity', 'min_5m_carbimpact', 'bolussnooze_dia_divisor', 'basal_step',
)

QUESTIONS = ('explore', 'earlier', 'gentler')
PHASE_KINDS = ('floor', 'oaat', 'grid', 'lhs', 'halving', 'coordinate', 'optuna', 'validate', 'cv', 'grow')
PHASE_STATES = ('pending', 'running', 'done', 'skipped', 'stopped')
TRIAL_STATUSES = ('done', 'failed', 'blocked', 'stale', 'running')
VERDICTS = ('pending', 'base', 'dead', 'candidate', 'tie', 'infeasible', 'recommended')
DAY_ROLES = ('search', 'holdout', 'quarantined')
PARAM_TYPES = ('float', 'int', 'bool', 'choice', 'scale')
SCALE_KEYS = ('sens', 'carb_ratio', 'current_basal', 'profile_percentage')   # profile items searched only as a multiplier
SCALE_PHONE = 1.0                                        # the phone's value of every scale: the recorded profile value
NEVER_UNLOCKABLE_PROFILE_KEYS = ('dia',)                 # enters through the recorded IOB; a VDF row would be inert
WEAK_EVIDENCE = ('weak evidence: this study varies keys that are locked by default (%s). A decision-level replay '
                 'keeps the glucose and insulin history the phone\'s own profile and caps produced; it cannot judge a '
                 'different profile or cap, only say how often and by how much the loop would have decided differently. '
                 'Unlocked by the person whose data this is (reason: %s); every proposal is reviewed by hand.')
STATUS_MAX_BYTES = 2048
ROUND_MAX_BYTES_PER_VARIANT = 2048
TRIAL_LINE_MAX_BYTES = 4096

_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
_SHORT_SHA_RE = re.compile(r'^([0-9a-f]{12}|[0-9a-f]{40})$')
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


class ContractError(ValueError):
    """A document violates its contract; the message lists every problem, one per line."""


# ----------------------------------------------------------------------------- hashes

def canonical_params(params):
    """{name: value} -> the canonical text the hashes are taken over (sorted, VDF value repr)."""
    return '\n'.join(f'{name} {vdf_value(value)}' for name, value in sorted(params.items()))


def _sha1(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()


def vhash(params):
    """sha1 over the canonical parameter text: the identity of a variant (the base variant is vhash({}))."""
    return _sha1(canonical_params(params))


BASE_VHASH = vhash({})


def key(params, day, settings_sha, code_sha, freeze_sha, objective_version):
    """sha1 over canonical params | day | settings_sha | code_sha | freeze_sha | objective_version."""
    return _sha1('|'.join([canonical_params(params), str(day), str(settings_sha), str(code_sha),
                           str(freeze_sha), str(int(objective_version))]))


def short(sha):
    """12-character display form of a sha1 hex (file names, cards, log lines)."""
    return (sha or '')[:12]


def code_sha(software_dir=SOFTWARE_DIR):
    """sha1 over determine_basal.py, emulator_core.py and every ns_*.py (file name + bytes, sorted names)."""
    names = ['determine_basal.py', 'emulator_core.py']
    names += sorted(os.path.basename(p) for p in glob.glob(os.path.join(software_dir, 'ns_*.py')))
    h = hashlib.sha1()
    for name in names:
        path = os.path.join(software_dir, name)
        if not os.path.isfile(path):
            continue
        h.update(name.encode('utf-8') + b'\0')
        with open(path, 'rb') as fh:
            h.update(fh.read())
        h.update(b'\0')
    return h.hexdigest()


def scrub_settings(settings):
    """A deep copy of a settings dict with nightscout.token / nightscout.api_secret_sha1 blanked."""
    out = json.loads(json.dumps(settings))
    ns = out.get('nightscout')
    if isinstance(ns, dict):
        for secret in ('token', 'api_secret_sha1'):
            if secret in ns:
                ns[secret] = None
    return out


def settings_sha(path=NS_SETTINGS_PATH, settings=None):
    """sha1 over the scrubbed, canonical (sorted, compact) JSON of ns_settings.json (or a given dict)."""
    if settings is None:
        with open(path, 'r', encoding='utf-8') as fh:
            settings = json.load(fh)
    text = json.dumps(scrub_settings(settings), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return _sha1(text)


def core_versions():
    """{'emulator_core.py': 'YYYY-MM-DD HH:MM', 'determine_basal.py': ..., 'emulator_optimize.py': ...}."""
    import emulator_core
    import determine_basal
    import emulator_optimize
    msg = {}
    emulator_core.get_version_core(msg)
    determine_basal.get_version_determine_basal(msg)
    emulator_optimize.get_version_optimize(msg)
    return {k: str(v) for k, v in msg.items()}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ----------------------------------------------------------------------------- locked keys

def split_param(name):
    """'<array> <item>' -> (array, item); ContractError when the name is not two words."""
    parts = str(name).split()
    if len(parts) != 2:
        raise ContractError(f'parameter "{name}" must be "<array> <item>" (VDF syntax)')
    return parts[0], parts[1]


def _locked_profile_keys(settings=None):
    """LOCKED_PROFILE_KEYS plus every key of the `safety` section of `settings` (a loaded ns_settings.json), so a
    key added there later is locked without a code change."""
    safety = set(LOCKED_PROFILE_KEYS)
    if isinstance(settings, dict) and isinstance(settings.get('safety'), dict):
        safety |= {k for k in settings['safety'] if not str(k).startswith('_')}
    return safety


def unlock_refusal(name):
    """Why `name` can never be unlocked (a new_parameter key, a non-profile group, `profile dia`, a malformed
    name), or None when a study may list it in `unlocked`."""
    try:
        array, item = split_param(name)
    except ContractError as e:
        return str(e)
    if array in LOCKED_GROUPS:
        return f'"{name}": {array} keys cannot be unlocked (emulator internals, DESIGN.md section 3)'
    if array not in SEARCHABLE_GROUPS:
        return f'"{name}": only {", ".join(SEARCHABLE_GROUPS)} keys can be unlocked'
    if item in NEVER_UNLOCKABLE_PROFILE_KEYS:
        return f'"{name}" cannot be unlocked: it enters through the recorded IOB, a VDF row would not change the replay'
    return None


def _is_unlocked(name, unlocked):
    if not unlocked:
        return False
    target = ' '.join(str(name).split())
    return any(' '.join(str(u).split()) == target for u in unlocked) and unlock_refusal(name) is None


def is_scale_key(name):
    """True for '<array> <item>' names that are searched only as a multiplier (profile sens, carb_ratio, ...)."""
    parts = str(name).split()
    return len(parts) == 2 and parts[0] == 'profile' and parts[1] in SCALE_KEYS


def scale_problems(name, param):
    """[problem, ...] for the scale rule: a scale only on SCALE_KEYS, SCALE_KEYS only as a scale, target = name."""
    out = []
    ptype = getattr(param, 'type', None)
    if ptype == 'scale':
        if not is_scale_key(name):
            out.append(f'"{name}": type scale is for {", ".join("profile " + k for k in SCALE_KEYS)} only')
        target = getattr(param, 'target', None)
        if target is not None and ' '.join(str(target).split()) != ' '.join(str(name).split()):
            out.append(f'"{name}": scale target "{target}" must be the parameter name itself')
    elif is_scale_key(name):
        out.append(f'"{name}" is a per-loop profile value: search it as type scale (a multiplier, phone 1.0), not {ptype}')
    elif getattr(param, 'target', None) is not None:
        out.append(f'"{name}": target is only for type scale')
    return out


def locked_reason(name, settings=None, unlocked=()):
    """Why a parameter may not be searched by an unattended run, or None when it may.

    `settings` (a loaded ns_settings.json) extends the safety list with whatever that file's `safety` section
    holds, so a key added there later is locked without a code change. `unlocked` (a study's explicit list,
    Study.unlocked) lifts the lock for a listed key unless `unlock_refusal(name)` forbids unlocking it; the
    reason that a study carries alongside is checked by Study, not here."""
    try:
        array, item = split_param(name)
    except ContractError as e:
        return str(e)
    if _is_unlocked(name, unlocked):
        return None
    if array in LOCKED_GROUPS:
        return f'"{name}": every {array} key is locked for unattended runs (DESIGN.md section 3)'
    if array not in VDF_GROUPS:
        return f'"{name}": unknown VDF group "{array}" (one of {", ".join(VDF_GROUPS)})'
    if array not in SEARCHABLE_GROUPS:
        return f'"{name}": group "{array}" holds replay inputs, not settings; only {", ".join(SEARCHABLE_GROUPS)} may be searched'
    if item in _locked_profile_keys(settings):
        return f'"{name}" is locked for unattended runs (safety or profile key, DESIGN.md section 3)'
    return None


def vdf_row_name(line):
    """'<array> <item>' of one VDF row (`<array> <item> <expression> [### comment]`), or None for a row setVariant
    would not action (blank, fewer than three fields). Tabs count as spaces, as in setVariant."""
    parts = str(line).replace('\t', ' ').split('###')[0].split()
    if len(parts) < 3:
        return None
    return f'{parts[0]} {parts[1]}'


def locked_vdf_row_reason(line, settings=None, unlocked=()):
    """Why a base VDF row may not be carried by an unattended run (the DESIGN.md section 3 locks: every
    `new_parameter` row, a `profile` row on a safety key or on sens / carb_ratio / current_basal / dia), or None.

    The locks hold for the base rows exactly as for the searched space: a study that may not search `profile
    max_iob` may not set it through its base VDF either. Rows of the other groups (state, temp, STAIR..., the input
    dicts) are not settings and pass here; the emulator applies them as any VDF. A key in `unlocked` (Study.unlocked)
    passes like in locked_reason."""
    name = vdf_row_name(line)
    if name is None:
        return None
    if _is_unlocked(name, unlocked):
        return None
    array, item = name.split(' ', 1)
    if array in LOCKED_GROUPS:
        return f'"{name}": every {array} key is locked for unattended runs (DESIGN.md section 3)'
    if array in SEARCHABLE_GROUPS and item in _locked_profile_keys(settings):
        return f'"{name}" is locked for unattended runs (safety or profile key, DESIGN.md section 3)'
    return None


# ----------------------------------------------------------------------------- dataclass <-> JSON machinery

def _is_dataclass_type(t):
    return isinstance(t, type) and dataclasses.is_dataclass(t)


def _strip_optional(t):
    """X | None -> (X, True); X -> (X, False)"""
    origin = typing.get_origin(t)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(t) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
        return t, type(None) in typing.get_args(t)
    return t, False


def _type_name(t):
    return getattr(t, '__name__', None) or str(t)


def _coerce(value, t, path):
    """Check `value` against annotation `t`; return the Python value (nested dataclasses built)."""
    t, optional = _strip_optional(t)
    if value is None:
        if optional or t is Any:
            return None
        raise ContractError(f'{path}: null is not allowed (expected {_type_name(t)})')
    if t is Any:
        return value
    origin = typing.get_origin(t)
    if origin is Literal:
        if value not in typing.get_args(t):
            raise ContractError(f'{path}: {value!r} is not one of {list(typing.get_args(t))}')
        return value
    if origin is typing.Union or origin is types.UnionType:                # a real union such as float | str
        for alt in typing.get_args(t):
            if alt is type(None):
                continue
            try:
                return _coerce(value, alt, path)
            except ContractError:
                pass
        raise ContractError(f'{path}: {value!r} matches none of {[_type_name(a) for a in typing.get_args(t)]}')
    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)):
            raise ContractError(f'{path}: expected a list, got {type(value).__name__}')
        args = typing.get_args(t)
        item_t = args[0] if args else Any
        return [_coerce(v, item_t, f'{path}[{i}]') for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise ContractError(f'{path}: expected an object, got {type(value).__name__}')
        args = typing.get_args(t)
        val_t = args[1] if len(args) == 2 else Any
        return {str(k): _coerce(v, val_t, f'{path}.{k}') for k, v in value.items()}
    if _is_dataclass_type(t):
        if not isinstance(value, dict):
            raise ContractError(f'{path}: expected an object for {t.__name__}, got {type(value).__name__}')
        return t.from_dict(value, path=path)
    if t is bool:
        if isinstance(value, bool):
            return value
        raise ContractError(f'{path}: expected a boolean, got {value!r}')
    if t is int:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                return int(value)
            raise ContractError(f'{path}: expected an integer, got {value!r}')
        return value
    if t is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError(f'{path}: expected a number, got {value!r}')
        return float(value)
    if t is str:
        if not isinstance(value, str):
            raise ContractError(f'{path}: expected a string, got {type(value).__name__}')
        return value
    raise ContractError(f'{path}: unsupported annotation {t!r}')


def _to_plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, float) and value != value:                        # NaN never reaches a file
        return None
    return value


class Document:
    """Mixin for the dataclasses: dict/JSON conversion, strict reading, validation protocol."""

    def to_dict(self):
        out = {}
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if value is None and f.default is None:
                continue
            out[f.name] = _to_plain(value)
        return out

    def to_json(self, indent=1):
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, allow_nan=False)

    def to_compact_json(self):
        return json.dumps(self.to_dict(), separators=(',', ':'), ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_dict(cls, data, strict=True, path=None):
        path = path or cls.__name__
        if not isinstance(data, dict):
            raise ContractError(f'{path}: expected an object, got {type(data).__name__}')
        hints = typing.get_type_hints(cls)
        fields = {f.name: f for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - set(fields))
        if unknown and strict:
            raise ContractError(f'{path}: unknown field(s) {", ".join(unknown)}')
        kwargs = {}
        missing = []
        for name, f in fields.items():
            if name in data:
                kwargs[name] = _coerce(data[name], hints[name], f'{path}.{name}')
            elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                missing.append(name)
        if missing:
            raise ContractError(f'{path}: missing required field(s) {", ".join(missing)}')
        return cls(**kwargs)

    @classmethod
    def from_json(cls, text, strict=True):
        return cls.from_dict(json.loads(text), strict=strict)

    @classmethod
    def load(cls, path, strict=True):
        with open(path, 'r', encoding='utf-8') as fh:
            return cls.from_dict(json.load(fh), strict=strict)

    def save(self, path, indent=1):
        """Write by atomic rename (the STATE.json rule; harmless for the others)."""
        write_json_atomic(path, self.to_dict(), indent=indent)

    def problems(self):
        """List of problem strings (empty = valid). Subclasses extend `_check(problems)`."""
        problems = []
        try:                                                                 # structural re-check through the strict reader
            type(self).from_dict(self.to_dict())
        except ContractError as e:
            problems.append(str(e))
        self._check(problems)
        return problems

    def _check(self, problems):
        pass

    def validate(self):
        """Raise ContractError listing every problem; return self when valid."""
        problems = self.problems()
        if problems:
            raise ContractError(f'{type(self).__name__}: ' + '\n'.join(problems))
        return self


def write_json_atomic(path, data, indent=1):
    """Write `data` as JSON by atomic rename. `indent=None` writes the compact form (the separators of
    `Document.to_compact_json`, which is what the size limits are checked on), with no trailing newline."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=indent, separators=(',', ':') if indent is None else None,
                  ensure_ascii=False, allow_nan=False)
        if indent is not None:
            fh.write('\n')
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- study.json

@dataclass
class StudyDay(Document):
    """One replayed UTC day. `tags` are typed by the user (workday, weekend, sport...), never inferred."""
    day: str
    role: Literal['search', 'holdout', 'quarantined'] = 'search'
    tags: list[str] = field(default_factory=list)
    quarantine_reason: Optional[str] = None

    def _check(self, problems):
        if not _DAY_RE.match(self.day):
            problems.append(f'day "{self.day}" is not a YYYY-MM-DD UTC date')


@dataclass
class SpaceParam(Document):
    """One searched parameter. float/int: low, high, step (step grid); bool: nothing; choice: choices; scale: low,
    high, step of a multiplier of the recorded profile value (1.0 on the grid, phone 1.0), target = the key itself."""
    type: Literal['float', 'int', 'bool', 'choice', 'scale']
    low: Optional[float] = None
    high: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[list[Any]] = None
    phone: Any = None                                    # the phone's current value (the centre of oaat), informational
    target: Optional[str] = None                         # scale: the '<array> <item>' the multiplier applies to

    def _check(self, problems, name='param'):
        if self.type == 'scale':
            if self.low is None or self.high is None or self.step is None:
                problems.append(f'{name}: scale needs low, high and step')
            else:
                if not 0 < self.low < self.high:
                    problems.append(f'{name}: scale needs 0 < low < high (got {self.low}, {self.high})')
                if not self.step > 0:
                    problems.append(f'{name}: step must be positive')
                elif not self.low - 1e-9 <= SCALE_PHONE <= self.high + 1e-9 or \
                        abs((SCALE_PHONE - self.low) / self.step - round((SCALE_PHONE - self.low) / self.step)) > 1e-6:
                    problems.append(f'{name}: scale grid must contain {SCALE_PHONE} (the phone value)')
            if self.choices:
                problems.append(f'{name}: scale takes no choices')
        elif self.type in ('float', 'int'):
            if self.low is None or self.high is None or self.step is None:
                problems.append(f'{name}: {self.type} needs low, high and step')
            else:
                if not self.low < self.high:
                    problems.append(f'{name}: low {self.low} must be below high {self.high}')
                if not self.step > 0:
                    problems.append(f'{name}: step must be positive')
                if self.type == 'int' and any(float(v) != int(v) for v in (self.low, self.high, self.step)):
                    problems.append(f'{name}: int parameter needs integer low/high/step')
        elif self.type == 'choice':
            if not self.choices:
                problems.append(f'{name}: choice needs a non-empty list of choices')
        elif self.type == 'bool' and self.choices:
            problems.append(f'{name}: bool takes no choices')

    def values(self):
        """The step grid (float/int/scale), [False, True] (bool) or the choices."""
        if self.type == 'bool':
            return [False, True]
        if self.type == 'choice':
            return list(self.choices or [])
        n = int(round((self.high - self.low) / self.step))
        vals = [self.low + i * self.step for i in range(n + 1)]
        if self.type == 'int':
            return [int(round(v)) for v in vals]
        digits = max(0, -int(f'{self.step:e}'.split('e')[1]) + 1)
        return [round(v, digits) for v in vals]


@dataclass
class Phase(Document):
    """One search phase; DESIGN.md section 5.4. Options not used by a kind stay None and are omitted."""
    kind: Literal['floor', 'oaat', 'grid', 'lhs', 'halving', 'coordinate', 'optuna', 'validate', 'cv', 'grow']
    state: Literal['pending', 'running', 'done', 'skipped', 'stopped'] = 'pending'
    points: Optional[int] = None                         # oaat: levels per parameter
    n: Optional[int] = None                              # lhs / optuna: points or trials
    seed: Optional[int] = None                           # lhs / optuna
    rungs: Optional[list[int | str]] = None              # halving: [2, 5, "all"]
    sampler: Optional[Literal['tpe', 'nsga2']] = None    # optuna
    top: Optional[int] = None                            # validate: top-N
    days: Optional[str] = None                           # validate: 'holdout'; oaat/lhs: 'screening'
    grid: Optional[dict[str, list[Any]]] = None          # grid: imported --from-grid parameters

    def _check(self, problems):
        for attr in ('points', 'n', 'top'):
            v = getattr(self, attr)
            if v is not None and v < 1:
                problems.append(f'phase {self.kind}: {attr} must be >= 1')
        if self.kind == 'optuna' and self.sampler is None:
            problems.append('phase optuna: sampler (tpe | nsga2) required')


@dataclass
class Budget(Document):
    hours: float = 6.0
    workers: int = 4

    def _check(self, problems):
        if not self.hours > 0:
            problems.append('budget.hours must be positive')
        if self.workers < 1:
            problems.append('budget.workers must be >= 1')


@dataclass
class Objective(Document):
    """Weights and envelopes of DESIGN.md section 5; shown on the page, never hidden."""
    scalar_weights: dict[str, float] = field(default_factory=lambda: {'rest': 2.0, 'lowside': 0.3})
    envelope: dict[str, float] = field(default_factory=lambda: {'max_3h_U': 1.0, 'max_day_pct_tdd': 15.0})
    feasible: dict[str, float] = field(default_factory=lambda: {'cap_binding_max_pct': 50.0})
    quarantine: dict[str, float] = field(default_factory=lambda: {'agreement_pct_min': 94.0, 'cgm_gap_max_min': 120.0})


@dataclass
class Study(Document):
    """studies/<study>/study.json (DESIGN.md section 4.5)."""
    name: str
    created: str
    source: str
    host: str
    settings_sha: str
    code_sha: str
    days: list[StudyDay]
    space: dict[str, SpaceParam]
    objective_version: int = 1
    contract_version: int = CONTRACT_VERSION
    core_versions: dict[str, str] = field(default_factory=dict)
    constraints: list[list[str]] = field(default_factory=list)
    base_vdf_lines: list[str] = field(default_factory=list)
    question: Literal['explore', 'earlier', 'gentler'] = 'explore'
    objective: Objective = field(default_factory=Objective)
    phases: list[Phase] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    sentinel_every: int = 25
    outputs: str = 'seconds/bg/iob/autosens/acce/bg_ISF/pp/dura/ISFs/insReq/SMB/basal'
    private_repo: bool = False                           # relaxes the GitHub privacy lint to "no transcripts"
    notes: list[str] = field(default_factory=list)
    unlocked: list[str] = field(default_factory=list)    # locked keys this study may vary ('<array> <item>'), audited
    unlock_reason: Optional[str] = None                  # required, non-empty, when `unlocked` is

    def unlock_caveat(self):
        """The weak-evidence text every surface shows for a study with unlocked keys, or None."""
        if not self.unlocked:
            return None
        return WEAK_EVIDENCE % (', '.join(self.unlocked), (self.unlock_reason or '').strip())

    @classmethod
    def new(cls, name, days, space, source='ns:', host='', question='explore', phases=None, budget=None,
            constraints=None, base_vdf_lines=None, objective_version=1, settings_path=NS_SETTINGS_PATH,
            software_dir=SOFTWARE_DIR, **extra):
        """Fill the stamps (created, settings_sha, code_sha, core_versions) and build a Study (not yet validated)."""
        if not host and os.path.isfile(settings_path):
            try:
                with open(settings_path, 'r', encoding='utf-8') as fh:
                    url = json.load(fh)['nightscout']['url'] or ''
                host = url.split('://', 1)[-1].split('/', 1)[0]
            except (OSError, KeyError, ValueError, TypeError):
                host = ''
        days = [d if isinstance(d, StudyDay) else StudyDay(**d) if isinstance(d, dict) else StudyDay(day=str(d))
                for d in days]
        space = {k: (v if isinstance(v, SpaceParam) else SpaceParam(**v)) for k, v in space.items()}
        phases = [p if isinstance(p, Phase) else Phase(**p) for p in (phases or [])]
        try:
            versions = core_versions()
        except Exception:                                # pragma: no cover - the core refuses to import
            versions = {}
        return cls(name=name, created=now_iso(), source=source, host=host,
                   settings_sha=settings_sha(settings_path) if os.path.isfile(settings_path) else '',
                   code_sha=code_sha(software_dir), days=days, space=space, objective_version=objective_version,
                   core_versions=versions, constraints=list(constraints or []),
                   base_vdf_lines=list(base_vdf_lines or []), question=question, phases=phases,
                   budget=budget if isinstance(budget, Budget) else Budget(**(budget or {})), **extra)

    def _check(self, problems, settings=None):
        if not _NAME_RE.match(self.name):
            problems.append(f'name "{self.name}" must be a short file-system safe label')
        if self.objective_version < 1:
            problems.append('objective_version must be >= 1')
        if self.sentinel_every < 1:
            problems.append('sentinel_every must be >= 1')
        if not self.days:
            problems.append('days: at least one day')
        seen = set()
        for d in self.days:
            d._check(problems)
            if d.day in seen:
                problems.append(f'day {d.day} listed twice')
            seen.add(d.day)
        if not self.space:
            problems.append('space: at least one parameter')
        if self.unlocked and not (self.unlock_reason or '').strip():
            problems.append('unlocked lists ' + ', '.join(self.unlocked) + ' but unlock_reason is empty '
                            '(an unlock is explicit and audited: say why)')
        seen_u = set()
        for name in self.unlocked:
            why = unlock_refusal(name)
            if why:
                problems.append(f'unlocked: {why}')
            if name in seen_u:
                problems.append(f'unlocked: "{name}" listed twice')
            seen_u.add(name)
        unlocked = self.unlocked if (self.unlock_reason or '').strip() else ()
        for name, p in self.space.items():
            reason = locked_reason(name, settings, unlocked)
            if reason:
                problems.append(f'locked key {reason}')
            p._check(problems, name=f'space "{name}"')
            problems.extend(scale_problems(name, p))
        for c in self.constraints:
            if len(c) < 3 or len(c) % 2 == 0:
                problems.append(f'constraint {c}: expected "<key> <op> <key> [<op> <key>...]"')
                continue
            for i, tok in enumerate(c):
                if i % 2 == 1:
                    if tok not in ('<=', '<'):
                        problems.append(f'constraint {c}: operator "{tok}" must be <= or <')
                elif tok not in self.space:
                    problems.append(f'constraint {c}: "{tok}" is not a searched parameter')
        for ph in self.phases:
            ph._check(problems)
        self.budget._check(problems)
        for line in self.base_vdf_lines:
            if vdf_row_name(line) in self.space:
                problems.append(f'base VDF line "{line.strip()}" sets a searched parameter')
            reason = locked_vdf_row_reason(line, settings, unlocked)
            if reason:
                problems.append(f'locked key in base VDF line "{line.strip()}": {reason}')

    def validate(self, settings=None):
        problems = self.problems()
        unlocked = self.unlocked if (self.unlock_reason or '').strip() else ()
        if settings is not None:                          # re-run the lock checks with the settings-derived safety list
            for name in self.space:
                reason = locked_reason(name, settings, unlocked)
                if reason and not any(reason in p for p in problems):
                    problems.append(f'locked key {reason}')
            for line in self.base_vdf_lines:
                reason = locked_vdf_row_reason(line, settings, unlocked)
                if reason and not any(reason in p for p in problems):
                    problems.append(f'locked key in base VDF line "{line.strip()}": {reason}')
        if problems:
            raise ContractError('Study: ' + '\n'.join(problems))
        return self

    def search_days(self):
        return [d.day for d in self.days if d.role == 'search']

    def holdout_days(self):
        return [d.day for d in self.days if d.role == 'holdout']


# ----------------------------------------------------------------------------- trials.jsonl

@dataclass
class TrialRow(Document):
    """One line of trials.jsonl: one (variant, day) evaluation. `metrics` keys belong to metrics.py."""
    trial: int
    key: str
    phase: str
    vhash: str
    variant: str
    params: dict[str, Any]
    day: str
    status: Literal['done', 'failed', 'blocked', 'stale', 'running']
    code_sha: str
    settings_sha: str
    freeze_sha: str
    seconds: Optional[float] = None
    worker: Optional[int] = None
    rung: Optional[int] = None
    round: Optional[int] = None
    started: Optional[str] = None
    finished: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)
    feasible: Optional[bool] = None
    flags: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None
    control: Optional[Literal['base', 'sentinel', 'null']] = None      # the automatic controls of section 5.1

    def _check(self, problems):
        if self.trial < 0:
            problems.append('trial must be >= 0')
        for attr in ('key', 'vhash'):
            if not _SHA_RE.match(getattr(self, attr)):
                problems.append(f'{attr} must be a 40-character sha1 hex')
        if not _DAY_RE.match(self.day):
            problems.append(f'day "{self.day}" is not a YYYY-MM-DD UTC date')
        if self.vhash != vhash(self.params):
            problems.append('vhash does not match params')
        if self.status == 'failed' and not self.error:
            problems.append('a failed trial needs an error text')
        if self.status == 'done' and not self.metrics:
            problems.append('a done trial needs metrics')
        for name in self.params:
            try:
                split_param(name)
            except ContractError as e:
                problems.append(str(e))

    def to_jsonl_line(self):
        """Compact JSON plus newline; < 4 KB (contract), else ContractError."""
        line = self.to_compact_json() + '\n'
        if len(line.encode('utf-8')) > TRIAL_LINE_MAX_BYTES:
            raise ContractError(f'trial {self.trial}: jsonl line exceeds {TRIAL_LINE_MAX_BYTES} bytes')
        return line

    def make_key(self, objective_version):
        return key(self.params, self.day, self.settings_sha, self.code_sha, self.freeze_sha, objective_version)


def append_trial(path, row):
    """Append one row with O_APPEND and a single write() (the append-only rule of DESIGN.md section 4.5)."""
    data = row.to_jsonl_line().encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read_trials(path, strict=True):
    """-> list[TrialRow]; a truncated last line (kill -9 mid-write) is skipped, never an error."""
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, 'r', encoding='utf-8') as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(TrialRow.from_dict(json.loads(line), strict=strict, path=f'trials.jsonl:{n}'))
            except json.JSONDecodeError:
                continue                                 # partial line at the end after a crash
    return rows


# ----------------------------------------------------------------------------- STATE.json

@dataclass
class State(Document):
    """studies/<study>/STATE.json, rewritten by atomic rename after every trial (the run's heartbeat)."""
    phase: str
    trials_done: int
    queue_len: int
    pid: int
    heartbeat: str
    stale_context: list[str] = field(default_factory=list)
    last_key: Optional[str] = None
    budget_left_s: Optional[float] = None
    disk_free_mb: Optional[float] = None
    started: Optional[str] = None
    round: Optional[int] = None
    exit_code: Optional[int] = None
    stop_reason: Optional[str] = None

    def _check(self, problems):
        if self.trials_done < 0 or self.queue_len < 0:
            problems.append('trials_done and queue_len must be >= 0')
        if self.exit_code is not None and self.exit_code not in (0, 2, 3):
            problems.append('exit_code must be 0 (finished/stopped), 2 (guard) or 3 (configuration refused)')


# ----------------------------------------------------------------------------- status.json / round.json

@dataclass
class Candidate(Document):
    """One variant as the skill and the page see it: verdict + why + the components of DESIGN.md section 5.2."""
    vhash: str
    variant: str
    verdict: Literal['pending', 'base', 'dead', 'candidate', 'tie', 'infeasible', 'recommended']
    why: str
    params: dict[str, Any] = field(default_factory=dict)
    rank: Optional[int] = None
    days: int = 0
    feasible: Optional[bool] = None
    effect: dict[str, float] = field(default_factory=dict)          # rise_vs_base_U, rest_vs_base_U, changed_vs_base, ...
    flags: list[str] = field(default_factory=list)
    tie_with: list[str] = field(default_factory=list)               # vhashes it cannot be told from

    def _check(self, problems):
        if not _SHA_RE.match(self.vhash):
            problems.append(f'candidate {self.variant}: vhash must be a 40-character sha1 hex')
        if not self.why:
            problems.append(f'candidate {self.variant}: why must not be empty')


@dataclass
class TopItem(Document):
    """A candidate as status.json carries it (slim: no params, the variant label names them; <= 4 effect numbers;
    `vhash` may be the 12-character short form, round.json and trials.jsonl hold the full hash)."""
    vhash: str
    variant: str
    verdict: Literal['pending', 'base', 'dead', 'candidate', 'tie', 'infeasible', 'recommended']
    why: str
    rank: Optional[int] = None
    effect: dict[str, float] = field(default_factory=dict)          # e.g. rise_vs_base_U, rest_vs_base_U, changed_vs_base

    def _check(self, problems):
        if not _SHORT_SHA_RE.match(self.vhash):
            problems.append(f'top {self.variant}: vhash must be a sha1 hex (12 or 40 characters)')
        if not self.why or len(self.why) > 120:
            problems.append(f'top {self.variant}: why must be 1..120 characters')
        if len(self.effect) > 4:
            problems.append(f'top {self.variant}: at most 4 effect numbers in status.json')


@dataclass
class DeadParameter(Document):
    key: str
    why: str
    floor_ratio: Optional[float] = None                  # max |effect| / floor over the screening days


@dataclass
class Status(Document):
    """studies/<study>/status.json: <= 2 KB, the skill's whole view of a run."""
    study: str
    question: str
    phase: str
    trials_done: int
    trials_planned: int
    running: bool
    heartbeat: str
    trials_failed: int = 0
    trials_blocked: int = 0
    pid: Optional[int] = None
    round: Optional[int] = None
    stale_context: list[str] = field(default_factory=list)
    floor: dict[str, float] = field(default_factory=dict)                 # over all days: max |base - phone| per component
                                                                          # (smb_U, ins_req_U, tbr_U, loops) + min agreement_pct
    days: dict[str, str] = field(default_factory=dict)                    # day -> 'search' | 'holdout' | 'quarantined: <why>'
    dead_parameters: list[str] = field(default_factory=list)
    top: list[TopItem] = field(default_factory=list)                      # review order (explore) or front, <= 5
    exit_code: Optional[int] = None
    stop_reason: Optional[str] = None
    report: Optional[str] = None                         # path of report/index.html
    artifact_url: Optional[str] = None
    proposals: dict[str, str] = field(default_factory=dict)              # vhash -> GitHub label state

    def _check(self, problems):
        if len(self.top) > 5:
            problems.append('top: at most 5 candidates (the skill reads this file into context)')
        for c in self.top:
            c._check(problems)
        size = len(self.to_compact_json().encode('utf-8'))
        if size > STATUS_MAX_BYTES:
            problems.append(f'status.json is {size} bytes, limit {STATUS_MAX_BYTES}')


@dataclass
class Round(Document):
    """studies/<study>/round.json: the variants of one round with verdicts; <= 2 KB per variant."""
    round: int
    phase: str
    started: str
    finished: Optional[str] = None
    trials: int = 0
    variants: list[Candidate] = field(default_factory=list)
    dead_parameters: list[DeadParameter] = field(default_factory=list)
    floors: dict[str, dict[str, float]] = field(default_factory=dict)
    front: list[str] = field(default_factory=list)                        # vhashes in review order / Pareto front
    notes: list[str] = field(default_factory=list)

    def _check(self, problems):
        if self.round < 0:
            problems.append('round must be >= 0')
        for c in self.variants:
            c._check(problems)
        limit = ROUND_MAX_BYTES_PER_VARIANT * max(1, len(self.variants))
        size = len(self.to_compact_json().encode('utf-8'))
        if size > limit:
            problems.append(f'round.json is {size} bytes for {len(self.variants)} variants, limit {limit}')


DOCUMENTS = {'study': Study, 'trial_row': TrialRow, 'state': State, 'status': Status, 'round': Round}


# ----------------------------------------------------------------------------- JSON Schema generation

def _schema_of(t, defs):
    t, optional = _strip_optional(t)
    if t is Any:
        return {}
    origin = typing.get_origin(t)
    if origin is Literal:
        s = {'enum': list(typing.get_args(t))}
    elif origin is typing.Union or origin is types.UnionType:
        s = {'anyOf': [_schema_of(a, defs) for a in typing.get_args(t) if a is not type(None)]}
    elif origin in (list, tuple):
        args = typing.get_args(t)
        s = {'type': 'array', 'items': _schema_of(args[0], defs) if args else {}}
    elif origin is dict:
        args = typing.get_args(t)
        s = {'type': 'object', 'additionalProperties': _schema_of(args[1], defs) if len(args) == 2 else {}}
    elif _is_dataclass_type(t):
        if t.__name__ not in defs:
            defs[t.__name__] = None                      # placeholder against recursion
            defs[t.__name__] = _object_schema(t, defs)
        s = {'$ref': f'#/$defs/{t.__name__}'}
    elif t is bool:
        s = {'type': 'boolean'}
    elif t is int:
        s = {'type': 'integer'}
    elif t is float:
        s = {'type': 'number'}
    elif t is str:
        s = {'type': 'string'}
    else:
        raise TypeError(f'no JSON schema for {t!r}')
    if optional:
        s = {'anyOf': [s, {'type': 'null'}]} if '$ref' in s or 'anyOf' in s or 'enum' in s else dict(s, type=[s['type'], 'null'])
    return s


def _object_schema(cls, defs):
    hints = typing.get_type_hints(cls)
    props, required = {}, []
    for f in dataclasses.fields(cls):
        s = _schema_of(hints[f.name], defs)
        if f.default is not dataclasses.MISSING and f.default is not None and not dataclasses.is_dataclass(f.default):
            s = dict(s, default=f.default)
        props[f.name] = s
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            required.append(f.name)
    out = {'type': 'object', 'properties': props, 'additionalProperties': False}
    if cls.__doc__:
        out['description'] = ' '.join(cls.__doc__.split())
    if required:
        out['required'] = required
    return out


def json_schema(cls):
    """JSON Schema (draft 2020-12) of a document dataclass, nested dataclasses under $defs."""
    defs = {}
    top = _object_schema(cls, defs)
    schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
              '$id': f'https://github.com/markusbaumg/APS-what-if/software/hindcast/schemas/{_doc_name(cls)}.schema.json',
              'title': cls.__name__, 'x-contract-version': CONTRACT_VERSION}
    schema.update(top)
    if defs:
        schema['$defs'] = dict(sorted(defs.items()))
    return schema


def _doc_name(cls):
    for name, c in DOCUMENTS.items():
        if c is cls:
            return name
    return cls.__name__.lower()


def schema_text(cls):
    return json.dumps(json_schema(cls), indent=1, ensure_ascii=False, sort_keys=False) + '\n'


def write_schemas(directory=SCHEMAS_DIR):
    """Write schemas/<doc>.schema.json for every document; returns the written paths."""
    os.makedirs(directory, exist_ok=True)
    paths = []
    for name, cls in DOCUMENTS.items():
        path = os.path.join(directory, f'{name}.schema.json')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(schema_text(cls))
        paths.append(path)
    return paths


def stale_schemas(directory=SCHEMAS_DIR):
    """Names of the schema files that differ from what the dataclasses generate (or are missing)."""
    stale = []
    for name, cls in DOCUMENTS.items():
        path = os.path.join(directory, f'{name}.schema.json')
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                current = fh.read()
        except OSError:
            current = None
        if current != schema_text(cls):
            stale.append(name)
    return stale


if __name__ == '__main__':
    if '--write-schemas' in sys.argv:
        for p in write_schemas():
            print('wrote', p)
    else:
        print(__doc__)
        print('code_sha    ', code_sha())
        print('settings_sha', settings_sha() if os.path.isfile(NS_SETTINGS_PATH) else '(no ns_settings.json)')
        print('stale schemas:', stale_schemas() or 'none')
