"""e2e 의 Agent 저장 본문. e2e_agent·e2e_chat·e2e_evaluation·e2e_install·e2e_runs·e2e_sandbox·e2e_telemetry 가 함께 쓴다.

`GET /agents/templates` 의 첫 템플릿에서 시작해 모델 노드에 provider 를 넣는다. 나머지는 플래그다.

- Tool 노드: `--ms-id ID` 는 MCP Server 의 Add 하나로 채운다(`--approve` 면 승인 뒤에만 부른다). `--source KIND:ID` 는
  소스만 바꾸고 템플릿의 Tool 목록을 두며, `--allow A,B` 를 주면 그 이름만 남긴다(타임아웃과 승인도 그 이름만).
  `--tools KIND:ID:TOOL` 은 거듭 주는 만큼 Tool 노드를 템플릿의 것 대신 두고, 노드마다 그 소스의 Tool 하나를 승인 없이
  허용한다(e2e_evaluation 의 쓰기 Tool 과 읽기 Tool 소스 둘). 셋 다 없으면 Tool 노드를 뺀다. `--outside A,B` 는 그 Tool 의 결과를 바깥 내용으로 표시하고, `--rememberable A,B` 는
  그 Tool 의 승인을 사람이 기억할 수 있게 한다(P43 2·5절). `--approval-for A,B` 는 그 Tool 의 승인 규칙을 승인과 거절로
  둔다. 셋은 Tool 노드가 있을 때만 쓴다.
- memory 노드: `--ds-id ID` 면 그 Data Service 를 체크포인터로 쓰고 `--long-term-days N` 이면 장기 기억(D34)을 켠다.
  없으면 memory 노드와 그것을 필요로 하는 승인을 뺀다(인메모리 경로). `--files` 는 파일 도구를, `--approve-edit` 는
  승인에 고치기를 더한다.
- 분당 한도는 끈다. 한 분에 턴을 많이 돌리기 때문이다. `--rate-limit` 이면 템플릿의 한도를 둔다.
- 자동 실행(P41 10절)은 세션 예산이 있어야 한다. `--session-tokens N` 은 세션 토큰 예산을, `--session-ttl S` 는 세션
  TTL(초)을 둔다.
- `--kb-id ID --kb-tool T` 는 knowledge 노드를 더한다. `--sub-name S` 는 메인이 위임으로 부르는 서브에이전트를 더한다.
  메인과 같은 모델 노드를 쓰고, `--sub-provider P` 면 그 provider 를 쓰는 제 모델 노드를 둔다. `--sub-tools` 는
  서브에이전트에도 Tool 노드를 잇는다.
- `--judge` 는 메인과 같은 provider 의 판정용 모델 노드와 그것을 가리키는 Tool 가드레일(injection_check, block)을 더해
  메인에 붙인다(P43 7절, e2e_agent 8j).
- `--second` 는 e2e_chat 4g 의 둘째 리비전(파일 도구를 끄고 출력 가드레일을 더한다), `--record-bodies` 는 관측의 본문
  기록을 켠다.
- `--system-prompt TEXT` 는 메인 에이전트의 시스템 프롬프트를 바꾼다. 리비전마다 내용이 달라지게 할 때 쓴다.
- `--code-execution` 은 메인 에이전트의 코드 실행(P40 9절)을 승인 없이 켜고, `--code-approval` 은 execute 를 승인과
  거절로 묻게 켠다. 서브에이전트는 메인의 작업 공간을 함께 쓰므로 칸은 메인에만 둔다. `--turn-timeout S` 는 메인의 턴
  시간 제한(초)이다.

    python3 agent_body.py TEMPLATES OUT --name N --display D --provider P [플래그...]

맥의 시스템 python3(3.9)에서도 돌아야 하므로 3.10 이후 문법을 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import json

ALLOWED_WITH_EDIT = ["approve", "edit", "reject"]


def build(template: dict, args: argparse.Namespace) -> dict:
    graph, layout = template["graph"], template["layout"]

    def nodes(node_type: str) -> list:
        return [n for n in graph["nodes"] if n["type"] == node_type]

    def node(node_type: str) -> dict:
        return nodes(node_type)[0]

    def drop(node_type: str) -> None:
        """그 종류의 노드와 거기 닿는 간선·배치를 뺀다."""
        gone = {n["id"] for n in nodes(node_type)}
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] not in gone]
        graph["edges"] = [e for e in graph["edges"] if not {e["source"], e["target"]} & gone]
        for node_id in gone:
            layout.pop(node_id, None)

    def attach(source: str, target: str, kind: str) -> None:
        edge = {"id": f"e-{source}-{target}", "source": source, "target": target, "kind": kind}
        graph["edges"].append(edge)

    main = next(n for n in nodes("agent") if n["data"]["role"] == "main")
    model = node("model")
    model["data"]["provider"] = args.provider
    if args.ms_id:
        approval = {"Add": ["approve"]} if args.approve else {}
        source = {"kind": "mcp_instance", "id": args.ms_id}
        node("tools")["data"].update(source=source, allow_tools=["Add"], approval=approval, timeouts={})
    elif args.tools:
        drop("tools")
        for i, spec in enumerate(args.tools, start=1):
            kind, source_id, tool = spec.split(":", 2)
            node_id = f"tools-{i}"
            data = {"source": {"kind": kind, "id": source_id}, "allow_tools": [tool], "approval": {}, "timeouts": {}}
            graph["nodes"].append({"id": node_id, "type": "tools", "data": data})
            attach(main["id"], node_id, "tools")
            layout[node_id] = {"x": 620 + 140 * i, "y": 80}
    elif args.source:
        kind, source_id = args.source.split(":", 1)
        tools = node("tools")["data"]
        tools["source"] = {"kind": kind, "id": source_id}
        if args.allow:
            allow = args.allow.split(",")
            tools["allow_tools"] = allow
            tools["timeouts"] = {k: v for k, v in tools["timeouts"].items() if k in allow}
            tools["approval"] = {k: v for k, v in tools["approval"].items() if k in allow}
    else:
        drop("tools")
    if args.outside or args.rememberable or args.approval_for:
        if not nodes("tools"):
            raise SystemExit(
                "--outside, --rememberable and --approval-for need a Tool node (--ms-id or --source)"
            )
        tools = node("tools")["data"]
        if args.outside:
            tools["outside"] = {name: True for name in args.outside.split(",")}
        if args.rememberable:
            tools["rememberable"] = args.rememberable.split(",")
        if args.approval_for:
            tools["approval"] = {name: ["approve", "reject"] for name in args.approval_for.split(",")}
    if args.ds_id:
        long_term = {"retention_days": args.long_term_days} if args.long_term_days else None
        node("memory")["data"].update(checkpointer={"data_service_id": args.ds_id}, long_term=long_term)
    else:
        drop("memory")
        for n in nodes("tools"):
            n["data"]["approval"] = {}
    if not args.rate_limit:
        node("entry")["data"]["rate_limit"] = None
    if args.session_ttl:
        node("entry")["data"]["session_ttl_seconds"] = args.session_ttl
    if args.files:
        # /memories/ 를 읽고 쓰는 것이 파일 도구라 장기 기억에는 이것이 켜져 있어야 한다(long_term_filesystem_disabled)
        main["data"]["builtin_tools"]["filesystem"] = True
    if args.approve_edit:
        for n in nodes("tools"):
            n["data"]["approval"] = {tool: ALLOWED_WITH_EDIT for tool in n["data"]["approval"]}
    if args.second:
        # 둘째 리비전은 파일 도구를 끈다. 장기 기억은 파일 도구로 읽고 쓰므로 함께 꺼야 한다
        main["data"]["builtin_tools"] = dict(main["data"]["builtin_tools"], filesystem=False)
    if args.sub_name:
        sub_data = dict(main["data"], role="sub", name=args.sub_name, description=args.sub_description)
        graph["nodes"].append({"id": "agent-sub", "type": "agent", "data": sub_data})
        attach(main["id"], "agent-sub", "delegate")
        sub_model = model["id"]
        if args.sub_provider:
            sub_model = "model-sub"
            data = dict(model["data"], name="서브 모델", provider=args.sub_provider)
            graph["nodes"].append({"id": sub_model, "type": "model", "data": data})
            layout[sub_model] = {"x": 940, "y": 260}
        attach("agent-sub", sub_model, "model")
        if args.sub_tools and nodes("tools"):
            attach("agent-sub", node("tools")["id"], "tools")
        layout["agent-sub"] = {"x": 780, "y": 420}
    if args.kb_id:
        knowledge = {
            "knowledge_base_id": args.kb_id,
            "tool_name": args.kb_tool,
            "description": "",
            "top_k": args.top_k,
            "min_score": None,
        }
        graph["nodes"].append({"id": "knowledge", "type": "knowledge", "data": knowledge})
        attach(main["id"], "knowledge", "knowledge")
        layout["knowledge"] = {"x": 780, "y": 60}
    if args.second:
        rules = [{"kind": "pii_mask", "value": {"types": ["email"]}}]
        guardrail = {"position": "output", "rules": rules, "on_violation": "flag"}
        graph["nodes"].append({"id": "guardrail-output", "type": "guardrail", "data": guardrail})
        attach(main["id"], "guardrail-output", "guardrail")
        layout["guardrail-output"] = {"x": 560, "y": 420}
    if args.judge:
        judge_model = dict(node("model")["data"], name="judge", fallback=None)
        graph["nodes"].append({"id": "model-judge", "type": "model", "data": judge_model})
        layout["model-judge"] = {"x": 940, "y": 420}
        rule = {"kind": "injection_check", "value": {"model": "model-judge", "max_chars": 8000, "max_calls": 20}}
        guardrail = {"position": "tool", "rules": [rule], "on_violation": "block", "message": "blocked by the judge"}
        graph["nodes"].append({"id": "guardrail-judge", "type": "guardrail", "data": guardrail})
        attach(main["id"], "guardrail-judge", "guardrail")
        layout["guardrail-judge"] = {"x": 720, "y": 420}
    if args.code_execution or args.code_approval:
        approval = ["approve", "reject"] if args.code_approval else []
        main["data"]["code_execution"] = {"approval": approval}
    if args.turn_timeout:
        main["data"]["turn_timeout_seconds"] = args.turn_timeout
    if args.system_prompt:
        main["data"]["system_prompt"] = args.system_prompt
    service = template["service"]
    if args.record_bodies:
        service = dict(service, observability={"record_bodies": True})
    if args.session_tokens:
        budget = dict(service.get("budget") or {}, session_tokens=args.session_tokens)
        service = dict(service, budget=budget)
    return {
        "name": args.name,
        "display_name": args.display,
        "graph": graph,
        "layout": layout,
        "service": service,
    }


def parser() -> argparse.ArgumentParser:
    """플래그의 파서다. 앞의 두 자리 인자(TEMPLATES, OUT)를 뺀 것이라 테스트가 플래그만으로 쓴다."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", required=True)
    ap.add_argument("--display", required=True)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--ms-id", default="", help="Tool 노드를 그 MCP Server 의 Add 하나로 채운다")
    ap.add_argument("--approve", action="store_true", help="--ms-id 의 Add 를 승인 뒤에만 부른다")
    ap.add_argument("--source", default="", help="KIND:ID. Tool 노드의 소스만 바꾼다")
    ap.add_argument(
        "--tools",
        action="append",
        default=[],
        help="KIND:ID:TOOL. 거듭 주는 만큼 그 소스의 Tool 하나를 허용하는 Tool 노드를 둔다",
    )
    ap.add_argument("--allow", default="", help="--source 의 Tool 목록을 이 이름들(쉼표로 가른다)만 남긴다")
    ap.add_argument("--ds-id", default="", help="없으면 memory 노드를 뺀다")
    ap.add_argument("--long-term-days", type=int, default=0, help="0 이면 장기 기억을 끈다")
    ap.add_argument("--files", action="store_true", help="파일 도구를 켠다")
    ap.add_argument("--approve-edit", action="store_true", help="승인에 고치기를 더한다")
    ap.add_argument("--rate-limit", action="store_true", help="템플릿의 분당 한도를 둔다")
    ap.add_argument("--outside", default="", help="결과를 바깥 내용으로 표시할 Tool(쉼표로 가른다)")
    ap.add_argument("--rememberable", default="", help="승인을 기억할 수 있는 Tool(쉼표로 가른다)")
    ap.add_argument("--approval-for", default="", help="승인과 거절을 묻는 Tool(쉼표로 가른다)")
    ap.add_argument("--session-tokens", type=int, default=0, help="세션 토큰 예산. 0 이면 템플릿대로다")
    ap.add_argument("--session-ttl", type=int, default=0, help="세션 TTL(초). 0 이면 템플릿대로다")
    ap.add_argument("--kb-id", default="")
    ap.add_argument("--kb-tool", default="")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--sub-name", default="")
    ap.add_argument("--sub-description", default="계산을 맡는 서브에이전트다.")
    ap.add_argument("--sub-provider", default="", help="서브에이전트가 이 provider 의 제 모델 노드를 쓴다")
    ap.add_argument("--sub-tools", action="store_true", help="서브에이전트에도 Tool 노드를 잇는다")
    ap.add_argument("--judge", action="store_true", help="판정 모델(injection_check)의 Tool 가드레일을 더한다")
    ap.add_argument("--second", action="store_true", help="e2e_chat 4g 의 둘째 리비전")
    ap.add_argument("--record-bodies", action="store_true")
    ap.add_argument("--code-execution", action="store_true", help="메인의 코드 실행을 승인 없이 켠다")
    ap.add_argument("--code-approval", action="store_true", help="메인의 코드 실행을 켜고 execute 를 승인과 거절로 묻는다")
    ap.add_argument("--turn-timeout", type=int, default=0, help="메인의 턴 시간 제한(초). 0 이면 템플릿대로다")
    ap.add_argument("--system-prompt", default="", help="메인 에이전트의 시스템 프롬프트")
    return ap


def main() -> None:
    ap = parser()
    ap.add_argument("templates")
    ap.add_argument("out")
    args = ap.parse_args()
    with open(args.templates, encoding="utf-8") as f:
        template = json.load(f)[0]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(build(template, args), f, ensure_ascii=False)


if __name__ == "__main__":
    main()
