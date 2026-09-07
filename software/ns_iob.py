"""
ns_iob.py  -  AAPS insulin-on-board calculation from Nightscout treatments.

Contract (see NIGHTSCOUT_SOURCE.md):
    calc = IobCalculator(treatments, basal_at, settings)
        treatments : ascending NS treatment records covering at least DIA hours before the first loop
        basal_at   : callable(ms) -> scheduled profile basal U/h (from ns_profile.ProfileBuilder)
        settings   : the ns_settings.json dict  (insulin.peak_minutes, insulin.min_dia_hours, insulin.dia_hours)
    calc.iob_array(now_ms, sensitivity_ratio=1.0)   -> list of 48 dicts, 5 min apart, starting at now
    calc.iob_at(t_ms, now_ms, sensitivity_ratio)     -> one dict
    calc.last_bolus_time(now_ms)                     -> ms (0 if none)
    calc.current_temp(now_ms)                        -> {'rate': U/h, 'duration': min left (0 = none), 'start': ms}
    calc.last_temp(now_ms)                           -> {'date': start ms, 'rate': U/h, 'duration': full minutes}

Each IOB dict: {'time', 'iob', 'activity', 'basaliob', 'bolussnooze', 'netbasalinsulin', 'hightempinsulin',
                'iobWithZeroTemp': {'iob', 'activity', 'basaliob'}}

Sources of truth to port:
    AAPS  InsulinOrefBasePlugin.iobCalcForTreatment   (exponential curve, peak / DIA, MIN_DIA = 5 h)
    AAPS  TemporaryBasalExtension.iobCalc             (temp basal net of profile basal * sensitivity ratio, 5 min chunks)
    AAPS  IobCobCalculatorPlugin.calculateIobArrayForSMB / calculateFromTreatmentsAndTemps (48 points, zero-temp variant)
    oref0 lib/iob/index.js                            (lastBolusTime, lastTemp, iobWithZeroTemp = zero temp of 240 min from now)

Validation: `python ns_iob.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z`
compares iob/basaliob/activity against devicestatus.openaps.iob of every loop.
"""
import sys


class IobCalculator:
    def __init__(self, treatments, basal_at, settings):
        raise NotImplementedError('to be implemented - see module docstring')

    def iob_array(self, now_ms, sensitivity_ratio=1.0):
        raise NotImplementedError

    def iob_at(self, t_ms, now_ms, sensitivity_ratio=1.0):
        raise NotImplementedError

    def last_bolus_time(self, now_ms):
        raise NotImplementedError

    def current_temp(self, now_ms):
        raise NotImplementedError

    def last_temp(self, now_ms):
        raise NotImplementedError


if __name__ == '__main__':
    print(__doc__)
    sys.exit(1)
