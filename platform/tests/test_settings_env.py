"""환경 변수에서 Settings 를 읽는 규칙 (config/__init__.py settings_from_env)."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from daegam.config import Settings, settings_from_env


def test_empty_env_gives_the_dataclass_defaults(bare_env):
    """기본값은 Settings 한 곳에만 있다. env 가 없으면 settings_from_env 도 같은 값을 쓴다."""
    got = settings_from_env()
    assert got == replace(Settings(), pod_id=got.pod_id)


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("DAEGAM_PLATFORM_PORT", "platform_port"),
        ("DAEGAM_RECONCILE_INTERVAL", "reconcile_interval"),
        ("DAEGAM_AUDIT_TTL_DAYS", "audit_ttl_days"),
        ("DAEGAM_LOCAL_TOKEN_TTL", "local_token_ttl"),
        ("DAEGAM_PAT_KEY_POOL", "pat_key_pool"),
        ("DAEGAM_PAT_MAX_DAYS", "pat_max_days"),
        ("DAEGAM_OAUTH_REFRESH_DAYS", "oauth_refresh_days"),
        ("DAEGAM_FEEDBACK_RETENTION_DAYS", "feedback_retention_days"),
        ("DAEGAM_AGENT_TURN_DRAIN_SECONDS", "agent_turn_drain_seconds"),
        ("DAEGAM_AGENT_SLEEP_AFTER_SECONDS", "agent_sleep_after_seconds"),
        ("DAEGAM_AGENT_HOUSEKEEPING_HOURS", "agent_housekeeping_hours"),
        ("DAEGAM_AGENT_HOUSEKEEPING_PARALLEL", "agent_housekeeping_parallel"),
        ("DAEGAM_AGENT_HOUSEKEEPING_HOLD_SECONDS", "agent_housekeeping_hold_seconds"),
        ("DAEGAM_AGENT_PREVIEWS_PER_TENANT", "agent_previews_per_tenant"),
        ("DAEGAM_EVALUATION_RUN_TOKENS", "evaluation_run_tokens"),
        ("DAEGAM_EVALUATION_RUNS_PER_DAY", "evaluation_runs_per_day"),
        ("DAEGAM_EVALUATION_RESULT_DAYS", "evaluation_result_days"),
    ],
)
def test_a_bad_number_falls_back_to_the_default_with_a_warning(bare_env, caplog, key, field):
    bare_env.setenv(key, "sixty")
    with caplog.at_level(logging.WARNING, logger="daegam.config"):
        got = settings_from_env()
    assert getattr(got, field) == getattr(Settings(), field)
    assert any(key in m for m in caplog.messages)


def test_numbers_are_read(bare_env):
    bare_env.setenv("DAEGAM_PLATFORM_PORT", " 9100 ")
    bare_env.setenv("DAEGAM_AUDIT_COLLECTOR_INTERVAL", "2.5")
    got = settings_from_env()
    assert got.platform_port == 9100 and got.audit_collector_interval == 2.5


@pytest.mark.parametrize(("raw", "pool"), [("7", 7), ("0", 1), ("-3", 1), ("500", 200)])
def test_pat_key_pool_is_clamped_to_its_range(bare_env, raw, pool):
    """개인 토큰 키 슬롯 pool 은 1 이상 200 이하로 자른다 (P32 11절)."""
    bare_env.setenv("DAEGAM_PAT_KEY_POOL", raw)
    assert settings_from_env().pat_key_pool == pool


@pytest.mark.parametrize(
    ("raw", "transcripts", "days"),
    [("30", None, 30), ("60", None, 30), ("10", "7", 7), ("0", None, 1), ("5", "0", 1)],
)
def test_evaluation_results_stay_within_the_transcripts_and_at_least_a_day(
    bare_env, raw, transcripts, days
):
    """평가 결과는 전사 보존 기간을 넘지 않고 1일 아래로 내리지 않는다 (P45 9절)."""
    bare_env.setenv("DAEGAM_EVALUATION_RESULT_DAYS", raw)
    if transcripts is not None:
        bare_env.setenv("DAEGAM_TRANSCRIPT_RETENTION_DAYS", transcripts)
    assert settings_from_env().evaluation_result_days == days


def test_evaluations_are_on_by_default_and_turned_off_by_env(bare_env):
    assert settings_from_env().evaluations_enabled is True
    bare_env.setenv("DAEGAM_EVALUATIONS_ENABLED", "false")
    assert settings_from_env().evaluations_enabled is False


def test_the_gate_counts_criteria_only_when_turned_on(bare_env):
    """채점 기준은 B6 이 켤 때까지 게이트에 들지 않는다 (P45 11절)."""
    assert settings_from_env().evaluation_gate_criteria is False
    bare_env.setenv("DAEGAM_EVALUATION_GATE_CRITERIA", "true")
    assert settings_from_env().evaluation_gate_criteria is True


@pytest.mark.parametrize(("raw", "days"), [("30", 30), ("0", 1), ("-5", 1)])
def test_feedback_retention_is_at_least_a_day(bare_env, raw, days):
    """답변 피드백의 보존 기간은 1일 아래로 내리지 않는다. 0 이면 만들자마자 지워진다 (P36 6절)."""
    bare_env.setenv("DAEGAM_FEEDBACK_RETENTION_DAYS", raw)
    assert settings_from_env().feedback_retention_days == days


@pytest.mark.parametrize(("raw", "seconds"), [(None, 120), ("300", 300), ("0", 0), ("-5", 0)])
def test_the_agent_turn_drain_is_never_negative(bare_env, raw, seconds):
    """Agent 의 턴 drain 은 기본 120초이고 0 아래로 내리지 않는다. 0 이면 기다리지 않는다 (P78
    5절)."""
    if raw is not None:
        bare_env.setenv("DAEGAM_AGENT_TURN_DRAIN_SECONDS", raw)
    assert settings_from_env().agent_turn_drain_seconds == seconds


def test_scale_to_zero_defaults(bare_env):
    """플랫폼 스위치는 켜져 있고 창은 0 이라 A4 전에는 어떤 Agent 도 스스로 잠들지 않는다. 매일
    깨우기는 24시간마다 클러스터에 둘씩이고 10분 깨워 둔다 (P44 14절)."""
    got = settings_from_env()
    assert got.agent_scale_to_zero is True
    assert got.agent_sleep_after_seconds == 0
    assert (got.agent_housekeeping_hours, got.agent_housekeeping_parallel) == (24.0, 2)
    assert got.agent_housekeeping_hold_seconds == 600


@pytest.mark.parametrize(
    ("raw", "seconds", "warned"),
    [
        ("0", 0, False),
        ("-5", 0, False),
        ("30", 120, True),
        ("120", 120, False),
        ("1800", 1800, False),
    ],
)
def test_the_sleep_window_is_zero_or_at_least_two_minutes(bare_env, caplog, raw, seconds, warned):
    """창은 0 이거나 120초 이상이다. 그 사이 값은 120 으로 올리고 경고한다 (P44 7절)."""
    bare_env.setenv("DAEGAM_AGENT_SLEEP_AFTER_SECONDS", raw)
    with caplog.at_level(logging.WARNING, logger="daegam.config"):
        assert settings_from_env().agent_sleep_after_seconds == seconds
    assert any("DAEGAM_AGENT_SLEEP_AFTER_SECONDS" in m for m in caplog.messages) is warned


def test_scale_to_zero_settings_are_read_and_clamped(bare_env):
    bare_env.setenv("DAEGAM_AGENT_SCALE_TO_ZERO", "false")
    bare_env.setenv("DAEGAM_AGENT_HOUSEKEEPING_HOURS", "0.01")
    bare_env.setenv("DAEGAM_AGENT_HOUSEKEEPING_PARALLEL", "0")
    got = settings_from_env()
    assert got.agent_scale_to_zero is False
    assert got.agent_housekeeping_hours == 0.05
    assert got.agent_housekeeping_parallel == 1
    bare_env.setenv("DAEGAM_AGENT_HOUSEKEEPING_HOURS", "6.5")
    bare_env.setenv("DAEGAM_AGENT_HOUSEKEEPING_PARALLEL", "500")
    got = settings_from_env()
    assert (got.agent_housekeeping_hours, got.agent_housekeeping_parallel) == (6.5, 50)


@pytest.mark.parametrize(("raw", "seconds"), [("90", 90), ("10", 60), ("86400", 3600)])
def test_the_housekeeping_hold_is_clamped(bare_env, raw, seconds):
    """매일 깨우기의 hold 는 60초에서 한 시간 사이로 자른다. 게이트가 짧은 hold 를 쓴다
    (P44 14절)."""
    bare_env.setenv("DAEGAM_AGENT_HOUSEKEEPING_HOLD_SECONDS", raw)
    assert settings_from_env().agent_housekeeping_hold_seconds == seconds


@pytest.mark.parametrize(
    ("key", "field"),
    [("DAEGAM_PAT_MAX_DAYS", "pat_max_days"), ("DAEGAM_OAUTH_REFRESH_DAYS", "oauth_refresh_days")],
)
@pytest.mark.parametrize(
    ("raw", "days", "warned"), [("30", 30, False), ("0", 1, False), ("3650", 365, True)]
)
def test_credential_lifetimes_are_capped_with_a_warning(
    bare_env, caplog, key, field, raw, days, warned
):
    """개인 토큰 만료의 상한과 OAuth 연결의 수명은 1 이상 365 이하다. 365 를 넘으면 자르고 경고한다.
    설정 실수 하나가 10년짜리 자격을 만들지 않게 한다 (P32 11절, P33 17절)."""
    bare_env.setenv(key, raw)
    with caplog.at_level(logging.WARNING, logger="daegam.config"):
        assert getattr(settings_from_env(), field) == days
    assert any(key in m for m in caplog.messages) is warned


@pytest.mark.parametrize(
    ("raw", "seconds", "warned"),
    [(None, 604800, False), ("86400", 86400, False), ("99999999", 7776000, True)],
)
def test_the_long_bound_is_capped_at_90_days(bare_env, caplog, raw, seconds, warned):
    """oidc 모드의 긴 한도는 기본 7일이고 90일을 넘으면 자르고 경고한다 (P53b 6.3절)."""
    if raw is not None:
        bare_env.setenv("DAEGAM_IDP_CONFIRM_SECONDS", raw)
    with caplog.at_level(logging.WARNING, logger="daegam.config"):
        assert settings_from_env().idp_confirm_seconds == seconds
    assert any("DAEGAM_IDP_CONFIRM_SECONDS" in m for m in caplog.messages) is warned


def test_a_long_bound_below_the_interactive_one_stops_the_start(bare_env):
    """긴 한도가 대화형 한도보다 짧으면 실행과 개인 토큰이 채팅보다 먼저 끊긴다. oidc 모드는
    기동하지 않는다. 두 한도를 쓰지 않는 local 과 header 는 차트가 늘 싣는 값으로 막히지 않는다."""
    bare_env.setenv("DAEGAM_TURN_OWNER_CONFIRM_SECONDS", "7200")
    bare_env.setenv("DAEGAM_IDP_CONFIRM_SECONDS", "3600")
    bare_env.setenv("DAEGAM_AUTH_MODE", "oidc")
    with pytest.raises(ValueError, match="DAEGAM_IDP_CONFIRM_SECONDS"):
        settings_from_env()
    for mode in ("local", "header"):
        bare_env.setenv("DAEGAM_AUTH_MODE", mode)
        assert settings_from_env().idp_confirm_seconds == 3600


def test_service_accounts_take_one_of_three_values(bare_env):
    """서비스 어카운트를 누가 만드는가 (P53b 6.7절). 기본은 tenant-admin 이고 모르는 값이면
    기동하지 않는다."""
    assert settings_from_env().service_accounts == "tenant-admin"
    bare_env.setenv("DAEGAM_SERVICE_ACCOUNTS", " Off ")
    assert settings_from_env().service_accounts == "off"
    bare_env.setenv("DAEGAM_SERVICE_ACCOUNTS", "everyone")
    with pytest.raises(ValueError, match="DAEGAM_SERVICE_ACCOUNTS"):
        settings_from_env()


def test_preview_settings_are_read_and_clamped(bare_env):
    """미리 보기의 스위치와 테넌트마다의 수 (P44b 10절). 수는 0 과 상한 사이로 자른다."""
    got = settings_from_env()
    assert (got.agent_previews_enabled, got.agent_previews_per_tenant) == (True, 3)
    bare_env.setenv("DAEGAM_AGENT_PREVIEWS_ENABLED", "false")
    bare_env.setenv("DAEGAM_AGENT_PREVIEWS_PER_TENANT", "-1")
    got = settings_from_env()
    assert (got.agent_previews_enabled, got.agent_previews_per_tenant) == (False, 0)
    bare_env.setenv("DAEGAM_AGENT_PREVIEWS_PER_TENANT", "500")
    assert settings_from_env().agent_previews_per_tenant == 50
