import type { CSSProperties } from 'react';

const bookshelfBaseShadow = `
  0 10px 22px -12px color-mix(in srgb, var(--ant-color-text) 28%, transparent),
  inset 0 1px 0 color-mix(in srgb, var(--ant-color-bg-container) 82%, transparent)
`;

const bookshelfHoverShadow = `
  0 16px 30px -12px color-mix(in srgb, var(--ant-color-text) 34%, transparent),
  inset 0 1px 0 color-mix(in srgb, var(--ant-color-bg-container) 88%, transparent)
`;

const bookshelfNewBaseShadow = `
  0 10px 24px -14px color-mix(in srgb, var(--ant-color-text) 30%, transparent),
  inset 0 1px 0 color-mix(in srgb, var(--ant-color-bg-container) 84%, transparent)
`;

const bookshelfNewHoverShadow = `
  0 16px 30px -12px color-mix(in srgb, var(--ant-color-text) 36%, transparent),
  inset 0 1px 0 color-mix(in srgb, var(--ant-color-bg-container) 90%, transparent)
`;

const promptTemplateBaseShadow = `
  0 6px 16px color-mix(in srgb, var(--ant-color-text) 11%, transparent),
  0 1px 0 color-mix(in srgb, var(--ant-color-white) 42%, transparent) inset
`;

// BookshelfPage 样式（书架/书本卡片）
export const bookshelfCardStyles = {
  container: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
    gap: '20px 18px',
    padding: '8px 0 16px',
    alignItems: 'stretch',
  } as CSSProperties,

  projectCard: {
    height: '100%',
    borderRadius: '4px 12px 12px 4px',
    overflow: 'hidden',
    background: 'linear-gradient(180deg, color-mix(in srgb, var(--ant-color-bg-container) 96%, var(--ant-color-text) 4%) 0%, color-mix(in srgb, var(--ant-color-bg-container) 88%, var(--ant-color-text) 12%) 100%)',
    boxShadow: bookshelfBaseShadow,
    transition: 'transform 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275), box-shadow 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275), border-color 0.3s ease',
    border: '1px solid color-mix(in srgb, var(--ant-color-text) 18%, transparent)',
    display: 'flex',
    flexDirection: 'column',
    position: 'relative',
    transformOrigin: 'center bottom',
    transformStyle: 'preserve-3d',
  } as CSSProperties,

  newProjectCard: {
    height: '100%',
    borderRadius: 12,
    overflow: 'hidden',
    background: 'linear-gradient(180deg, color-mix(in srgb, var(--ant-color-bg-container) 94%, var(--ant-color-warning) 6%) 0%, color-mix(in srgb, var(--ant-color-bg-container) 86%, var(--ant-color-warning) 14%) 100%)',
    boxShadow: bookshelfNewBaseShadow,
    border: '2px dashed color-mix(in srgb, var(--ant-color-warning) 40%, var(--ant-color-border) 60%)',
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'center',
    alignItems: 'center',
    transition: 'transform 0.3s ease, box-shadow 0.3s ease, border-color 0.3s ease, background-color 0.3s ease',
    position: 'relative',
  } as CSSProperties,
};

export const bookshelfCardHoverHandlers = {
  onMouseEnter: (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.currentTarget;
    if (target.dataset.cardStyle !== 'bookshelf-book') {
      return;
    }

    if (target.dataset.bookKind === 'new') {
      target.style.transform = 'translateY(-6px)';
      target.style.boxShadow = bookshelfNewHoverShadow;
      target.style.borderColor = 'color-mix(in srgb, var(--ant-color-warning) 48%, var(--ant-color-border) 52%)';
      target.style.background = 'linear-gradient(180deg, color-mix(in srgb, var(--ant-color-bg-container) 92%, var(--ant-color-warning) 8%) 0%, color-mix(in srgb, var(--ant-color-bg-container) 82%, var(--ant-color-warning) 18%) 100%)';
      return;
    }

    target.style.transform = 'translateY(-8px) rotateX(2deg)';
    target.style.boxShadow = bookshelfHoverShadow;
    target.style.borderColor = 'color-mix(in srgb, var(--ant-color-primary) 28%, var(--ant-color-border) 72%)';
  },
  onMouseLeave: (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.currentTarget;
    if (target.dataset.cardStyle !== 'bookshelf-book') {
      return;
    }

    const isNewBook = target.dataset.bookKind === 'new';
    target.style.transform = 'translateY(0) rotateX(0)';
    target.style.boxShadow = isNewBook ? bookshelfNewBaseShadow : bookshelfBaseShadow;
    target.style.borderColor = isNewBook
      ? 'color-mix(in srgb, var(--ant-color-warning) 40%, var(--ant-color-border) 60%)'
      : 'color-mix(in srgb, var(--ant-color-text) 18%, transparent)';
    if (isNewBook) {
      target.style.background = 'linear-gradient(180deg, color-mix(in srgb, var(--ant-color-bg-container) 94%, var(--ant-color-warning) 6%) 0%, color-mix(in srgb, var(--ant-color-bg-container) 86%, var(--ant-color-warning) 14%) 100%)';
    }
  },
};

