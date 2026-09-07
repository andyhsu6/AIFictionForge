import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Form,
  Input,
  Layout,
  Row,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
  message,
  theme,
} from 'antd';
import {
  BookOutlined,
  GlobalOutlined,
  LockOutlined,
  MailOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { authApi } from '../services/api';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ThemeSwitch from '../components/ThemeSwitch';
import { syncLanguageWithServer } from '../utils/languageSync';
import { useTranslation } from 'react-i18next';

const { Title, Paragraph, Text } = Typography;

interface AuthConfig {
  local_auth_enabled: boolean;
  linuxdo_enabled: boolean;
  email_auth_enabled: boolean;
  email_register_enabled: boolean;
}

interface LocalLoginValues {
  username: string;
  password: string;
}

interface EmailLoginValues {
  email: string;
  code: string;
}

interface EmailRegisterValues {
  email: string;
  code: string;
  password: string;
  confirmPassword: string;
  display_name?: string;
}

interface ResetPasswordValues {
  email: string;
  code: string;
  new_password: string;
  confirmNewPassword: string;
}

export default function Login() {
  const { t, i18n } = useTranslation('login');
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(true);
  const [authConfig, setAuthConfig] = useState<AuthConfig>({
    local_auth_enabled: false,
    linuxdo_enabled: false,
    email_auth_enabled: false,
    email_register_enabled: false,
  });
  const [localForm] = Form.useForm<LocalLoginValues>();
  const [emailLoginForm] = Form.useForm<EmailLoginValues>();
  const [emailRegisterForm] = Form.useForm<EmailRegisterValues>();
  const [resetPasswordForm] = Form.useForm<ResetPasswordValues>();
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;
  const primaryButtonShadow = `0 8px 20px ${alphaColor(token.colorPrimary, 0.28)}`;
  const hoverButtonShadow = `0 12px 28px ${alphaColor(token.colorPrimary, 0.36)}`;
  const [loginCodeSending, setLoginCodeSending] = useState(false);
  const [registerCodeSending, setRegisterCodeSending] = useState(false);
  const [resetCodeSending, setResetCodeSending] = useState(false);
  const [loginCountdown, setLoginCountdown] = useState(0);
  const [registerCountdown, setRegisterCountdown] = useState(0);
  const [resetCountdown, setResetCountdown] = useState(0);
  const [showResetPassword, setShowResetPassword] = useState(false);

  const localAuthEnabled = authConfig.local_auth_enabled;
  const linuxdoEnabled = authConfig.linuxdo_enabled;
  const emailAuthEnabled = authConfig.email_auth_enabled;
  const emailRegisterEnabled = authConfig.email_register_enabled;

  useEffect(() => {
    const timers = [
      { value: loginCountdown, setter: setLoginCountdown },
      { value: registerCountdown, setter: setRegisterCountdown },
      { value: resetCountdown, setter: setResetCountdown },
    ].map(({ value, setter }) => {
      if (value <= 0) {
        return null;
      }

      return window.setInterval(() => {
        setter((prev) => {
          if (prev <= 1) {
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
    });

    return () => {
      timers.forEach((timer) => {
        if (timer) {
          window.clearInterval(timer);
        }
      });
    };
  }, [loginCountdown, registerCountdown, resetCountdown]);

  useEffect(() => {
    const checkAuth = async () => {
      try {
        await authApi.getCurrentUser();
        const redirect = searchParams.get('redirect') || '/';
        navigate(redirect);
      } catch {
        try {
          const config = await authApi.getAuthConfig();
          setAuthConfig(config);
        } catch (error) {
          console.error('获取认证配置失败:', error);
          setAuthConfig({
            local_auth_enabled: false,
            linuxdo_enabled: true,
            email_auth_enabled: false,
            email_register_enabled: false,
          });
        }
        setChecking(false);
      }
    };
    checkAuth();
  }, [navigate, searchParams]);

  const handleLoginSuccess = () => {
    message.success(t('toast.loginSuccess'));
    syncLanguageWithServer();
    const redirect = searchParams.get('redirect') || '/';
    navigate(redirect);
  };

  const handleLocalLogin = async (values: LocalLoginValues) => {
    try {
      setLoading(true);
      const response = await authApi.localLogin(values.username, values.password);
      if (response.success) {
        handleLoginSuccess();
      }
    } catch (error) {
      console.error('本地登录失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleEmailLogin = async (values: EmailLoginValues) => {
    try {
      setLoading(true);
      const response = await authApi.emailLogin({
        email: values.email,
        code: values.code,
      });
      if (response.success) {
        handleLoginSuccess();
      }
    } catch (error) {
      console.error('邮箱验证码登录失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const sendLoginCode = async () => {
    try {
      const values = await emailLoginForm.validateFields(['email']);
      setLoginCodeSending(true);
      const result = await authApi.sendEmailCode({ email: values.email, scene: 'login' });
      message.success(result.message || t('toast.codeSent'));
      setLoginCountdown(result.resend_interval_seconds || 60);
    } catch (error) {
      console.error('发送 login 验证码失败:', error);
    } finally {
      setLoginCodeSending(false);
    }
  };

  const sendRegisterCode = async () => {
    try {
      const values = await emailRegisterForm.validateFields(['email']);
      setRegisterCodeSending(true);
      const result = await authApi.sendEmailCode({ email: values.email, scene: 'register' });
      message.success(result.message || t('toast.codeSent'));
      setRegisterCountdown(result.resend_interval_seconds || 60);
    } catch (error) {
      console.error('发送 register 验证码失败:', error);
    } finally {
      setRegisterCodeSending(false);
    }
  };

  const sendResetCode = async () => {
    try {
      const values = await resetPasswordForm.validateFields(['email']);
      setResetCodeSending(true);
      const result = await authApi.sendEmailCode({ email: values.email, scene: 'reset_password' });
      message.success(result.message || t('toast.codeSent'));
      setResetCountdown(result.resend_interval_seconds || 60);
    } catch (error) {
      console.error('发送 reset_password 验证码失败:', error);
    } finally {
      setResetCodeSending(false);
    }
  };

  const handleEmailRegister = async (values: EmailRegisterValues) => {
    try {
      setLoading(true);
      const response = await authApi.emailRegister({
        email: values.email,
        code: values.code,
        password: values.password,
        display_name: values.display_name?.trim() || undefined,
      });
      if (response.success) {
        message.success(t('toast.registerSuccess'));
        emailRegisterForm.resetFields(['code', 'password', 'confirmPassword']);
        setRegisterCountdown(0);
        handleLoginSuccess();
      }
    } catch (error) {
      console.error('邮箱注册失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleResetPassword = async (values: ResetPasswordValues) => {
    try {
      setLoading(true);
      const result = await authApi.resetEmailPassword({
        email: values.email,
        code: values.code,
        new_password: values.new_password,
      });
      message.success(result.message || t('toast.resetSuccess'));
      resetPasswordForm.resetFields(['code', 'new_password', 'confirmNewPassword']);
      setResetCountdown(0);
      setShowResetPassword(false);
    } catch (error) {
      console.error('重置密码失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleLinuxDOLogin = async () => {
    try {
      setLoading(true);
      const response = await authApi.getLinuxDOAuthUrl();

      const redirect = searchParams.get('redirect');
      if (redirect) {
        sessionStorage.setItem('login_redirect', redirect);
      }

      window.location.href = response.auth_url;
    } catch (error) {
      console.error('获取授权地址失败:', error);
      message.error(t('toast.authUrlFailed'));
      setLoading(false);
    }
  };

  const loginTips = useMemo(() => {
    const tips: string[] = [
      t('tips.linuxdoFirstLogin'),
    ];

    if (localAuthEnabled) {
      tips.unshift(t('tips.localDefaultAccount'));
    }

    if (emailAuthEnabled) {
      tips.push(t('tips.emailResetPassword'));
    }

    return tips;
  }, [emailAuthEnabled, localAuthEnabled, t]);

  const featureItems = [
    {
      icon: <RobotOutlined />,
      title: t('features.multiModel.title'),
      description: t('features.multiModel.desc'),
    },
    {
      icon: <ThunderboltOutlined />,
      title: t('features.wizard.title'),
      description: t('features.wizard.desc'),
    },
    {
      icon: <TeamOutlined />,
      title: t('features.characters.title'),
      description: t('features.characters.desc'),
    },
    {
      icon: <BookOutlined />,
      title: t('features.chapters.title'),
      description: t('features.chapters.desc'),
    },
  ];

  const renderLocalLogin = () => (
    <>
      <Form
        form={localForm}
        layout="vertical"
        onFinish={handleLocalLogin}
        size="large"
        style={{ marginTop: 16 }}
      >
        <Form.Item
          name="username"
          label={t('local.username')}
          rules={[{ required: true, message: t('local.usernameRequired') }]}
        >
          <Input
            prefix={<UserOutlined style={{ color: token.colorTextTertiary }} />}
            placeholder={t('local.usernamePlaceholder')}
            autoComplete="username"
            style={{ height: 46, borderRadius: 12 }}
          />
        </Form.Item>
        <Form.Item
          name="password"
          label={t('local.password')}
          rules={[{ required: true, message: t('local.passwordRequired') }]}
        >
          <Input.Password
            prefix={<LockOutlined style={{ color: token.colorTextTertiary }} />}
            placeholder={t('local.passwordPlaceholder')}
            autoComplete="current-password"
            style={{ height: 46, borderRadius: 12 }}
          />
        </Form.Item>
        <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
          <Button
            type="primary"
            htmlType="submit"
            loading={loading}
            block
            style={{
              height: 46,
              fontSize: 16,
              fontWeight: 600,
              background: `linear-gradient(90deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.86)} 100%)`,
              border: 'none',
              borderRadius: '12px',
              boxShadow: primaryButtonShadow,
            }}
          >
            {t('local.submit')}
          </Button>
        </Form.Item>
      </Form>

      {linuxdoEnabled ? (
        <>
          <Divider style={{ margin: '18px 0 16px' }}>{t('local.thirdParty')}</Divider>
          {renderLinuxDOLogin()}
        </>
      ) : null}
    </>
  );

  const renderEmailLogin = () => {
    if (showResetPassword) {
      return (
        <div style={{ marginTop: 16 }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space style={{ width: '100%', justifyContent: 'space-between' }}>
              <Title level={5} style={{ margin: 0 }}>{t('reset.title')}</Title>
              <Button type="link" style={{ paddingInline: 0 }} onClick={() => setShowResetPassword(false)}>
                {t('reset.backToCodeLogin')}
              </Button>
            </Space>

            <Card size="small" bordered={false} style={{ borderRadius: 12, background: token.colorFillAlter }}>
              <Form
                form={resetPasswordForm}
                layout="vertical"
                onFinish={handleResetPassword}
                size="middle"
              >
                <Form.Item
                  name="email"
                  label={t('email.registerEmail')}
                  rules={[
                    { required: true, message: t('email.registerEmailRequired') },
                    { type: 'email', message: t('email.invalidEmail') },
                  ]}
                >
                  <Input prefix={<MailOutlined />} placeholder={t('email.registerEmailPlaceholder')} />
                </Form.Item>
                <Form.Item label={t('reset.codeLabel')} required style={{ marginBottom: 12 }}>
                  <Space.Compact style={{ width: '100%' }}>
                    <Form.Item
                      name="code"
                      noStyle
                      rules={[
                        { required: true, message: t('reset.codeRequired') },
                        { len: 6, message: t('email.codeLength') },
                      ]}
                    >
                      <Input placeholder={t('reset.codePlaceholder')} maxLength={6} />
                    </Form.Item>
                    <Button
                      onClick={sendResetCode}
                      loading={resetCodeSending}
                      disabled={resetCountdown > 0}
                    >
                      {resetCountdown > 0 ? t('email.resendIn', { s: resetCountdown }) : t('email.sendCode')}
                    </Button>
                  </Space.Compact>
                </Form.Item>
                <Form.Item
                  name="new_password"
                  label={t('reset.newPassword')}
                  rules={[
                    { required: true, message: t('reset.newPasswordRequired') },
                    { min: 6, message: t('email.passwordMin') },
                  ]}
                >
                  <Input.Password prefix={<LockOutlined />} placeholder={t('reset.newPasswordPlaceholder')} />
                </Form.Item>
                <Form.Item
                  name="confirmNewPassword"
                  label={t('reset.confirmNewPassword')}
                  dependencies={['new_password']}
                  rules={[
                    { required: true, message: t('reset.confirmNewPasswordRequired') },
                    ({ getFieldValue }) => ({
                      validator(_, value) {
                        if (!value || getFieldValue('new_password') === value) {
                          return Promise.resolve();
                        }
                        return Promise.reject(new Error(t('reset.passwordMismatch')));
                      },
                    }),
                  ]}
                >
                  <Input.Password prefix={<LockOutlined />} placeholder={t('reset.confirmNewPasswordPlaceholder')} />
                </Form.Item>
                <Button type="default" htmlType="submit" loading={loading} block>
                  {t('reset.submit')}
                </Button>
              </Form>
            </Card>
          </Space>
        </div>
      );
    }

    return (
      <Form
        form={emailLoginForm}
        layout="vertical"
        onFinish={handleEmailLogin}
        size="large"
        style={{ marginTop: 16 }}
      >
        <Form.Item
          name="email"
          label={t('email.address')}
          rules={[
            { required: true, message: t('email.addressRequired') },
            { type: 'email', message: t('email.invalidEmail') },
          ]}
        >
          <Input
            prefix={<MailOutlined style={{ color: token.colorTextTertiary }} />}
            placeholder={t('email.registeredPlaceholder')}
            autoComplete="email"
            style={{ height: 46, borderRadius: 12 }}
          />
        </Form.Item>

        <Form.Item label={t('email.loginCode')} required style={{ marginBottom: 24 }}>
          <Space.Compact style={{ width: '100%' }}>
            <Form.Item
              name="code"
              noStyle
              rules={[
                { required: true, message: t('email.loginCodeRequired') },
                { len: 6, message: t('email.codeLength') },
              ]}
            >
              <Input
                prefix={<SafetyCertificateOutlined style={{ color: token.colorTextTertiary }} />}
                placeholder={t('email.loginCodePlaceholder')}
                maxLength={6}
                style={{ height: 46, borderRadius: '12px 0 0 12px' }}
              />
            </Form.Item>
            <Button
              style={{ height: 46 }}
              onClick={sendLoginCode}
              loading={loginCodeSending}
              disabled={loginCountdown > 0}
            >
              {loginCountdown > 0 ? t('email.resendIn', { s: loginCountdown }) : t('email.sendCode')}
            </Button>
          </Space.Compact>
        </Form.Item>

        <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
          <Button
            type="primary"
            htmlType="submit"
            loading={loading}
            block
            style={{
              height: 46,
              fontSize: 16,
              fontWeight: 600,
              background: `linear-gradient(90deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.86)} 100%)`,
              border: 'none',
              borderRadius: '12px',
              boxShadow: primaryButtonShadow,
            }}
          >
            {t('email.codeLogin')}
          </Button>
        </Form.Item>

        <div style={{ marginTop: 12, textAlign: 'right' }}>
          <Button type="link" style={{ paddingInline: 0 }} onClick={() => setShowResetPassword(true)}>
            {t('email.forgotPassword')}
          </Button>
        </div>
      </Form>
    );
  };

  const renderEmailRegister = () => (
    <Form
      form={emailRegisterForm}
      layout="vertical"
      onFinish={handleEmailRegister}
      size="large"
      style={{ marginTop: 16 }}
    >
      <Form.Item
        name="email"
        label={t('email.registerEmail')}
        rules={[
          { required: true, message: t('email.registerEmailRequired') },
          { type: 'email', message: t('email.invalidEmail') },
        ]}
      >
        <Input
          prefix={<MailOutlined style={{ color: token.colorTextTertiary }} />}
          placeholder={t('email.registerEmailPlaceholder')}
          autoComplete="email"
          style={{ height: 46, borderRadius: 12 }}
        />
      </Form.Item>

      <Form.Item label={t('register.emailCode')} required style={{ marginBottom: 12 }}>
        <Space.Compact style={{ width: '100%' }}>
          <Form.Item
            name="code"
            noStyle
            rules={[
              { required: true, message: t('register.emailCodeRequired') },
              { len: 6, message: t('email.codeLength') },
            ]}
          >
            <Input
              prefix={<SafetyCertificateOutlined style={{ color: token.colorTextTertiary }} />}
              placeholder={t('register.codePlaceholder')}
              maxLength={6}
              style={{ height: 46, borderRadius: '12px 0 0 12px' }}
            />
          </Form.Item>
          <Button
            style={{ height: 46 }}
            onClick={sendRegisterCode}
            loading={registerCodeSending}
            disabled={registerCountdown > 0}
          >
            {registerCountdown > 0 ? t('email.resendIn', { s: registerCountdown }) : t('email.sendCode')}
          </Button>
        </Space.Compact>
      </Form.Item>

      <Form.Item
        name="display_name"
        label={t('register.nickname')}
        rules={[{ max: 50, message: t('register.nicknameMax') }]}
      >
        <Input
          prefix={<UserOutlined style={{ color: token.colorTextTertiary }} />}
          placeholder={t('register.nicknamePlaceholder')}
          autoComplete="nickname"
          style={{ height: 46, borderRadius: 12 }}
        />
      </Form.Item>

      <Form.Item
        name="password"
        label={t('register.password')}
        rules={[
          { required: true, message: t('register.passwordRequired') },
          { min: 6, message: t('email.passwordMin') },
        ]}
      >
        <Input.Password
          prefix={<LockOutlined style={{ color: token.colorTextTertiary }} />}
          placeholder={t('register.passwordPlaceholder')}
          autoComplete="new-password"
          style={{ height: 46, borderRadius: 12 }}
        />
      </Form.Item>

      <Form.Item
        name="confirmPassword"
        label={t('register.confirmPassword')}
        dependencies={['password']}
        rules={[
          { required: true, message: t('register.confirmPasswordRequired') },
          ({ getFieldValue }) => ({
            validator(_, value) {
              if (!value || getFieldValue('password') === value) {
                return Promise.resolve();
              }
              return Promise.reject(new Error(t('register.passwordMismatch')));
            },
          }),
        ]}
      >
        <Input.Password
          prefix={<LockOutlined style={{ color: token.colorTextTertiary }} />}
          placeholder={t('register.confirmPasswordPlaceholder')}
          autoComplete="new-password"
          style={{ height: 46, borderRadius: 12 }}
        />
      </Form.Item>

      <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
        <Button
          type="primary"
          htmlType="submit"
          loading={loading}
          block
          style={{
            height: 46,
            fontSize: 16,
            fontWeight: 600,
            background: `linear-gradient(90deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.86)} 100%)`,
            border: 'none',
            borderRadius: '12px',
            boxShadow: primaryButtonShadow,
          }}
        >
          {t('register.submit')}
        </Button>
      </Form.Item>

      <Text type="secondary" style={{ marginTop: 12, display: 'block' }}>
        {t('register.hint')}
      </Text>
    </Form>
  );

  const renderLinuxDOLogin = () => (
    <div>
      <Button
        type="primary"
        size="large"
        icon={(
          <img
            src="/favicon.ico"
            alt="LinuxDO"
            style={{
              width: 20,
              height: 20,
              marginRight: 8,
              verticalAlign: 'middle',
            }}
          />
        )}
        loading={loading}
        onClick={handleLinuxDOLogin}
        block
        style={{
          height: 46,
          fontSize: 16,
          fontWeight: 600,
          background: `linear-gradient(90deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.86)} 100%)`,
          border: 'none',
          borderRadius: '12px',
          boxShadow: primaryButtonShadow,
          transition: 'all 0.3s ease',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.transform = 'translateY(-2px)';
          e.currentTarget.style.boxShadow = hoverButtonShadow;
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.transform = 'translateY(0)';
          e.currentTarget.style.boxShadow = primaryButtonShadow;
        }}
      >
        {t('linuxdo.login')}
      </Button>
    </div>
  );

  const authTabs = [
    ...(localAuthEnabled
      ? [
          {
            key: 'local-login',
            label: t('tabs.local'),
            children: renderLocalLogin(),
          },
        ]
      : []),
    ...(emailAuthEnabled
      ? [
          {
            key: 'email-login',
            label: t('tabs.emailLogin'),
            children: renderEmailLogin(),
          },
        ]
      : []),
    ...(emailAuthEnabled && emailRegisterEnabled
      ? [
          {
            key: 'email-register',
            label: t('tabs.emailRegister'),
            children: renderEmailRegister(),
          },
        ]
      : []),
  ];

  if (checking) {
    return (
      <div
        style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          minHeight: '100vh',
          background: token.colorBgLayout,
        }}
      >
        <Spin size="large" style={{ color: token.colorPrimary }} />
      </div>
    );
  }

  return (
    <>
      <Layout style={{ minHeight: '100vh', background: token.colorBgLayout }}>
        <div
          style={{
            position: 'fixed',
            top: 20,
            right: 20,
            zIndex: 10,
            padding: '8px 10px',
            borderRadius: 12,
            background: alphaColor(token.colorBgContainer, 0.9),
            border: `1px solid ${token.colorBorderSecondary}`,
            backdropFilter: 'blur(6px)',
            display: 'flex',
            alignItems: 'center',
            gap: 10,
          }}
        >
          <GlobalOutlined />
          <Select
            size="small"
            variant="borderless"
            value={i18n.language?.startsWith('zh') ? 'zh' : 'en'}
            onChange={(value) => i18n.changeLanguage(value)}
            style={{ minWidth: 96 }}
            options={[
              { value: 'zh', label: '简体中文' },
              { value: 'en', label: 'English' },
            ]}
          />
          <ThemeSwitch size="small" />
        </div>
        <Row style={{ minHeight: '100vh' }}>
          <Col xs={0} lg={11}>
            <section
              style={{
                height: '100%',
                padding: '44px 64px 88px',
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
                position: 'relative',
                overflow: 'hidden',
                backgroundColor: alphaColor(token.colorBgContainer, 0.78),
                backgroundImage: `linear-gradient(${alphaColor(token.colorTextSecondary, 0.06)} 1px, transparent 1px), linear-gradient(90deg, ${alphaColor(token.colorTextSecondary, 0.06)} 1px, transparent 1px)`,
                backgroundSize: '68px 68px',
              }}
            >
              <div
                style={{
                  position: 'absolute',
                  inset: 0,
                  background: `radial-gradient(circle at 25% 20%, ${alphaColor(token.colorPrimary, 0.12)} 0%, transparent 50%)`,
                  pointerEvents: 'none',
                }}
              />

              <div
                style={{
                  position: 'relative',
                  zIndex: 1,
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'space-between',
                  gap: 34,
                  width: '100%',
                }}
              >
                <Space align="center" size={14}>
                  <div
                    style={{
                      width: 46,
                      height: 46,
                      borderRadius: 14,
                      background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.7)} 100%)`,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      boxShadow: primaryButtonShadow,
                    }}
                  >
                    <img
                      src="/logo.svg"
                      alt="AIFictionForge"
                      style={{ width: 26, height: 26, filter: 'brightness(0) invert(1)' }}
                    />
                  </div>
                  <Title level={3} style={{ margin: 0, color: token.colorText }}>
                    AIFictionForge
                  </Title>
                </Space>

                <Space direction="vertical" size={32} style={{ width: '100%' }}>
                  <div style={{ maxWidth: 'min(860px, 100%)' }}>
                    <Title
                      level={1}
                      style={{
                        marginBottom: 22,
                        color: token.colorText,
                        lineHeight: 1.12,
                        fontWeight: 800,
                        fontSize: 'clamp(52px, 3vw, 78px)',
                      }}
                    >
                      {t('hero.line1')}
                      <br />
                      <span
                        style={{
                          backgroundImage: `linear-gradient(90deg, ${token.colorPrimary} 0%, #d946ef 100%)`,
                          WebkitBackgroundClip: 'text',
                          backgroundClip: 'text',
                          WebkitTextFillColor: 'transparent',
                          color: token.colorPrimary,
                        }}
                      >
                        {t('hero.line2')}
                      </span>
                    </Title>
                    <Paragraph
                      style={{
                        fontSize: 'clamp(18px, 1vw, 22px)',
                        lineHeight: 1.85,
                        color: token.colorTextSecondary,
                        marginBottom: 0,
                        maxWidth: 800,
                      }}
                    >
                      {t('hero.description')}
                    </Paragraph>
                  </div>

                  <Row gutter={[20, 20]} style={{ width: '100%', maxWidth: 'min(920px, 100%)' }}>
                    {featureItems.map((item) => (
                      <Col span={12} key={item.title}>
                        <Card
                          size="small"
                          bordered={false}
                          style={{
                            height: '100%',
                            minHeight: 120,
                            borderRadius: 16,
                            background: alphaColor(token.colorBgContainer, 0.9),
                          }}
                          bodyStyle={{ padding: 16 }}
                        >
                          <Space direction="vertical" size={8}>
                            <Space size={10} style={{ color: token.colorPrimary, fontWeight: 700, fontSize: 15 }}>
                              {item.icon}
                              <span>{item.title}</span>
                            </Space>
                            <Paragraph style={{ marginBottom: 0, color: token.colorTextSecondary, fontSize: 14, lineHeight: 1.65 }}>
                              {item.description}
                            </Paragraph>
                          </Space>
                        </Card>
                      </Col>
                    ))}
                  </Row>
                </Space>

                <Space size={[10, 14]} wrap style={{ maxWidth: 'min(860px, 100%)' }}>
                  <Tag color="blue">OpenAI</Tag>
                  <Tag color="geekblue">Gemini</Tag>
                  <Tag color="purple">Claude</Tag>
                  <Tag color="cyan">LinuxDO OAuth</Tag>
                  <Tag color="green">Docker Compose</Tag>
                  <Tag color="gold">PostgreSQL</Tag>
                </Space>
              </div>

              <Paragraph
                style={{
                  marginBottom: 0,
                  fontSize: 12,
                  color: token.colorTextTertiary,
                  position: 'relative',
                  zIndex: 1,
                  letterSpacing: 0.4,
                }}
              >
                © 2026 AIFictionForge · GPLv3 License
              </Paragraph>
            </section>
          </Col>

          <Col xs={24} lg={13}>
            <section
              style={{
                minHeight: '100vh',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: '48px min(7vw, 72px)',
                background: token.colorBgLayout,
              }}
            >
              <div style={{ width: '100%', maxWidth: 520 }}>
                <Space direction="vertical" size={4}>
                  <Title level={2} style={{ marginBottom: 0, fontWeight: 700, color: token.colorText }}>
                    {t('panel.welcomeBack')}
                  </Title>
                  <Paragraph style={{ marginBottom: 0, color: token.colorTextSecondary }}>
                    {t('panel.subtitle')}
                  </Paragraph>
                </Space>

                <div style={{ marginTop: 22 }}>
                  {authTabs.length > 0 ? (
                    <Tabs defaultActiveKey={authTabs[0].key} items={authTabs} />
                  ) : null}

                  {!localAuthEnabled && !linuxdoEnabled && !emailAuthEnabled ? (
                    <Alert
                      type="warning"
                      showIcon
                      message={t('alert.noAuthMethod')}
                      description={t('alert.noAuthMethodDesc')}
                    />
                  ) : null}

                  {emailAuthEnabled && !emailRegisterEnabled ? (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginTop: 12, borderRadius: 12 }}
                      message={t('alert.registerClosed')}
                      description={t('alert.registerClosedDesc')}
                    />
                  ) : null}

                  <Divider style={{ margin: '20px 0 14px' }} />
                  <Alert
                    type="info"
                    showIcon
                    icon={<SafetyCertificateOutlined />}
                    style={{ background: alphaColor(token.colorPrimary, 0.06), borderRadius: 12 }}
                    message={t('alert.loginNotes')}
                    description={(
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {loginTips.map((tip) => (
                          <li key={tip} style={{ marginBottom: 4 }}>
                            {tip}
                          </li>
                        ))}
                      </ul>
                    )}
                  />
                </div>
              </div>
            </section>
          </Col>
        </Row>
      </Layout>
    </>
  );
}
