"""
test_metrics.py  -  H-201: the per-trial metrics on the five fixture CSVs and on synthetic series

The fixture CSVs (results/example_2026-09-06/, git-ignored) must reproduce the documented table of
NIGHTSCOUT_SOURCE.md: base +1.37 U, dura_ISF_weight 0.5 -> -0.4 U vs base all in rise, 9 loops with a
smaller SMB, @max 9.7 % -> 9.4 %, base identical to autoISF_max_1.3__dura_ISF_weight_0.7.  P and E are
tested on constructed 1-min series exactly as the backlog states them.
"""
import json
import math
import os

import pytest

from hindcast import metrics as M
from hindcast.tests.conftest import EXAMPLE_VARIANTS

MIN = 60.0
V05_13 = 'autoISF_max_1.3__dura_ISF_weight_0.5'
V07_13 = 'autoISF_max_1.3__dura_ISF_weight_0.7'
V05_15 = 'autoISF_max_1.5__dura_ISF_weight_0.5'
V07_15 = 'autoISF_max_1.5__dura_ISF_weight_0.7'


def assignment(variant):
    if variant == 'base':
        return []
    a, d = variant.split('__')
    return [('profile autoISF_max', float(a.rsplit('_', 1)[1])), ('profile dura_ISF_weight', float(d.rsplit('_', 1)[1]))]


@pytest.fixture(scope='module')
def settings(ns_settings_path):
    with open(ns_settings_path, encoding='utf-8') as fh:
        return json.load(fh)


@pytest.fixture(scope='module')
def example_metrics(example_csvs, settings):
    """{variant: trial_metrics} of the five fixture CSVs, caps per loop via effective_cap (profile_history aware)"""
    base = M.LoopSeries.from_csv(example_csvs['base'])
    out = {}
    for v, path in example_csvs.items():
        a = assignment(v)
        out[v] = M.trial_metrics(path, None if v == 'base' else base,
                                 M.effective_cap(a, [], settings, 'autoISF_max'),
                                 M.effective_cap(a, [], settings, 'autoISF_min'))
    return out


# ----------------------------------------------------------------------------- the documented table
def test_objective_version_is_one():
    assert M.OBJECTIVE_VERSION == 1


def test_optimizer_metrics_are_imported_not_copied():
    import emulator_optimize
    assert M.metrics_from_csv is emulator_optimize.metrics_from_csv
    assert M.read_result_csv is emulator_optimize.read_result_csv
    assert M.effective_cap is emulator_optimize.effective_cap


def test_documented_table(example_metrics):
    m = example_metrics
    assert all(m[v]['loops'] == 340 for v in EXAMPLE_VARIANTS)
    assert round(m['base']['delta_U'], 2) == 1.37                      # the replay bias
    assert round(m[V05_13]['delta_vs_base_U'], 1) == -0.4             # dura 0.5 asks for 0.4 U less than base ...
    assert round(m[V05_13]['rise_vs_base_U'], 2) == -0.36             # ... in the rising phase (4.86 -> 4.51 U)
    assert abs(m[V05_13]['rest_vs_base_U']) < M.TOL_U                 # the rest moves less than one bolus increment
    assert m[V05_13]['smb_less'] == 9                                  # 9 loops with a smaller SMB than the phone
    assert m[V05_13]['smb_more_vs_base'] == 0
    assert round(m['base']['at_autoISF_max_pct'], 1) == 9.7           # factor on the 1.3 cap (profile_history)
    assert round(m[V05_13]['at_autoISF_max_pct'], 1) == 9.4
    assert round(m[V05_15]['at_autoISF_max_pct'], 1) == 3.8           # reaches 1.5 in 3.8 %
    assert round(m[V07_15]['delta_vs_base_U'], 2) == 0.01             # autoISF_max 1.3 -> 1.5 changes almost nothing


