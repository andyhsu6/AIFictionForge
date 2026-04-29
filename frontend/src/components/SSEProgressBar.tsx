import React from 'react';
import { theme } from 'antd';

interface SSEProgressBarProps {
  loading: boolean;
  progress: number;
  message: string;
}

export const SSEProgressBar: React.FC<SSEProgressBarProps> = ({
  loading,
  progress,
  message
}) => {
  const { token } = theme.useToken();

  if (!loading) return null;

  return (
    <div style={{ marginTop: 16 }}>
      {/* 进度条 */}
      <div style={{
        height: 8,
        background: token.colorFillTertiary,
        borderRadius: 4,
        overflow: 'hidden',
        marginBottom: 8
      }}>
        <div style={{
          height: '100%',
          background: progress === 100 ? token.colorSuccess : token.colorPrimary,
          width: `${progress}%`,
          transition: 'all 0.3s ease',
          borderRadius: 4
        }} />
      </div>
      
      {/* 进度信息 */}
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        fontSize: 14
      }}>
        <span style={{ color: token.colorTextSecondary }}>
          {message || '准备生成...'}
        </span>
        <span style={{ 
          fontWeight: 'bold',
          color: progress === 100 ? token.colorSuccess : token.colorPrimary
        }}>
          {progress}%
        </span>
      </div>
    </div>
  );
};