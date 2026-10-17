# Parameters: what a study may vary

Only the `profile` group may be searched. Ranges and steps live in `hindcast/spaces/autoisf_tierA.json`
and `autoisf_tierB.json` (plus the unlock-only `profile_tierC.json` and `safety_tierS.json`); the phone's current value always comes from `ns_settings.json`, never from
this page. AAPS preference titles and keys come from `hindcast/prefs_map.json`; "(title not verified)"
means the map has no checked title, so quote the VDF key to the human and the AAPS key if there is one.

Every description says what the loop would decide differently, never what glucose would do. The final
autoISF factor divides the profile ISF: a factor above 1 makes the loop ask for more insulin for the same
inputs, a factor below 1 for less.

## Tier A (`--tier A`): the autoISF caps and weights with most leverage, the SMB delivery ratio

| VDF key | AAPS preference (screen: autoISF settings) | AAPS key | unit | what it changes in the decision |
|---|---|---|---|---|
| `profile autoISF_max` | autoISF max | `autoISF_max` | factor | upper cap of the final factor; with the factor on the cap in more than a quarter of loops the weights below are not observable, so vary the cap first |
| `profile autoISF_min` | autoISF min | `autoISF_min` | factor | lower cap of the final factor; the loop would ask for less insulin when the terms pull the factor below 1 |
| `profile dura_ISF_weight` | dura ISF weight | `dura_ISF_weight` | weight | strength of the plateau term (glucose sitting above target for a while); a larger weight raises the factor on long plateaus |
| `profile bgAccel_ISF_weight` | bgAccel ISF weight | `bgAccel_ISF_weight` | weight | strength of the acceleration term on a rising, accelerating trace (parabola fit); the loop reacts earlier on such rises |
| `profile bgBrake_ISF_weight` | bgBrake ISF weight | `bgBrake_ISF_weight` | weight | the same term on a decelerating trace; a larger weight lowers the factor there |
| `profile smb_delivery_ratio` | SMB delivery ratio | `openapsama_smb_delivery_ratio` | ratio | share of insulinReq that one SMB delivers |
| `profile smb_delivery_ratio_min` | (title not verified) | `openapsama_smb_delivery_ratio_min` | ratio | lower end of the delivery-ratio interpolation over the glucose range above target |
| `profile smb_delivery_ratio_max` | (title not verified) | `openapsama_smb_delivery_ratio_max` | ratio | upper end of that interpolation |
| `profile smb_delivery_ratio_bg_range` | (title not verified) | `openapsama_smb_delivery_ratio_bg_range` | mg/dl | width of the interpolation range; the `ns_settings.json` value is fitted to the phone's echoes (`_overrides`) |
| `profile iob_threshold_percent` | iob threshold percent | `iob_threshold_percent` | % | share of max_iob above which SMBs are limited (iobTH); 100 = off |

Constraints the sampler keeps: `smb_delivery_ratio_min <= smb_delivery_ratio <= smb_delivery_ratio_max`,
`autoISF_min <= autoISF_max`.

## Tier B (`--tier B`): the rest of the autoISF and SMB family