def test_base_identical_to_phone_settings_variant(example_metrics):
    """1.3 / 0.7 were the phone's settings that morning: the consistency check that no state leaks between runs"""
    b, v = example_metrics['base'], example_metrics[V07_13]
    for key, val in b.items():
        if key in ('flags', 'is_base'):
            continue
        if isinstance(val, float):
            assert v[key] == pytest.approx(val, abs=1e-9), key
        else:
            assert v[key] == val, key
    assert v['changed_vs_base_loops'] == 0
    assert v['delta_vs_base_U'] == 0.0 and v['rise_vs_base_U'] == 0.0 and v['rest_vs_base_U'] == 0.0


def test_matches_summary_json(example_metrics, example_summary):
    """every optimizer metric equals summary.json (metrics_from_csv is the optimizer's), delta_vs_base_U too"""
    for r in example_summary['results']:
        ours = example_metrics[r['variant']]
        for key, val in r['metrics'].items():
            if key == 'seconds':
                continue
            if isinstance(val, float):
                assert ours[key] == pytest.approx(val, abs=1e-6), (r['variant'], key)
            else:
                assert ours[key] == val, (r['variant'], key)


def test_base_metrics_shape(example_metrics):
    b = example_metrics['base']
    assert b['is_base'] is True
    assert b['prehypo_extra_U'] == 0 and b['env_3h_max_U'] == 0 and b['env_day_pct_tdd'] == 0
    assert b['changed_vs_base'] == 0 and b['changed_loops'] == b['changed_loops_base'] > 0
    assert b['lows_recorded'] == 0                                     # no recorded low that morning
    assert 90 < b['agreement_pct'] <= 100 and b['smb_exact_pct'] == pytest.approx(100 * 314 / 340)
    assert b['cap_binding_pct'] == pytest.approx(b['at_autoISF_max_pct'] + b['at_autoISF_min_pct'])
    assert b['max_single_smb_U'] == 0.25 and b['smb_over_maxbolus_loops'] == 0
    assert b['tt_smb_off_pct'] is None
    for flag in ('lowside_no_pred', 'tdd_from_window', 'zero_temp_rate_proxy', 'tt_smb_off_unknown'):
        assert flag in b['flags']
    assert 'cap_unknown' not in b['flags'] and 'maxbolus_unknown' not in b['flags']


def test_tdd_argument_and_cap_unknown(example_csvs):
    base = M.LoopSeries.from_csv(example_csvs['base'])
    m = M.trial_metrics(example_csvs[V05_13], base, tdd_phone_U=30.0)
    assert 'tdd_from_window' not in m['flags'] and m['tdd_phone_U'] == 30.0
    assert m['env_day_pct_tdd'] == pytest.approx(100 * abs(m['env_day_U']) / 30.0)
    assert m['cap_binding_pct'] is None and 'cap_unknown' in m['flags']


def test_loop_count_mismatch_raises(example_csvs, no_change_csv):
    base = M.LoopSeries.from_csv(no_change_csv)                       # 180 loops vs 340
    with pytest.raises(ValueError, match='loop counts differ'):
        M.trial_metrics(example_csvs[V05_13], base)


def test_base_as_path(example_csvs):
    m = M.trial_metrics(example_csvs[V05_13], example_csvs['base'])
    assert round(m['delta_vs_base_U'], 1) == -0.4


# ----------------------------------------------------------------------------- synthetic series
def series(n, step_s=MIN, bg=120.0, **cols):
    t = [i * step_s for i in range(n)]
    bgs = [bg] * n if isinstance(bg, (int, float)) else list(bg)
    return M.LoopSeries.from_arrays(t, bgs, **cols)


def test_loop_intervals_cap_and_median():
    dt = M.loop_intervals_h([0, 60, 120, 7200, 7260])
    assert dt[0] == pytest.approx(1 / 60) and dt[2] == 0.5            # the 118 min gap is capped at 30 min
    assert dt[-1] == pytest.approx(1 / 60)                            # last loop: median interval (60 s)
    assert M.loop_intervals_h([]) == []


