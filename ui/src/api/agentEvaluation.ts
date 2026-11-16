/**
 * Agent 의 되돌리기와 평가 (P45, D109). 플랫폼 `api/agents.py` 의 되돌리기와 리비전 목록, `api/agent_evaluation.py` 의 세트,
 * 사례, 실행, 배포 게이트를 부른다.
 *
 * 범위는 서버가 본다. 세트와 사례, 실행의 판정 읽기는 남의 대화를 읽는 `agent:approve` 이고, 세트와 사례의 쓰기는 `agent:write`
 * 를, 실행의 시작과 취소와 게이트는 `agent:deploy` 를 더한다. 실행 하나의 결과 내용은 시작한 사람만 받는다(9절). 되돌리기는
 * `agent:write` 와 `agent:deploy` 이고, 배포 게이트가 막으면 409 `evaluation_required` 에 까닭을 싣는다(12절).
 */
import { api, unwrap } from "./client";
import type {
  Agent,
  AgentRevision,
  AgentRollbackBody,
  EvaluationCase,
  EvaluationCaseBody,
  EvaluationCaseDraft,
  EvaluationCaseList,
  EvaluationGate,
  EvaluationGateBody,
  EvaluationRun,
  EvaluationRunDetail,
  EvaluationRunList,
  EvaluationSet,
  EvaluationSetBody,
} from "./types";

/** 그 Agent 의 리비전 모두와 그 내용의 지문(`spec_digest`). 배포 탭의 되돌리기가 배포된 내용과 견준다. */
export async function listAgentRevisions(agentId: string): Promise<AgentRevision[]> {
  return unwrap(await api.GET("/agents/{agent_id}/revisions", { params: { path: { agent_id: agentId } } }));
}

/**
 * 리비전 `revision` 의 정의를 새 리비전으로 저장하고 배포한다 (6절). 배포된 내용과 같으면 409 `nothing_to_roll_back`, 오늘의
 * 플랫폼으로 검증되지 않으면 422 `invalid_graph` 이고 아무것도 바꾸지 않는다.
 */
export async function rollbackAgent(agentId: string, body: AgentRollbackBody): Promise<Agent> {
  return unwrap(await api.POST("/agents/{agent_id}/rollback", { params: { path: { agent_id: agentId } }, body }));
}

/** 배포 게이트를 켜거나 바꾼다. null 이면 끈다. 답의 `gate` 는 저장한 게이트이고 끄면 null 이다. */
export async function putEvaluationGate(agentId: string, body: EvaluationGateBody | null): Promise<EvaluationGate | null> {
  const answer = unwrap(await api.PUT("/agents/{agent_id}/evaluation-gate", { params: { path: { agent_id: agentId } }, body }));
  return answer.gate;
}

// -- 세트 (7절) ------------------------------------------------------------------------------

export async function listEvaluationSets(agentId: string): Promise<EvaluationSet[]> {
  return unwrap(await api.GET("/agents/{agent_id}/evaluation-sets", { params: { path: { agent_id: agentId } } }));
}

export async function createEvaluationSet(agentId: string, body: EvaluationSetBody): Promise<EvaluationSet> {
  return unwrap(await api.POST("/agents/{agent_id}/evaluation-sets", { params: { path: { agent_id: agentId } }, body }));
}

/** 이름, 채점 모델, 반복 수를 바꾼다. 사례 수 × 반복 수가 상한을 넘으면 422 `evaluation_set_full` 이다. */
export async function putEvaluationSet(agentId: string, setId: string, body: EvaluationSetBody): Promise<EvaluationSet> {
  return unwrap(await api.PUT("/agents/{agent_id}/evaluation-sets/{set_id}", { params: { path: { agent_id: agentId, set_id: setId } }, body }));
}

/** 세트와 그 사례를 지운다. 배포 게이트가 이 세트를 읽으면 409 `evaluation_set_in_gate` 다. */
export async function deleteEvaluationSet(agentId: string, setId: string): Promise<void> {
  unwrap(await api.DELETE("/agents/{agent_id}/evaluation-sets/{set_id}", { params: { path: { agent_id: agentId, set_id: setId } } }));
}

// -- 사례 (7절, 8절) ---------------------------------------------------------------------------

/** 세트의 사례와 지금 세트의 지문. 읽기도 감사에 남는다. */
export async function listEvaluationCases(agentId: string, setId: string): Promise<EvaluationCaseList> {
  return unwrap(await api.GET("/agents/{agent_id}/evaluation-sets/{set_id}/cases", { params: { path: { agent_id: agentId, set_id: setId } } }));
}

