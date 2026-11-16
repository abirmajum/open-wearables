"""Write the synthetic Garmin, Ultrahuman and WHOOP seeds that ``scripts/rumi_fixture_server.py`` replays.

The seeds are dated around ``ANCHOR_DATE``; the server shifts every timestamp and date by whole days so the last
seeded day lands on its ``--today``. All values are invented. Field names follow Garmin's Health and Activity API
push payloads, Ultrahuman's ``/user_data/metrics`` response and WHOOP's v2 collections, including fields Open
Wearables does not read, so a dump of the API shows what is dropped.

Usage (from ``backend/``)::

    uv run python scripts/rumi_fixture_seeds/generate.py
"""

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

SEEDS_DIR = Path(__file__).resolve().parent
ANCHOR_DATE = date(2026, 1, 15)
DAYS = 10
GARMIN_OFFSET = 3600  # UTC+01:00
GARMIN_USER_ID = "GARMIN_USER_ID"  # replaced per fixture user by the server
# Ultrahuman users away from UTC (offset -> seconds east of UTC): a user east of UTC starts each day on the previous
# UTC date.
ULTRAHUMAN_ZONES = {"+04:00": 4 * 3600}
FIT_CALLBACK_URL = "https://apis.garmin.com/wellness-api/rest/activityFile?id={activity_id}&token=fixture"
WHOOP_USER_ID = "WHOOP_USER_ID"  # replaced per fixture user by the server, with an integer as WHOOP sends
# A WHOOP account per zone, matching the device it is paired with: Garmin's +01:00, Ultrahuman's UTC, and +03:00 alone.
WHOOP_ZONES = {"+03:00": 3 * 3600, "+01:00": GARMIN_OFFSET, "+00:00": 0}


def _days() -> list[tuple[int, date]]:
    """(index, local date), oldest first; the last one is the anchor day."""
    return [(i, ANCHOR_DATE - timedelta(days=DAYS - 1 - i)) for i in range(DAYS)]


def _epoch(day: date, hour: float, offset: int = 0) -> int:
    """Unix seconds of a local wall-clock hour on ``day`` in a zone ``offset`` seconds east of UTC."""
    local = datetime.combine(day, time(), tzinfo=timezone.utc) + timedelta(hours=hour)
    return int(local.timestamp()) - offset


def _wave(i: int, base: float, swing: float, period: int = 5) -> float:
    """A value that moves day by day, so a wrong day shows up as a wrong value."""
    return round(base + swing * (((i * 3) % period) - (period - 1) / 2) / ((period - 1) / 2), 1)


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------

# The last complete day carries Garmin's real cadences; the other days stay sparse so dumps stay small.
DENSE_DAY = DAYS - 2
# That day's run (activity 21000101), as seconds from local midnight.
DENSE_RUN = (int(7.5 * 3600), int(7.5 * 3600) + 2940)
DENSE_WAKE = int(6.4 * 3600)
# Garmin's default heart-rate zones: the top of zones 0 to 5 at 50 to 100 percent of a 190 bpm maximum.
GARMIN_HR_ZONE_TOPS = (95, 114, 133, 152, 171, 190)


def _dense_heart_rate(t: int) -> int:
    """Beats per minute ``t`` seconds after local midnight on the dense day: asleep, the run, awake, winding down."""
    k = t // 15
    if t < DENSE_WAKE:
        return 50 + (k * 7) % 6
    if DENSE_RUN[0] <= t < DENSE_RUN[1]:
        return 140 + ((t - DENSE_RUN[0]) // 60) % 25
    if t >= 22 * 3600:
        return 56 + (k * 5) % 10
    return 60 + (k * 13) % 31


def _dense_stress(t: int) -> int:
    """Garmin's stress level every 3 minutes; -2 is too much motion to measure, -1 off the wrist."""
    if DENSE_RUN[0] <= t < DENSE_RUN[1]:
        return -2
    if 19 * 3600 <= t < 19 * 3600 + 900:
        return -1
    k = t // 180
    return 8 + k % 12 if t < DENSE_WAKE else 22 + (k * 17) % 48


def _dense_body_battery(t: int) -> int:
    """Charges overnight from 31 to 92, drains through the day, faster on the run."""
    if t < DENSE_WAKE:
        return 31 + round(61 * t / DENSE_WAKE)
    drained = (t - DENSE_WAKE) * 58 // (86400 - DENSE_WAKE) + (12 if t >= DENSE_RUN[1] else 0)
    return max(5, 92 - drained)


def _garmin_daily(i: int, day: date, is_today: bool) -> dict[str, Any]:
    start = _epoch(day, 0, GARMIN_OFFSET)
    hours = 13 if is_today else 24
    steps = int(_wave(i, 9000, 2500))
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-dly-{day.isoformat()}",
        "calendarDate": day.isoformat(),
        "activityType": "WALKING",
        "activeKilocalories": int(_wave(i, 520, 120)),
        "bmrKilocalories": 1680,
        "consumedCalories": 0,
        "steps": steps,
        "pushes": 0,
        "distanceInMeters": round(steps * 0.78, 1),
        "pushDistanceInMeters": 0.0,
        "durationInSeconds": hours * 3600,
        "activeTimeInSeconds": int(_wave(i, 5400, 1500)),
        "startTimeInSeconds": start,
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
        "moderateIntensityDurationInSeconds": int(_wave(i, 1800, 600)),
        "vigorousIntensityDurationInSeconds": int(_wave(i, 900, 600)),
        "floorsClimbed": int(_wave(i, 9, 4)),
        "floorsDescended": int(_wave(i, 8, 4)),
        "minHeartRateInBeatsPerMinute": int(_wave(i, 47, 3)),
        "averageHeartRateInBeatsPerMinute": int(_wave(i, 66, 4)),
        "maxHeartRateInBeatsPerMinute": int(_wave(i, 158, 12)),
        "restingHeartRateInBeatsPerMinute": int(_wave(i, 52, 3)),
        "timeOffsetHeartRateSamples": (
            {str(t): _dense_heart_rate(t) for t in range(0, 86400, 15)}
            if i == DENSE_DAY
            else {str(h * 3600): 55 + (h * 7 + i) % 40 for h in range(hours)}
        ),
        "averageStressLevel": int(_wave(i, 31, 8)),
        "maxStressLevel": int(_wave(i, 88, 7)),
        "stressDurationInSeconds": int(_wave(i, 14400, 3600)),
        "restStressDurationInSeconds": int(_wave(i, 28800, 3600)),
        "activityStressDurationInSeconds": int(_wave(i, 7200, 1800)),
        "lowStressDurationInSeconds": int(_wave(i, 9000, 1800)),
        "mediumStressDurationInSeconds": int(_wave(i, 3600, 900)),
        "highStressDurationInSeconds": int(_wave(i, 1800, 600)),
        "stressQualifier": "balanced",
        "stepsGoal": 10000,
        "intensityDurationGoalInSeconds": 9000,
        "floorsClimbedGoal": 10,
        "bodyBatteryChargedValue": int(_wave(i, 62, 15)),
        "bodyBatteryDrainedValue": int(_wave(i, 58, 15)),
    }


