import { Modal, Button, Space, theme } from 'antd';
import { useEffect, useState } from 'react';

interface AnnouncementModalProps {
  visible: boolean;
  onClose: () => void;
  onDoNotShowToday: () => void;
  onNeverShow: () => void;
}

export default function AnnouncementModal({ visible, onClose, onDoNotShowToday, onNeverShow }: AnnouncementModalProps) {
  const [qqImageError, setQqImageError] = useState(false);
  const [wxImageError, setWxImageError] = useState(false);
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

  useEffect(() => {
    if (visible) {
      setQqImageError(false);
      setWxImageError(false);
    }
  }, [visible]);

  const handleDoNotShowToday = () => {
    onDoNotShowToday();
    onClose();
  };

  const handleNeverShow = () => {
    onNeverShow();
    onClose();
  };

  return (
    <Modal
      title={
        <div style={{
          fontSize: '20px',
          fontWeight: 600,
          color: token.colorPrimary,
          textAlign: 'center',
        }}>
          🎉 欢迎使用 AI小说创作助手
        </div>
      }
      open={visible}
      onCancel={onClose}
      footer={
        <Space style={{ width: '100%', justifyContent: 'center' }}>
          <Button
            onClick={handleDoNotShowToday}
            size="large"
            style={{
              borderRadius: '8px',
              height: '40px',
              fontSize: '14px',
            }}
          >
            今日内不再展示
          </Button>
          <Button
            type="primary"
            onClick={handleNeverShow}
            size="large"
            style={{
              borderRadius: '8px',
              height: '40px',
              fontSize: '14px',
              background: token.colorPrimary,
              borderColor: token.colorPrimary,
              boxShadow: `0 8px 20px ${alphaColor(token.colorPrimary, 0.32)}`,
            }}
          >
            永不再展示
          </Button>
        </Space>
      }
      width={700}
      centered
      styles={{
        body: {
          padding: '20px',
          background: token.colorBgContainer,
        },
        header: {
          background: `linear-gradient(135deg, ${alphaColor(token.colorPrimary, 0.1)} 0%, ${alphaColor(token.colorBgContainer, 0.98)} 100%)`,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          padding: '16px 24px',
        },
        footer: {
          background: token.colorBgContainer,
          borderTop: `1px solid ${token.colorBorderSecondary}`,
          padding: '16px 24px',
        },
      }}
    >
      <div style={{ textAlign: 'center' }}>
        <div style={{
          marginBottom: '12px',
          fontSize: '15px',
          color: token.colorTextSecondary,
          lineHeight: '1.5',
        }}>
          <p style={{ marginBottom: '8px' }}>👋 欢迎加入我们的交流群！在这里你可以：</p>
          <ul style={{
            textAlign: 'left',
            marginLeft: '40px',
            marginTop: '0',
            marginBottom: '12px',
          }}>
            <li>💬 与其他创作者交流心得</li>
            <li>💡 获取最新功能更新和使用技巧</li>
            <li>🐛 反馈问题和建议</li>
            <li>📚 分享创作经验和灵感</li>
          </ul>
          <p style={{ fontWeight: 600, color: token.colorText, marginBottom: '12px' }}>
            扫描下方二维码加入交流群：
          </p>
        </div>

        <div style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'flex-start',
          gap: '24px',
          padding: '16px',
          background: token.colorBgLayout,
          borderRadius: '8px',
          flexWrap: 'wrap',
        }}>
          {/* QQ 二维码 */}
          <div style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            minWidth: '200px',
          }}>
            <p style={{ fontWeight: 600, color: token.colorText, marginBottom: '8px', fontSize: '14px' }}>
              QQ交流群
            </p>
            {!qqImageError ? (
              <div style={{
                display: 'flex',
                justifyContent: 'center',
                alignItems: 'center',
                background: token.colorBgContainer,
                borderRadius: '8px',
                padding: '6px',
                boxShadow: `0 2px 8px ${alphaColor(token.colorText, 0.12)}`,
              }}>
                <img
                  src="/qq.jpg"
                  alt="QQ交流群二维码"
                  style={{
                    maxWidth: '180px',
                    maxHeight: '180px',
                    width: 'auto',
                    height: 'auto',
                    display: 'block',
                    objectFit: 'contain',
                  }}
                  onError={() => setQqImageError(true)}
                />
              </div>
            ) : (
              <div style={{
                width: '180px',
                height: '180px',
                display: 'flex',
                justifyContent: 'center',
                alignItems: 'center',
                background: token.colorBgContainer,
                borderRadius: '8px',
                color: token.colorTextTertiary,
              }}>
                <p>二维码加载失败</p>
              </div>
            )}
          </div>

          {/* 微信二维码 */}
          <div style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            minWidth: '200px',
          }}>
            <p style={{ fontWeight: 600, color: token.colorText, marginBottom: '8px', fontSize: '14px' }}>
              微信交流群
            </p>
            {!wxImageError ? (
              <div style={{
                display: 'flex',
                justifyContent: 'center',
                alignItems: 'center',
                background: token.colorBgContainer,
                borderRadius: '8px',
                padding: '6px',
                boxShadow: `0 2px 8px ${alphaColor(token.colorText, 0.12)}`,
              }}>
                <img
                  src="/WX.png"
                  alt="微信交流群二维码"
                  style={{
                    maxWidth: '180px',
                    maxHeight: '180px',
                    width: 'auto',
                    height: 'auto',
                    display: 'block',
                    objectFit: 'contain',
                  }}
                  onError={() => setWxImageError(true)}
                />
              </div>
            ) : (
              <div style={{
                width: '180px',
                height: '180px',
                display: 'flex',
                justifyContent: 'center',
                alignItems: 'center',
                background: token.colorBgContainer,
                borderRadius: '8px',
                color: token.colorTextTertiary,
              }}>
                <p>二维码加载失败</p>
              </div>
            )}
          </div>
        </div>

        <div style={{
          marginTop: '16px',
          padding: '10px',
          background: token.colorWarningBg,
          borderRadius: '8px',
          border: `1px solid ${token.colorWarningBorder}`,
          fontSize: '13px',
          color: token.colorWarning,
        }}>
          💡 提示：选择"今日内不再展示"当天不再显示，选择"永不再展示"将永久隐藏此公告
        </div>
      </div>
    </Modal>
  );
}