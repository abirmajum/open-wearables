from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory

# Metric types that carry a ``values`` timeline (Ultrahuman type → SeriesType).
ACTIVITY_SAMPLE_SERIES: dict[str, SeriesType] = {
    "hr": SeriesType.heart_rate,
    "hrv": SeriesType.heart_rate_variability_rmssd,
    "temp": SeriesType.skin_temperature,
    "steps": SeriesType.steps,
    "glucose": SeriesType.blood_glucose,
}

# Single daily values (Ultrahuman type → SeriesType), each carrying its own day_start_timestamp.
DAILY_SCALAR_SERIES: dict[str, SeriesType] = {
    "vo2_max": SeriesType.vo2_max,
    "active_minutes": SeriesType.exercise_time,
    "inactive_time": SeriesType.sedentary_time,
    "sleep_rhr": SeriesType.resting_heart_rate,
    "temperature_deviation": SeriesType.skin_temperature_deviation,
}

# Daily scores (Ultrahuman type → category), each recorded at its own day_start_timestamp.
DAILY_SCORES: dict[str, HealthScoreCategory] = {
    "recovery_index": HealthScoreCategory.RECOVERY,
    "movement_index": HealthScoreCategory.MOVEMENT_INDEX,
    "metabolic_score": HealthScoreCategory.METABOLIC,
}

# The day's metrics stored with each score (component name → path from the metric type to an object carrying
# ``value``). Recovery's names are Whoop's where the meaning matches, so the recovery summary reads them as vitals.
SCORE_COMPONENTS: dict[HealthScoreCategory, dict[str, tuple[str, ...]]] = {
    HealthScoreCategory.RECOVERY: {
        "resting_heart_rate": ("sleep_rhr",),
        "hrv_rmssd_milli": ("avg_sleep_hrv",),
        "spo2_percentage": ("Sleep", "spo2"),
        "skin_temp_deviation_celsius": ("temperature_deviation",),
    },
    HealthScoreCategory.MOVEMENT_INDEX: {
        "movements": ("movements",),
    },
    HealthScoreCategory.METABOLIC: {
        "average_glucose_mg_dl": ("average_glucose",),
        "glucose_variability_percent": ("glucose_variability",),
        "hba1c_percent": ("hba1c",),
        "time_in_target_percent": ("time_in_target",),
    },
    HealthScoreCategory.SLEEP: {
        "spo2_percentage": ("Sleep", "spo2"),
        "toss_and_turn": ("Sleep", "toss_turn"),
        "morning_alertness": ("morning_alertness",),
    },
}

TIMESERIES: frozenset[SeriesType] = frozenset(
    {
        *ACTIVITY_SAMPLE_SERIES.values(),  # /user_data/metrics timelines
        *DAILY_SCALAR_SERIES.values(),  # /user_data/metrics, one value per day
    }
)

# Ultrahuman has no workout support.
WORKOUT_FIELDS: frozenset[str] = frozenset()

# EventRecordDetail fields populated by data_247.py (sleep records)
SLEEP_FIELDS: frozenset[str] = frozenset(
    {
        "sleep_total_duration_minutes",
        "sleep_time_in_bed_minutes",
        "sleep_efficiency_score",
        "sleep_deep_minutes",
        "sleep_rem_minutes",
        "sleep_light_minutes",
        "sleep_awake_minutes",
        "is_nap",
        "sleep_stages",
    }
)

HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset({*DAILY_SCORES.values(), HealthScoreCategory.SLEEP})
