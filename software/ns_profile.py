"""
ns_profile.py  -  build the oref1/autoISF `profile` object from the Nightscout profile store,
temporary targets, profile switches and ns_settings.json.

Contract (see NIGHTSCOUT_SOURCE.md):
    pb = ProfileBuilder(ns_profiles, treatments, settings)
    pb.basal_at(ms)                              -> scheduled basal U/h at ms (incl. profile-switch percentage)
    pb.dia_hours(ms)                             -> DIA hours of the active profile at ms
    pb.profile_at(now_ms, console_error=None)    -> full profile dict for determine_basal.py  (mg/dl inside)
    pb.activity_monitor_json(console_error, now_ms) -> {'recentSteps5Minutes', 'recentSteps10Minutes', 'recentSteps15Minutes',
                                                        'recentSteps30Minutes', 'recentSteps60Minutes', 'phone_moved', 'time_since_start'}
    pb.autoisf_version(console_error)            -> '3.2.0' from 'start AutoISF 3.2.0' or settings default

Keys determine_basal.py reads from profile (grep "profile\\[" determine_basal.py):
    sens current_basal max_iob temptargetSet maxSMBBasalMinutes iob_threshold_percent carb_ratio autoISF_version
    A52_risk_enable target_bg maxUAMSMBBasalMinutes skip_neutral_temps out_units min_bg max_bg bgBrake_ISF_weight
    smb_delivery_ratio_min smb_delivery_ratio_max remainingCarbsFraction remainingCarbsCap meal_addon exercise_mode
    enableSMB_with_temptarget enableSMB_with_COB enableSMB_always enableSMB_after_carbs calibrationStart calibrationIgnore
    calibrationDuration bgAccel_ISF_weight autoISF_min allowSMB_with_high_temptarget time_since_start SMBInterval
    smb_max_range_extension smb_delivery_ratio smb_delivery_ratio_bg_range sensitivity_raises_target resistance_lowers_target
    profile_percentage pp_ISF_weight pp_ISF_hours phone_moved parabola_fit_source meal_type_weight max_daily_safety_multiplier
    max_daily_basal max_basal lower_ISFrange_weight low_temptarget_lowers_sensitivity inactivity_scale_factor
    inactivity_idle_start inactivity_idle_end ignore_inactivity_overnight higher_ISFrange_weight high_temptarget_raises_sensitivity
    half_basal_exercise_target enableUAM enableSMB_EvenOn_OddOff_always enable_pp_ISF_always enable_dura_ISF_with_COB
    enable_autoISF dura_ISF_weight drift_ISF_weight delta_ISFrange_weight current_basal_safety_multiplier carbsReqThreshold
    bolus_increment autosens_max autoISF_max adv_target_adjustments activity_scale_factor activity_detection dia lgsThreshold
    (+ recentSteps* / phone_moved / time_since_start merged in by emulator_core.get_profile from the activity monitor json)

Rules
    * glucose values inside the profile are mg/dl (NS mmol * 18); out_units = 'mmol/L' when NS units are mmol
    * active NS profile at time t: newest profile document with startDate <= t, its defaultProfile (or settings.nightscout.profile_name);
      a 'Profile Switch' treatment (profile, percentage, timeshift, duration) overrides while active
    * temp target: 'Temporary Target' treatment active at t (created_at <= t < created_at + duration) -> min_bg = max_bg = target,
      temptargetSet = True; duration 0 = cancel
    * settings.profile_history.changes: a change {when, parameter, from, to} means the profile key `parameter` had value `from`
      before `when` (only for keys that exist in the settings sections, e.g. autoISF_max)
    * activity monitor: reproduce the state printed in consoleError with synthetic step counts (thresholds in determine_basal.activityMonitor)

Validation: `python ns_profile.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z`
compares against consoleError: 'profile.sens: X, sens: Y', 'Basal unchanged: X', 'CR: X', suggested.targetBG,
'SMB enabled; current target X has even decimal', 'User setting iobTH=..'.
"""
import sys


class ProfileBuilder:
    def __init__(self, ns_profiles, treatments, settings):
        raise NotImplementedError('to be implemented - see module docstring')

    def basal_at(self, ms):
        raise NotImplementedError

    def dia_hours(self, ms):
        raise NotImplementedError

    def profile_at(self, now_ms, console_error=None):
        raise NotImplementedError

    def activity_monitor_json(self, console_error, now_ms):
        raise NotImplementedError

    def autoisf_version(self, console_error):
        raise NotImplementedError


if __name__ == '__main__':
    print(__doc__)
    sys.exit(1)
