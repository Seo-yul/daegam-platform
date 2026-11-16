/**
 * 마크다운 본문 (D61). `Markdown` 이 늦게 싣는 청크이고 react-markdown·remark-gfm(GFM 표·목록·할 일·취소선)으로 그린다.
 *
 * React 요소로만 그리므로 원문 HTML 은 요소가 되지 않고 글자로 보인다. 주소는 `lib/markdownLinks` 의 규칙 한 곳에서 거른다.
 * 이미지는 그리지 않고 "이미지: 설명" 글자(사전의 `detail.markdown.image`)와 같은 규칙의 링크로 보인다. CSP 가 원격 이미지를 이미 막지만 `data:` 까지
 * 그리지 않아 모델이 만든 이미지 주소로 데이터를 내보내는 길을 부품에서 끊는다.
 * 인용 번호(P36 7절)를 받으면 `lib/citations` 의 remark 플러그인이 표에 있는 `[n]` 을 `sup[data-cite]` 로 바꾸고, 그 요소는
 * 부르는 쪽이 준 칩으로 그린다. 각주 번호처럼 표시가 없는 `sup` 은 그대로다.
 */
import { createContext, memo, useContext, useMemo, type ComponentProps, type ComponentType, type ElementType, type HTMLAttributes, type JSX, type ReactNode } from "react";
import ReactMarkdown, { type Components, type ExtraProps, type Options } from "react-markdown";
import { Link } from "react-router";
import remarkGfm from "remark-gfm";

import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { citeNumber, CITE_PROPERTY, remarkCitations, type CitationMarkdown } from "@/lib/citations";
import { classifyLink, markdownUrlTransform } from "@/lib/markdownLinks";
import { cn } from "@/lib/utils";

import { CodeBlock, Paragraph } from "./blocks";

type HastElement = NonNullable<ExtraProps["node"]>;
type PluggableList = NonNullable<Options["remarkPlugins"]>;
type Props<Tag extends keyof JSX.IntrinsicElements> = ComponentProps<Tag> & ExtraProps;

// 취소선은 물결표 둘(~~글~~)만 본다. 한국어 답에 흔한 "10~20분", "월~금" 의 물결표 하나가 사이 글을 지우지 않게 한다
const REMARK_PLUGINS: PluggableList = [[remarkGfm, { singleTilde: false }]];

const LINK_CLASS = "text-accent underline underline-offset-2 hover:no-underline";
/** 제목은 말풍선 글 크기(14px)에 맞춰 줄인다. */
const HEADING_SIZE = { h1: "text-base", h2: "text-[15px]", h3: "text-sm", h4: "text-sm", h5: "text-[13px]", h6: "text-[13px]" } as const;
const CODE_LANGUAGE_PREFIX = "language-";
/** GFM 이 할 일 목록의 ul 에 다는 클래스. */
const TASK_LIST_CLASS = "contains-task-list";

/** 코드 블록의 testId. 부품 표가 모듈 상수라 문맥으로 넘긴다. */
const CodeTestId = createContext<string | undefined>(undefined);
/** 링크 안이다. 링크 안의 이미지가 링크를 또 만들지 않게 한다. */
const InsideLink = createContext(false);
/**
 * 인용 번호 칩을 그리는 부품. 부품 표가 모듈 상수라 문맥으로 넘긴다. 표에 넣으려고 부품을 새로 만들면 표가 바뀔 때마다 칩이 새
 * 타입이 되어 다시 마운트되고, 스트리밍 중에 앞 답의 열린 작은 창이 닫힌다.
 */
const CitationChipSlot = createContext<ComponentType<{ n: number }> | null>(null);

function MarkdownBody({ text, codeTestId, citations }: { text: string; codeTestId?: string; citations?: CitationMarkdown }) {
  const t = useT();
  const remarkPlugins = useMemo<PluggableList>(() => (citations ? [...REMARK_PLUGINS, [remarkCitations, citations.known]] : REMARK_PLUGINS), [citations]);
  // 각주 머리(낭독기만 읽는다)의 글. react-markdown 의 기본값은 영어라 화면 언어로 주고, 언어가 바뀔 때만 새로 만든다
  const footnoteLabel = t.detail.markdown.footnotes;
  const remarkRehypeOptions = useMemo(() => ({ footnoteLabel }), [footnoteLabel]);
  return (
    <CodeTestId.Provider value={codeTestId}>
      <CitationChipSlot.Provider value={citations?.Chip ?? null}>
        <ReactMarkdown remarkPlugins={remarkPlugins} remarkRehypeOptions={remarkRehypeOptions} urlTransform={markdownUrlTransform} components={COMPONENTS}>
          {text}
        </ReactMarkdown>
      </CitationChipSlot.Provider>
    </CodeTestId.Provider>
  );
}

/** 스트리밍 중에는 마지막 말풍선만 글이 바뀐다. 앞 말풍선들은 글이 그대로라 다시 파싱하지 않는다. */
export default memo(MarkdownBody);

