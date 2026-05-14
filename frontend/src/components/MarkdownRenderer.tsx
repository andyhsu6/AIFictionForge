import React from 'react';
import { Typography, theme } from 'antd';

const { Paragraph, Title, Text } = Typography;

interface MarkdownRendererProps {
  content?: string | null;
  compact?: boolean;
}

const isSafeUrl = (url: string) => {
  const trimmed = url.trim();
  return trimmed.startsWith('http://')
    || trimmed.startsWith('https://')
    || trimmed.startsWith('mailto:')
    || trimmed.startsWith('/')
    || trimmed.startsWith('#');
};

const parseInlineMarkdown = (text: string, keyPrefix: string): React.ReactNode[] => {
  const nodes: React.ReactNode[] = [];
  const pattern = /(\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let index = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    const [, , linkText, linkUrl, codeText, boldText, italicText] = match;
    const key = `${keyPrefix}-inline-${index}`;

    if (linkText && linkUrl) {
      const href = isSafeUrl(linkUrl) ? linkUrl.trim() : '#';
      nodes.push(
        <a key={key} href={href} target={href.startsWith('http') ? '_blank' : undefined} rel={href.startsWith('http') ? 'noreferrer' : undefined}>
          {linkText}
        </a>,
      );
    } else if (codeText) {
      nodes.push(<Text key={key} code>{codeText}</Text>);
    } else if (boldText) {
      nodes.push(<strong key={key}>{parseInlineMarkdown(boldText, `${key}-bold`)}</strong>);
    } else if (italicText) {
      nodes.push(<em key={key}>{parseInlineMarkdown(italicText, `${key}-italic`)}</em>);
    }

    lastIndex = pattern.lastIndex;
    index += 1;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
};

const isBlockStart = (line: string) => {
  return /^#{1,6}\s+/.test(line)
    || /^\s*([-*+])\s+/.test(line)
    || /^\s*\d+\.\s+/.test(line)
    || /^>\s?/.test(line)
    || /^```/.test(line);
};

export default function MarkdownRenderer({ content, compact = false }: MarkdownRendererProps) {
  const { token } = theme.useToken();
  const lines = (content || '').replace(/\r\n/g, '\n').split('\n');
  const blocks: React.ReactNode[] = [];

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    const codeFenceMatch = trimmed.match(/^```\s*([\w-]*)\s*$/);
    if (codeFenceMatch) {
      const language = codeFenceMatch[1];
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) {
        i += 1;
      }
      blocks.push(
        <pre key={`code-${i}`} className="announcement-markdown-code">
          {language && <div className="announcement-markdown-code-lang">{language}</div>}
          <code>{codeLines.join('\n')}</code>
        </pre>,
      );
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = Math.min(5, Math.max(2, headingMatch[1].length + 1)) as 2 | 3 | 4 | 5;
      blocks.push(
        <Title key={`heading-${i}`} level={level} className="announcement-markdown-heading">
          {parseInlineMarkdown(headingMatch[2], `heading-${i}`)}
        </Title>,
      );
      i += 1;
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      const quoteLines: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ''));
        i += 1;
      }
      blocks.push(
        <blockquote key={`quote-${i}`} className="announcement-markdown-quote">
          {quoteLines.map((item, index) => (
            <div key={`quote-line-${i}-${index}`}>{parseInlineMarkdown(item, `quote-${i}-${index}`)}</div>
          ))}
        </blockquote>,
      );
      continue;
    }

    if (/^\s*([-*+])\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*+])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+])\s+/, ''));
        i += 1;
      }
      blocks.push(
        <ul key={`ul-${i}`} className="announcement-markdown-list">
          {items.map((item, index) => <li key={`ul-${i}-${index}`}>{parseInlineMarkdown(item, `ul-${i}-${index}`)}</li>)}
        </ul>,
      );
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i += 1;
      }
      blocks.push(
        <ol key={`ol-${i}`} className="announcement-markdown-list">
          {items.map((item, index) => <li key={`ol-${i}-${index}`}>{parseInlineMarkdown(item, `ol-${i}-${index}`)}</li>)}
        </ol>,
      );
      continue;
    }

    const paragraphLines: string[] = [];
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) {
      paragraphLines.push(lines[i]);
      i += 1;
    }
    const paragraphText = paragraphLines.join('\n');
    blocks.push(
      <Paragraph key={`p-${i}`} className="announcement-markdown-paragraph">
        {parseInlineMarkdown(paragraphText, `p-${i}`)}
      </Paragraph>,
    );
  }

  return (
    <div className={`announcement-markdown ${compact ? 'announcement-markdown-compact' : ''}`}>
      <style>
        {`
          .announcement-markdown {
            color: ${token.colorText};
            line-height: 1.75;
            word-break: break-word;
          }
          .announcement-markdown p,
          .announcement-markdown .announcement-markdown-paragraph {
            margin-bottom: ${compact ? 8 : 12}px;
            white-space: pre-wrap;
          }
          .announcement-markdown-heading {
            margin-top: ${compact ? 8 : 14}px !important;
            margin-bottom: ${compact ? 6 : 10}px !important;
          }
          .announcement-markdown-list {
            padding-left: 1.6em;
            margin: 0 0 ${compact ? 8 : 12}px;
          }
          .announcement-markdown-list li {
            margin-bottom: 4px;
          }
          .announcement-markdown-quote {
            margin: 0 0 ${compact ? 8 : 12}px;
            padding: 8px 12px;
            border-left: 4px solid ${token.colorPrimary};
            background: ${token.colorFillTertiary};
            border-radius: 8px;
            color: ${token.colorTextSecondary};
          }
          .announcement-markdown-code {
            position: relative;
            margin: 0 0 ${compact ? 8 : 12}px;
            padding: ${compact ? '10px 12px' : '14px 16px'};
            overflow: auto;
            border-radius: 10px;
            background: ${token.colorFillQuaternary};
            border: 1px solid ${token.colorBorderSecondary};
          }
          .announcement-markdown-code code {
            font-family: Consolas, Monaco, 'Courier New', monospace;
            font-size: 13px;
            white-space: pre;
          }
          .announcement-markdown-code-lang {
            margin-bottom: 8px;
            color: ${token.colorTextTertiary};
            font-size: 12px;
          }
          .announcement-markdown a {
            color: ${token.colorPrimary};
          }
        `}
      </style>
      {blocks.length > 0 ? blocks : <Text type="secondary">暂无内容</Text>}
    </div>
  );
}
