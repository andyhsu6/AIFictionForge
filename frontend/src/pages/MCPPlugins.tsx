import { useState, useEffect } from 'react';
import {
  Card,
  Button,
  Space,
  Typography,
  Modal,
  Form,
  Input,
  Switch,
  Select,
  message,
  Tag,
  Spin,
  Empty,
  Alert,
  Row,
  Col,
  theme,
} from 'antd';
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ThunderboltOutlined,
  InfoCircleOutlined,
  ToolOutlined,
  ApiOutlined,
  QuestionCircleOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { mcpPluginApi, settingsApi } from '../services/api';
import type { MCPPlugin, MCPTool } from '../types';

const { Paragraph, Text, Title } = Typography;
const { TextArea } = Input;

export default function MCPPluginsPage() {
  const { t } = useTranslation('mcpPlugins');
  const [isMobile, setIsMobile] = useState(window.innerWidth <= 768);
  const [form] = Form.useForm();
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

  const statusStyles = {
    success: {
      bg: token.colorSuccessBg,
      border: token.colorSuccessBorder,
      text: token.colorSuccessText,
    },
    info: {
      bg: token.colorInfoBg,
      border: token.colorInfoBorder,
      text: token.colorInfoText,
    },
    warning: {
      bg: token.colorWarningBg,
      border: token.colorWarningBorder,
      text: token.colorWarningText,
    },
    error: {
      bg: token.colorErrorBg,
      border: token.colorErrorBorder,
      text: token.colorErrorText,
    },
  };

  // 响应式监听窗口大小变化
  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth <= 768);
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);
  const [modal, contextHolder] = Modal.useModal();
  const [loading, setLoading] = useState(false);
  const [plugins, setPlugins] = useState<MCPPlugin[]>([]);
  const [modalVisible, setModalVisible] = useState(false);
  const [editingPlugin, setEditingPlugin] = useState<MCPPlugin | null>(null);
  const [testingPluginId, setTestingPluginId] = useState<string | null>(null);
  const [viewingTools, setViewingTools] = useState<{ pluginId: string; tools: MCPTool[] } | null>(null);
  const [checkingFunctionCalling, setCheckingFunctionCalling] = useState(false);
  const [modelSupportStatus, setModelSupportStatus] = useState<'unknown' | 'supported' | 'unsupported'>('unknown');

  useEffect(() => {
    const initPage = async () => {
      setLoading(true);
      try {
        // 1. 并行获取插件列表和当前设置
        const [pluginsData, settings] = await Promise.all([
          mcpPluginApi.getPlugins(),
          settingsApi.getSettings()
        ]);
        
        setPlugins(pluginsData);

        // 2. 检查配置一致性
        const verifiedConfigStr = localStorage.getItem('mcp_verified_config');
        if (verifiedConfigStr) {
          try {
            const verifiedConfig = JSON.parse(verifiedConfigStr);
            const currentConfig = {
              provider: settings.api_provider,
              baseUrl: settings.api_base_url,
              model: settings.llm_model
            };

            // 比较关键配置是否发生变更
            const isConfigChanged =
              verifiedConfig.provider !== currentConfig.provider ||
              verifiedConfig.baseUrl !== currentConfig.baseUrl ||
              verifiedConfig.model !== currentConfig.model;

            if (isConfigChanged) {
              // 配置已变更
              setModelSupportStatus('unknown');
              
              // 检查是否有正在运行的插件
              const activePlugins = pluginsData.filter(p => p.enabled);
              if (activePlugins.length > 0) {
                // 自动禁用所有插件
                message.loading({ content: t('toast.autoDisableLoading'), key: 'auto_disable' });
                
                await Promise.all(activePlugins.map(p => mcpPluginApi.togglePlugin(p.id, false)));
                
                // 重新加载插件列表状态
                const updatedPlugins = await mcpPluginApi.getPlugins();
                setPlugins(updatedPlugins);
                
                message.success({ content: t('toast.autoDisableSuccess'), key: 'auto_disable' });
                
                modal.warning({
                  title: t('configChange.title'),
                  centered: true,
                  content: t('configChange.content'),
                  okText: t('configChange.ok'),
                });
              } else {
                // 没有运行中的插件，仅提示
                message.info(t('toast.configChanged'));
              }
              
              // 清除旧的验证状态
              localStorage.removeItem('mcp_verified_config');
            } else {
              // 配置未变更，恢复验证状态（根据缓存的状态恢复）
              const cachedStatus = verifiedConfig.status || 'supported';
              setModelSupportStatus(cachedStatus as 'unknown' | 'supported' | 'unsupported');
            }
          } catch (e) {
            console.error('Failed to parse verified config:', e);
            localStorage.removeItem('mcp_verified_config');
          }
        }
      } catch (error) {
        console.error('Init page failed:', error);
        message.error(t('toast.initFailed'));
      } finally {
        setLoading(false);
      }
    };
    initPage();
  }, [modal]);

  const loadPlugins = async () => {
    try {
      const data = await mcpPluginApi.getPlugins();
      setPlugins(data);
    } catch (error) {
      console.error('Load plugins failed:', error);
      message.error(t('toast.loadListFailed'));
    }
  };

  const handleCreate = () => {
    if (modelSupportStatus !== 'supported') {
      modal.confirm({
        title: t('modelCheckModal.title'),
        centered: true,
        icon: <WarningOutlined />,
        content: t('modelCheckModal.content'),
        okText: t('modelCheckModal.ok'),
        cancelText: t('modelCheckModal.cancel'),
        onOk: handleCheckFunctionCalling,
      });
      return;
    }
    setEditingPlugin(null);
    form.resetFields();
    form.setFieldsValue({
      enabled: true,
      category: 'search',
      config_json: `{
  "mcpServers": {
    "exa": {
      "type": "http",
      "url": "https://mcp.exa.ai/mcp?exaApiKey=YOUR_API_KEY",
      "headers": {}
    }
  }
}`
    });
    setModalVisible(true);
  };

  const handleEdit = (plugin: MCPPlugin) => {
    setEditingPlugin(plugin);

    // 重构为标准MCP配置格式
    const mcpConfig: Record<string, Record<string, Record<string, unknown>>> = {
      mcpServers: {
        [plugin.plugin_name]: {
          type: plugin.plugin_type || 'http'
        }
      }
    };

    if (plugin.plugin_type === 'http' || plugin.plugin_type === 'streamable_http' || plugin.plugin_type === 'sse') {
      mcpConfig.mcpServers[plugin.plugin_name].url = plugin.server_url;
      mcpConfig.mcpServers[plugin.plugin_name].headers = plugin.headers || {};
    } else {
      mcpConfig.mcpServers[plugin.plugin_name].command = plugin.command;
      mcpConfig.mcpServers[plugin.plugin_name].args = plugin.args || [];
      mcpConfig.mcpServers[plugin.plugin_name].env = plugin.env || {};
    }

    form.setFieldsValue({
      config_json: JSON.stringify(mcpConfig, null, 2),
      enabled: plugin.enabled,
      category: plugin.category || 'general',
    });
    setModalVisible(true);
  };

  const handleDelete = (plugin: MCPPlugin) => {
    modal.confirm({
      title: t('deleteModal.title'),
      content: t('deleteModal.content', { name: plugin.display_name || plugin.plugin_name }),
      centered: true,
      okText: t('deleteModal.ok'),
      cancelText: t('deleteModal.cancel'),
      okType: 'danger',
      onOk: async () => {
        try {
          await mcpPluginApi.deletePlugin(plugin.id);
          message.success(t('toast.pluginDeleted'));
          loadPlugins();
        } catch (error) {
          console.error('Delete plugin failed:', error);
          message.error(t('toast.deleteFailed'));
        }
      },
    });
  };

  const handleToggle = async (plugin: MCPPlugin, enabled: boolean) => {
    try {
      await mcpPluginApi.togglePlugin(plugin.id, enabled);
      message.success(enabled ? t('toast.pluginEnabled') : t('toast.pluginDisabled'));
      loadPlugins();
    } catch (error) {
      console.error('Toggle plugin failed:', error);
      message.error(t('toast.toggleFailed'));
    }
  };

  const handleTest = async (pluginId: string) => {
    setTestingPluginId(pluginId);
    try {
      const result = await mcpPluginApi.testPlugin(pluginId);

      // 测试完成后，无论成功失败都刷新插件列表以更新状态
      await loadPlugins();

      if (result.success) {
        const suggestions = result.suggestions || [];
        const aiChoice = suggestions.find((s: string) => s.startsWith('🤖'))?.replace('🤖 AI选择: ', '') || '';
        const paramsStr = suggestions.find((s: string) => s.startsWith('📝'))?.replace('📝 参数: ', '') || '';
        const callTime = suggestions.find((s: string) => s.startsWith('⏱️'))?.replace('⏱️ 耗时: ', '') || '';
        const resultStr = suggestions.find((s: string) => s.startsWith('📊'))?.replace('📊 结果:\n', '') || '';

        modal.success({
          title: t('testSuccess.title'),
          centered: true,
          width: isMobile ? '95%' : 700,
          content: (
            <div style={{ padding: '8px 0' }}>
              <div style={{ marginBottom: 16, padding: 12, background: statusStyles.success.bg, border: `1px solid ${statusStyles.success.border}`, borderRadius: 8 }}>
                <Typography.Text strong style={{ color: statusStyles.success.text, fontSize: 14 }}>
                  ✓ {result.message}
                </Typography.Text>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 12, marginBottom: 16 }}>
                <div style={{ padding: 12, background: token.colorBgLayout, borderRadius: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{t('testSuccess.availableTools')}</Text>
                  <div><Text strong style={{ fontSize: 20 }}>{result.tools_count || 0}</Text></div>
                </div>
                <div style={{ padding: 12, background: token.colorBgLayout, borderRadius: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{t('testSuccess.totalResponseTime')}</Text>
                  <div><Text strong style={{ fontSize: 20 }}>{result.response_time_ms?.toFixed(0) || 0}ms</Text></div>
                </div>
              </div>

              {aiChoice && (
                <div style={{ marginBottom: 12, padding: 12, background: statusStyles.info.bg, borderRadius: 8, border: `1px solid ${statusStyles.info.border}` }}>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{t('testSuccess.aiSelectedTool')}</Text>
                  <Text code strong>{aiChoice}</Text>
                  {callTime && <Tag color="blue" style={{ marginLeft: 8 }}>{callTime}</Tag>}
                </div>
              )}

              {paramsStr && (
                <div style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{t('testSuccess.callParams')}</Text>
                  <pre style={{ margin: 0, padding: 8, background: token.colorBgLayout, borderRadius: 4, fontSize: 12, overflow: 'auto', maxHeight: 100 }}>
                    {(() => { try { return JSON.stringify(JSON.parse(paramsStr), null, 2); } catch { return paramsStr; } })()}
                  </pre>
                </div>
              )}

              {resultStr && (
                <div style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{t('testSuccess.resultPreview')}</Text>
                  <pre style={{ margin: 0, padding: 8, background: token.colorBgLayout, borderRadius: 4, fontSize: 11, overflow: 'auto', maxHeight: 150, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {resultStr}
                  </pre>
                </div>
              )}

              <Alert message={t('testSuccess.autoUpdatedActive')} type="success" showIcon />
            </div>
          ),
        });
      } else {
        modal.error({
          title: t('testFailed.title'),
          centered: true,
          width: isMobile ? '90%' : 600,
          content: (
            <div style={{ padding: '8px 0' }}>
              <div style={{ marginBottom: 16 }}>
                <Alert
                  message={result.message || t('testFailed.message')}
                  type="error"
                  showIcon
                />
              </div>

              {result.error && (
                <div style={{
                  padding: 16,
                  background: statusStyles.error.bg,
                  border: `1px solid ${statusStyles.error.border}`,
                  borderRadius: 8,
                  marginBottom: 16
                }}>
                  <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 8 }}>{t('testFailed.errorInfo')}</Text>
                  <Text style={{ fontSize: 13, color: statusStyles.error.text, fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {result.error}
                  </Text>
                </div>
              )}

              {result.suggestions && result.suggestions.length > 0 && (
                <div style={{
                  padding: 16,
                  background: statusStyles.warning.bg,
                  border: `1px solid ${statusStyles.warning.border}`,
                  borderRadius: 8,
                  marginBottom: 16
                }}>
                  <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 8 }}>{t('testFailed.suggestions')}</Text>
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
                    {result.suggestions.map((s: string, i: number) => (
                      <li key={i} style={{ marginBottom: 4 }}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}

              <Alert
                message={t('testFailed.updatedCheck')}
                type="warning"
                showIcon
              />
            </div>
          ),
        });
      }
    } catch {
      message.error(t('toast.testFailed'));
    } finally {
      setTestingPluginId(null);
    }
  };

  const handleViewTools = async (pluginId: string) => {
    try {
      const result = await mcpPluginApi.getPluginTools(pluginId);
      setViewingTools({ pluginId, tools: result.tools });
    } catch (error) {
      console.error('Get tools failed:', error);
      message.error(t('toast.getToolsFailed'));
    }
  };

  const handleCheckFunctionCalling = async () => {
    // 从设置中获取当前配置
    setCheckingFunctionCalling(true);
    try {
      const settings = await settingsApi.getSettings();
      
      if (!settings.llm_model) {
        message.warning(t('toast.configureModelFirst'));
        return;
      }

      const result = await settingsApi.checkFunctionCalling({
        api_key: settings.api_key,
        api_base_url: settings.api_base_url || '',
        provider: settings.api_provider || 'openai',
        llm_model: settings.llm_model,
      });

      // 无论成功失败，都缓存当前测试的配置和状态
      const configToCache = {
        provider: settings.api_provider,
        baseUrl: settings.api_base_url,
        model: settings.llm_model,
        status: result.success && result.supported ? 'supported' : 'unsupported',
        testedAt: new Date().toISOString()
      };
      localStorage.setItem('mcp_verified_config', JSON.stringify(configToCache));

      if (result.success && result.supported) {
        setModelSupportStatus('supported');

        modal.success({
          title: t('fcCheck.successTitle'),
          centered: true,
          width: isMobile ? '95%' : 700,
          content: (
            <div style={{ padding: '8px 0' }}>
              <div style={{ marginBottom: 16, padding: 12, background: statusStyles.success.bg, border: `1px solid ${statusStyles.success.border}`, borderRadius: 8 }}>
                <Typography.Text strong style={{ color: statusStyles.success.text, fontSize: 14 }}>
                  ✓ {result.message}
                </Typography.Text>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 12, marginBottom: 16 }}>
                <div style={{ padding: 12, background: token.colorBgLayout, borderRadius: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{t('fcCheck.apiProvider')}</Text>
                  <div><Text strong style={{ fontSize: 16 }}>{result.provider}</Text></div>
                </div>
                <div style={{ padding: 12, background: token.colorBgLayout, borderRadius: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{t('fcCheck.responseTime')}</Text>
                  <div><Text strong style={{ fontSize: 16 }}>{result.response_time_ms?.toFixed(0) || 0}ms</Text></div>
                </div>
              </div>

              <div style={{ marginBottom: 12, padding: 12, background: statusStyles.info.bg, borderRadius: 8, border: `1px solid ${statusStyles.info.border}` }}>
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{t('fcCheck.modelInfo')}</Text>
                <Text code strong>{result.model}</Text>
                {result.details?.finish_reason && (
                  <Tag color="green" style={{ marginLeft: 8 }}>finish_reason: {result.details.finish_reason}</Tag>
                )}
              </div>

              {result.details && (
                <div style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{t('fcCheck.detailTitle')}</Text>
                  <div style={{ padding: 8, background: token.colorBgLayout, borderRadius: 4, fontSize: 12 }}>
                    <div>{t('fcCheck.toolCallCount', { count: result.details.tool_call_count || 0 })}</div>
                    <div>{t('fcCheck.testTool', { tool: result.details.test_tool || 'N/A' })}</div>
                    <div>{t('fcCheck.responseType', { type: result.details.response_type || 'N/A' })}</div>
                  </div>
                </div>
              )}

              {result.tool_calls && result.tool_calls.length > 0 && (
                <div style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{t('fcCheck.toolCallExample')}</Text>
                  <pre style={{ margin: 0, padding: 8, background: token.colorBgLayout, borderRadius: 4, fontSize: 11, overflow: 'auto', maxHeight: 150 }}>
                    {JSON.stringify(result.tool_calls[0], null, 2)}
                  </pre>
                </div>
              )}

              {result.suggestions && result.suggestions.length > 0 && (
                <div style={{ padding: 12, background: statusStyles.success.bg, border: `1px solid ${statusStyles.success.border}`, borderRadius: 8 }}>
                  <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>{t('fcCheck.suggestions')}</Text>
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12 }}>
                    {result.suggestions.map((s: string, i: number) => (
                      <li key={i} style={{ marginBottom: 4 }}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ),
        });
      } else {
        setModelSupportStatus('unsupported');
        modal.warning({
          title: t('fcCheck.failTitle'),
          centered: true,
          width: isMobile ? '95%' : 700,
          content: (
            <div style={{ padding: '8px 0' }}>
              <div style={{ marginBottom: 16 }}>
                <Alert
                  message={result.message || t('fcCheck.unsupportedMessage')}
                  type="warning"
                  showIcon
                />
              </div>

              {result.error && (
                <div style={{
                  padding: 16,
                  background: statusStyles.warning.bg,
                  border: `1px solid ${statusStyles.warning.border}`,
                  borderRadius: 8,
                  marginBottom: 16
                }}>
                  <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 8 }}>{t('fcCheck.errorInfo')}</Text>
                  <Text style={{ fontSize: 13, fontFamily: 'monospace' }}>
                    {result.error}
                  </Text>
                </div>
              )}

              {result.response_preview && (
                <div style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{t('fcCheck.responsePreview')}</Text>
                  <pre style={{ margin: 0, padding: 8, background: token.colorBgLayout, borderRadius: 4, fontSize: 11, overflow: 'auto', maxHeight: 100, whiteSpace: 'pre-wrap' }}>
                    {result.response_preview}
                  </pre>
                </div>
              )}

              {result.suggestions && result.suggestions.length > 0 && (
                <div style={{
                  padding: 16,
                  background: statusStyles.info.bg,
                  border: `1px solid ${statusStyles.info.border}`,
                  borderRadius: 8
                }}>
                  <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 8 }}>{t('fcCheck.suggestions')}</Text>
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
                    {result.suggestions.map((s: string, i: number) => (
                      <li key={i} style={{ marginBottom: 4 }}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ),
        });
      }
    } catch (error) {
      console.error('Check function calling failed:', error);
      message.error(t('toast.checkFailed'));
      setModelSupportStatus('unsupported');
    } finally {
      setCheckingFunctionCalling(false);
    }
  };

  const handleSubmit = async (values: { config_json: string; enabled: boolean; category?: string }) => {
    setLoading(true);
    try {
      // 验证JSON格式
      try {
        JSON.parse(values.config_json);
      } catch {
        message.error(t('toast.jsonFormatError'));
        setLoading(false);
        return;
      }

      const data = {
        config_json: values.config_json,
        enabled: values.enabled,
        category: values.category || 'general',
      };

      // 统一使用简化API，后端会自动判断是创建还是更新
      await mcpPluginApi.createPluginSimple(data);
      message.success(editingPlugin ? t('toast.pluginUpdated') : t('toast.pluginCreated'));

      setModalVisible(false);
      form.resetFields();
      loadPlugins();
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: string } } };
      const errorMsg = err?.response?.data?.detail || t('toast.operationFailed');
      message.error(errorMsg);
    } finally {
      setLoading(false);
    }
  };

  const getStatusTag = (plugin: MCPPlugin) => {
    if (!plugin.enabled) {
      return <Tag color="default">{t('status.disabled')}</Tag>;
    }
    switch (plugin.status) {
      case 'active':
        return <Tag color="success" icon={<CheckCircleOutlined />}>{t('status.active')}</Tag>;
      case 'error':
        return (
          <Tag color="error" icon={<CloseCircleOutlined />} title={plugin.last_error}>{t('status.error')}</Tag>
        );
      default:
        return <Tag color="default">{t('status.inactive')}</Tag>;
    }
  };

  return (
    <>
      {contextHolder}
      <div style={{
        minHeight: '90vh',
        background: `linear-gradient(180deg, ${token.colorBgLayout} 0%, ${alphaColor(token.colorPrimary, 0.08)} 100%)`,
        padding: isMobile ? '20px 16px 70px' : '24px 24px 70px',
        display: 'flex',
        flexDirection: 'column',
      }}>
        <div style={{
          maxWidth: 1400,
          margin: '0 auto',
          width: '100%',
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
        }}>
          {/* 顶部导航卡片 */}
          <Card
            variant="borderless"
            style={{
              background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.8)} 50%, ${token.colorPrimaryHover} 100%)`,
              borderRadius: isMobile ? 16 : 24,
              boxShadow: `0 12px 40px ${alphaColor(token.colorPrimary, 0.25)}, 0 4px 12px ${alphaColor(token.colorText, 0.08)}`,
              marginBottom: isMobile ? 20 : 24,
              border: 'none',
              position: 'relative',
              overflow: 'hidden'
            }}
          >
            {/* 装饰性背景元素 */}
            <div style={{ position: 'absolute', top: -60, right: -60, width: 200, height: 200, borderRadius: '50%', background: alphaColor(token.colorWhite, 0.08), pointerEvents: 'none' }} />
            <div style={{ position: 'absolute', bottom: -40, left: '30%', width: 120, height: 120, borderRadius: '50%', background: alphaColor(token.colorWhite, 0.05), pointerEvents: 'none' }} />
            <div style={{ position: 'absolute', top: '50%', right: '15%', width: 80, height: 80, borderRadius: '50%', background: alphaColor(token.colorWhite, 0.06), pointerEvents: 'none' }} />

            <Row align="middle" justify="space-between" gutter={[16, 16]} style={{ position: 'relative', zIndex: 1 }}>
              <Col xs={24} sm={12}>
                <Space direction="vertical" size={4}>
                  <Space align="center">
                    <Title level={isMobile ? 3 : 2} style={{ margin: 0, color: token.colorWhite, textShadow: `0 2px 4px ${alphaColor(token.colorText, 0.2)}` }}>
                      <ToolOutlined style={{ color: alphaColor(token.colorWhite, 0.9), marginRight: 8 }} />
                      {t('title')}
                    </Title>
                  </Space>
                  <Text style={{ fontSize: isMobile ? 12 : 14, color: alphaColor(token.colorWhite, 0.85), marginLeft: isMobile ? 40 : 48 }}>
                    {t('subtitle')}
                  </Text>
                </Space>
              </Col>
              <Col xs={24} sm={12}>
                <Space size={12} style={{ display: 'flex', justifyContent: isMobile ? 'flex-start' : 'flex-end', width: '100%' }}>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={handleCreate}
                    style={{
                      borderRadius: 12,
                      background: alphaColor(token.colorWarning, 0.95),
                      border: `1px solid ${alphaColor(token.colorWhite, 0.3)}`,
                      boxShadow: `0 4px 16px ${alphaColor(token.colorWarning, 0.4)}`,
                      color: token.colorWhite,
                      fontWeight: 600
                    }}
                  >
                    {t('addPlugin')}
                  </Button>
                </Space>
              </Col>
            </Row>

            <div style={{ marginTop: isMobile ? 16 : 24, display: 'flex', gap: isMobile ? 12 : 16, flexDirection: isMobile ? 'column' : 'row' }}>
              <Card
                variant="borderless"
                style={{
                  flex: 1,
                  borderRadius: 12,
                  background: alphaColor(token.colorBgContainer, 0.9),
                  border: `1px solid ${alphaColor(token.colorBorder, 0.6)}`,
                  backdropFilter: 'blur(10px)',
                  boxShadow: `0 4px 12px ${alphaColor(token.colorText, 0.06)}`
                }}
                styles={{ body: { padding: isMobile ? 14 : 20 } }}
              >
                <div style={{
                  display: 'flex',
                  flexDirection: isMobile ? 'column' : 'row',
                  justifyContent: 'space-between',
                  alignItems: isMobile ? 'stretch' : 'center',
                  gap: isMobile ? 12 : 0
                }}>
                  <Space align="start" style={{ flex: 1 }}>
                    <div style={{
                      width: isMobile ? 36 : 40,
                      height: isMobile ? 36 : 40,
                      borderRadius: '50%',
                      background: modelSupportStatus === 'supported' ? statusStyles.success.bg : modelSupportStatus === 'unsupported' ? statusStyles.error.bg : statusStyles.info.bg,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      border: `1px solid ${modelSupportStatus === 'supported' ? statusStyles.success.border : modelSupportStatus === 'unsupported' ? statusStyles.error.border : statusStyles.info.border}`,
                      flexShrink: 0
                    }}>
                      {modelSupportStatus === 'supported' ? (
                        <CheckCircleOutlined style={{ fontSize: isMobile ? 18 : 20, color: statusStyles.success.text }} />
                      ) : modelSupportStatus === 'unsupported' ? (
                        <CloseCircleOutlined style={{ fontSize: isMobile ? 18 : 20, color: statusStyles.error.text }} />
                      ) : (
                        <QuestionCircleOutlined style={{ fontSize: isMobile ? 18 : 20, color: statusStyles.info.text }} />
                      )}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Text strong style={{ fontSize: isMobile ? 14 : 16, display: 'block', color: token.colorText }}>{t('modelCheck.title')}</Text>
                      <Text type="secondary" style={{ fontSize: isMobile ? 12 : 13, display: 'block', lineHeight: 1.5 }}>
                        {modelSupportStatus === 'supported'
                          ? t('modelCheck.statusSupported')
                          : modelSupportStatus === 'unsupported'
                            ? t('modelCheck.statusUnsupported')
                            : t('modelCheck.statusUnknown')}
                      </Text>
                    </div>
                  </Space>
                  <Button
                    type={modelSupportStatus === 'supported' ? 'default' : 'primary'}
                    icon={<ApiOutlined />}
                    onClick={handleCheckFunctionCalling}
                    loading={checkingFunctionCalling}
                    style={{ borderRadius: 8, width: isMobile ? '100%' : 'auto' }}
                    size={isMobile ? 'middle' : 'middle'}
                  >
                    {modelSupportStatus === 'unknown' ? t('modelCheck.startCheck') : t('modelCheck.reCheck')}
                  </Button>
                </div>
              </Card>

              <Card
                variant="borderless"
                style={{
                  flex: 1,
                  borderRadius: 12,
                  background: alphaColor(token.colorInfoBg, 0.7),
                  border: `1px solid ${alphaColor(token.colorInfoBorder, 0.8)}`,
                  backdropFilter: 'blur(10px)',
                  boxShadow: `0 4px 12px ${alphaColor(token.colorText, 0.06)}`
                }}
                styles={{ body: { padding: isMobile ? 14 : 20 } }}
              >
                <Space align="start">
                  <InfoCircleOutlined style={{ fontSize: isMobile ? 18 : 20, color: token.colorPrimary, marginTop: 2, flexShrink: 0 }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Text strong style={{ fontSize: isMobile ? 14 : 16, display: 'block', color: token.colorText, marginBottom: 4 }}>{t('whatIsMCP.title')}</Text>
                    <Text style={{ fontSize: isMobile ? 12 : 13, display: 'block', color: token.colorTextSecondary, lineHeight: 1.6 }}>
                      {t('whatIsMCP.content')}
                    </Text>
                  </div>
                </Space>
              </Card>
            </div>
          </Card>

          {/* 主内容区 */}
          <div style={{ flex: 1 }}>
            {/* 模型能力未验证时的警告提示 */}
            {modelSupportStatus !== 'supported' && plugins.length > 0 && (
              <Alert
                message={
                  modelSupportStatus === 'unsupported'
                    ? t('warning.unsupported')
                    : t('warning.checkRequired')
                }
                type={modelSupportStatus === 'unsupported' ? 'error' : 'warning'}
                showIcon
                icon={modelSupportStatus === 'unsupported' ? <CloseCircleOutlined /> : <WarningOutlined />}
                style={{ marginBottom: 16, borderRadius: 8 }}
                action={
                  <Button size="small" type="primary" onClick={handleCheckFunctionCalling} loading={checkingFunctionCalling}>
                    {modelSupportStatus === 'unknown' ? t('modelCheck.startCheck') : t('modelCheck.reCheck')}
                  </Button>
                }
              />
            )}

            {/* 插件列表 */}
            <Spin spinning={loading}>
              {plugins.length === 0 ? (
                <Empty
                  description={t('empty.noPlugins')}
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  style={{ padding: isMobile ? '40px 0' : '60px 0' }}
                >
                  <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>
                    {t('empty.addFirst')}
                  </Button>
                </Empty>
              ) : (
                <Space direction="vertical" size={isMobile ? 'small' : 'middle'} style={{ width: '100%' }}>
                  {plugins.map((plugin) => (
                    <Card
                      key={plugin.id}
                      size="small"
                      style={{
                        borderRadius: 8,
                        border: `1px solid ${token.colorBorderSecondary}`,
                      }}
                      styles={{ body: { padding: isMobile ? 12 : 16 } }}
                    >
                      <div
                        style={{
                          display: 'flex',
                          flexDirection: 'column',
                          gap: isMobile ? 12 : 16,
                        }}
                      >
                        {/* 插件信息区域 */}
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <Space direction="vertical" size="small" style={{ width: '100%' }}>
                            {/* 标题和状态标签 */}
                            <div style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: '6px',
                              flexWrap: 'wrap',
                              justifyContent: 'space-between'
                            }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap', flex: 1 }}>
                                <Text strong style={{ fontSize: isMobile ? '14px' : '16px' }}>
                                  {plugin.display_name || plugin.plugin_name}
                                </Text>
                                {getStatusTag(plugin)}
                              </div>
                              {/* 移动端：开关放在标题行右侧 */}
                              {isMobile && (
                                <Switch
                                  title={modelSupportStatus !== 'supported' ? t('tooltip.checkFirst') : (plugin.enabled ? t('tooltip.disablePlugin') : t('tooltip.enablePlugin'))}
                                  checked={plugin.enabled}
                                  onChange={(checked) => handleToggle(plugin, checked)}
                                  disabled={modelSupportStatus !== 'supported'}
                                  size="small"
                                  checkedChildren={t('switch.on')}
                                  unCheckedChildren={t('switch.off')}
                                  style={{
                                    flexShrink: 0,
                                    height: 16,
                                    minHeight: 16,
                                    lineHeight: '16px'
                                  }}
                                />
                              )}
                            </div>
                            
                            {/* 类型和分类标签 */}
                            <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                              <Tag color={plugin.plugin_type === 'http' || plugin.plugin_type === 'streamable_http' || plugin.plugin_type === 'sse' ? 'blue' : 'cyan'} style={{ fontSize: isMobile ? 11 : 12 }}>
                                {plugin.plugin_type?.toUpperCase() || 'UNKNOWN'}
                              </Tag>
                              {plugin.category && plugin.category !== 'general' && (
                                <Tag color="purple" style={{ fontSize: isMobile ? 11 : 12 }}>{plugin.category}</Tag>
                              )}
                            </div>
                            
                            {plugin.description && (
                              <Paragraph
                                type="secondary"
                                style={{
                                  margin: 0,
                                  fontSize: isMobile ? '12px' : '13px',
                                }}
                                ellipsis={{ rows: 2 }}
                              >
                                {plugin.description}
                              </Paragraph>
                            )}

                            {/* 只显示有值的URL或命令，脱敏处理敏感信息 */}
                            {(plugin.plugin_type === 'http' || plugin.plugin_type === 'streamable_http' || plugin.plugin_type === 'sse') && plugin.server_url && (
                              <div style={{
                                fontSize: isMobile ? '11px' : '12px',
                                overflow: 'hidden',
                                textOverflow: 'ellipsis',
                                whiteSpace: 'nowrap'
                              }}>
                                <Text type="secondary" code style={{ fontSize: 'inherit' }}>
                                  {(() => {
                                    // 脱敏处理：隐藏URL中的API Key
                                    const url = plugin.server_url;
                                    try {
                                      const urlObj = new URL(url);
                                      // 替换查询参数中的敏感信息
                                      const params = new URLSearchParams(urlObj.search);
                                      let maskedUrl = `${urlObj.protocol}//${urlObj.host}${urlObj.pathname}`;

                                      const sensitiveKeys = ['apiKey', 'api_key', 'key', 'token', 'secret', 'password', 'auth'];
                                      let hasParams = false;

                                      params.forEach((value, key) => {
                                        const isSensitive = sensitiveKeys.some(k => key.toLowerCase().includes(k.toLowerCase()));
                                        const maskedValue = isSensitive ? '***' : value;
                                        maskedUrl += (hasParams ? '&' : '?') + `${key}=${maskedValue}`;
                                        hasParams = true;
                                      });

                                      return maskedUrl;
                                    } catch {
                                      // 如果URL解析失败，尝试简单替换
                                      return url.replace(/([?&])(apiKey|api_key|key|token|secret|password|auth)=([^&]+)/gi, '$1$2=***');
                                    }
                                  })()}
                                </Text>
                              </div>
                            )}

                            {plugin.plugin_type === 'stdio' && plugin.command && (
                              <div style={{
                                fontSize: isMobile ? '11px' : '12px',
                                overflow: 'hidden',
                                textOverflow: 'ellipsis',
                                whiteSpace: 'nowrap'
                              }}>
                                <Text type="secondary" code style={{ fontSize: 'inherit' }}>
                                  {plugin.command} {plugin.args?.join(' ')}
                                </Text>
                              </div>
                            )}

                            {/* 显示最后错误信息 */}
                            {plugin.last_error && (
                              <Text type="danger" style={{ fontSize: isMobile ? '11px' : '12px' }}>
                                {t('pluginList.lastError', { error: plugin.last_error })}
                              </Text>
                            )}
                          </Space>
                        </div>

                        {/* 操作按钮区域 */}
                        <div style={{
                          display: 'flex',
                          justifyContent: isMobile ? 'flex-end' : 'flex-start',
                          alignItems: 'center',
                          gap: isMobile ? 8 : 8,
                          flexWrap: 'wrap',
                          borderTop: isMobile ? `1px solid ${token.colorBorderSecondary}` : 'none',
                          paddingTop: isMobile ? 12 : 0
                        }}>
                          {/* 桌面端显示开关 */}
                          {!isMobile && (
                            <Switch
                              title={modelSupportStatus !== 'supported' ? t('tooltip.checkFirst') : (plugin.enabled ? t('tooltip.disablePlugin') : t('tooltip.enablePlugin'))}
                              checked={plugin.enabled}
                              onChange={(checked) => handleToggle(plugin, checked)}
                              disabled={modelSupportStatus !== 'supported'}
                              checkedChildren={t('switch.on')}
                              unCheckedChildren={t('switch.off')}
                            />
                          )}
                          <Button
                            title={modelSupportStatus !== 'supported' ? t('tooltip.checkFirst') : t('tooltip.testConnect')}
                            icon={<ThunderboltOutlined />}
                            onClick={() => handleTest(plugin.id)}
                            loading={testingPluginId === plugin.id}
                            disabled={modelSupportStatus !== 'supported'}
                            size={isMobile ? 'small' : 'middle'}
                          >
                            {!isMobile && t('button.test')}
                          </Button>
                          <Button
                            title={modelSupportStatus !== 'supported' ? t('tooltip.checkFirst') : t('tooltip.viewTools')}
                            icon={<ToolOutlined />}
                            onClick={() => handleViewTools(plugin.id)}
                            disabled={modelSupportStatus !== 'supported' || !plugin.enabled || plugin.status !== 'active'}
                            size={isMobile ? 'small' : 'middle'}
                          >
                            {!isMobile && t('button.tools')}
                          </Button>
                          <Button
                            title={modelSupportStatus !== 'supported' ? t('tooltip.checkFirst') : t('tooltip.edit')}
                            icon={<EditOutlined />}
                            onClick={() => handleEdit(plugin)}
                            disabled={modelSupportStatus !== 'supported'}
                            size={isMobile ? 'small' : 'middle'}
                          >
                            {!isMobile && t('button.edit')}
                          </Button>
                          <Button
                            title={modelSupportStatus !== 'supported' ? t('tooltip.checkFirst') : t('tooltip.delete')}
                            danger
                            icon={<DeleteOutlined />}
                            onClick={() => handleDelete(plugin)}
                            disabled={modelSupportStatus !== 'supported'}
                            size={isMobile ? 'small' : 'middle'}
                          >
                            {!isMobile && t('button.delete')}
                          </Button>
                        </div>
                      </div>
                    </Card>
                  ))}
                </Space>
              )}
            </Spin>
          </div>
        </div>

        {/* 创建/编辑插件模态框 */}
        <Modal
          title={editingPlugin ? t('formModal.editTitle') : t('formModal.addTitle')}
          open={modalVisible}
          centered
          onCancel={() => {
            setModalVisible(false);
            form.resetFields();
          }}
          onOk={() => form.submit()}
          width={isMobile ? '100%' : 600}
          confirmLoading={loading}
          okText={t('button.save')}
          cancelText={t('button.cancel')}
        >
          <Form form={form} layout="vertical" onFinish={handleSubmit}>
            <Form.Item
              label={t('form.configJsonLabel')}
              name="config_json"
              rules={[{ required: true, message: t('form.configJsonRequired') }]}
              extra={t('form.configJsonExtra')}
            >
              <TextArea
                rows={isMobile ? 12 : 16}
                placeholder={`${t('form.configJsonPlaceholder')}
{
  "mcpServers": {
    "exa": {
      "type": "streamable_http",
      "url": "https://mcp.exa.ai/mcp?exaApiKey=YOUR_API_KEY",
      "headers": {}
    }
  }
}`}
                style={{ fontFamily: 'monospace', fontSize: '13px' }}
              />
            </Form.Item>

            <Form.Item
              label={t('form.categoryLabel')}
              name="category"
              rules={[{ required: true, message: t('form.categoryRequired') }]}
              extra={t('form.categoryExtra')}
            >
              <Select placeholder={t('form.categoryPlaceholder')}>
                <Select.Option value="search">{t('category.search')}</Select.Option>
                <Select.Option value="analysis">{t('category.analysis')}</Select.Option>
                <Select.Option value="filesystem">{t('category.filesystem')}</Select.Option>
                <Select.Option value="database">{t('category.database')}</Select.Option>
                <Select.Option value="api">{t('category.api')}</Select.Option>
                <Select.Option value="generation">{t('category.generation')}</Select.Option>
                <Select.Option value="general">{t('category.general')}</Select.Option>
              </Select>
            </Form.Item>
          </Form>
        </Modal>

        {/* 查看工具列表模态框 */}
        <Modal
          title={
            <Space>
              <ToolOutlined style={{ color: token.colorPrimary }} />
              <span>{t('toolsModal.title')}</span>
              {viewingTools && viewingTools.tools.length > 0 && (
                <Tag color="blue">{t('toolsModal.toolCount', { count: viewingTools.tools.length })}</Tag>
              )}
            </Space>
          }
          open={!!viewingTools}
          onCancel={() => setViewingTools(null)}
          footer={[
            <Button key="close" type="primary" onClick={() => setViewingTools(null)}>
              {t('toolsModal.close')}
            </Button>,
          ]}
          width={isMobile ? '95%' : 800}
          centered
          styles={{
            body: {
              maxHeight: isMobile ? '60vh' : '70vh',
              overflowY: 'auto',
              padding: isMobile ? '16px' : '24px'
            }
          }}
        >
          {viewingTools && (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              {viewingTools.tools.length === 0 ? (
                <Empty
                  description={t('empty.noTools')}
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  style={{ padding: '40px 0' }}
                />
              ) : (
                viewingTools.tools.map((tool, index) => (
                  <Card
                    key={index}
                    size="small"
                    style={{
                      borderRadius: 8,
                      border: `1px solid ${token.colorBorderSecondary}`,
                      boxShadow: `0 2px 4px ${alphaColor(token.colorText, 0.08)}`
                    }}
                    title={
                      <Space>
                        <Text code strong style={{ fontSize: isMobile ? '13px' : '14px', color: token.colorPrimary }}>
                          {tool.name}
                        </Text>
                        <Tag color="processing" style={{ fontSize: '11px' }}>
                          #{index + 1}
                        </Tag>
                      </Space>
                    }
                  >
                    <Space direction="vertical" size="small" style={{ width: '100%' }}>
                      {tool.description && (
                        <div>
                          <Text type="secondary" style={{ fontSize: isMobile ? '12px' : '13px', display: 'block', marginBottom: 4 }}>
                            {t('toolsModal.description')}
                          </Text>
                          <Paragraph
                            style={{
                              margin: 0,
                              fontSize: isMobile ? '12px' : '13px',
                              padding: '8px 12px',
                              background: token.colorBgLayout,
                              borderRadius: 4,
                              borderLeft: `3px solid ${token.colorInfo}`
                            }}
                          >
                            {tool.description}
                          </Paragraph>
                        </div>
                      )}
                      {tool.inputSchema && (
                        <div>
                          <Text type="secondary" style={{ fontSize: isMobile ? '12px' : '13px', display: 'block', marginBottom: 4 }}>
                            {t('toolsModal.inputSchema')}
                          </Text>
                          <pre
                            style={{
                              margin: 0,
                              padding: isMobile ? '8px' : '12px',
                              background: token.colorBgLayout,
                              borderRadius: 4,
                              fontSize: isMobile ? '11px' : '12px',
                              overflow: 'auto',
                              maxHeight: '200px',
                              border: `1px solid ${token.colorBorderSecondary}`,
                              lineHeight: 1.6
                            }}
                          >
                            {JSON.stringify(tool.inputSchema, null, 2)}
                          </pre>
                        </div>
                      )}
                    </Space>
                  </Card>
                ))
              )}
            </Space>
          )}
        </Modal>
      </div>
    </>
  );
}