def test_prehypo_P_backlog_example():
    """a +0.5 U SMB 90 min before a 3.5 mmol reading -> P = 0.5"""
    n = 181                                                           # 0..180 min, 1-min loops
    bg = [120.0] * n
    bg[150] = 3.5 * M.MMOL_TO_MGDL                                    # 63 mg/dl at minute 150
    smb_e = [0.0] * n
    smb_e[60] = 0.5                                                   # 90 min before the low: inside the 120 min window
    s = series(n, bg=bg, smb_emul=smb_e)
    base = series(n, bg=bg)
    assert M.prehypo_extra_U(s.t, s.bg, s.give_emul, base.give_emul) == pytest.approx(0.5)
    m = M.trial_metrics(None, base, series=s)
    assert m['prehypo_extra_U'] == pytest.approx(0.5)
    assert m['lows_recorded'] == 1 and m['prehypo_loops'] == 120      # minutes 30..149
    # outside the window, a reduction, or the base giving the same: nothing counted
    smb_far = [0.0] * n
    smb_far[20] = 0.5                                                 # 130 min before
    assert M.prehypo_extra_U(s.t, s.bg, series(n, bg=bg, smb_emul=smb_far).give_emul, base.give_emul) == 0
    assert M.prehypo_extra_U(s.t, s.bg, base.give_emul, s.give_emul) == 0          # less insulin is not penalised
    assert M.prehypo_extra_U(s.t, s.bg, s.give_emul, s.give_emul) == 0
    assert M.prehypo_extra_U(s.t, [120.0] * n, s.give_emul, base.give_emul) == 0   # no recorded low: P is 0


def test_prehypo_uses_tbr_give_too():
    n = 121
    bg = [120.0] * n
    bg[-1] = 60.0
    s = series(n, bg=bg, rate_emul=[0.6] * n)                         # 0.6 U/h for 2 h = 1.2 U before the low
    base = series(n, bg=bg)
    assert M.prehypo_extra_U(s.t, s.bg, s.give_emul, base.give_emul) == pytest.approx(1.2, abs=0.02)


def test_envelope_backlog_example():
    """a day-long +0.02 U/loop drift on 1-min loops -> env_3h_max_U = 3.6 (180 loops x 0.02 U)"""
    n = 1440
    s = series(n, smb_emul=[0.02] * n)
    base = series(n)
    e = M.envelope(s.t, s.give_emul, base.give_emul, tdd_ref_U=40.0)
    assert e['env_3h_max_U'] == pytest.approx(3.6)
    assert e['env_3h_signed_U'] == pytest.approx(3.6)
    assert e['env_day_U'] == pytest.approx(28.8)
    assert e['env_day_pct_tdd'] == pytest.approx(72.0)
    m = M.trial_metrics(None, base, series=s, tdd_phone_U=40.0)
    assert m['env_3h_max_U'] == pytest.approx(3.6) and m['env_day_pct_tdd'] == pytest.approx(72.0)
    assert M.feasibility_day(m, M.trial_metrics(None, None, series=base), M.floors_from_base(M.trial_metrics(None, None, series=base)))


def test_envelope_is_two_sided_and_windowed():
    n = 600                                                           # 10 h of 1-min loops
    minus = [0.0] * n
    minus[100] = -1.0
    plus = [0.0] * n
    plus[100] = 1.0
    plus[400] = -1.0                                                  # 5 h apart: never in one 3 h window
    base = series(n)
    e = M.envelope(base.t, series(n, smb_emul=minus).give_emul, base.give_emul, None)
    assert e['env_3h_max_U'] == pytest.approx(1.0) and e['env_3h_signed_U'] == pytest.approx(-1.0)
    assert e['env_day_pct_tdd'] is None                               # TDD unknown
    e = M.envelope(base.t, series(n, smb_emul=plus).give_emul, base.give_emul, 20.0)
    assert e['env_3h_max_U'] == pytest.approx(1.0) and e['env_day_U'] == pytest.approx(0.0)
    assert e['env_day_pct_tdd'] == pytest.approx(0.0)
    plus[250] = -1.0                                                  # 150 min after the +1: inside one window -> cancels
    e = M.envelope(base.t, series(n, smb_emul=plus).give_emul, base.give_emul, 20.0)
    assert e['env_3h_max_U'] == pytest.approx(2.0)                   # ... but the window 221..400 holds both -1 U
    plus[400] = 0.0
    plus[450] = -1.0                                                  # 200 min apart: no 3 h window holds both
    e = M.envelope(base.t, series(n, smb_emul=plus).give_emul, base.give_emul, 20.0)
    assert e['env_3h_max_U'] == pytest.approx(1.0) and e['env_day_U'] == pytest.approx(-1.0)