def _garmin_dense_epochs(day: date) -> list[dict[str, Any]]:
    """One 15-minute epoch for every quarter hour of the dense day."""
    epochs = []
    for t in range(0, 86400, 900):
        start = _epoch(day, 0, GARMIN_OFFSET) + t
        running = DENSE_RUN[0] <= t < DENSE_RUN[1]
        steps = 2400 if running else 0 if t < DENSE_WAKE or t >= 22 * 3600 else (t // 900 * 137) % 900
        activity_type, code, intensity = (
            ("RUNNING", "1", "HIGHLY_ACTIVE")
            if running
            else ("WALKING", "8", "ACTIVE")
            if steps
            else ("SEDENTARY", "0", "SEDENTARY")
        )
        epochs.append(
            {
                "userId": GARMIN_USER_ID,
                "summaryId": f"x3f1-{start:x}-{code}",
                "activityType": activity_type,
                "activeKilocalories": 190 if running else steps // 25 + 2,
                "steps": steps,
                "distanceInMeters": round(steps * (1.12 if running else 0.78), 1),
                "durationInSeconds": 900,
                "activeTimeInSeconds": 900 if running else min(900, steps * 6 // 10),
                "startTimeInSeconds": start,
                "startTimeOffsetInSeconds": GARMIN_OFFSET,
                "met": 9.8 if running else 3.1 if steps else 1.0,
                "intensity": intensity,
                "meanMotionIntensity": 6.5 if running else 2.4 if steps else 0.0,
                "maxMotionIntensity": 7.0 if running else 5.0 if steps else 0.0,
                "meanHeartRateInBeatsPerMinute": round(sum(_dense_heart_rate(t + s) for s in range(0, 900, 15)) / 60),
            }
        )
    return epochs


def _garmin_epochs(i: int, day: date, is_today: bool) -> list[dict[str, Any]]:
    if i == DENSE_DAY:
        return _garmin_dense_epochs(day)
    epochs = []
    for hour in range(8, 13 if is_today else 22, 2):
        start = _epoch(day, hour, GARMIN_OFFSET)
        for activity_type, code, steps in (("WALKING", "8", 420 + i * 10 + hour), ("SEDENTARY", "0", 0)):
            epochs.append(
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": f"x3f1-{start:x}-{code}",
                    "activityType": activity_type,
                    "activeKilocalories": 18 if steps else 2,
                    "steps": steps,
                    "distanceInMeters": round(steps * 0.78, 1),
                    "durationInSeconds": 900,
                    "activeTimeInSeconds": 420 if steps else 0,
                    "startTimeInSeconds": start,
                    "startTimeOffsetInSeconds": GARMIN_OFFSET,
                    "met": 3.1 if steps else 1.0,
                    "intensity": "ACTIVE" if steps else "SEDENTARY",
                    "meanMotionIntensity": 2.4 if steps else 0.0,
                    "maxMotionIntensity": 5.0 if steps else 0.0,
                    "meanHeartRateInBeatsPerMinute": 92 if steps else 64,
                }
            )
    return epochs


def _garmin_sleep(i: int, day: date) -> dict[str, Any]:
    """The night that ends on ``day``: 23:00 the evening before to about 06:40 local."""
    start = _epoch(day - timedelta(days=1), 23, GARMIN_OFFSET)
    deep, light, rem, awake = 5400 + i * 60, 13800, 6000 - i * 30, 1200
    duration = deep + light + rem + awake
    levels: dict[str, list[dict[str, int]]] = {"deep": [], "light": [], "rem": [], "awake": []}
    cursor = start
    for stage, seconds in (
        ("light", light // 2),
        ("deep", deep),
        ("rem", rem // 2),
        ("awake", awake),
        ("light", light - light // 2),
        ("rem", rem - rem // 2),
    ):
        levels[stage].append({"startTimeInSeconds": cursor, "endTimeInSeconds": cursor + seconds})
        cursor += seconds
    score = int(_wave(i, 80, 9))
    sleep: dict[str, Any] = {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-slp-{day.isoformat()}",
        "calendarDate": day.isoformat(),
        "durationInSeconds": duration,
        "startTimeInSeconds": start,
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
        "unmeasurableSleepInSeconds": 0,
        "deepSleepDurationInSeconds": deep,
        "lightSleepDurationInSeconds": light,
        "remSleepInSeconds": rem,
        "awakeDurationInSeconds": awake,
        "sleepLevelsMap": levels,
        "validation": "ENHANCED_FINAL",
        "timeOffsetSleepSpo2": {str(m * 1800): 93 + (m + i) % 5 for m in range(duration // 1800)},
        "timeOffsetSleepRespiration": {
            str(m * 1800): round(13.5 + ((m + i) % 4) * 0.4, 1) for m in range(duration // 1800)
        },
        "overallSleepScore": {"value": score, "qualifierKey": "GOOD" if score >= 80 else "FAIR"},
        "sleepScores": {
            "totalDuration": {"qualifierKey": "GOOD"},
            "stress": {"qualifierKey": "EXCELLENT"},
            "awakeCount": {"qualifierKey": "GOOD"},
            "remPercentage": {"qualifierKey": "GOOD"},
            "restlessness": {"qualifierKey": "FAIR"},
            "lightPercentage": {"qualifierKey": "EXCELLENT"},
            "deepPercentage": {"qualifierKey": "EXCELLENT"},
        },
        # In-sleep aggregates as GarminSleepJSON (app/schemas/providers/garmin/wellness_import.py) names them.
        "averageHeartRate": int(_wave(i, 54, 3)),
        "lowestHeartRate": int(_wave(i, 47, 3)),
        "highestHeartRate": int(_wave(i, 72, 5)),
        "respirationAvg": round(_wave(i, 14.2, 0.8), 1),
        "respirationLow": round(_wave(i, 11.8, 0.6), 1),
        "respirationHigh": round(_wave(i, 17.6, 0.9), 1),
        "avgOxygenSaturation": round(_wave(i, 95.0, 1.5), 1),
        "lowOxygenSaturationPercentage": round(_wave(i, 89.0, 2.0), 1),
        "highOxygenSaturationPercentage": 99.0,
        "sleepHRV": {"value": int(_wave(i, 48, 7))},
        "naps": [],
    }
    if i == DAYS - 6:
        # Garmin leaves a night it could not score without overallSleepScore
        del sleep["overallSleepScore"]
    if i == DAYS - 3:
        nap_start = _epoch(day, 14, GARMIN_OFFSET)
        sleep["naps"] = [
            {
                "napDurationInSeconds": 1800,
                "napStartTimeInSeconds": nap_start,
                "napOffsetInSeconds": GARMIN_OFFSET,
                "napValidation": "DEVICE",
                "napValidationTime": nap_start + 1800,
            }
        ]
    return sleep


def _garmin_hrv(i: int, day: date) -> dict[str, Any]:
    start = _epoch(day - timedelta(days=1), 23, GARMIN_OFFSET)
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-hrv-{day.isoformat()}",
        "calendarDate": day.isoformat(),
        "lastNightAvg": int(_wave(i, 48, 7)),
        "lastNight5MinHigh": int(_wave(i, 71, 8)),
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
        "durationInSeconds": 26400,
        "startTimeInSeconds": start,
        "hrvValues": {str(m * 1800): 40 + (m * 5 + i) % 20 for m in range(14)},
    }


def _garmin_stress(i: int, day: date, is_today: bool) -> dict[str, Any]:
    start = _epoch(day, 0, GARMIN_OFFSET)
    slots = (13 if is_today else 24) * 2
    battery = [min(100, max(5, 35 + (s if s < 14 else 14) * 4 - max(0, s - 14) * 2 - i)) for s in range(slots)]
    if i == DENSE_DAY:
        return {
            "userId": GARMIN_USER_ID,
            "summaryId": f"x3f1-str-{day.isoformat()}",
            "calendarDate": day.isoformat(),
            "startTimeInSeconds": start,
            "startTimeOffsetInSeconds": GARMIN_OFFSET,
            "durationInSeconds": 86400,
            "timeOffsetStressLevelValues": {str(t): _dense_stress(t) for t in range(0, 86400, 180)},
            "timeOffsetBodyBatteryValues": {str(t): _dense_body_battery(t) for t in range(0, 86400, 180)},
        }
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-str-{day.isoformat()}",
        "calendarDate": day.isoformat(),
        "startTimeInSeconds": start,
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
        "durationInSeconds": slots * 1800,
        "timeOffsetStressLevelValues": {
            str(s * 1800): (-1 if s == 3 else 15 + (s * 11 + i) % 60) for s in range(slots)
        },
        "timeOffsetBodyBatteryValues": {str(s * 1800): battery[s] for s in range(slots)},
    }


def _garmin_respiration(i: int, day: date, is_today: bool) -> dict[str, Any]:
    start = _epoch(day, 0, GARMIN_OFFSET)
    hours = 13 if is_today else 24
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-rsp-{day.isoformat()}",
        "startTimeInSeconds": start,
        "durationInSeconds": hours * 3600,
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
        "timeOffsetEpochToBreaths": (
            {str(t): round((12.4 if t < DENSE_WAKE else 14.8) + ((t // 60) % 7) * 0.3, 1) for t in range(0, 86400, 60)}
            if i == DENSE_DAY
            else {str(h * 3600): round(13.0 + ((h + i) % 6) * 0.5, 1) for h in range(hours)}
        ),
    }


def _garmin_pulse_ox(i: int, day: date) -> dict[str, Any]:
    start = _epoch(day - timedelta(days=1), 23, GARMIN_OFFSET)
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-pox-{day.isoformat()}",
        "calendarDate": day.isoformat(),
        "startTimeInSeconds": start,
        "durationInSeconds": 25200,
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
        # The pulse oximeter reads every minute through the night on the dense day
        "timeOffsetSpo2Values": (
            {str(t): 93 + (t // 60 * 3) % 6 for t in range(0, 25200, 60)}
            if i == DENSE_DAY
            else {str(h * 3600): 94 + (h + i) % 5 for h in range(7)}
        ),
        "onDemand": False,
    }


def _garmin_skin_temp(i: int, day: date) -> dict[str, Any]:
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-skt-{day.isoformat()}",
        "calendarDate": day.isoformat(),
        "avgDeviationCelsius": round(_wave(i, 0.1, 0.4), 1),
        "durationInSeconds": 25200,
        "startTimeInSeconds": _epoch(day - timedelta(days=1), 23, GARMIN_OFFSET),
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
    }


def _garmin_user_metrics(i: int, day: date) -> dict[str, Any]:
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"x3f1-um-{day.isoformat()}",
        "calendarDate": day.isoformat(),
        "vo2Max": 47.0 + (1.0 if i >= 6 else 0.0),
        "vo2MaxCycling": 45.0,
        "fitnessAge": 31,
        "enhanced": True,
    }


def _garmin_activity_summary(activity_id: int, day: date, hour: float, kind: str) -> dict[str, Any]:
    start = _epoch(day, hour, GARMIN_OFFSET)
    if kind == "RUNNING":
        return {
            "userId": GARMIN_USER_ID,
            "summaryId": f"{activity_id}",
            "activityId": activity_id,
            "activityName": "Morning Run",
            "activityType": "RUNNING",
            "durationInSeconds": 2940,
            "startTimeInSeconds": start,
            "startTimeOffsetInSeconds": GARMIN_OFFSET,
            "averageHeartRateInBeatsPerMinute": 152,
            "maxHeartRateInBeatsPerMinute": 174,
            "averageRunCadenceInStepsPerMinute": 168.0,
            "maxRunCadenceInStepsPerMinute": 184.0,
            "averageSpeedInMetersPerSecond": 3.05,
            "maxSpeedInMetersPerSecond": 4.1,
            "averagePaceInMinutesPerKilometer": 5.46,
            "maxPaceInMinutesPerKilometer": 4.07,
            "activeKilocalories": 612,
            "deviceName": "forerunner965",
            "distanceInMeters": 8967.0,
            "steps": 8240,
            "totalElevationGainInMeters": 84.0,
            "totalElevationLossInMeters": 81.0,
            "startingLatitudeInDegree": 50.0614,
            "startingLongitudeInDegree": 19.9366,
            "manual": False,
            "isWebUpload": False,
        }
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"{activity_id}",
        "activityId": activity_id,
        "activityName": "Evening Ride",
        "activityType": "CYCLING",
        "durationInSeconds": 4500,
        "startTimeInSeconds": start,
        "startTimeOffsetInSeconds": GARMIN_OFFSET,
        "averageHeartRateInBeatsPerMinute": 138,
        "maxHeartRateInBeatsPerMinute": 166,
        "averageBikingCadenceInRevPerMinute": 86.0,
        "maxBikingCadenceInRevPerMinute": 112.0,
        "averageSpeedInMetersPerSecond": 7.4,
        "maxSpeedInMetersPerSecond": 13.2,
        "activeKilocalories": 905,
        "deviceName": "edge1050",
        "distanceInMeters": 33300.0,
        "totalElevationGainInMeters": 412.0,
        "totalElevationLossInMeters": 405.0,
        "startingLatitudeInDegree": 50.0614,
        "startingLongitudeInDegree": 19.9366,
        "manual": False,
        "isWebUpload": False,
    }


def _garmin_activity_details(summary: dict[str, Any]) -> dict[str, Any]:
    start = summary["startTimeInSeconds"]
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": f"{summary['activityId']}-detail",
        "activityId": summary["activityId"],
        "summary": {k: v for k, v in summary.items() if k != "userId"},
        "samples": [
            {
                "startTimeInSeconds": start + s * 60,
                "latitudeInDegree": 50.0614 + s * 0.0004,
                "longitudeInDegree": 19.9366 + s * 0.0005,
                "elevationInMeters": 210.0 + (s % 10),
                "airTemperatureCelcius": 6.0,
                "heartRate": 140 + s % 25,
                "speedMetersPerSecond": 3.0 + (s % 5) * 0.05,
                "stepsPerMinute": 168.0,
                "totalDistanceInMeters": s * 183.0,
                "timerDurationInSeconds": s * 60,
                "clockDurationInSeconds": s * 60,
                "movingDurationInSeconds": s * 60,
            }
            for s in range(summary["durationInSeconds"] // 60)
        ],
        "laps": [{"startTimeInSeconds": start}, {"startTimeInSeconds": start + summary["durationInSeconds"] // 2}],
    }


def _garmin_fit_spec(summary: dict[str, Any]) -> dict[str, Any]:
    """What the server writes into the FIT file it serves for the activity's ``callbackURL``."""
    start, duration = summary["startTimeInSeconds"], summary["durationInSeconds"]
    half = duration // 2
    records = [{"offset": s * 60, "heart_rate": 140 + s % 25, "speed": 3.05} for s in range(duration // 60)]
    zone_seconds = [0] * len(GARMIN_HR_ZONE_TOPS)
    for record in records:
        zone_seconds[next(z for z, top in enumerate(GARMIN_HR_ZONE_TOPS) if record["heart_rate"] <= top)] += 60
    return {
        "activity_id": summary["activityId"],
        "start_time": start,
        "records": records,
        "laps": [
            {"start": start, "end": start + half, "distance_meters": 4480, "avg_hr": 148, "max_hr": 166},
            {"start": start + half, "end": start + duration, "distance_meters": 4487, "avg_hr": 156, "max_hr": 174},
        ],
        "session": {"end": start + duration, "aerobic_effect": 3.4, "anaerobic_effect": 1.2, "training_load": 142.0},
        # The session's time_in_zone message, as a Garmin watch writes it at the end of an activity
        "time_in_zone": {
            "time_in_hr_zone": zone_seconds,
            "hr_zone_high_boundary": list(GARMIN_HR_ZONE_TOPS),
            "max_heart_rate": GARMIN_HR_ZONE_TOPS[-1],
            "resting_heart_rate": 50,
            "threshold_heart_rate": 171,
        },
    }


def garmin_seed() -> dict[str, Any]:
    days = _days()
    today_index = DAYS - 1
    run = _garmin_activity_summary(21000101, days[DAYS - 2][1], 7.5, "RUNNING")
    ride = _garmin_activity_summary(21000102, days[DAYS - 5][1], 18, "CYCLING")
    run_details = _garmin_activity_details(run)
    first_day, last_day = days[0][1], days[-1][1]
    pushes: list[dict[str, Any]] = [
        {"dailies": [_garmin_daily(i, d, i == today_index) for i, d in days]},
        {"epochs": [e for i, d in days for e in _garmin_epochs(i, d, i == today_index)]},
        {"sleeps": [_garmin_sleep(i, d) for i, d in days]},
        {"hrv": [_garmin_hrv(i, d) for i, d in days]},
        {"stressDetails": [_garmin_stress(i, d, i == today_index) for i, d in days]},
        {"allDayRespiration": [_garmin_respiration(i, d, i == today_index) for i, d in days]},
        {"pulseox": [_garmin_pulse_ox(i, d) for i, d in days]},
        {"skinTemp": [_garmin_skin_temp(i, d) for i, d in days]},
        {"userMetrics": [_garmin_user_metrics(i, d) for i, d in days]},
        {
            "bodyComps": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": f"x3f1-bc-{day.isoformat()}",
                    "measurementTimeInSeconds": _epoch(day, 7.25, GARMIN_OFFSET),
                    "measurementTimeOffsetInSeconds": GARMIN_OFFSET,
                    "muscleMassInGrams": muscle,
                    "boneMassInGrams": 3400,
                    "bodyWaterInPercent": water,
                    "bodyFatInPercent": fat,
                    "bodyMassIndex": bmi,
                    "weightInGrams": weight,
                }
                for day, weight, muscle, fat, water, bmi in (
                    (first_day, 74300, 33100, 17.8, 58.9, 23.0),
                    (last_day, 73800, 33200, 17.4, 59.2, 22.8),
                )
            ]
        },
        {
            "bloodPressures": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": f"x3f1-bp-{days[DAYS - 3][1].isoformat()}",
                    "measurementTimeInSeconds": _epoch(days[DAYS - 3][1], 8, GARMIN_OFFSET),
                    "measurementTimeOffsetInSeconds": GARMIN_OFFSET,
                    "systolic": 118,
                    "diastolic": 76,
                    "pulse": 58,
                    "sourceType": "MANUAL",
                }
            ]
        },
        {
            "healthSnapshot": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": f"x3f1-hs-{days[DAYS - 4][1].isoformat()}",
                    "calendarDate": days[DAYS - 4][1].isoformat(),
                    "startTimeInSeconds": _epoch(days[DAYS - 4][1], 12, GARMIN_OFFSET),
                    "durationInSeconds": 120,
                    "startTimeOffsetInSeconds": GARMIN_OFFSET,
                    "summaries": [
                        {"summaryType": "heart_rate", "minValue": 58.0, "maxValue": 66.0, "avgValue": 61.0},
                        {"summaryType": "respiration", "minValue": 12.0, "maxValue": 15.0, "avgValue": 13.4},
                        {"summaryType": "stress", "minValue": 10.0, "maxValue": 30.0, "avgValue": 19.0},
                        {"summaryType": "pulse_ox", "minValue": 95.0, "maxValue": 98.0, "avgValue": 96.5},
                        {"summaryType": "rmssd_hrv", "minValue": 38.0, "maxValue": 61.0, "avgValue": 49.0},
                        {"summaryType": "sdrr_hrv", "minValue": 44.0, "maxValue": 72.0, "avgValue": 57.0},
                    ],
                }
            ]
        },
        {
            "moveIQActivities": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": f"x3f1-miq-{day.isoformat()}-{kind.lower()}",
                    "calendarDate": day.isoformat(),
                    "startTimeInSeconds": _epoch(day, hour, GARMIN_OFFSET),
                    "durationInSeconds": duration,
                    "startTimeOffsetInSeconds": GARMIN_OFFSET,
                    "activityType": kind,
                    "activitySubType": sub,
                }
                for day, hour, duration, kind, sub in (
                    (days[DAYS - 6][1], 12.5, 1680, "Walking", "Hurried"),
                    (days[DAYS - 3][1], 17.0, 1260, "Running", "Running"),
                )
            ]
        },
        {
            "mct": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": f"x3f1-mct-{days[2][1].isoformat()}",
                    "periodStartDate": days[2][1].isoformat(),
                    "dayInCycle": DAYS - 2,
                    "periodLength": 5,
                    "currentPhase": 2,
                    "currentPhaseType": "FOLLICULAR",
                    "lengthOfCurrentPhase": 9,
                    "daysUntilNextPhase": 2,
                    "predictedCycleLength": 29,
                    "isPredictedCycle": False,
                    "cycleLength": None,
                    "lastUpdatedTimeInSeconds": _epoch(last_day, 7, GARMIN_OFFSET),
                    "hasSpecifiedCycleLength": True,
                    "hasSpecifiedPeriodLength": True,
                    "fertileWindowStart": 12,
                    "lengthOfFertileWindow": 6,
                }
            ]
        },
        {"activities": [run, ride]},
        {"activityDetails": [run_details]},
        {
            "activityFiles": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": f"{run['activityId']}-file",
                    "activityId": run["activityId"],
                    "fileType": "FIT",
                    "callbackURL": FIT_CALLBACK_URL.format(activity_id=run["activityId"]),
                    "startTimeInSeconds": run["startTimeInSeconds"],
                    "manual": False,
                }
            ]
        },
    ]
    today_daily = _garmin_daily(today_index, days[today_index][1], True)
    # Garmin re-sends today's summary as the day goes on; the later push carries the evening's stress
    pushes.append(
        {
            "dailies": [
                {
                    **today_daily,
                    "durationInSeconds": 20 * 3600,
                    "averageStressLevel": today_daily["averageStressLevel"] + 9,
                    "maxStressLevel": 97,
                    "stressDurationInSeconds": today_daily["stressDurationInSeconds"] + 5400,
                    "highStressDurationInSeconds": today_daily["highStressDurationInSeconds"] + 2700,
                    "stressQualifier": "stressful",
                    "bodyBatteryDrainedValue": today_daily["bodyBatteryDrainedValue"] + 20,
                }
            ]
        }
    )
    last_night = _garmin_sleep(today_index, days[today_index][1])
    # Garmin re-sends last night later with its end moved: the user dozed 20 more minutes
    pushes.append(
        {
            "sleeps": [
                {
                    **last_night,
                    "durationInSeconds": last_night["durationInSeconds"] + 1200,
                    "lightSleepDurationInSeconds": last_night["lightSleepDurationInSeconds"] + 1200,
                    "overallSleepScore": {
                        "value": last_night["overallSleepScore"]["value"] + 2,
                        "qualifierKey": "GOOD",
                    },
                }
            ]
        }
    )
    return {
        "anchor_date": ANCHOR_DATE.isoformat(),
        "user_id_placeholder": GARMIN_USER_ID,
        "pushes": pushes,
        "fit_files": {FIT_CALLBACK_URL.format(activity_id=run["activityId"]): _garmin_fit_spec(run)},
    }


