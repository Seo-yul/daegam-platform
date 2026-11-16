/**
 * 왼쪽 팔레트. 노드 7종은 끌어다 놓거나 눌러서 캔버스에 넣고, 템플릿은 누르면 캔버스를 그것으로 바꾼다.
 * 모델은 노드다 (D28). 지식도 노드다 (D32). Knowledge Base 하나가 검색 도구 하나로 에이전트에 붙는다.
 */
import type { DragEvent } from "react";

import type { AgentTemplate } from "@/api/types";
import { useT } from "@/i18n";
import { NODE_TYPES, type NodeType } from "@/lib/agentGraph";
import { cn } from "@/lib/utils";

import { NODE_DRAG_MIME } from "./AgentCanvas";
import { NODE_ICONS } from "./nodes";
import { kindColor, kindTint } from "@/lib/kindColor";

export function Palette({
  templates,
  templatesLoading = false,
  templatesError = null,
  onAdd,
  onTemplate,
  disabled = false,
}: {
  templates: AgentTemplate[];
  /** 템플릿을 아직 읽는 중인가. 못 읽은 것·하나도 없는 것과 문구를 가른다. */
  templatesLoading?: boolean;
  templatesError?: unknown;
  onAdd: (type: NodeType) => void;
  onTemplate: (template: AgentTemplate) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const text = t.canvas.palette;
  function onDragStart(e: DragEvent<HTMLButtonElement>, type: NodeType) {
    e.dataTransfer.setData(NODE_DRAG_MIME, type);
    e.dataTransfer.effectAllowed = "move";
  }
  return (
    // nokey: 팔레트 안에서 삭제 키가 캔버스 노드를 지우지 않게 한다 (Inspector 와 같은 이유)
    <aside className="nokey surface flex w-56 shrink-0 flex-col gap-3 overflow-y-auto rounded-card p-3" data-testid="palette">
      <div>
        <div className="px-1 pb-1.5 text-xs font-medium text-muted">{text.nodes}</div>
        <div className="flex flex-col gap-1.5">
          {NODE_TYPES.map((type) => {
            const Icon = NODE_ICONS[type];
            return (
              <button
                key={type}
                type="button"
                draggable={!disabled}
                disabled={disabled}
                data-testid={`palette-${type}`}
                title={text.addHint}
                onDragStart={(e) => onDragStart(e, type)}
                onClick={() => onAdd(type)}
                className={cn("flex w-full cursor-grab items-start gap-2.5 rounded-item border border-line bg-surface-strong px-2.5 py-2 text-left transition-colors hover:bg-hover active:cursor-grabbing disabled:cursor-not-allowed disabled:opacity-50")}
              >
                <span className="mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[8px]" style={{ background: kindTint(kindColor(type)), color: kindColor(type) }} aria-hidden>
                  <Icon size={13} />
                </span>
                <span className="min-w-0">
                  <span className="block text-[12.5px] font-semibold text-heading">{t.canvas.nodeLabels[type]}</span>
                  <span className="block text-[10.5px] leading-snug text-muted">{text.hints[type]}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
      <div>
        <div className="px-1 pb-1.5 text-xs font-medium text-muted">{text.templates}</div>
        {templates.length ? (
          <div className="flex flex-col gap-1.5">
            {templates.map((template) => (
              <button
                key={template.id}
                type="button"
                disabled={disabled}
                data-testid={`palette-template-${template.id}`}
                onClick={() => onTemplate(template)}
                className="rounded-item border border-accent-line bg-accent-soft px-2.5 py-2 text-left transition-colors hover:border-accent disabled:opacity-50"
              >
                <span className="block text-[12.5px] font-semibold text-heading">{template.display_name}</span>
                <span className="mt-0.5 line-clamp-3 block text-[10.5px] leading-snug text-muted">{template.description}</span>
              </button>
            ))}
          </div>
        ) : templatesLoading ? (
          <p className="px-1 text-[11px] text-muted">{text.templatesLoading}</p>
        ) : templatesError ? (
          <p className="px-1 text-[11px] text-warn-fg" data-testid="palette-templates-error">
            {text.templatesError}
          </p>
        ) : (
          <p className="px-1 text-[11px] text-muted">{text.templatesEmpty}</p>
        )}
      </div>
    </aside>
  );
}
