import React from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Spin, Button, theme } from 'antd';
import { LoadingOutlined, StopOutlined } from '@ant-design/icons';

interface SSEProgressModalProps {
  visible: boolean;
  progress: number;
  message: string;
  title?: string;
  showPercentage?: boolean;
  showIcon?: boolean;
  onCancel?: () => void;
  cancelButtonText?: string;
}

/**
 * 统一的SSE进度显示Modal组件
 * 用于在Modal中显示AI生成进度，样式与SSELoadingOverlay保持一致
 */
export const SSEProgressModal: React.FC<SSEProgressModalProps> = ({
  visible,
  progress,
  message,
  title,
  showPercentage = true,
  showIcon = true,
  onCancel,
  cancelButtonText,
}) => {
  const { token } = theme.useToken();
  const { t } = useTranslation();

  if (!visible) return null;

  return (
    <Modal
      title={null}
      open={visible}
      footer={null}
      closable={false}
      centered
      width={500}
      maskClosable={false}
      keyboard={false}
      styles={{
        body: {
          padding: '40px 40px 32px',
        }
      }}
    >
      <div>
        {/* 标题和图标 */}
        {showIcon && (
          <div style={{
            textAlign: 'center',
            marginBottom: 24
          }}>
            <Spin
              indicator={<LoadingOutlined style={{ fontSize: 48, color: token.colorPrimary }} spin />}
            />
            <div style={{
              fontSize: 20,
              fontWeight: 'bold',
              marginTop: 16,
              color: token.colorText
            }}>
              {title || t('aiGenerating')}
            </div>
          </div>
        )}

        {/* 进度条 */}
        <div style={{
          marginBottom: showPercentage ? 16 : 24
        }}>
          <div style={{
            height: 12,
            background: token.colorBgLayout,
            borderRadius: 6,
            overflow: 'hidden',
            marginBottom: showPercentage ? 12 : 0
          }}>
            <div style={{
              height: '100%',
              background: progress === 100
                ? `linear-gradient(90deg, ${token.colorSuccess} 0%, ${token.colorSuccess} 100%)`
                : `linear-gradient(90deg, ${token.colorPrimary} 0%, ${token.colorPrimary} 100%)`,
              width: `${progress}%`,
              transition: 'all 0.3s ease',
              borderRadius: 6,
              boxShadow: progress > 0 ? token.boxShadow : 'none'
            }} />
          </div>

          {/* 进度百分比 */}
          {showPercentage && (
            <div style={{
              textAlign: 'center',
              fontSize: 32,
              fontWeight: 'bold',
              color: progress === 100 ? token.colorSuccess : token.colorPrimary,
              marginBottom: 8
            }}>
              {progress}%
            </div>
          )}
        </div>

        {/* 状态消息 */}
        <div style={{
          textAlign: 'center',
          fontSize: 16,
          color: token.colorTextSecondary,
          minHeight: 24,
          padding: '0 20px',
          marginBottom: 16
        }}>
          {message || t('preparing')}
        </div>

        {/* 提示文字 */}
        <div style={{
          textAlign: 'center',
          fontSize: 13,
          color: token.colorTextTertiary,
          marginBottom: onCancel ? 16 : 0
        }}>
          {t('doNotClosePage')}
        </div>

        {/* 取消按钮 */}
        {onCancel && (
          <div style={{
            textAlign: 'center',
            marginTop: 16
          }}>
            <Button
              danger
              size="large"
              icon={<StopOutlined />}
              onClick={onCancel}
            >
              {cancelButtonText || t('cancelTask')}
            </Button>
          </div>
        )}
      </div>
    </Modal>
  );
};

export default SSEProgressModal;