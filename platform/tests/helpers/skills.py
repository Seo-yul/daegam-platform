"""Skill API 를 부르는 테스트가 함께 쓰는 도우미 (D64). 본문은 frontmatter 가 있는 SKILL.md 하나와
그 밖의 파일이다."""

from __future__ import annotations

from tests.helpers.consts import SK


def skill_md(name: str = "demo", description: str = "Does a thing.", body: str = "# Body\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def skill_files(name: str = "demo", body: str = "# Body\n", **extra: str) -> list[dict[str, str]]:
    files = [{"path": "SKILL.md", "content": skill_md(name, body=body)}]
    files += [{"path": path, "content": content} for path, content in extra.items()]
    return files


def created_skill(client, name: str = "demo", headers: dict | None = None, **extra: str) -> dict:
    """SKILL.md 와 extra(경로 → 내용) 파일로 만든 Skill 문서. 201 이 아니면 그 자리에서 실패한다."""
    r = client.post(SK, json={"files": skill_files(name, **extra)}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def revise_skill(client, skill: dict, body: str, expect: int, headers: dict | None = None):
    """SKILL.md 본문만 바꾼 새 리비전을 expect_revision 과 함께 올린 응답."""
    return client.post(
        f"{SK}/{skill['id']}/revisions",
        json={"files": skill_files(skill["name"], body=body), "expect_revision": expect},
        headers=headers,
    )
