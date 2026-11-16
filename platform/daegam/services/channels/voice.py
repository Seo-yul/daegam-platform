"""Teams 봇이 대화에 고정 줄을 올리는 곳 (P47 6.2절·6.3절).

받는 경로, 연결의 확인, 연결의 정리가 함께 쓴다. 올릴 대화는 검증한 활동이 실은 것(연결이 없을 때는
멤버 확인을 지난 대화, 연결이 있으면 연결의 대화)이고, 주소 규칙과 봇 토큰은 `connector.py` 가
지킨다. 실패는 로그 한 줄로 접고 False 다. 고정 줄 하나를 못 올렸다고 받는 경로나 화면의 요청이
실패하지는 않는다.
"""

from __future__ import annotations

import logging

from daegam.services.channels.connector import ConnectorFailed, ServiceUrlRefused, TeamsConnector
from daegam.services.channels.errors import ChannelError
from daegam.services.channels.texts import text
from daegam.store.channels import TeamsAccount
from daegam.store.settings import MongoPlatformSettingsStore, PlatformSettings

logger = logging.getLogger("daegam.channels")


class TeamsVoice:
    def __init__(self, connector: TeamsConnector, platform_settings: MongoPlatformSettingsStore):
        self.connector = connector
        self.platform_settings = platform_settings

    async def say(
        self,
        service_url: str,
        conversation_id: str,
        key: str,
        language: str | None = None,
        *,
        app_id: str | None = None,
        settings: PlatformSettings | None = None,
        **values: str,
    ) -> bool:
        """대화에 글 하나를 올린다. app_id 를 주면 지금 봇의 App ID 가 그것일 때만 올린다. 앞 봇의
        대화에 지금 봇의 토큰을 보내지 않으려는 것이다. settings 는 부르는 쪽이 이미 읽은 Platform
        Settings 이고, 없으면 여기서 읽는다."""
        settings = settings or await self.platform_settings.get()
        teams = settings.channels.teams
        if not teams.app_id or not teams.entra_tenant_id:
            return False
        if app_id is not None and app_id != teams.app_id:
            return False
        activity = {"type": "message", "text": text(key, language, **values)}
        try:
            await self.connector.post(
                service_url,
                conversation_id,
                activity,
                tenant=teams.entra_tenant_id,
                app_id=teams.app_id,
                ca_bundle=settings.network.ca_bundle,
            )
        except ServiceUrlRefused as e:
            logger.warning("teams line %s not posted: service url refused (%s)", key, e.reason)
            return False
        except ConnectorFailed as e:
            logger.warning("teams line %s not posted: connector answered %s", key, e.status)
            return False
        except ChannelError as e:
            logger.warning("teams line %s not posted: %s", key, e.code)
            return False
        return True

    async def say_to(
        self, account: TeamsAccount, key: str, language: str | None = None, **values: str
    ) -> bool:
        """연결이나 확인 대기의 대화에 올린다."""
        return await self.say(
            account.service_url,
            account.conversation_id,
            key,
            language,
            app_id=account.app_id,
            **values,
        )


__all__ = ["TeamsVoice"]
