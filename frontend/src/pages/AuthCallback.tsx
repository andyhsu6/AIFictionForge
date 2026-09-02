import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Spin, Result, Button, Modal, Input, message, theme } from 'antd';
import { authApi } from '../services/api';
import { syncLanguageWithServer } from '../utils/languageSync';
import { useTranslation } from 'react-i18next';

export default function AuthCallback() {
  const { t } = useTranslation('authCallback');
  const navigate = useNavigate();
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading');
  const [errorMessage, setErrorMessage] = useState('');
  const [showPasswordModal, setShowPasswordModal] = useState(false);
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;
  interface PasswordStatus {
    has_password: boolean;
    has_custom_password: boolean;
    username: string;
    default_password: string;
  }
  const [passwordStatus, setPasswordStatus] = useState<PasswordStatus | null>(null);
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [settingPassword, setSettingPassword] = useState(false);

  useEffect(() => {
    const handleCallback = async () => {
      try {
        // 后端会通过 Cookie 自动设置认证信息
        // 这里只需要验证登录状态
        const currentUser = await authApi.getCurrentUser();
        syncLanguageWithServer();

        // 检查是否是首次登录（通过 Cookie 标记）
        const isFirstLogin = document.cookie.includes('first_login=true');
        
        setStatus('success');

        if (isFirstLogin) {
          // 首次登录：生成默认密码并显示提示
          const defaultPassword = `${currentUser.username}@666`;
          const pwdStatus = {
            has_password: false,
            has_custom_password: false,
            username: currentUser.username,
            default_password: defaultPassword
          };
          setPasswordStatus(pwdStatus);

          // 清除首次登录标记 Cookie
          document.cookie = 'first_login=; path=/; max-age=0';

          // 显示密码初始化弹窗
          setTimeout(() => {
            setShowPasswordModal(true);
          }, 1000);
          return;
        }

        // 非首次登录：正常流程
        // 从 sessionStorage 获取重定向地址
        const redirect = sessionStorage.getItem('login_redirect') || '/';
        sessionStorage.removeItem('login_redirect');

        // 延迟一下再跳转，让用户看到成功提示
        setTimeout(() => {
          navigate(redirect);
        }, 1000);
      } catch (error) {
        console.error('登录失败:', error);
        setStatus('error');
        setErrorMessage(t('error.loginFailedRetry'));
      }
    };

    handleCallback();
  }, [navigate]);

  if (status === 'loading') {
    return (
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
        background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
      }}>
        <div style={{ textAlign: 'center' }}>
          <Spin size="large" />
          <div style={{ marginTop: 20, color: token.colorWhite, fontSize: 16 }}>
            {t('status.processingLogin')}
          </div>
        </div>
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
        background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
      }}>
        <Result
          status="error"
          title={t('error.loginFailed')}
          subTitle={errorMessage}
          extra={
            <Button type="primary" onClick={() => navigate('/login')}>
              {t('actions.backToLogin')}
            </Button>
          }
          style={{ background: token.colorBgContainer, padding: 40, borderRadius: 8 }}
        />
      </div>
    );
  }

  const handleSetPassword = async () => {
    // 如果没有输入新密码，使用默认密码
    const passwordToSet = newPassword || passwordStatus?.default_password;
    
    if (!passwordToSet) {
      message.error(t('password.inputNewPassword'));
      return;
    }
    if (passwordToSet.length < 6) {
      message.error(t('password.minLength'));
      return;
    }
    if (newPassword && newPassword !== confirmPassword) {
      message.error(t('password.mismatch'));
      return;
    }

    setSettingPassword(true);
    try {
      // 首次登录使用初始化接口，后续使用修改接口
      const isFirstLogin = !passwordStatus?.has_password;
      if (isFirstLogin) {
        await authApi.initializePassword(passwordToSet);
        message.success(t('password.initSuccess'));
      } else {
        await authApi.setPassword(passwordToSet);
        message.success(t('password.setSuccess'));
      }
      setShowPasswordModal(false);

      // 继续后续流程
      const redirect = sessionStorage.getItem('login_redirect') || '/';
      sessionStorage.removeItem('login_redirect');

      setTimeout(() => {
        navigate(redirect);
      }, 500);
    } catch {
      message.error(t('password.setFailed'));
    } finally {
      setSettingPassword(false);
    }
  };

  const handleSkipPasswordSetting = async () => {
    // 首次登录时，如果跳过设置，使用默认密码初始化
    const isFirstLogin = !passwordStatus?.has_password;
    if (isFirstLogin && passwordStatus?.default_password) {
      try {
        await authApi.initializePassword(passwordStatus.default_password);
      } catch (error) {
        console.error('初始化默认密码失败:', error);
      }
    }

    setShowPasswordModal(false);

    // 继续后续流程
    const redirect = sessionStorage.getItem('login_redirect') || '/';
    sessionStorage.removeItem('login_redirect');

    setTimeout(() => {
      navigate(redirect);
    }, 500);
  };

  return (
    <>
      <Modal
        title={t('password.modalTitle')}
        open={showPasswordModal}
        centered
        onOk={handleSetPassword}
        onCancel={handleSkipPasswordSetting}
        confirmLoading={settingPassword}
        okText={t('password.okSetPassword')}
        cancelText={t('password.skipForNow')}
        width={500}
      >
        <div style={{ marginBottom: 20 }}>
          <p>{t('password.linuxDoSuccess')}</p>
          <p>{t('password.autoGeneratedHint')}</p>
          {passwordStatus?.default_password && (
            <div style={{
              background: token.colorFillTertiary,
              padding: 12,
              borderRadius: 4,
              marginTop: 12
            }}>
              <strong>{t('password.accountLabel')}</strong>{passwordStatus.username}<br />
              <strong>{t('password.defaultPasswordLabel')}</strong><code style={{
                background: token.colorBgContainer,
                padding: '2px 8px',
                borderRadius: 3,
                color: token.colorPrimary,
                fontSize: 14
              }}>{passwordStatus.default_password}</code>
            </div>
          )}
        </div>

        <div style={{ marginTop: 20 }}>
          <div style={{ marginBottom: 12 }}>
            <label>{t('password.newPasswordLabel')}</label>
            <Input.Password
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={t('password.newPasswordPlaceholder')}
              style={{ marginTop: 4 }}
            />
          </div>
          <div>
            <label>{t('password.confirmPasswordLabel')}</label>
            <Input.Password
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder={t('password.confirmPasswordPlaceholder')}
              style={{ marginTop: 4 }}
            />
          </div>
        </div>
      </Modal>

      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
        background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
      }}>
        <Result
          status="success"
          title={t('status.loginSuccess')}
          subTitle={showPasswordModal ? t("status.setPrompt") : t("status.redirecting")}
          style={{ background: alphaColor(token.colorBgContainer, 0.96), padding: 40, borderRadius: 8 }}
        />
      </div>
    </>
  );
}