def test_hindsight_covariates_positional():
    n = 400
    bg = [120.0] * n
    bg[300] = 65.0                                                    # a low at minute 300
    bg[20] = 200.0                                                    # a high at minute 20
    cov = M.hindsight_covariates([i * MIN for i in range(n)], bg)
    assert cov['before_low_2h'][180] == 1 and cov['before_low_2h'][179] == 0 and cov['before_low_2h'][299] == 1
    assert cov['before_low_2h'][300] == 0                             # the low itself is not "before"
    assert cov['low_within_2h'] == cov['before_low_2h']
    assert cov['high_within_3h'][0] == 1 and cov['high_within_3h'][20] == 0 and cov['high_within_3h'][21] == 0
    assert sum(cov['before_low_2h']) == 120


def test_changed_flags_and_agreement():
    n = 10
    s = series(n, smb_phone=[0.1] * n, smb_emul=[0.1] * 8 + [0.2, 0.1],
               insreq_phone=[0.5] * n, insreq_emul=[0.5] * 9 + [0.56],
               rate_phone=[0.0] * n, rate_emul=[0.0] * 7 + [0.05, 0.0, 0.0],      # exactly the tolerance: not changed
               isf_phone=[80.0] * n, isf_emul=[80.0] * 9 + [81.5])
    ch = M.changed_flags(s)
    assert ch == [False] * 8 + [True, True]
    a = M.agreement(s)
    assert a['smb_exact_pct'] == 90 and a['smb_within_pct'] == 90 and a['insreq_within_pct'] == 90
    assert a['rate_within_pct'] == 100 and a['isf_within_pct'] == 90 and a['agreement_pct'] == 80
    other = series(n, smb_emul=[0.1] * n, insreq_emul=[0.5] * n)
    assert M.changed_flags(s, other) == [False] * 8 + [True, True]    # vs another emulated series: SMB 0.2, insReq 0.56


def test_lowside_flags_recorded_rule_and_min_guard():
    n = 12
    bg = [100.0] * 6 + [88.0, 86.0, 84.0, 82.0, 80.0, 78.0]           # below 5 mmol and falling from loop 6 on
    smb_e = [0.0] * n
    smb_e[2] = 0.1                                                    # bg 100: not low side
    smb_e[8] = 0.1                                                    # bg 84 falling: low side
    s = series(n, step_s=300, bg=bg, smb_emul=smb_e)
    base = series(n, step_s=300, bg=bg)
    m = M.trial_metrics(None, base, series=s)
    assert m['lowside_flags'] == 1 and m['lowside_rule'] == 'recorded_bg_only' and 'lowside_no_pred' in m['flags']
    guard = [None] * n
    guard[2] = 65.0                                                   # emulated minGuardBG below 3.9 mmol at loop 2
    m = M.trial_metrics(None, base, series=s, min_guard=guard)
    assert m['lowside_flags'] == 2 and m['lowside_rule'] == 'pred_partial' and 'lowside_pred_partial' in m['flags']
    m = M.trial_metrics(None, base, series=s, min_guard=[150.0] * 2 + [65.0] + [150.0] * 9)
    assert m['lowside_flags'] == 2 and m['lowside_rule'] == 'pred_and_recorded_bg'
    assert not any(f.startswith('lowside') for f in m['flags'])
    # an SMB smaller than the phone's is never a low-side flag
    s2 = series(n, step_s=300, bg=bg, smb_phone=[0.2] * n, smb_emul=[0.1] * n)
    assert M.trial_metrics(None, series(n, step_s=300, bg=bg), series=s2)['lowside_flags'] == 0


