import React from 'react';
import { Spin, theme } from 'antd';
import { LoadingOutlined } from '@ant-design/icons';

interface SSELoadingOverlayProps {
  loading: boolean;
  progress: number;
  message: string;
}

export const SSELoadingOverlay: React.FC<SSELoadingOverlayProps> = ({
  loading,
  progress,
  message
}) => {
  const { token } = theme.useToken();

  if (!loading) return null;

  return (
    <div style={{
      position: 'fixed',
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      background: token.colorBgMask,
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'center',
      zIndex: 9999
    }}>
      <div style={{
        background: token.colorBgElevated,
        borderRadius: 12,
        padding: '40px 60px',
        minWidth: 400,
        maxWidth: 600,
        boxShadow: token.boxShadowSecondary
      }}>
        {/* 标题和图标 */}
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
            color: token.colorTextHeading
          }}>
            AI生成中...
          </div>
        </div>

        {/* 进度条 */}
        <div style={{
          marginBottom: 16
        }}>
          <div style={{
            height: 12,
            background: token.colorFillTertiary,
            borderRadius: 6,
            overflow: 'hidden',
            marginBottom: 12
          }}>
            <div style={{
              height: '100%',
              background: progress === 100
                ? `linear-gradient(90deg, ${token.colorSuccess} 0%, ${token.colorSuccessActive} 100%)`
                : `linear-gradient(90deg, ${token.colorPrimary} 0%, ${token.colorPrimaryActive} 100%)`,
              width: `${progress}%`,
              transition: 'all 0.3s ease',
              borderRadius: 6,
              boxShadow: progress > 0 ? token.boxShadow : 'none'
            }} />
          </div>

          {/* 进度百分比 */}
          <div style={{
            textAlign: 'center',
            fontSize: 32,
            fontWeight: 'bold',
            color: progress === 100 ? token.colorSuccess : token.colorPrimary,
            marginBottom: 8
          }}>
            {progress}%
          </div>
        </div>

        {/* 状态消息 */}
        <div style={{
          textAlign: 'center',
          fontSize: 16,
          color: token.colorText,
          minHeight: 24,
          padding: '0 20px'
        }}>
          {message || '准备生成...'}
        </div>

        {/* 提示文字 */}
        <div style={{
          textAlign: 'center',
          fontSize: 13,
          color: token.colorTextTertiary,
          marginTop: 16
        }}>
          请勿关闭页面,生成过程需要一定时间
        </div>
      </div>
    </div>
  );
};