/** 링크. 거른 주소를 다시 갈라 앱 안 경로는 react-router 링크, 바깥 주소는 새 탭, 나머지는 글자로 그린다. */
function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  const link = classifyLink(href ?? "");
  if (link.kind === "text") return <>{children}</>;
  const body = <InsideLink.Provider value>{children}</InsideLink.Provider>;
  if (link.kind === "app") {
    return (
      <Link to={link.to} className={LINK_CLASS}>
        {body}
      </Link>
    );
  }
  return (
    <a href={link.href} target="_blank" rel="noopener noreferrer nofollow" title={link.title} className={LINK_CLASS}>
      {body}
    </a>
  );
}

/** 인용 플러그인이 만든 `sup[data-cite]` 는 칩이고 그 밖의 `sup`(각주 번호)은 그대로다. */
function CitationSup({ node, ...props }: Props<"sup">) {
  const Chip = useContext(CitationChipSlot);
  const n = citeNumber(node?.properties[CITE_PROPERTY]);
  return n === null || !Chip ? <sup {...props} /> : <Chip n={n} />;
}

/** 이미지는 그리지 않는다. "이미지: 설명" 글자가 링크 규칙을 지나면 링크가 된다. */
function MarkdownImage({ src, alt }: Props<"img">) {
  const t = useT();
  const insideLink = useContext(InsideLink);
  const label = t.detail.markdown.image(alt);
  if (insideLink || typeof src !== "string") return <>{label}</>;
  return <MarkdownLink href={src}>{label}</MarkdownLink>;
}

/** 코드 펜스로 감싼 코드 블록. react-markdown 은 `pre > code.language-x` 로 주므로 안쪽 `code` 에서 글과 언어를 읽는다. */
function MarkdownPre({ node }: Props<"pre">) {
  const testId = useContext(CodeTestId);
  const code = node?.children.find((child): child is HastElement => child.type === "element" && child.tagName === "code");
  return <CodeBlock text={codeText(code)} lang={codeLanguage(code)} testId={testId} />;
}

function codeText(code: HastElement | undefined): string {
  const text = (code?.children ?? []).map((child) => (child.type === "text" ? child.value : "")).join("");
  // 코드 펜스 안의 글은 마지막 줄바꿈을 남긴다. 복사한 코드 끝에 빈 줄이 붙지 않게 하나만 뗀다
  return text.endsWith("\n") ? text.slice(0, -1) : text;
}

function codeLanguage(code: HastElement | undefined): string | undefined {
  const classes = code?.properties.className;
  const name = Array.isArray(classes) ? classes.map(String).find((c) => c.startsWith(CODE_LANGUAGE_PREFIX)) : undefined;
  return name?.slice(CODE_LANGUAGE_PREFIX.length);
}

/**
 * 마크다운 요소 하나를 이 화면의 클래스를 더해 그린다. `Tag` 는 HTML 태그 이름이거나 className 을 받는 부품(문단, 표)이다.
 * react-markdown 이 넘기는 hast `node` 는 DOM 속성이 아니므로 떼어 낸다.
 */
function styled(Tag: ElementType, base?: string) {
  return function MarkdownElement({ node: _node, className, ...props }: HTMLAttributes<HTMLElement> & ExtraProps) {
    return <Tag className={cn(base, className)} {...props} />;
  };
}

function heading(tag: keyof typeof HEADING_SIZE) {
  return styled(tag, cn("font-semibold text-heading", HEADING_SIZE[tag]));
}

const COMPONENTS: Components = {
  p: styled(Paragraph),
  a: ({ href, children }) => <MarkdownLink href={href}>{children}</MarkdownLink>,
  img: MarkdownImage,
  pre: MarkdownPre,
  sup: CitationSup,
  code: styled("code", "rounded-badge border border-line bg-sunken px-1 py-px font-mono text-[12px] text-heading"),
  h1: heading("h1"),
  h2: heading("h2"),
  h3: heading("h3"),
  h4: heading("h4"),
  h5: heading("h5"),
  h6: heading("h6"),
  // 할 일 목록(GFM)은 글머리표 대신 칸을 앞세운다
  ul: ({ node: _node, className, ...props }) => <ul className={cn("flex list-disc flex-col gap-0.5 pl-5", className?.includes(TASK_LIST_CLASS) && "list-none pl-1", className)} {...props} />,
  ol: styled("ol", "flex list-decimal flex-col gap-0.5 pl-5"),
  li: styled("li", "[overflow-wrap:anywhere]"),
  // 할 일 칸은 읽기 전용이다. GFM 이 만드는 input 은 이것뿐이다
  input: ({ checked }) => <input type="checkbox" checked={Boolean(checked)} disabled readOnly className="mr-1.5 align-middle" />,
  blockquote: styled("blockquote", "flex flex-col gap-2 border-l-2 border-line-strong pl-3 text-muted"),
  hr: () => <hr className="border-line" />,
  table: styled(Table, "text-[13px]"),
  thead: styled(THead, "text-[12px] tracking-normal normal-case"),
  tbody: styled(TBody),
  tr: styled(TR),
  th: styled(TH, "px-2.5 py-1.5"),
  td: styled(TD, "px-2.5 py-1.5"),
};