/**
 * 사례 하나를 넣는다. 개인정보로 보이는 것이 있으면 422 `evaluation_case_pii` 이고, 손으로 쓴 사례는 `acknowledgeable: true`
 * 라 `pii_acknowledged` 를 실어 다시 보내면 저장된다. 복사한 사례(origin)는 확인으로 저장되지 않는다.
 */
export async function createEvaluationCase(agentId: string, setId: string, body: EvaluationCaseBody): Promise<EvaluationCase> {
  return unwrap(await api.POST("/agents/{agent_id}/evaluation-sets/{set_id}/cases", { params: { path: { agent_id: agentId, set_id: setId } }, body }));
}

export async function putEvaluationCase(agentId: string, setId: string, caseId: string, body: Omit<EvaluationCaseBody, "origin">): Promise<EvaluationCase> {
  return unwrap(
    await api.PUT("/agents/{agent_id}/evaluation-sets/{set_id}/cases/{case_id}", { params: { path: { agent_id: agentId, set_id: setId, case_id: caseId } }, body }),
  );
}

export async function deleteEvaluationCase(agentId: string, setId: string, caseId: string): Promise<void> {
  unwrap(await api.DELETE("/agents/{agent_id}/evaluation-sets/{set_id}/cases/{case_id}", { params: { path: { agent_id: agentId, set_id: setId, case_id: caseId } } }));
}

/**
 * 평가를 받은 턴의 질문과 답을 저장하기 전의 사례로 받는다 (8절). 대화한 사람이 동의하지 않았으면 409
 * `evaluation_consent_missing`, 다시 생성으로 걷혔거나 전사의 보존 기간이 지난 턴은 404 `turn_not_found` 다.
 */
export async function copyRatedTurn(agentId: string, setId: string, sessionId: string, turnId: string): Promise<EvaluationCaseDraft> {
  return unwrap(
    await api.POST("/agents/{agent_id}/evaluation-sets/{set_id}/cases/from-turn", {
      params: { path: { agent_id: agentId, set_id: setId } },
      body: { session_id: sessionId, turn_id: turnId },
    }),
  );
}

// -- 실행 (9절) ------------------------------------------------------------------------------

/** 세트의 실행(새것이 앞)과 부른 사람이 시작하기 전에 확인할 사례. */
export async function listEvaluationRuns(agentId: string, setId: string): Promise<EvaluationRunList> {
  return unwrap(await api.GET("/agents/{agent_id}/evaluation-sets/{set_id}/runs", { params: { path: { agent_id: agentId, set_id: setId } } }));
}

/**
 * 리비전 하나를 그 세트로 평가한다(202). setDigest 는 시작하는 사람이 확인한 세트의 지문이고 그 사이 세트가 바뀌었으면 409
 * `evaluation_set_changed` 다. revision 을 주지 않으면 저장한 최신 리비전이다.
 */
export async function startEvaluationRun(agentId: string, setId: string, setDigest: string, revision?: number): Promise<EvaluationRun> {
  return unwrap(
    await api.POST("/agents/{agent_id}/evaluation-sets/{set_id}/runs", {
      params: { path: { agent_id: agentId, set_id: setId } },
      body: { set_digest: setDigest, ...(revision !== undefined ? { revision } : {}) },
    }),
  );
}

/** 실행 하나. 결과의 내용은 시작한 사람만 받고 다른 사람은 `content_hidden` 이다. 읽기도 감사에 남는다. */
export async function getEvaluationRun(agentId: string, setId: string, runId: string): Promise<EvaluationRunDetail> {
  return unwrap(
    await api.GET("/agents/{agent_id}/evaluation-sets/{set_id}/runs/{run_id}", { params: { path: { agent_id: agentId, set_id: setId, run_id: runId } } }),
  );
}

/** 도는 실행을 취소하고 그 미리 보기를 끝낸다. 끝난 실행이면 409 `state_conflict` 다. */
export async function cancelEvaluationRun(agentId: string, setId: string, runId: string): Promise<void> {
  unwrap(
    await api.DELETE("/agents/{agent_id}/evaluation-sets/{set_id}/runs/{run_id}", { params: { path: { agent_id: agentId, set_id: setId, run_id: runId } } }),
  );
}
