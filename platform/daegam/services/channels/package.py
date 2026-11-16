"""Teams 앱 패키지 (P47 6.1절).

Teams 관리자가 업로드하는 zip 이다. 매니페스트(`manifest.json`), 영어 글(`en-us.json`), 인장에서
그린 두 아이콘(192 픽셀 색 아이콘, 32 픽셀 흰 윤곽 아이콘, `daegam/data/teams/`)을 담는다. 봇 id 와
플랫폼 호스트는 설치의 값이라 저장소에 패키지를 두지 않고 요청마다 짓는다(phase-82). 매니페스트는
`personal` 범위만 두고, 이름과 설명과 명령의 글은 `texts.py` 의 `manifest.` 열쇠다. 기본 언어는
한국어다. 매니페스트 판과 지역화 파일의 꼴은 Teams 의 공개 스키마를 따른 것이고, 업로드가 받는지는
F0.2 가 잰다.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from daegam.services.channels.texts import text

MANIFEST_VERSION = "1.17"
SCHEMA = (
    f"https://developer.microsoft.com/en-us/json-schemas/teams/v{MANIFEST_VERSION}/"
    "MicrosoftTeams.schema.json"
)
LOCALIZATION_SCHEMA = (
    f"https://developer.microsoft.com/en-us/json-schemas/teams/v{MANIFEST_VERSION}/"
    "MicrosoftTeams.Localization.schema.json"
)
# 패키지의 판. 매니페스트의 내용이 바뀌면 올린다. Teams 는 같은 판의 다시 올리기를 새것으로 보지
# 않는다
APP_VERSION = "1.0.0"
ICONS = Path(__file__).resolve().parents[2] / "data" / "teams"
ACCENT = "#C8372D"
DEFAULT_LANGUAGE_TAG = "ko-kr"
EN_LANGUAGE_TAG = "en-us"
EN_FILE = "en-us.json"
COMMANDS = ("link", "unlink", "agents", "new", "stop", "help")
SCOPES = ["personal"]


def _commands(language: str) -> list[dict[str, str]]:
    return [
        {
            "title": text(f"manifest.commands.{c}", language),
            "description": text(f"manifest.commands.{c}.description", language),
        }
        for c in COMMANDS
    ]


def manifest(app_id: str, public_url: str, host: str) -> dict[str, Any]:
    """Teams 앱 매니페스트. 봇 id 와 앱 id 는 봇의 App ID 다."""
    name = text("manifest.name", "ko")
    return {
        "$schema": SCHEMA,
        "manifestVersion": MANIFEST_VERSION,
        "version": APP_VERSION,
        "id": app_id,
        "localizationInfo": {
            "defaultLanguageTag": DEFAULT_LANGUAGE_TAG,
            "additionalLanguages": [{"languageTag": EN_LANGUAGE_TAG, "file": EN_FILE}],
        },
        "developer": {
            "name": name,
            "websiteUrl": public_url,
            "privacyUrl": public_url,
            "termsOfUseUrl": public_url,
        },
        "name": {"short": name, "full": name},
        "description": {
            "short": text("manifest.short", "ko"),
            "full": text("manifest.full", "ko"),
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": ACCENT,
        "bots": [
            {
                "botId": app_id,
                "scopes": SCOPES,
                "supportsFiles": False,
                "isNotificationOnly": False,
                "commandLists": [{"scopes": SCOPES, "commands": _commands("ko")}],
            }
        ],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [host],
    }


def localization() -> dict[str, str]:
    """영어 지역화 파일. 매니페스트의 글 칸을 경로로 가리킨다."""
    name = text("manifest.name", "en")
    out = {
        "$schema": LOCALIZATION_SCHEMA,
        "name.short": name,
        "name.full": name,
        "description.short": text("manifest.short", "en"),
        "description.full": text("manifest.full", "en"),
    }
    for i, command in enumerate(_commands("en")):
        prefix = f"bots[0].commandLists[0].commands[{i}]"
        out[f"{prefix}.title"] = command["title"]
        out[f"{prefix}.description"] = command["description"]
    return out


def app_package(app_id: str, public_url: str, host: str) -> bytes:
    """업로드할 zip 의 바이트."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest(app_id, public_url, host), indent=2))
        z.writestr(EN_FILE, json.dumps(localization(), indent=2, ensure_ascii=False))
        for icon in ("color.png", "outline.png"):
            z.writestr(icon, (ICONS / icon).read_bytes())
    return buf.getvalue()


__all__ = ["APP_VERSION", "app_package", "localization", "manifest"]