| VDF key | AAPS preference | AAPS key | unit | what it changes in the decision |
|---|---|---|---|---|
| `profile pp_ISF_weight` | pp ISF weight | `pp_ISF_weight` | weight | strength of the post-meal term (glucose rise after a carb entry) |
| `profile pp_ISF_hours` | (title not verified) | none | h | hours after the last carb entry during which the post-meal term applies |
| `profile enable_pp_ISF_always` | (title not verified) | none | on/off | apply the post-meal term at any time, not only after a carb entry |
| `profile higher_ISFrange_weight` | higher ISFrange weight | `higher_ISFrange_weight` | weight | strength of the glucose-level term above target |
| `profile lower_ISFrange_weight` | lower ISFrange weight | `lower_ISFrange_weight` | weight | strength of the glucose-level term below target |
| `profile delta_ISFrange_weight` | delta ISFrange weight | none | weight | strength of the delta part of the glucose-level term; 0.0 is the autoISF default |
| `profile smb_max_range_extension` | SMB max range extension | `openapsama_smb_max_range_extension` | factor | multiplies maxBolus, the size limit of one SMB; the `ns_settings.json` value is fitted to the printed maxBolus |
| `profile SMBInterval` | (title not verified) | `smbinterval` | min | minimum minutes between two SMBs |
| `profile maxSMBBasalMinutes` | Max minutes of basal to limit SMB to | `smbmaxminutes` | min | size limit of one SMB in minutes of basal, with carbs on board |
| `profile maxUAMSMBBasalMinutes` | Max minutes of basal to limit SMB to for UAM | `uamsmbmaxminutes` | min | the same limit for unannounced meals |
| `profile activity_scale_factor` | (title not verified) | `activity_scale_factor` | factor | how strongly a recorded activity lowers the factor; Nightscout has no step data, so only days with the activity-monitor echo show it |
| `profile inactivity_scale_factor` | (title not verified) | `inactivity_scale_factor` | factor | how strongly recorded inactivity raises the factor; same restriction |
| `profile enable_dura_ISF_with_COB` | (title not verified) | none | on/off | plateau term also with carbs on board; the port reads it only for autoISF before 3.0.1, so on these days it is not testable |
| `profile parabola_fit_source` | (title not verified) | none | readings | how many readings the parabola fit of the acceleration term uses |

## Coupling notes

- Vary `autoISF_max` before any weight when the cap-binding share is high: a factor held on the cap
  hides every weight behind it ("the factor would have sat on the cap").
- `bgAccel_ISF_weight` and `bgBrake_ISF_weight` act on the same fit; move them together or one at a time
  with the other at the phone's value.
- The delivery-ratio keys are one interpolation; keep the chain order (the sampler does).
- `smb_max_range_extension`, `maxSMBBasalMinutes`, `maxUAMSMBBasalMinutes` and `SMBInterval` only matter
  in loops where SMB is enabled; on days with many temp-target loops that disable SMB they are often
  not testable.
- `iob_threshold_percent` gates SMB entirely above iobTH; a variant that changes it changes how many
  loops would have microbolused at all.

## Locked (refused by `hc study new`, exit 3, unless the human unlocks them explicitly)

- Every `new_parameter` key, and every group except `profile` (the others are replay inputs).
- `profile sens`, `carb_ratio`, `current_basal`, `dia`.
- The safety section: `max_iob`, `max_basal`, `max_daily_safety_multiplier`,
  `current_basal_safety_multiplier`, `lgsThreshold`, `bolus_increment`, the treatments safety limits,
  `autosens_min` / `autosens_max` and the rest of `contracts.LOCKED_PROFILE_KEYS`, plus any key of the
  `safety` section of `ns_settings.json`.

These stay available to a human with the plain emulator (`emulator_batch.py` and a VDF). Hindcast
searches them only in a study that unlocks them explicitly, on the human's word and with a reason:

- `--tier C`: scales of profile `sens`, `carb_ratio`, `current_basal` (and `profile_percentage`, which is
  not locked), each a multiplier of the value every loop recorded; 1.0 is the recorded profile.
- `--tier S`: the caps `max_iob` and `max_basal`; a cap study only reports how often the cap binds.
- `hc study new ... --unlock KEY [KEY ...] --unlock-reason TEXT`; without it the tier is refused with
  exit 3 and the keys are named. The keys and the reason are stored in study.json.
- Such a study is **weak evidence**: the replay keeps the glucose and insulin history the phone's own
  profile and caps produced, so it cannot judge a different profile or cap. Say so on every card.
- `profile dia` and every `new_parameter` key can never be unlocked.

## Cannot be tested with this method

Basal rates, DIA and insulin peak, carb ratio, the autosens ratio and the glucose smoother enter through
the recorded inputs the replay takes as given. Changing them would change the history itself, which a
decision replay cannot follow. Say "cannot be tested with this method" and do not offer them.