# ---------------------------------------------------------------------------
# Ultrahuman
# ---------------------------------------------------------------------------


def _ultrahuman_day(i: int, day: date, is_today: bool, offset: int = 0) -> dict[str, Any]:
    """One ``/user_data/metrics?date=`` response for a user ``offset`` seconds east of UTC: every time is that
    zone's local time, so the day starts at its local midnight."""

    def at(d: date, hour: float) -> int:
        return _epoch(d, hour, offset)

    def values(d: date, hours: range, value: Any) -> list[dict[str, Any]]:
        return [{"value": value(h), "timestamp": at(d, h)} for h in hours]

    midnight = at(day, 0)
    hours = range(13 if is_today else 24)
    bed_start, bed_end = at(day - timedelta(days=1), 23.25), at(day, 6.75)
    deep, light, rem, awake = 4800 + i * 60, 12600, 6300 - i * 30, 3300 - i * 30
    time_in_bed = bed_end - bed_start
    total_sleep = time_in_bed - awake
    graph, cursor = [], bed_start
    for stage, seconds in (
        ("awake", 600),
        ("light_sleep", light // 2),
        ("deep_sleep", deep),
        ("rem_sleep", rem // 2),
        ("awake", awake - 600),
        ("light_sleep", light - light // 2),
        ("rem_sleep", rem - rem // 2),
    ):
        graph.append({"start": cursor, "end": min(cursor + seconds, bed_end), "type": stage})
        cursor += seconds
    sleep_hr, sleep_hrv = int(_wave(i, 53, 3)), int(_wave(i, 51, 8))
    spo2 = int(_wave(i, 97, 1))
    sleep_score = int(_wave(i, 81, 8))
    steps_values = values(day, hours, lambda h: 0 if h < 7 else 300 + (h * 37 + i) % 500)

    def single(metric: str, title: str, value: Any, **extra: Any) -> dict[str, Any]:
        return {"type": metric, "object": {"value": value, "title": title, "day_start_timestamp": midnight, **extra}}

    return {
        "data": {
            "metric_data": [
                {
                    "type": "hr",
                    "object": {
                        "day_start_timestamp": midnight,
                        "title": "Heart Rate",
                        "values": values(day, hours, lambda h: 52 + (h * 5 + i) % 45),
                        "last_reading": {"value": 64, "timestamp": at(day, hours[-1])},
                        "unit": "bpm",
                    },
                },
                {
                    "type": "temp",
                    "object": {
                        "day_start_timestamp": midnight,
                        "title": "Skin Temperature",
                        "values": values(day, hours, lambda h: round(34.6 + ((h + i) % 6) * 0.2, 1)),
                        "last_reading": {"value": 35.2, "timestamp": at(day, hours[-1])},
                        "unit": "°C",
                    },
                },
                {
                    "type": "hrv",
                    "object": {
                        "day_start_timestamp": midnight,
                        "title": "HRV",
                        "values": values(day, range(0, 7), lambda h: 44 + (h * 3 + i) % 15),
                        "subtitle": "Average",
                        "avg": sleep_hrv,
                        "trend_title": "Stable",
                        "trend_direction": "neutral",
                    },
                },
                {
                    "type": "steps",
                    "object": {
                        "day_start_timestamp": midnight,
                        "values": steps_values,
                        "subtitle": "Average",
                        "total": sum(v["value"] for v in steps_values),
                        "avg": 7800,
                        "trend_title": "Increasing",
                        "trend_direction": "up",
                    },
                },
                {
                    "type": "night_rhr",
                    "object": {
                        "day_start_timestamp": midnight,
                        "title": "Resting HR",
                        "values": values(day, range(0, 7), lambda h: sleep_hr - 1 + h % 3),
                        "subtitle": "Sleep Time Average",
                        "avg": sleep_hr,
                        "trend_title": "Stable",
                        "trend_direction": "neutral",
                    },
                },
                single("temperature_deviation", "Temperature Deviation", round(_wave(i, 0.0, 0.3), 1), unit="°C"),
                {"type": "sleep_rhr", "object": {"value": sleep_hr, "day_start_timestamp": midnight}},
                {"type": "avg_sleep_hrv", "object": {"value": sleep_hrv, "day_start_timestamp": midnight}},
                {
                    "type": "Sleep",
                    "object": {
                        "bedtime_start": bed_start,
                        "bedtime_end": bed_end,
                        "quick_metrics": [
                            {
                                "title": "SLEEP SCORE",
                                "display_text": str(sleep_score),
                                "value": sleep_score,
                                "type": "sleep_score",
                            },
                            {
                                "title": "TIME IN BED",
                                "display_text": "",
                                "unit": None,
                                "value": time_in_bed,
                                "type": "time_in_bed",
                            },
                            {"title": "TOTAL SLEEP", "display_text": "", "value": total_sleep, "type": "total_sleep"},
                            {
                                "title": "EFFICIENCY",
                                "display_text": "",
                                "value": round(total_sleep * 100 / time_in_bed),
                                "type": "sleep_efic",
                            },
                            {
                                "title": "AVG HEART RATE",
                                "display_text": "",
                                "unit": "bpm",
                                "value": sleep_hr,
                                "type": "avg_hr",
                            },
                            {"title": "AVG HRV", "display_text": "", "value": sleep_hrv, "type": "avg_hrv"},
                        ],
                        "quick_metrics_tiled": [
                            {
                                "title": "TOTAL SLEEP",
                                "value": "",
                                "tag": "Optimal",
                                "tag_color": "00B984",
                                "type": "total_sleep",
                            }
                        ],
                        "sleep_stages": [
                            {
                                "title": "Deep Sleep",
                                "type": "deep_sleep",
                                "percentage": round(deep * 100 / time_in_bed),
                                "stage_time_text": "",
                                "stage_time": deep,
                            },
                            {
                                "title": "Light Sleep",
                                "type": "light_sleep",
                                "percentage": round(light * 100 / time_in_bed),
                                "stage_time_text": "",
                                "stage_time": light,
                            },
                            {
                                "title": "REM Sleep",
                                "type": "rem_sleep",
                                "percentage": round(rem * 100 / time_in_bed),
                                "stage_time_text": "",
                                "stage_time": rem,
                            },
                            {
                                "title": "Awake",
                                "type": "awake",
                                "percentage": round(awake * 100 / time_in_bed),
                                "stage_time_text": "",
                                "stage_time": awake,
                            },
                        ],
                        "sleep_graph": {"title": "Time In Bed", "data": graph},
                        "movement_graph": [],
                        "hr_graph": [],
                        "hrv_graph": [],
                        "temp_graph": [],
                        "respiratory_graph": [],
                        "summary": [
                            {
                                "title": "Sleep Efficiency",
                                "state": "optimal",
                                "state_title": "Optimal",
                                "score": int(_wave(i, 86, 6)),
                            },
                            {
                                "title": "Temperature",
                                "state": "optimal",
                                "state_title": "Optimal",
                                "score": int(_wave(i, 90, 5)),
                            },
                            {
                                "title": "Restfulness",
                                "state": "good",
                                "state_title": "Good",
                                "score": int(_wave(i, 78, 8)),
                            },
                            {"title": "HR Drop", "state": "good", "state_title": "Good", "score": int(_wave(i, 74, 9))},
                            {
                                "title": "Timing",
                                "state": "optimal",
                                "state_title": "Optimal",
                                "score": int(_wave(i, 88, 6)),
                            },
                        ],
                        "sleep_inertia_trend": {},
                        "sleep_inertia_interpretation": {},
                        "score_trend": {},
                        "index_tracking_params": [],
                        "spo2": {"title": "Average Oxygen Saturation", "value": spo2, "is_beta": True},
                        "toss_turn": {"title": "Toss & Turn", "value": 10 + i % 5},
                        "sleep_cycles": {
                            "title": "Sleep Cycles • 5",
                            "legend": [
                                {"title": "3 Full", "color": "#008A46"},
                                {"title": "2 Partial", "color": "#004223"},
                            ],
                            "cycles": [
                                {
                                    "startTime": bed_start + c * 5400,
                                    "endTime": bed_start + (c + 1) * 5400,
                                    "cycleType": "full" if c < 3 else "partial",
                                }
                                for c in range(5)
                            ],
                        },
                        "tags_section": {},
                    },
                },
                single("recovery_index", "Recovery Index", int(_wave(i, 74, 12))),
                single("movement_index", "Movement Index", int(_wave(i, 66, 10))),
                single("active_minutes", "Active Minutes", int(_wave(i, 38, 12))),
                single("inactive_time", "Inactive Time", float(int(_wave(i, 420, 60)))),
                single("weekly_active_minutes", "Weekly Active Minutes", 210 + i * 4),
                single("vo2_max", "VO2 Max", 44.0),
                single("movements", "Movements", 3000 + i * 25),
                single("morning_alertness", "Morning Alertness", 6 + i % 3, unit="/10", status="Good"),
                single("metabolic_score", "Metabolic Score", int(_wave(i, 72, 9))),
                {
                    "type": "glucose",
                    "object": {
                        "day_start_timestamp": midnight,
                        "title": "Glucose",
                        "values": values(day, hours, lambda h: 88 + (h * 9 + i) % 40),
                    },
                },
                single("average_glucose", "Average Glucose", int(_wave(i, 98, 6))),
                single("glucose_variability", "Glucose Variability", round(_wave(i, 14.0, 3.0), 1)),
                single("hba1c", "HbA1c", 5.2),
                single("time_in_target", "Time In Target", int(_wave(i, 91, 5))),
            ]
        },
        "error": None,
        "status": 200,
    }


def ultrahuman_seed() -> dict[str, Any]:
    """The UTC user's responses, and the same days for each user elsewhere under their offset."""

    def responses(offset: int) -> dict[str, Any]:
        return {d.isoformat(): _ultrahuman_day(i, d, i == DAYS - 1, offset) for i, d in _days()}

    return {
        "anchor_date": ANCHOR_DATE.isoformat(),
        "responses": responses(0),
        "responses_by_zone": {zone: responses(offset) for zone, offset in ULTRAHUMAN_ZONES.items()},
    }


# ---------------------------------------------------------------------------
# WHOOP
# ---------------------------------------------------------------------------


def _whoop_time(day: date, hour: float, offset: int) -> str:
    """A local wall-clock hour as WHOOP writes instants: UTC, milliseconds, ``Z``."""
    instant = datetime.fromtimestamp(_epoch(day, hour, offset), tz=timezone.utc)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _whoop_id(zone: str, kind: str, n: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rumi-fixture:whoop:{zone}:{kind}:{n}"))


def _whoop_night(i: int, day: date) -> tuple[float, float]:
    """Sleep onset (hours from local midnight of the evening before ``day``) and wake (hours into ``day``)."""
    return 22.75 + ((i * 7) % 5) * 0.1, 6.4 + ((i * 3) % 4) * 0.1


def _whoop_sleep(i: int, day: date, zone: str, offset: int) -> dict[str, Any]:
    onset, wake = _whoop_night(i, day)
    in_bed = round((24 - onset + wake) * 3_600_000)
    awake, deep, rem = 1_680_000 + i * 30_000, 5_700_000 + (i % 4) * 180_000, 6_000_000 - (i % 3) * 240_000
    end = _whoop_time(day, wake, offset)
    return {
        "id": _whoop_id(zone, "sleep", i),
        "cycle_id": _whoop_cycle_id(zone, i),
        "user_id": WHOOP_USER_ID,
        "created_at": end,
        "updated_at": end,
        "start": _whoop_time(day - timedelta(days=1), onset, offset),
        "end": end,
        "timezone_offset": zone,
        "nap": False,
        "score_state": "SCORED",
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": in_bed,
                "total_awake_time_milli": awake,
                "total_no_data_time_milli": 0,
                "total_light_sleep_time_milli": in_bed - awake - deep - rem,
                "total_slow_wave_sleep_time_milli": deep,
                "total_rem_sleep_time_milli": rem,
                "sleep_cycle_count": 4 + i % 2,
                "disturbance_count": 8 + (i * 5) % 7,
            },
            "sleep_needed": {
                "baseline_milli": 27_900_000,
                "need_from_sleep_debt_milli": 900_000 + (i % 3) * 600_000,
                "need_from_recent_strain_milli": 420_000 + (i % 4) * 150_000,
                # The night after the nap: WHOOP takes the nap off what the night needs
                "need_from_recent_nap_milli": -1_920_000 if i == DAYS - 2 else 0,
            },
            "respiratory_rate": round(_wave(i, 14.6, 0.6), 1),
            "sleep_performance_percentage": int(_wave(i, 84, 12)),
            "sleep_consistency_percentage": int(_wave(i, 78, 8)),
            "sleep_efficiency_percentage": round((in_bed - awake) * 100 / in_bed, 2),
        },
    }


def _whoop_nap(i: int, day: date, zone: str, offset: int) -> dict[str, Any]:
    in_bed, awake = 1_920_000, 240_000
    end = _whoop_time(day, 14.7, offset)
    return {
        "id": _whoop_id(zone, "nap", i),
        "cycle_id": _whoop_cycle_id(zone, i),
        "user_id": WHOOP_USER_ID,
        "created_at": end,
        "updated_at": end,
        "start": _whoop_time(day, 14.7 - in_bed / 3_600_000, offset),
        "end": end,
        "timezone_offset": zone,
        "nap": True,
        "score_state": "SCORED",
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": in_bed,
                "total_awake_time_milli": awake,
                "total_no_data_time_milli": 0,
                "total_light_sleep_time_milli": 1_260_000,
                "total_slow_wave_sleep_time_milli": 420_000,
                "total_rem_sleep_time_milli": 0,
                "sleep_cycle_count": 0,
                "disturbance_count": 1,
            },
            "sleep_needed": {
                "baseline_milli": 0,
                "need_from_sleep_debt_milli": 0,
                "need_from_recent_strain_milli": 0,
                "need_from_recent_nap_milli": 0,
            },
            "respiratory_rate": 14.9,
            "sleep_performance_percentage": None,
            "sleep_consistency_percentage": None,
            "sleep_efficiency_percentage": round((in_bed - awake) * 100 / in_bed, 2),
        },
    }


def _whoop_cycle_id(zone: str, i: int) -> int:
    return 1_100_000 + list(WHOOP_ZONES).index(zone) * 100 + i


def _whoop_cycle(i: int, day: date, zone: str, offset: int, revision: int = 0) -> dict[str, Any]:
    """The cycle that opens with the night ending on ``day``; the last one is still running. ``revision`` 1 is
    the running cycle WHOOP reports later in the day."""
    onset, _ = _whoop_night(i, day)
    running = i == DAYS - 1
    if running:
        updated_hour, strain, kilojoule, steps, avg_hr, max_hr = (
            (20.5, 9.8, 7_900.4, 9_120, 69, 162) if revision else (10.0, 6.1, 4_102.7, 4_210, 71, 128)
        )
        updated_at, end = _whoop_time(day, updated_hour, offset), None
    else:
        next_onset, _ = _whoop_night(i + 1, day + timedelta(days=1))
        end = _whoop_time(day, next_onset, offset)
        updated_at = _whoop_time(day + timedelta(days=1), 7.0, offset)
        strain, kilojoule = round(_wave(i, 11.8, 4.2), 1), round(_wave(i, 9_840.0, 1_400.0), 1)
        steps, avg_hr, max_hr = int(_wave(i, 9_400, 2_600)), int(_wave(i, 68, 4)), int(_wave(i, 166, 10))
    return {
        "id": _whoop_cycle_id(zone, i),
        "user_id": WHOOP_USER_ID,
        "created_at": _whoop_time(day - timedelta(days=1), onset, offset),
        "updated_at": updated_at,
        "start": _whoop_time(day - timedelta(days=1), onset, offset),
        "end": end,
        "timezone_offset": zone,
        "score_state": "SCORED",
        "step_count": steps,
        "score": {"strain": strain, "kilojoule": kilojoule, "average_heart_rate": avg_hr, "max_heart_rate": max_hr},
    }


def _whoop_recovery(i: int, day: date, zone: str, offset: int) -> dict[str, Any]:
    _, wake = _whoop_night(i, day)
    scored = _whoop_time(day, wake + 0.25, offset)
    return {
        "cycle_id": _whoop_cycle_id(zone, i),
        "sleep_id": _whoop_id(zone, "sleep", i),
        "user_id": WHOOP_USER_ID,
        "created_at": scored,
        "updated_at": scored,
        "score_state": "SCORED",
        "score": {
            "user_calibrating": False,
            "recovery_score": int(_wave(i, 61, 27)),
            "resting_heart_rate": int(_wave(i, 54, 4)),
            "hrv_rmssd_milli": round(_wave(i, 58.4, 12.0) + 0.137, 3),
            "spo2_percentage": round(_wave(i, 95.8, 1.2) + 0.04, 2),
            "skin_temp_celsius": round(_wave(i, 33.6, 0.5) + 0.03, 2),
        },
    }


# (day index, local start hour, minutes, sport_name, sport_id, score); zone_durations are milliseconds from zone 0 up.
_WHOOP_WORKOUTS: tuple[tuple[int, float, int, str, int, dict[str, Any]], ...] = (
    # Garmin records the same run: its activity starts at 07:30 the same morning
    (
        DAYS - 2,
        7.52,
        48,
        "running",
        0,
        {
            "strain": 12.4,
            "average_heart_rate": 151,
            "max_heart_rate": 173,
            "kilojoule": 2_590.2,
            "percent_recorded": 100,
            "distance_meter": None,
            "altitude_gain_meter": None,
            "altitude_change_meter": None,
            "zones": (30_000, 120_000, 540_000, 1_260_000, 870_000, 60_000),
        },
    ),
    (
        DAYS - 4,
        7.0,
        55,
        "cycling",
        1,
        {
            "strain": 13.1,
            "average_heart_rate": 142,
            "max_heart_rate": 168,
            "kilojoule": 3_020.6,
            "percent_recorded": 100,
            # Distance only arrives when the ride was recorded in the WHOOP app with location
            "distance_meter": 24_150.0,
            "altitude_gain_meter": 186.4,
            "altitude_change_meter": 4.2,
            "zones": (60_000, 300_000, 1_140_000, 1_380_000, 420_000, 0),
        },
    ),
    (
        DAYS - 7,
        17.67,
        65,
        "weightlifting",
        45,
        {
            "strain": 8.9,
            "average_heart_rate": 118,
            "max_heart_rate": 152,
            "kilojoule": 1_460.3,
            "percent_recorded": 98.5,
            "distance_meter": None,
            "altitude_gain_meter": None,
            "altitude_change_meter": None,
            "zones": (600_000, 1_500_000, 1_200_000, 480_000, 120_000, 0),
        },
    ),
)
_WHOOP_ZONE_KEYS = ("zero", "one", "two", "three", "four", "five")


def _whoop_workout(n: int, zone: str, offset: int) -> dict[str, Any]:
    i, hour, minutes, sport, sport_id, score = _WHOOP_WORKOUTS[n]
    day = _days()[i][1]
    end = _whoop_time(day, hour + minutes / 60, offset)
    assert sum(score["zones"]) == minutes * 60_000
    return {
        "id": _whoop_id(zone, "workout", n),
        "user_id": WHOOP_USER_ID,
        "created_at": _whoop_time(day, hour + minutes / 60 + 0.1, offset),
        "updated_at": _whoop_time(day, hour + minutes / 60 + 0.1, offset),
        "start": _whoop_time(day, hour, offset),
        "end": end,
        "timezone_offset": zone,
        "sport_name": sport,
        "sport_id": sport_id,
        "score_state": "SCORED",
        "score": {
            **{k: v for k, v in score.items() if k != "zones"},
            "zone_durations": {f"zone_{k}_milli": v for k, v in zip(_WHOOP_ZONE_KEYS, score["zones"], strict=True)},
        },
    }


def whoop_seed() -> dict[str, Any]:
    """Each zone's WHOOP account: one zone per fixture user, so their nights line up with their other device."""

    def account(zone: str, offset: int) -> dict[str, Any]:
        days = _days()
        nap_index = DAYS - 3
        return {
            "sleeps": [_whoop_sleep(i, d, zone, offset) for i, d in days]
            + [_whoop_nap(nap_index, days[nap_index][1], zone, offset)],
            "recoveries": [_whoop_recovery(i, d, zone, offset) for i, d in days],
            "cycles": [_whoop_cycle(i, d, zone, offset) for i, d in days],
            "cycles_refreshed": [_whoop_cycle(DAYS - 1, days[-1][1], zone, offset, revision=1)],
            "workouts": [_whoop_workout(n, zone, offset) for n in range(len(_WHOOP_WORKOUTS))],
            "body_measurement": {"height_meter": 1.78, "weight_kilogram": 76.2, "max_heart_rate": 191},
        }

    return {
        "anchor_date": ANCHOR_DATE.isoformat(),
        "user_id_placeholder": WHOOP_USER_ID,
        "accounts": {zone: account(zone, offset) for zone, offset in WHOOP_ZONES.items()},
    }


def main() -> None:
    for name, seed in (("garmin", garmin_seed()), ("ultrahuman", ultrahuman_seed()), ("whoop", whoop_seed())):
        path = SEEDS_DIR / f"{name}.json"
        path.write_text(json.dumps(seed, indent=1, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(SEEDS_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
