import { Field, Input } from "@/components/ui/input";
import { useT } from "@/i18n";

/** Chunk size 의 범위다. 서버 store/knowledge.py 의 `Chunking.size` (ge=200, le=4000) 와 같다. */
export const CHUNK_SIZE_MIN = 200;
export const CHUNK_SIZE_MAX = 4000;
/** Max chunks 의 상한이다. 서버 store/knowledge.py 의 `Limits.max_chunks` (le=1_000_000) 와 같다. */
export const MAX_CHUNKS_LIMIT = 1_000_000;

/** chunking·limits 입력칸의 값이다. 칸에 적힌 문자열 그대로다. */
export interface ChunkingValue {
  size: string;
  overlap: string;
  maxChunks: string;
}

/** Chunk size·Overlap·Max chunks 세 칸이다. 새로 만들기는 필수로, 상세 편집은 선택으로 그린다. */
export function ChunkingFields({ value, onChange, required = false }: { value: ChunkingValue; onChange: (patch: Partial<ChunkingValue>) => void; required?: boolean }) {
  const t = useT();
  const text = t.knowledgeBases.chunking;
  return (
    <div className="grid grid-cols-3 gap-3">
      <Field label={text.size} htmlFor="kb-chunk-size" hint={text.sizeHint(CHUNK_SIZE_MIN, CHUNK_SIZE_MAX)}>
        <Input id="kb-chunk-size" data-testid="kb-chunk-size" type="number" min={CHUNK_SIZE_MIN} max={CHUNK_SIZE_MAX} required={required} value={value.size} onChange={(e) => onChange({ size: e.target.value })} />
      </Field>
      <Field label={text.overlap} htmlFor="kb-chunk-overlap" hint={text.overlapHint}>
        <Input id="kb-chunk-overlap" data-testid="kb-chunk-overlap" type="number" min={0} required={required} value={value.overlap} onChange={(e) => onChange({ overlap: e.target.value })} />
      </Field>
      <Field label={text.maxChunks} htmlFor="kb-max-chunks" hint={text.maxChunksHint}>
        <Input id="kb-max-chunks" data-testid="kb-max-chunks" type="number" min={1} max={MAX_CHUNKS_LIMIT} required={required} value={value.maxChunks} onChange={(e) => onChange({ maxChunks: e.target.value })} />
      </Field>
    </div>
  );
}