// PromptTemplates 页面卡片样式
export const promptTemplateCardStyles = {
  templateCard: {
    height: '100%',
    borderRadius: 14,
    overflow: 'hidden',
    border: '1px solid color-mix(in srgb, var(--ant-color-text) 8%, transparent)',
    background: 'linear-gradient(180deg, color-mix(in srgb, var(--ant-color-bg-container) 97%, var(--ant-color-primary) 3%) 0%, var(--ant-color-bg-container) 100%)',
    boxShadow: promptTemplateBaseShadow,
    transition: 'transform 0.28s cubic-bezier(0.22, 1, 0.36, 1), box-shadow 0.28s cubic-bezier(0.22, 1, 0.36, 1), border-color 0.28s ease',
  } as CSSProperties,
};

export const promptTemplateCardHoverHandlers = {
  onMouseEnter: (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.currentTarget;
    target.style.transform = 'translateY(-6px)';
    target.style.boxShadow = `
      0 14px 24px color-mix(in srgb, var(--ant-color-text) 16%, transparent),
      0 1px 0 color-mix(in srgb, var(--ant-color-white) 48%, transparent) inset
    `;
    target.style.borderColor = 'color-mix(in srgb, var(--ant-color-primary) 24%, transparent)';
  },
  onMouseLeave: (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.currentTarget;
    target.style.transform = 'translateY(0)';
    target.style.boxShadow = promptTemplateBaseShadow;
    target.style.borderColor = 'color-mix(in srgb, var(--ant-color-text) 8%, transparent)';
  },
};

export const promptTemplateGridConfig = {
  xs: 24,
  sm: 12,
  lg: 8,
  xl: 6,
};

// WorldSetting 页面卡片样式
export const worldSettingCardStyles = {
  sectionCard: {
    borderRadius: 14,
    border: '1px solid color-mix(in srgb, var(--ant-color-text) 7%, transparent)',
    boxShadow: '0 4px 12px color-mix(in srgb, var(--ant-color-text) 8%, transparent)',
    background: 'linear-gradient(180deg, color-mix(in srgb, var(--ant-color-bg-container) 98%, var(--ant-color-primary) 2%) 0%, var(--ant-color-bg-container) 100%)',
    transition: 'box-shadow 0.24s ease, border-color 0.24s ease',
  } as CSSProperties,
};

// Characters 页面（CharacterCard + 网格）样式
export const characterCardStyles = {
  characterCard: {
    display: 'flex',
    flexDirection: 'column',
    borderRadius: 12,
  } as CSSProperties,

  organizationCard: {
    display: 'flex',
    flexDirection: 'column',
    backgroundColor: 'var(--ant-color-bg-layout)',
    borderRadius: 12,
  } as CSSProperties,

  nameEllipsis: {
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  } as CSSProperties,

  descriptionBlock: {
    marginTop: 12,
    maxHeight: 200,
    overflow: 'hidden',
  } as CSSProperties,
};

export const charactersPageGridConfig = {
  gutter: 0,
  xs: 24,
  sm: 12,
  md: 12,
  lg: 6,
  xl: 6,
  xxl: 5,
};

// 页面通用文本样式（仅用于信息展示，不与卡片结构耦合）
export const commonTextStyles = {
  label: {
    fontSize: 12,
    color: 'color-mix(in srgb, var(--ant-color-text) 55%, transparent)',
  } as CSSProperties,

  value: {
    fontSize: 14,
    color: 'var(--ant-color-text)',
  } as CSSProperties,

  description: {
    fontSize: 12,
    color: 'color-mix(in srgb, var(--ant-color-text) 55%, transparent)',
    lineHeight: 1.6,
  } as CSSProperties,
};
