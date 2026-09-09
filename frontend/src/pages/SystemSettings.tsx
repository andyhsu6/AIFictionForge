import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Col, Form, Input, InputNumber, Row, Select, Space, Spin, Switch, Typography, message, theme } from 'antd';
import { CheckCircleOutlined, ReloadOutlined, SaveOutlined, SendOutlined, SettingOutlined } from '@ant-design/icons';
import { authApi, settingsApi } from '../services/api';
import type { SystemSMTPSettings, SystemSMTPSettingsUpdate, User } from '../types';
import { useTranslation } from 'react-i18next';

const { Title, Text, Paragraph } = Typography;
const { Option } = Select;

const qqDefaults: Pick<SystemSMTPSettings, 'smtp_provider' | 'smtp_host' | 'smtp_port' | 'smtp_use_ssl' | 'smtp_use_tls'> = {
  smtp_provider: 'qq',
  smtp_host: 'smtp.qq.com',
  smtp_port: 465,
  smtp_use_ssl: true,
  smtp_use_tls: false,
};

export default function SystemSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const { t } = useTranslation('systemSettings');
  const { token } = theme.useToken();
  const [form] = Form.useForm<SystemSMTPSettingsUpdate>();
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testTargetEmail, setTestTargetEmail] = useState('');

  const pageBackground = `linear-gradient(180deg, ${token.colorBgLayout} 0%, ${token.colorFillSecondary} 100%)`;
  const headerBackground = `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`;
  const footerSafeOffset = 88;

  const loadData = async () => {
    setInitialLoading(true);
    try {
      const [user, smtpSettings] = await Promise.all([
        authApi.getCurrentUser(),
        settingsApi.getSystemSMTPSettings(),
      ]);
      setCurrentUser(user);
      form.setFieldsValue(smtpSettings);
    } catch (error) {
      console.error('加载系统设置失败:', error);
      message.error(t('loadFailed'));
    } finally {
      setInitialLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleProviderChange = (value: string) => {
    if (value === 'qq') {
      form.setFieldsValue(qqDefaults);
    }
  };

  const handleSave = async (values: SystemSMTPSettingsUpdate) => {
    setSaving(true);
    try {
      const payload = values.smtp_provider === 'qq'
        ? {
            ...values,
            ...qqDefaults,
            smtp_username: values.smtp_username,
            smtp_password: values.smtp_password,
            smtp_from_email: values.smtp_from_email,
            smtp_from_name: values.smtp_from_name,
            email_auth_enabled: values.email_auth_enabled,
            email_register_enabled: values.email_register_enabled,
            verification_code_ttl_minutes: values.verification_code_ttl_minutes,
            verification_resend_interval_seconds: values.verification_resend_interval_seconds,
          }
        : values;
      const result = await settingsApi.updateSystemSMTPSettings(payload);
      form.setFieldsValue(result);
      message.success(t('saveSuccess'));
    } catch (error) {
      console.error('保存系统设置失败:', error);
      message.error(t('saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = useCallback(async () => {
    const toEmail = testTargetEmail.trim();
    if (!toEmail) {
      message.warning(t('test.fillTargetFirst'));
      return;
    }

    setTesting(true);
    try {
      const result = await settingsApi.testSystemSMTPSettings({ to_email: toEmail });
      if (result.success) {
        message.success(result.message);
      } else {
        message.error(result.message || t('test.failed'));
      }
    } catch (error) {
      console.error('测试 SMTP 配置失败:', error);
      message.error(t('test.error'));
    } finally {
      setTesting(false);
    }
  }, [testTargetEmail]);

  if (initialLoading) {
    return (
      <div style={{ minHeight: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', background: token.colorBgLayout }}>
        <Spin size="large" />
      </div>
    );
  }

  if (!currentUser?.is_admin) {
    return (
      <div style={{ padding: 24 }}>
        <Alert type="error" showIcon message={t('noAccess.title')} description={t('noAccess.desc')} />
      </div>
    );
  }

  return (
    <div
      style={{
        minHeight: `calc(100vh - ${footerSafeOffset}px)`,
        boxSizing: 'border-box',
        background: pageBackground,
        padding: 24,
        paddingBottom: footerSafeOffset,
      }}
    >
      <div style={{ maxWidth: 1400, margin: '0 auto', width: '100%' }}>
      {/* 嵌入 shell 时标题由 shell 顶栏提供，这里只留一行说明 */}
      {embedded ? (
        <Text type="secondary" style={{ display: 'block', marginBottom: 20 }}>
          {t('page.subtitle')}
        </Text>
      ) : (
      <Card
        variant="borderless"
        style={{
          marginBottom: 24,
          borderRadius: 20,
          overflow: 'hidden',
          boxShadow: `0 12px 32px ${token.colorFillSecondary}`,
        }}
        styles={{ body: { padding: 0 } }}
      >
        <div style={{ background: headerBackground, padding: '28px 32px', color: '#fff' }}>
          <Space direction="vertical" size={6}>
            <Space>
              <SettingOutlined />
              <Title level={3} style={{ color: '#fff', margin: 0 }}>{t('page.title')}</Title>
            </Space>
            <Paragraph style={{ color: 'rgba(255,255,255,0.88)', margin: 0 }}>
              {t('page.subtitle')}
            </Paragraph>
          </Space>
        </div>
      </Card>
      )}

      <Form form={form} layout="vertical" onFinish={handleSave}>
        <Row gutter={24}>
          <Col xs={24} xl={16}>
            <Card title={t('mail.cardTitle')} variant="borderless" style={{ borderRadius: 16 }}>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 20 }}
                message={t('mail.qqNoteTitle')}
                description={t('mail.qqNoteDesc')}
              />

              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_provider" label={t('mail.provider')} rules={[{ required: true, message: t('mail.providerRequired') }]}>
                    <Select onChange={handleProviderChange}>
                      <Option value="qq">{t('mail.providerQQ')}</Option>
                      <Option value="custom">{t('mail.providerCustom')}</Option>
                    </Select>
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_host" label={t('mail.host')} rules={[{ required: true, message: t('mail.hostRequired') }]}>
                    <Input placeholder={t('mail.hostPlaceholder')} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_port" label={t('mail.port')} rules={[{ required: true, message: t('mail.portRequired') }]}>
                    <InputNumber style={{ width: '100%' }} min={1} max={65535} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_username" label={t('mail.username')} rules={[{ required: true, message: t('mail.usernameRequired') }]}>
                    <Input placeholder={t('mail.usernamePlaceholder')} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_password" label={t('mail.password')} rules={[{ required: true, message: t('mail.passwordRequired') }]}>
                    <Input.Password placeholder={t('mail.passwordPlaceholder')} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_from_email" label={t('mail.fromEmail')}>
                    <Input placeholder={t('mail.fromEmailPlaceholder')} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_from_name" label={t('mail.fromName')} rules={[{ required: true, message: t('mail.fromNameRequired') }]}>
                    <Input placeholder="AIFictionForge" />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_use_ssl" label={t('mail.useSsl')} valuePropName="checked">
                    <Switch />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="smtp_use_tls" label={t('mail.useTls')} valuePropName="checked">
                    <Switch />
                  </Form.Item>
                </Col>
              </Row>
            </Card>
          </Col>

          <Col xs={24} xl={8}>
            <Card title={t('register.cardTitle')} variant="borderless" style={{ borderRadius: 16, marginBottom: 24 }}>
              <Form.Item name="email_auth_enabled" label={t('register.emailAuth')} valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="email_register_enabled" label={t('register.emailRegister')} valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="verification_code_ttl_minutes" label={t('register.codeTtl')} rules={[{ required: true, message: t('register.codeTtlRequired') }]}>
                <InputNumber style={{ width: '100%' }} min={1} max={120} />
              </Form.Item>
              <Form.Item name="verification_resend_interval_seconds" label={t('register.resendInterval')} rules={[{ required: true, message: t('register.resendIntervalRequired') }]}>
                <InputNumber style={{ width: '100%' }} min={10} max={3600} />
              </Form.Item>
            </Card>

            <Card title={t('actions.cardTitle')} variant="borderless" style={{ borderRadius: 16 }}>
              <Space direction="vertical" style={{ width: '100%' }} size={12}>
                <Input
                  value={testTargetEmail}
                  onChange={(e) => setTestTargetEmail(e.target.value)}
                  placeholder={t('test.targetPlaceholder')}
                />
                <Button icon={<ReloadOutlined />} onClick={loadData} block>
                  {t('actions.reload')}
                </Button>
                <Button icon={<SendOutlined />} loading={testing} onClick={handleTest} block>
                  {t('actions.sendTest')}
                </Button>
                <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={saving} block onClick={() => form.submit()}>
                  {t('actions.save')}
                </Button>
                <Alert
                  type="success"
                  showIcon
                  icon={<CheckCircleOutlined />}
                  message={t('test.tipTitle')}
                  description={<Text type="secondary">{t('test.tipDesc')}</Text>}
                />
              </Space>
            </Card>
          </Col>
        </Row>
      </Form>
      </div>
    </div>
  );
}
