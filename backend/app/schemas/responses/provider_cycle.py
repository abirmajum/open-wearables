from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, model_validator


class CycleSource(BaseModel):
    provider: Literal["whoop"] = "whoop"
    source: str = "whoop"


class CycleSummary(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=64)
    source: CycleSource = Field(default_factory=CycleSource)
    start_time: AwareDatetime
    end_time: AwareDatetime | None
    updated_at: AwareDatetime
    fetched_at: AwareDatetime
    score_state: Literal["SCORED", "PENDING_SCORE", "UNSCORABLE"]
    zone_offset: str | None = None
    steps: int | None = Field(default=None, ge=0)
    strain: float | None = Field(default=None, ge=0, le=21)
    energy_kj: float | None = Field(default=None, ge=0)
    total_calories_kcal: float | None = Field(default=None, ge=0)
    avg_heart_rate_bpm: float | None = Field(default=None, ge=0)
    max_heart_rate_bpm: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_interval(self) -> "CycleSummary":
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("cycle end precedes start")
        return self

    @computed_field
    @property
    def provisional(self) -> bool:
        return self.end_time is None or self.score_state != "SCORED"
