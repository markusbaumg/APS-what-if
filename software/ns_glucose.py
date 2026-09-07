"""
ns_glucose.py  -  rebuild the AAPS/autoISF GlucoseStatus from Nightscout data.

Contract (see NIGHTSCOUT_SOURCE.md):
    gh = GlucoseHistory(entries, devicestatus, settings)
    gs = gh.status_at(now_ms, record)      -> dict or None

Keys of the returned dict (all glucose in mg/dl, deltas in mg/dl per 5 minutes):
    glucose, date, delta, short_avgdelta, long_avgdelta, noise,
    dura_ISF_minutes, dura_ISF_average,
    parabola_fit_minutes, parabola_fit_correlation, parabola_fit_a0, parabola_fit_a1, parabola_fit_a2,
    parabola_fit_last_delta, parabola_fit_next_delta, bg_acceleration

Sources of truth to port:
    AAPS  core/.../iob/GlucoseStatusCalculatorImpl.kt   (delta, short_avgdelta, long_avgdelta from 5-min bucketed data)
    AAPS  autoISF GlucoseStatusAutoIsf / parabola fit / dura_ISF  (plugins/aps/.../openAPSAutoISF)
    emulator_core.getHistBG / getBestParabolaBG           (the emulator author's port, used when a logfile lacks the values)

Validation: `python ns_glucose.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z`
compares against devicestatus consoleError lines (tick, dura_ISF minutes, acce_ISF factor, parabolic extremum).
"""
import sys


class GlucoseHistory:
    def __init__(self, entries, devicestatus, settings):
        raise NotImplementedError('to be implemented - see module docstring')

    def status_at(self, now_ms, record=None):
        raise NotImplementedError


if __name__ == '__main__':
    print(__doc__)
    sys.exit(1)
