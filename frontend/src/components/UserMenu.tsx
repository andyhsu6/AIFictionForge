import { useState, useEffect } from 'react';
import { App, Dropdown, Avatar, Space, Typography, Modal, Form, Input, Button, theme } from 'antd';
import { UserOutlined, LogoutOutlined, TeamOutlined, CrownOutlined, LockOutlined } from '@ant-design/icons';
import { authApi } from '../services/api';
import type { User } from '../types';
import type { MenuProps } from 'antd';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;

interface UserMenuProps {
  /** 是否总是显示完整信息（用于移动端侧边栏） */
  showFullInfo?: boolean;
  /** 紧凑模式（用于折叠侧边栏，仅展示头像） */
  compact?: boolean;
}

export default function UserMenu({ showFullInfo = false, compact = false }: UserMenuProps) {
  const { message } = App.useApp();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [showChangePassword, setShowChangePassword] = useState(false);
  const [changePasswordForm] = Form.useForm();
  const [changingPassword, setChangingPassword] = useState(false);
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

  useEffect(() => {
    loadCurrentUser();
  }, []);

  const loadCurrentUser = async () => {
    try {
      const user = await authApi.getCurrentUser();
      setCurrentUser(user);
    } catch (error) {
      console.error('load current user failed:', error);
    }
  };

  const handleLogout = async () => {
    try {
      await authApi.logout();
      message.success(t('userMenu.loggedOut'));
      window.location.href = '/login';
    } catch (error) {
      console.error('logout failed:', error);
      message.error(t('userMenu.logoutFailed'));
    }
  };

  const handleShowUserManagement = () => {
    if (!currentUser?.is_admin) {
      message.warning(t('userMenu.adminOnly'));
      return;
    }
    navigate('/user-management');
  };

  const handleChangePassword = async (values: { oldPassword: string; newPassword: string }) => {
    try {
      setChangingPassword(true);
      await authApi.setPassword(values.newPassword);
      message.success(t('userMenu.passwordChanged'));
      setShowChangePassword(false);
      changePasswordForm.resetFields();
    } catch (error: unknown) {
      console.error('change password failed:', error);
      const err = error as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || t('userMenu.passwordChangeFailed'));
    } finally {
      setChangingPassword(false);
    }
  };

  const menuItems: MenuProps['items'] = [
    {
      key: 'user-info',
      label: (
        <div style={{ padding: '8px 0' }}>
          <Text strong>{currentUser?.display_name || currentUser?.username}</Text>
          <br />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t('userMenu.trustLevel', { level: currentUser?.trust_level ?? 0 })}
            {currentUser?.is_admin && ` · ${t('userMenu.admin')}`}
          </Text>
        </div>
      ),
      disabled: true,
    },
    {
      type: 'divider',
    },
    ...(currentUser?.is_admin ? [
      {
        key: 'user-management',
        icon: <TeamOutlined />,
        label: t('userMenu.userManagement'),
        onClick: handleShowUserManagement,
      },
      {
        type: 'divider' as const,
      }
    ] : []),
    {
        key: 'change-password',
        icon: <LockOutlined />,
        label: t('userMenu.changePassword'),
      onClick: () => setShowChangePassword(true),
    },
    {
      type: 'divider',
    },
    {
        key: 'logout',
        icon: <LogoutOutlined />,
        label: t('userMenu.logout'),
      onClick: handleLogout,
    },
  ];

  if (!currentUser) {
    return null;
  }

  return (
    <>
      <Dropdown menu={{ items: menuItems }} placement="bottomRight">
        <div
          style={{
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: compact ? 0 : 12,
            padding: compact ? '4px' : '8px 16px',
            background: alphaColor(token.colorBgContainer, 0.65), // 保持半透明以配合 Backdrop
            backdropFilter: 'blur(10px)',
            WebkitBackdropFilter: 'blur(10px)',
            borderRadius: compact ? 16 : 24,
            border: `1px solid ${token.colorBorder}`,
            transition: 'all 0.3s ease',
            boxShadow: `0 8px 20px ${alphaColor(token.colorText, 0.08)}`,
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = token.colorBgContainer; // 悬浮时变实
            e.currentTarget.style.transform = 'translateY(-2px)';
            e.currentTarget.style.boxShadow = `0 12px 28px ${alphaColor(token.colorText, 0.14)}`;
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = alphaColor(token.colorBgContainer, 0.65);
            e.currentTarget.style.transform = 'translateY(0)';
            e.currentTarget.style.boxShadow = `0 8px 20px ${alphaColor(token.colorText, 0.08)}`;
          }}
        >
          <div style={{ position: 'relative' }}>
            <Avatar
              src={currentUser.avatar_url}
              icon={<UserOutlined />}
              size={compact ? 32 : 40}
              style={{
                backgroundColor: token.colorPrimary,
                border: `3px solid ${token.colorWhite}`,
                boxShadow: `0 8px 20px ${alphaColor(token.colorText, 0.12)}`,
              }}
            />
            {currentUser.is_admin && (
              <div style={{
                position: 'absolute',
                bottom: -2,
                right: -2,
                width: 18,
                height: 18,
                background: `linear-gradient(135deg, ${token.colorWarning} 0%, ${token.colorWarningHover} 100%)`,
                borderRadius: '50%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                border: `2px solid ${token.colorWhite}`,
                boxShadow: `0 2px 4px ${alphaColor(token.colorText, 0.2)}`,
              }}>
                <CrownOutlined style={{ fontSize: 9, color: token.colorWhite }} />
              </div>
            )}
          </div>
          <Space direction="vertical" size={0} style={{ display: compact ? 'none' : ((window.innerWidth <= 768 && !showFullInfo) ? 'none' : 'flex') }}>
            <Text strong style={{
              color: token.colorText,
              fontSize: 14,
              lineHeight: '20px',
            }}>
              {currentUser.display_name || currentUser.username}
            </Text>
            <Text style={{
              color: token.colorTextSecondary,
              fontSize: 12,
              lineHeight: '18px',
            }}>
              {currentUser.is_admin ? t('userMenu.adminBadge') : t('userMenu.trustLevelBadge', { level: currentUser.trust_level })}
            </Text>
          </Space>
        </div>
      </Dropdown>

      <Modal
        title={t('userMenu.changePassword')}
        open={showChangePassword}
        onCancel={() => {
          setShowChangePassword(false);
          changePasswordForm.resetFields();
        }}
        footer={null}
        width={480}
        centered
      >
        <Form
          form={changePasswordForm}
          layout="vertical"
          onFinish={handleChangePassword}
          autoComplete="off"
        >
          <Form.Item
            label={t('userMenu.newPassword')}
            name="newPassword"
            rules={[
              { required: true, message: t('userMenu.newPasswordRequired') },
              { min: 6, message: t('userMenu.passwordMin') },
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder={t('userMenu.newPasswordPlaceholder')}
              autoComplete="new-password"
            />
          </Form.Item>

          <Form.Item
            label={t('userMenu.confirmPassword')}
            name="confirmPassword"
            dependencies={['newPassword']}
            rules={[
              { required: true, message: t('userMenu.confirmPasswordRequired') },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('newPassword') === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error(t('userMenu.passwordMismatch')));
                },
              }),
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder={t('userMenu.confirmPasswordPlaceholder')}
              autoComplete="new-password"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setShowChangePassword(false);
                changePasswordForm.resetFields();
              }}>
                {t('cancel')}
              </Button>
              <Button type="primary" htmlType="submit" loading={changingPassword}>
                {t('userMenu.submitChanges')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}