def test_min_guard_helpers():
    assert M.min_guard_from_levels(None) is None
    assert M.min_guard_from_levels({'minPredBG': 90}) is None        # SMB enabled: no guard stored
    assert M.min_guard_from_levels({'minGuardBG1': 66, 'minGuardBG': 70}) == 70.0
    assert M.min_guard_from_levels({'minGuardBG1': 66}) == 66.0
    reason = 'COB: 0, Dev: 1.2, BGI: -0.5, ISF: 4.3, CR: 10, Target: 5.6, minPredBG 5.1, minGuardBG 3.6, IOBpredBG 5.9; '
    assert M.min_guard_from_reason(reason, 'mmol/L') == pytest.approx(3.6 * 18)
    assert M.min_guard_from_reason(reason.replace('3.6', '86'), 'mg/dl') == 86.0
    assert M.min_guard_from_reason('minGuardBG 3.4<3.8', 'mmol/L') == pytest.approx(3.4 * 18)
    assert M.min_guard_from_reason('no guard here') is None


def test_zero_temp_explicit_and_proxy():
    n = 6
    base = series(n, rate_emul=[0.5, 0.5, 0.0, 0.0, 0.5, 0.5])
    s = series(n, rate_emul=[0.0, 0.5, 0.0, 0.5, 0.0, 0.5])
    m = M.trial_metrics(None, base, series=s)
    assert m['zero_temp_vs_base'] == 2 - 1 and 'zero_temp_rate_proxy' in m['flags']    # loops 0, 4 vs loop 3
    s.zero_temp = [True, False, True, False, False, False]
    base.zero_temp = [False, False, True, True, False, False]
    m = M.trial_metrics(None, base, series=s)
    assert m['zero_temp_vs_base'] == 1 - 1 and 'zero_temp_rate_proxy' not in m['flags']


def test_maxbolus_and_tt_smb_off():
    n = 4
    s = series(n, smb_emul=[0.3, 0.8, 0.0, 0.9], maxbolus_phone=[0.7, 0.7, 0.0, 0.0])
    m = M.trial_metrics(None, None, series=s)
    assert m['max_single_smb_U'] == 0.9 and m['smb_over_maxbolus_loops'] == 1 and m['maxbolus_phone_min_U'] == 0.7
    s = series(n, smb_emul=[0.3, 0.8, 0.0, 0.9])
    m = M.trial_metrics(None, None, series=s, smb_off=[True, False, False, True])
    assert m['smb_over_maxbolus_loops'] is None and 'maxbolus_unknown' in m['flags']
    assert m['tt_smb_off_pct'] == 50.0 and 'tt_smb_off_unknown' not in m['flags']


def test_per_loop_input_length_checked():
    with pytest.raises(ValueError):
        series(5, min_guard=[1.0, 2.0])
    with pytest.raises(ValueError):
        M.LoopSeries.from_arrays([0, 60], [100, 100], smb_emul=[0.1])


def test_empty_series():
    s = M.LoopSeries.from_arrays([], [])
    assert M.trial_metrics(None, None, series=s) == {'loops': 0, 'flags': ['no_loops']}
    assert M.agreement(s)['agreement_pct'] is None


def test_from_csv_columns(no_change_csv):
    s = M.LoopSeries.from_csv(no_change_csv)
    assert s.n == 180 and len(s.dt_h) == 180 and len(s.give_emul) == 180
    assert all(v is None for v in s.min_guard) and s.zero_temp is None and s.smb_off is None
    assert 80 < min(s.bg) and max(s.bg) < 400                         # mg/dl
    m = M.trial_metrics(no_change_csv)
    assert m['loops'] == 180 and m['is_base'] is True
