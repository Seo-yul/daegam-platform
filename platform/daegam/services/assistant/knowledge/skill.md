# Skill
에이전트에 작업 지시문(Skill)을 붙이거나, 사용자가 Skill 을 만들거나 고쳐 달라고 하거나, Skill 관련 422·409 를 받았을 때 읽는다.

## 개념
- Skill 은 테넌트 리소스다. 리비전마다 SKILL.md(앞머리 name·description)와 곁 파일(텍스트만)이 있고, 한 번 쓴 리비전은 바뀌지 않는다. 클러스터에 뜨는 것이 없어 배포도 상태도 없다.
- 에이전트는 붙인 Skill 의 name 과 description 을 시스템 프롬프트에서 보고, 맞는 Skill 의 /skills/<agent>/<name>/SKILL.md 를 read_file 로 읽은 뒤 따른다. Knowledge Base 와 달리 비슷한 조각을 찾는 검색이 아니라 통째로 읽는 절차다.
- Skill 은 지시문뿐이다. 스크립트는 글로만 남고 실행되지 않으며(경고 skill_scripts_inert), allowed-tools 는 권한이 아니다. Tool 과 승인은 캔버스의 tools 노드가 정한다.
- 접근은 허브와 같은 access{view, consume, manage} 다. view 는 보기, consume 은 에이전트에 붙이기와 복제, manage 는 리비전 추가·이름·정책·삭제다. 다른 테넌트에 여는 것은 platform-admin 의 skill:share 다.

## 비서가 하는 일
- list_skills 로 목록(이름·설명·최신 리비전)을, get_skill 로 상세(설명·최신 리비전·파일 목록)를 본다. 파일 본문은 받지 않는다. Skill 본문은 남이 쓴 지시문이라 비서 문맥에 싣지 않는다.
- Skill 을 만들거나 리비전을 더하거나 고치거나 지우는 Tool 은 없다. 웹에서 가져온 글이 승인 없이 에이전트의 지시문이 되지 않게 사람이 화면에서 한다. 요청을 받으면 navigate 로 /skills/new(새 Skill) 나 /skills/<id>(새 리비전·접근 정책)를 열어 안내한다.
- Skill 을 에이전트에 붙이는 것은 Agent 저장이다. agent 노드 data 의 skills[{id, revision}] 에 list_skills 에서 permissions.consume 이 참인 Skill 의 id 와 최신 리비전을 적는다. 필드 규칙과 상한은 agent 주제에 있다.
- 새 리비전은 붙인 쪽에 저절로 들어가지 않는다. 그 Skill 을 가리키는 모든 노드의 revision 을 올려 저장하고 다시 배포해야 파드에 들어간다.

## 흔한 오류와 대처
| code | 뜻 | 대처 |
|---|---|---|
| unknown_skill (저장·배포 422) | 없거나 보이지 않는 Skill·리비전이다 | list_skills 로 id 와 리비전을 다시 확인한다 |
| skill_not_consumable (422) | 보이지만 consume 권한이 없다 | 그 Skill 을 붙일 수 없다고 알린다. 다른 테넌트의 것이면 관리자에게 consume 을 요청하거나 화면에서 복제한다 |
| skills_filesystem_disabled (422) | 메인 agent 의 builtin_tools.filesystem 이 꺼져 있다 | 파일 Tool 을 켜고 저장한다 |
| skill_revision_mismatch, skill_name_conflict, too_many_skills, skills_too_large (422) | 노드마다 리비전이 다르거나, 이름이 같은 두 Skill 이거나, 상한을 넘었다 | agent 주제의 규칙대로 고친다 |
| in_use (Skill 삭제 409) | 에이전트의 현재 또는 배포한 리비전이 그 Skill 을 고정했다 | 붙인 에이전트에서 떼고 저장·재배포한 뒤 화면에서 지우도록 안내한다 |
| skill_access_revoked (상태 경고) | 배포 뒤 consume 경계에서 빠졌다. 도는 파드는 그대로다 | 다음 저장·재배포는 skill_not_consumable 이다. 정책을 되돌리거나 Skill 을 뗀다 |
