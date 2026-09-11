import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Tooltip, theme } from 'antd';
import { EditOutlined, PlusOutlined } from '@ant-design/icons';

interface PartialRegenerateToolbarProps {
  visible: boolean;
  position: { top: number; left: number };
  onRegenerate: () => void;
  onContinue?: () => void;
  selectedText: string;
}

/**
 * 局部重写浮动工具栏
 * 当用户在章节内容编辑器中选中文本时显示
 */
export const PartialRegenerateToolbar: React.FC<PartialRegenerateToolbarProps> = ({
  visible,
  position,
  onRegenerate,
  onContinue,
  selectedText
}) => {
  const { token } = theme.useToken();
  const { t } = useTranslation();

  if (!visible || !selectedText) return null;

  // 限制显示的选中文本长度
  const displayText = selectedText.length > 20 
    ? selectedText.substring(0, 20) + '...' 
    : selectedText;

  return (
    <div
      style={{
        position: 'fixed',
        top: position.top,
        left: position.left,
        zIndex: 10000,
        background: token.colorBgElevated,
        borderRadius: 8,
        boxShadow: token.boxShadow,
        padding: '6px 8px',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        animation: 'fadeIn 0.2s ease-out',
        border: `1px solid ${token.colorBorderSecondary}`,
      }}
    >
      <Tooltip
        title={t('partialToolbar.rewriteTooltip', { text: displayText })}
        placement="top"
      >
        <Button
          type="primary"
          size="small"
          icon={<EditOutlined />}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onRegenerate();
          }}
          style={{
            // 使用当前 Ant Design 主题 token，避免明暗主题下 CSS 变量失效或对比度不足。
            background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
            color: token.colorTextLightSolid,
            borderColor: token.colorPrimary,
            fontWeight: 500,
            boxShadow: token.boxShadowSecondary,
          }}
        >
          {t('partialToolbar.aiRewrite')}
        </Button>
      </Tooltip>
      {onContinue && (
        <Tooltip
          title={t('partialToolbar.continueTooltip')}
          placement="top"
        >
          <Button
            size="small"
            icon={<PlusOutlined />}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onContinue();
            }}
            style={{
              // 与重写按钮同一主题 token 体系，保证明暗主题下对比度
              color: token.colorPrimary,
              borderColor: token.colorPrimary,
              fontWeight: 500,
            }}
          >
            {t('partialToolbar.aiContinue')}
          </Button>
        </Tooltip>
      )}
      <span style={{ 
        fontSize: 12, 
        color: token.colorTextTertiary,
        maxWidth: 150,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }}>
        {t('partialToolbar.selectedChars', { count: selectedText.length })}
      </span>
    </div>
  );
};

// 添加动画样式
const style = document.createElement('style');
style.textContent = `
  @keyframes fadeIn {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
`;
if (!document.head.querySelector('style[data-partial-regenerate-toolbar]')) {
  style.setAttribute('data-partial-regenerate-toolbar', 'true');
  document.head.appendChild(style);
}

export default PartialRegenerateToolbar;